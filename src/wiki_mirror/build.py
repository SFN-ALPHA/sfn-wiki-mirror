"""The download and parse stages of a build, checkpointed per chunk, and the build-state file.

`__main__` calls `run_download` (Enterprise -> NAS), then `run_parse` (NAS -> article store), then
`index.run_index` (store -> Qdrant). Each stage can be killed at any point and re-run: a download
resumes from its `.part` file, a parse skips the chunks the store already holds. The build state
(`data_dir/state/build.json`) tells the API's `/v1/wiki/health` and a client's health check where a
build is; R2's index stage updates it through `write_build_state`.

The parse stage's process handoff (docs/DESIGN.md section 5):

* A chunk expands to about 2 GB of HTML and 100,000 articles. Returning those through a
  `ProcessPoolExecutor` result would pickle hundreds of MB into the parent per chunk, on an
  8 GB box. Instead each worker parses ONE chunk and streams its articles to a spool file,
  `data_dir/tmp/parse_<date>/<chunk>.ndjson.zst` (one JSON object per line, a zstd stream), then
  returns only the article count.
* The parent is the single SQLite writer. When a worker finishes it reads the spool in batches of
  500 articles into `ArticleStore.add_many`, deletes the spool, and marks the chunk done. Memory
  stays flat (one batch), JSON rather than pickle keeps the spool format inspectable and free of
  deserialisation risk, and at most `parse_workers + 1` chunks are in flight, so the spool
  directory never holds more than that many finished chunks if the writer falls behind.
* Workers are `spawn`ed, not forked: the parent holds an open SQLite connection and fork would
  hand it to the children. With `parse_workers == 1` the same code runs in-process (no pool).

Non-obvious constraints:

* **Phases.** A stage sets its phase while it runs, `idle` when it finishes cleanly, and `failed`
  with a short error when it does not. `idle` between stages is deliberate: a standalone
  `download` must not leave the service reporting "building" forever.
* **Chunk failures stop the stage, not the in-flight work.** After the first failed chunk no new
  chunk is started; chunks already running finish (their work is kept), then the stage raises
  `BuildError` naming how many failed and the first one.
* **Pruning is download-time and conservative.** Only directories named like this snapshot id
  plus an 8-digit date are ever deleted, the one being built never is, and a failure to delete
  (the NAS is a network share) is logged, not fatal.
* **One build at a time.** `build_lock` takes a non-blocking `flock` on `state/build.lock`; the
  command line holds it across download, parse and index, so a second run (a hand-started stage
  while the monthly job is going, or `build-now` twice) refuses at once instead of racing the
  first for the same `.part` files, spool directory and store. The kernel drops the lock when the
  process dies, so a killed build never leaves a stale one; the spawned parse workers do not
  inherit it (`os.open` descriptors are close-on-exec).
* **A corrupt chunk is deleted, not skipped.** When a worker finds a chunk archive truncated or
  damaged (`EOFError`, `gzip.BadGzipFile`, `zlib.error`, `tarfile.ReadError`) the stage deletes
  that file and its `.etag` sidecar and fails; the next run's download then sees the chunk
  missing and fetches it again. Left in place, a chunk of the right size but wrong bytes would
  pass the download's size check and fail every parse until someone found it by hand.
* **A finished store is never reopened.** A store flagged complete may be the one being served;
  `run_parse` returns it untouched when it already covers the snapshot's chunks and fails loudly
  (`BuildError`) when Enterprise now lists a different chunk set for the same date.
* **One parser version per store.** A new store records `parse.PARSER_VERSION`; resuming an
  unfinished store begun by another version fails (`BuildError`) rather than mix the text of two
  parsers under one recorded version (docs/DESIGN.md section 12.6). Articles cross the spool with
  their `text_hash`, which the store checks against the text when it writes.

See docs/DESIGN.md sections 4, 5 and 12.
"""

from __future__ import annotations

import contextlib
import fcntl
import gzip
import io
import json
import logging
import multiprocessing
import os
import re
import shutil
import tarfile
import threading
import zlib
from collections.abc import Callable, Iterator
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path
from typing import Any, Final

import zstandard

from wiki_mirror.config import MirrorConfig
from wiki_mirror.enterprise import ChunkBudget, EnterpriseClient
from wiki_mirror.parse import PARSER_VERSION, iter_chunk_articles
from wiki_mirror.records import Article, SnapshotMeta
from wiki_mirror.store import (
    ArticleStore,
    StoreError,
    snapshot_from_json,
    snapshot_to_json,
    store_path,
)

__all__ = [
    "BuildError",
    "BuildLockedError",
    "BuildState",
    "build_lock",
    "chunk_path",
    "describe_failure",
    "ensure_free_space",
    "latest_downloaded_snapshot",
    "read_build_state",
    "read_snapshot_meta",
    "run_download",
    "run_parse",
    "snapshot_dir",
    "write_build_state",
]

#: The snapshot description a finished download leaves beside its chunks.
SNAPSHOT_FILE: Final = "snapshot.json"

logger = logging.getLogger("wiki_mirror.build")

#: The phases a build reports (docs/DESIGN.md section 5; `serving` is set by the index stage).
PHASES: Final = frozenset({"idle", "downloading", "parsing", "indexing", "serving", "failed"})

#: Articles per `add_many` transaction. Large enough that commit cost vanishes, small enough that
#: a batch of 1 MB articles is a few hundred MB at worst.
_INGEST_BATCH: Final = 500

#: zstd level for the spool files: they live seconds, so speed beats ratio.
_SPOOL_LEVEL: Final = 3

#: Longest error text kept in the state file (it is shown on a health endpoint).
_MAX_ERROR_CHARS: Final = 300

#: What a damaged or truncated chunk archive raises while it is read (`parse.iter_chunk_articles`
#: documents each). Any of these means the file's bytes are wrong, so the file is deleted and
#: fetched again; every other failure (a parser bug, a full disk) leaves the file alone.
_CORRUPT_ARCHIVE: Final = (EOFError, gzip.BadGzipFile, zlib.error, tarfile.ReadError)

_BYTES_PER_GB: Final = 1024**3


class BuildError(RuntimeError):
    """A build stage failed. The message is short and holds no secret."""


class BuildLockedError(BuildError):
    """Another build holds the lock; this run did nothing and changed no state."""


@contextlib.contextmanager
def build_lock(data_dir: Path) -> Iterator[None]:
    """Hold the build lock for the length of the block: one build at a time.

    A non-blocking exclusive `flock` on `data_dir/state/build.lock`. The kernel releases it when
    the holder exits or dies, however it dies, so there is no stale lock to clear by hand.

    Args:
        data_dir: The service's local data directory.

    Raises:
        BuildLockedError: Another process holds the lock ("a build is already running").
        OSError: The lock file cannot be created or opened (for instance it belongs to another
            user because a stage was once run by hand as root).
    """
    path = data_dir / "state" / "build.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o640)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            msg = "a build is already running"
            raise BuildLockedError(msg) from None
        yield
    finally:
        os.close(descriptor)  # closing the descriptor releases the lock


def ensure_free_space(directory: Path, floor_gb: int, what: str) -> None:
    """Refuse to start a large local write when the disk is nearly full.

    Args:
        directory: Somewhere on the disk that will be written (a missing directory is judged by
            its nearest existing parent, which is on the same disk).
        floor_gb: Free space, in GB, that must remain; 0 disables the check
            (`MirrorConfig.min_free_disk_gb`).
        what: What is about to be created, for the message ("a new article store").

    Raises:
        BuildError: Less than `floor_gb` GB is free. Nothing has been created.
    """
    if floor_gb <= 0:
        return
    probe = directory
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < floor_gb * _BYTES_PER_GB:
        msg = (
            f"not enough free disk to create {what}: {free / _BYTES_PER_GB:.1f} GB free under "
            f"{probe}, the floor is {floor_gb} GB (min_free_disk_gb)"
        )
        raise BuildError(msg)


@dataclass(frozen=True, slots=True)
class BuildState:
    """Where the current (or last) build is. Serialised to `state/build.json`."""

    #: `YYYY-MM-DD` of the snapshot being built; empty before any build.
    snapshot_date: str = ""
    #: One of `PHASES`.
    phase: str = "idle"
    chunks_total: int = 0
    chunks_downloaded: int = 0
    chunks_parsed: int = 0
    passages_indexed: int = 0
    #: Articles in the article store.
    articles: int = 0
    #: Why the build failed; empty otherwise. Short, never a secret.
    error: str = ""
    #: When this file was last written, ISO-8601 UTC (stamped by `write_build_state`).
    updated_at: str = ""

    def __post_init__(self) -> None:
        """Refuse a phase name this service does not know."""
        if self.phase not in PHASES:
            msg = f"unknown build phase {self.phase!r}"
            raise ValueError(msg)


def snapshot_dir(config: MirrorConfig, snapshot: SnapshotMeta) -> Path:
    """The NAS directory holding one snapshot's chunks: `<snapshot_dir>/<id>_<date>`."""
    return config.snapshot_dir / f"{snapshot.identifier}_{snapshot.tag}"


def chunk_path(directory: Path, chunk_id: str) -> Path:
    """Where a chunk's archive goes inside a snapshot directory."""
    return directory / f"{chunk_id}.tar.gz"


# -- build state ----------------------------------------------------------------------------


def _state_file(data_dir: Path) -> Path:
    """The state file's path."""
    return data_dir / "state" / "build.json"


def read_build_state(data_dir: Path) -> BuildState:
    """Read the build state.

    Args:
        data_dir: The service's local data directory.

    Returns:
        The state; an idle, empty one when no build has run. A file that cannot be read or
        parsed also gives an idle state, with the problem in `error`, so a health check
        degrades loudly instead of crashing.
    """
    path = _state_file(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not an object")  # handled below, with the other bad-file cases
        known = {field.name for field in fields(BuildState)}
        return BuildState(**{key: value for key, value in raw.items() if key in known})
    except FileNotFoundError:
        return BuildState()
    except (OSError, ValueError, TypeError):
        return BuildState(error="the build state file is unreadable")


def write_build_state(
    data_dir: Path,
    state: BuildState,
    *,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Write the build state atomically (temp file in the same directory, then rename).

    Args:
        data_dir: The service's local data directory.
        state: The state to record; its `updated_at` is replaced with the current time.
        utc_now: The clock; tests inject theirs.
    """
    stamped = replace(state, updated_at=utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"))
    path = _state_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(_state_dict(stamped), indent=2), encoding="utf-8")
    temp.replace(path)


def _state_dict(state: BuildState) -> dict[str, Any]:
    """The state as a plain dict (dataclasses.asdict, without its recursive deep copy)."""
    return {field.name: getattr(state, field.name) for field in fields(state)}


class _Tracker:
    """The build state for one stage, updated safely from several threads."""

    def __init__(self, data_dir: Path, snapshot: SnapshotMeta) -> None:
        """Start from the stored state if it is for this snapshot, else from a fresh one."""
        self._data_dir = data_dir
        stored = read_build_state(data_dir)
        # A new snapshot starts from zero; the same snapshot keeps its counters (a re-run).
        self._state = (
            stored
            if stored.snapshot_date == snapshot.date
            else BuildState(snapshot_date=snapshot.date)
        )
        self._lock = threading.Lock()

    def update(self, **changes: Any) -> None:
        """Replace fields and write the file."""
        with self._lock:
            self._state = replace(self._state, **changes)
            write_build_state(self._data_dir, self._state)

    def bump(self, name: str, by: int = 1) -> None:
        """Add to a counter field and write the file."""
        with self._lock:
            self._state = replace(self._state, **{name: getattr(self._state, name) + by})
            write_build_state(self._data_dir, self._state)

    def fail(self, exc: BaseException) -> None:
        """Record a failure with a short, secret-free reason."""
        self.update(phase="failed", error=_short(exc))


def _short(exc: BaseException) -> str:
    """A one-line description of an exception for the state file."""
    return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]


def describe_failure(exc: BaseException) -> str:
    """What the build state's `error` may say about an exception, for health and `status`.

    A `BuildError` (and so an `IndexBuildError`) carries its message, because its contract is a
    short text with no secret, and "disk below the floor" or "no complete download" is what the
    operator needs to read. Any other exception is the class name alone: a library's message can
    carry a URL, a path or a header.
    """
    return _short(exc) if isinstance(exc, BuildError) else type(exc).__name__


# -- download -------------------------------------------------------------------------------


def run_download(config: MirrorConfig, client: EnterpriseClient) -> SnapshotMeta:
    """Download every chunk of the configured snapshot to the NAS, resuming what is there.

    Args:
        config: The service settings (`snapshot_dir`, `download_concurrency`,
            `monthly_chunk_budget`, `retain_snapshots`).
        client: A signed-in-on-demand Enterprise client.

    Returns:
        The snapshot that was downloaded, for the parse stage.

    Raises:
        BuildError: One or more chunks failed (the message names the first). The partial
            downloads are kept; re-running resumes them.
        EnterpriseError: The snapshot could not be described.
    """
    snapshot = client.snapshot(config.snapshot_identifier)
    target = snapshot_dir(config, snapshot)
    target.mkdir(parents=True, exist_ok=True)
    tracker = _Tracker(config.data_dir, snapshot)
    tracker.update(
        phase="downloading",
        chunks_total=len(snapshot.chunk_ids),
        chunks_downloaded=0,
        error="",
    )
    budget = ChunkBudget(
        config.data_dir / "state" / "enterprise_budget.json", config.monthly_chunk_budget
    )
    stop = threading.Event()

    def fetch(chunk_id: str) -> None:
        if stop.is_set():
            return
        client.download_chunk(
            snapshot.identifier, chunk_id, chunk_path(target, chunk_id), budget=budget
        )
        tracker.bump("chunks_downloaded")

    failures: list[tuple[str, BaseException]] = []
    with ThreadPoolExecutor(max_workers=config.download_concurrency) as pool:
        futures = {pool.submit(fetch, chunk_id): chunk_id for chunk_id in snapshot.chunk_ids}
        for future in as_completed(futures):
            problem = future.exception()
            if problem is not None:
                failures.append((futures[future], problem))
                # Stops chunks that have not started; the ones running finish and keep their bytes.
                stop.set()
    if failures:
        chunk_id, problem = failures[0]
        error = BuildError(
            f"{len(failures)} of {len(snapshot.chunk_ids)} chunk downloads failed;"
            f" first: {chunk_id}: {_short(problem)}"
        )
        tracker.fail(error)
        raise error from problem
    # Written last: its presence marks the directory as a complete download, which is how a
    # standalone `parse` finds what is on disk without asking Enterprise (which may already
    # name a newer snapshot).
    _write_snapshot_meta(target, snapshot)
    tracker.update(phase="idle", error="")
    _prune_snapshots(config, target)
    logger.info(
        "downloaded snapshot %s (%d chunks)",
        snapshot.date,
        len(snapshot.chunk_ids),
        extra={"component": "build", "event": "download_done"},
    )
    return snapshot


def _write_snapshot_meta(directory: Path, snapshot: SnapshotMeta) -> None:
    """Record the snapshot description beside its chunks, atomically (temp file, then rename)."""
    target = directory / SNAPSHOT_FILE
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(snapshot_to_json(snapshot), encoding="utf-8")
    temp.replace(target)


def read_snapshot_meta(path: Path) -> SnapshotMeta:
    """Read the snapshot description a download left on the NAS.

    Args:
        path: A snapshot directory (`snapshot_dir(...)`) or its `snapshot.json` file.

    Raises:
        BuildError: The file is missing or is not a snapshot description.
    """
    target = path / SNAPSHOT_FILE if path.is_dir() else path
    try:
        return snapshot_from_json(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        msg = f"{target} is missing or is not a snapshot description"
        raise BuildError(msg) from None


def latest_downloaded_snapshot(config: MirrorConfig) -> SnapshotMeta | None:
    """The newest snapshot fully downloaded to the NAS, or None.

    "Fully" means its directory has a readable `snapshot.json` (written when a download
    finishes) and every chunk archive that file lists. Directories that do not meet that are
    skipped, so an interrupted newer download never hides an older complete one.

    Args:
        config: The service settings (`snapshot_dir`, `snapshot_identifier`).
    """
    for entry in _snapshot_dirs(config):
        try:
            snapshot = read_snapshot_meta(entry)
        except BuildError:
            continue
        if all(chunk_path(entry, chunk_id).is_file() for chunk_id in snapshot.chunk_ids):
            return snapshot
    return None


def _snapshot_dirs(config: MirrorConfig) -> list[Path]:
    """Snapshot directories for the configured snapshot id on the NAS, newest first.

    Only names of the form `<identifier>_<8 digits>` count; symlinks and files are ignored. An
    unreadable or missing `snapshot_dir` gives an empty list.
    """
    pattern = re.compile(rf"^{re.escape(config.snapshot_identifier)}_\d{{8}}$")
    try:
        found = [
            entry
            for entry in config.snapshot_dir.iterdir()
            if entry.is_dir() and not entry.is_symlink() and pattern.match(entry.name)
        ]
    except OSError:
        return []
    return sorted(found, key=lambda entry: entry.name, reverse=True)


def _prune_snapshots(config: MirrorConfig, current: Path) -> None:
    """Delete snapshot directories beyond `retain_snapshots`; `current` is always kept."""
    found = _snapshot_dirs(config)
    keep = set(found[: config.retain_snapshots]) | {current}
    for entry in found:
        if entry in keep:
            continue
        try:
            shutil.rmtree(entry)
        except OSError:
            # Not fatal: the next build tries again; the NAS has room (docs/DESIGN.md section 4).
            logger.warning(
                "could not remove the old snapshot %s",
                entry.name,
                extra={"component": "build", "event": "prune_failed"},
            )
        else:
            logger.info(
                "removed the old snapshot %s",
                entry.name,
                extra={"component": "build", "event": "pruned"},
            )


# -- parse ----------------------------------------------------------------------------------


def run_parse(config: MirrorConfig, snapshot: SnapshotMeta) -> Path:
    """Parse the downloaded chunks into the snapshot's article store, skipping finished ones.

    Args:
        config: The service settings (`snapshot_dir`, `data_dir`, `parse_workers`).
        snapshot: The snapshot to parse (from `run_download`, or the store's own record).

    Returns:
        The path of the finalized store, ready to serve and index.

    Raises:
        BuildError: A chunk is missing from the NAS, a chunk failed to parse (a corrupt archive
            is deleted so the next download fetches it again), the disk is below
            `min_free_disk_gb` when the store would be created, the existing store belongs to a
            different snapshot or was begun by another parser version, or a finished store
            exists for a different chunk list. Chunks parsed before the failure stay done.
    """
    directory = snapshot_dir(config, snapshot)
    path = store_path(config.data_dir, snapshot)
    tracker = _Tracker(config.data_dir, snapshot)
    try:
        finished = _finished_store(path, snapshot)
    except BuildError as exc:
        tracker.fail(exc)
        raise
    if finished:
        # Nothing to do, and the finished store may be being served: do not reopen it for
        # writing (that would switch it back to WAL under the API's feet).
        tracker.update(phase="idle", error="")
        return path
    store: ArticleStore | None = None
    try:
        if path.exists():
            store = ArticleStore.open(path, readonly=False)
            if (store.snapshot.identifier, store.snapshot.date) != (
                snapshot.identifier,
                snapshot.date,
            ):
                msg = f"{path} holds a different snapshot ({store.snapshot.date})"
                raise BuildError(msg)
            if store.parser_version != PARSER_VERSION:
                # Resuming would fill one store with the output of two parsers, and the recorded
                # version could then be true of only some of its text.
                msg = (
                    f"{path} was begun by parser version {store.parser_version} and this code is "
                    f"{PARSER_VERSION}; delete the unfinished store to parse the snapshot again"
                )
                raise BuildError(msg)
        else:
            ensure_free_space(config.data_dir, config.min_free_disk_gb, "a new article store")
            store = ArticleStore.create(path, snapshot, parser_version=PARSER_VERSION)
        done = store.chunks_done()
        pending = [chunk_id for chunk_id in snapshot.chunk_ids if chunk_id not in done]
        missing = [
            chunk_id for chunk_id in pending if not chunk_path(directory, chunk_id).is_file()
        ]
        if missing:
            msg = f"{len(missing)} chunks are not downloaded (first: {missing[0]}); run download"
            raise BuildError(msg)
        tracker.update(
            phase="parsing",
            chunks_total=len(snapshot.chunk_ids),
            chunks_parsed=len(done),
            articles=store.count(),
            error="",
        )
        spool = config.data_dir / "tmp" / f"parse_{snapshot.tag}"
        shutil.rmtree(spool, ignore_errors=True)  # leftovers of a crashed run
        spool.mkdir(parents=True)

        def ingest(chunk_id: str) -> None:
            _ingest_spool(store, spool / f"{chunk_id}.ndjson.zst")
            store.mark_chunk_done(chunk_id)
            tracker.update(
                chunks_parsed=len(store.chunks_done()), articles=store.count(), error=""
            )

        jobs = [
            (chunk_id, chunk_path(directory, chunk_id), spool / f"{chunk_id}.ndjson.zst")
            for chunk_id in pending
        ]
        failures = _run_jobs(jobs, config.parse_workers, ingest)
        shutil.rmtree(spool, ignore_errors=True)
        if failures:
            chunk_id, problem = failures[0]
            discarded = _discard_corrupt_chunks(directory, failures)
            # The advice comes before the cause: the state file truncates long messages.
            advice = f" ({discarded} corrupt files deleted; download again)" if discarded else ""
            error = BuildError(
                f"{len(failures)} of {len(pending)} chunks failed to parse{advice};"
                f" first: {chunk_id}: {_short(problem)}"
            )
            raise error from problem
        store.finalize()
        tracker.update(phase="idle", articles=store.count(), error="")
    except BaseException as exc:
        tracker.fail(exc)
        raise
    finally:
        if store is not None:
            store.close()
    logger.info(
        "parsed snapshot %s into %s",
        snapshot.date,
        path.name,
        extra={"component": "build", "event": "parse_done"},
    )
    return path


def _finished_store(path: Path, snapshot: SnapshotMeta) -> bool:
    """Whether a finished store for this snapshot already holds every chunk.

    Returns:
        True when `path` is a complete store covering all of the snapshot's chunks; False when
        there is no store there, or it is incomplete or unreadable (the caller resumes it).

    Raises:
        BuildError: `path` is a complete store, but for a different chunk list than the one
            Enterprise lists now. A finished store may be serving and is never reopened for
            writing, so the only honest answer is to say so and leave the choice to the operator.
    """
    if not path.is_file():
        return False
    try:
        probe = ArticleStore.open(path, readonly=True)
    except StoreError:
        return False  # incomplete or unreadable: the caller resumes or reports it
    try:
        recorded = probe.snapshot.date
        covered = set(snapshot.chunk_ids) <= probe.chunks_done()
    finally:
        probe.close()
    if recorded != snapshot.date:
        msg = f"{path} holds a different snapshot ({recorded})"
        raise BuildError(msg)
    if not covered:
        msg = (
            f"{path.name} is already built for {snapshot.date} from a different chunk list "
            f"than the {len(snapshot.chunk_ids)} chunks Enterprise lists now; a finished store "
            "is never reopened. Delete it (it is not served if the alias points elsewhere) to "
            "rebuild"
        )
        raise BuildError(msg)
    return True


def _discard_corrupt_chunks(
    directory: Path, failures: list[tuple[str, BaseException]]
) -> int:
    """Delete the chunk archives whose failure says their bytes are wrong, and their sidecars.

    The next download finds the chunk missing and fetches it again (a chunk file of the right
    size would otherwise be accepted as downloaded for ever).

    Args:
        directory: The snapshot's directory on the NAS.
        failures: `(chunk_id, exception)` pairs from the parse jobs.

    Returns:
        How many chunk archives were deleted. A deletion that fails (the NAS is a share) is
        logged and not counted; the stage fails either way.
    """
    discarded = 0
    for chunk_id, problem in failures:
        if not isinstance(problem, _CORRUPT_ARCHIVE):
            continue
        archive = chunk_path(directory, chunk_id)
        try:
            archive.unlink(missing_ok=True)
            archive.with_name(archive.name + ".etag").unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "could not delete the corrupt chunk %s",
                chunk_id,
                extra={"component": "build", "event": "discard_failed"},
            )
            continue
        discarded += 1
        logger.warning(
            "deleted the corrupt chunk %s (%s); the next download fetches it again",
            chunk_id,
            type(problem).__name__,
            extra={"component": "build", "event": "chunk_discarded"},
        )
    return discarded


def _run_jobs(
    jobs: list[tuple[str, Path, Path]],
    workers: int,
    ingest: Callable[[str], None],
) -> list[tuple[str, BaseException]]:
    """Run chunk-to-spool jobs, calling `ingest(chunk_id)` in this process as each completes.

    At most `workers + 1` jobs are in flight, so a slow writer cannot let finished spools pile
    up. After the first failure no new job starts; running jobs finish and are ingested.

    Args:
        jobs: `(chunk_id, chunk archive, spool file)` triples.
        workers: Parse processes; 1 (or a single job) runs in this process with no pool.
        ingest: Called with a chunk id once its spool file is complete.

    Returns:
        The failures as `(chunk_id, exception)`; empty when everything parsed.
    """
    failures: list[tuple[str, BaseException]] = []
    if workers <= 1 or len(jobs) <= 1:
        for chunk_id, source, spool_file in jobs:
            try:
                _spool_chunk(str(source), str(spool_file))
                ingest(chunk_id)
            except Exception as exc:
                # Reported per chunk, then the stage stops: any failure here (corrupt archive,
                # full disk) is a chunk failure, and run_parse raises it as a BuildError.
                failures.append((chunk_id, exc))
                break
        return failures

    queue = iter(jobs)
    pending: dict[Future[int], str] = {}
    # Spawn, not fork: the parent holds an open SQLite connection (module docstring).
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=min(workers, len(jobs)), mp_context=context) as pool:

        def top_up() -> None:
            while not failures and len(pending) < workers + 1:
                job = next(queue, None)
                if job is None:
                    return
                chunk_id, source, spool_file = job
                try:
                    pending[pool.submit(_spool_chunk, str(source), str(spool_file))] = chunk_id
                except BrokenProcessPool as exc:
                    failures.append((chunk_id, exc))

        top_up()
        while pending:
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                chunk_id = pending.pop(future)
                problem = future.exception()
                if problem is None:
                    try:
                        ingest(chunk_id)
                    except Exception as exc:  # an ingest failure is a chunk failure too
                        failures.append((chunk_id, exc))
                else:
                    failures.append((chunk_id, problem))
            top_up()
    return failures


def _spool_chunk(source: str, spool_file: str) -> int:
    """Parse one chunk archive into a spool file (runs in a worker process).

    Writes to a temporary name and renames at the end, so the parent only ever sees a complete
    spool. Arguments are strings because they cross a process boundary.

    Returns:
        The number of articles written.
    """
    count = 0
    temp = spool_file + ".partial"
    compressor = zstandard.ZstdCompressor(level=_SPOOL_LEVEL)
    with Path(temp).open("wb") as raw, compressor.stream_writer(raw) as out:
        for article in iter_chunk_articles(Path(source)):
            line = json.dumps(_article_dict(article), ensure_ascii=False) + "\n"
            out.write(line.encode("utf-8"))
            count += 1
    Path(temp).replace(spool_file)
    return count


def _ingest_spool(store: ArticleStore, spool_file: Path) -> None:
    """Load a spool file into the store in bounded batches, then delete it."""
    with spool_file.open("rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as reader:
        lines = (json.loads(line) for line in io.BufferedReader(reader))
        for batch in batched((_article_from_dict(data) for data in lines), _INGEST_BATCH):
            store.add_many(batch)
    spool_file.unlink()


def _article_dict(article: Article) -> dict[str, Any]:
    """An article as a JSON-able dict (the spool format)."""
    return {
        "page_id": article.page_id,
        "title": article.title,
        "revision_id": article.revision_id,
        "revision_time": article.revision_time,
        "text": article.text,
        "text_hash": article.text_hash,
        "abstract": article.abstract,
        "categories": list(article.categories),
        "redirects": list(article.redirects),
        "is_disambiguation": article.is_disambiguation,
        "license": article.license,
    }


def _article_from_dict(data: dict[str, Any]) -> Article:
    """Inverse of `_article_dict`."""
    return Article(
        page_id=data["page_id"],
        title=data["title"],
        revision_id=data["revision_id"],
        revision_time=data["revision_time"],
        text=data["text"],
        text_hash=data["text_hash"],
        abstract=data["abstract"],
        categories=tuple(data["categories"]),
        redirects=tuple(data["redirects"]),
        is_disambiguation=data["is_disambiguation"],
        license=data["license"],
    )
