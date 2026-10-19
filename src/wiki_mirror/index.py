"""The passage index: every article, cut into passages and loaded into Qdrant as sparse vectors.

The last stage of a build (`download` -> `parse` -> `index`, `wiki_mirror.__main__`).
It reads the finished article store through `records.ArticleSource`, cuts each article into
passages with the chunker (`text.sanitize`), encodes each passage with the sparse encoder
(`text.sparse`) and upserts it into a new collection
`wikipedia_lexical_<born>`, where `<born>` is the 8-digit tag of the snapshot the collection is
first built from. When every passage is in, the alias `wikipedia_lexical` is pointed at that
collection in one atomic call and the collections it no longer needs are deleted, so the serving
side (`serving.ServedSnapshot`) follows the alias and a full rebuild swaps in without a moment of
downtime or a half-built index being served (the build-beside-live, swap-the-alias pattern). The
article stores and index states of snapshots that fall out of the kept pair are deleted with them.

This module is the *full build*: the rare path (a new chunker, a damaged index) that builds a
collection beside the live one. From W3 on, the monthly update changes the live collection in
place instead and only moves `applied_snapshot` in its index state (below), never the alias
(docs/DESIGN.md section 12.2).

Non-obvious constraints:

* **Lexical only; the semantic index is a second collection.** The collection has one sparse
  vector, `lexical`, with `Modifier.IDF` (Qdrant computes inverse document frequency from the
  points it holds, so the idf is never stored in the vectors). W2's dense vectors go in their own
  collection, `wikipedia_semantic_<born>`, sharing point ids and payload (docs/DESIGN.md section
  12.2), so nothing here changes when it arrives. `indexable_chunks` and `passage_payload` are
  what the two builds share: the cut and the payload.
* **A collection is named for its birth, and its index state says what it holds.** The name
  never changes, but the monthly update (W3) rewrites the collection's points in place, so the
  snapshot it reflects drifts away from `<born>`. The truth is `applied_snapshot` in the
  collection's index state file (`state/index_lexical_<born>.json`, below): the tag of the
  snapshot whose article store the passages were cut from. A full build sets it to `<born>` when
  it completes; `applied_snapshot()` is how `serving` reads it. Serving pairs a collection with
  the store of its applied snapshot, so moving `applied_snapshot` is all W3 has to do to make
  the API switch stores, with no alias move.
* **Point ids are deterministic** (`records.passage_point_id`), so re-running over the same
  article overwrites its own points: resume after a crash is "repeat the last few batches".
* **No text in the payload.** The article store serves the text; a payload with text would
  double a 13M-passage index for nothing. The payload is exactly `page_id`, `revision_id`,
  `chunk`, `section`, `disambiguation` and `text_hash`. The hash is the article's
  (`records.text_hash`): it names which version of the text the passage was cut from, so a
  search can refuse a hit whose text is not the one it would cut the snippet from.
* **Encoding is CPU-bound pure Python**, so it is spread over a process pool; the main process
  only upserts. Results are consumed in submission order, which keeps the checkpoint (the last
  page id fully indexed) honest: everything at or below it is in Qdrant. The pool uses the
  `spawn` start method, because forking a process that holds Qdrant client threads is unsafe.
* **Resuming seeks.** The store is read with `iter_articles(after=<checkpoint>)`, so a resumed
  run decompresses only the articles it still has to index.
* **The chunker is fingerprinted.** A passage's number is its place in `chunk_text`'s cut of the
  article, and search re-cuts the stored text to find the snippet, so the cut must be the same at
  query time as at index time. `chunker_fingerprint()` (the chunk sizes plus `CHUNKER_VERSION`,
  which a person bumps when `text.sanitize`'s algorithm changes) is written into the index state of
  every collection; `serving` warns, and health reads `degraded`, when it differs from the running
  code's, and a resume refuses to mix two cuts in one collection. The point payload is unchanged.
* **The index state is also the build checkpoint.** One JSON file per collection, written
  atomically: how far the build got (`last_page_id`, counts, `complete`), the chunker, and
  `applied_snapshot` (empty until the build completes). Losing it costs a restart of the
  collection, and a collection that serves with no usable state is never rebuilt over.
* **Segments are capped** (`_MAX_SEGMENT_KB`). When Qdrant's optimizer rewrites a segment to
  clear deleted points (every W3 update deletes some) it needs free disk the size of that
  segment; uncapped, 13M points can make one segment a third of the 139 GB disk.
* **Local files follow the collections.** After a swap, the article stores and index states of
  snapshots older than the new one are deleted unless their collection is kept (the new one and
  the one it replaced, the way back; see `_swap_alias`): a kept collection keeps the store of its
  `applied_snapshot`. Newer files are never touched: they are the resumable work of a build in
  progress.
* **A new store or collection needs free disk** (`min_free_disk_gb`), checked before it is
  created.
* **The build state is written through `build.write_build_state`**; tests inject their own
  reader and writer.

See docs/DESIGN.md sections 5, 6 and 12.
"""

from __future__ import annotations

import contextlib
import json
import logging
import multiprocessing
import re
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, NamedTuple

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException

from wiki_mirror.build import (
    BuildError,
    describe_failure,
    ensure_free_space,
    read_build_state,
    write_build_state,
)
from wiki_mirror.config import MirrorConfig
from wiki_mirror.passages import CHUNK_CONFIG, CHUNKER_VERSION
from wiki_mirror.passages import chunker_fingerprint as _chunker_fingerprint
from wiki_mirror.records import (
    PASSAGE_STRIDE,
    Article,
    ArticleSource,
    SnapshotMeta,
    passage_point_id,
)
from wiki_mirror.text.sanitize import Chunk, chunk_text
from wiki_mirror.text.sparse import SparseEncoder

__all__ = [
    "ALIAS",
    "CHUNKER_VERSION",
    "CHUNK_CONFIG",
    "INDEX_FORMAT",
    "SPARSE_VECTOR",
    "IndexBuildError",
    "aliased_collection",
    "applied_snapshot",
    "chunker_fingerprint",
    "collection_name",
    "index_state_file",
    "indexable_chunks",
    "indexed_chunker",
    "passage_payload",
    "run_index",
]

logger = logging.getLogger("wiki_mirror.index")

#: The name the serving side follows. An alias, so a full rebuild replaces the index atomically.
#: `lexical` because W2 adds `wikipedia_semantic` beside it (docs/DESIGN.md section 12.2).
ALIAS: Final = "wikipedia_lexical"

#: Bumped by hand whenever what is stored in Qdrant or beside it changes shape in a way a
#: running service cannot read from an older build: the collection naming, the point payload
#: fields, or the index-state file's layout (W1.5 was such a change). It is the `index=` part of
#: `deploy/FORMAT`, which `update.sh` compares across commits so that a nightly update
#: never restarts the API onto an index it cannot read (docs/DESIGN.md section 12).
INDEX_FORMAT: Final = 1

#: Name of the sparse vector in the collection.
SPARSE_VECTOR: Final = "lexical"

#: Collections this module creates, and so the only ones it may delete:
#: `wikipedia_lexical_<born>`, where the group is the born tag.
_OWN_COLLECTION: Final = re.compile(r"^wikipedia_lexical_(\d{8})$")

#: What a snapshot tag looks like (`SnapshotMeta.tag`), the form `applied_snapshot` takes.
_SNAPSHOT_TAG: Final = re.compile(r"\d{8}")

#: Largest segment Qdrant may build, in kilobytes (its unit). When the optimizer rewrites a
#: segment to clear deleted points it needs free space the size of that segment; uncapped, 13M
#: points can make one segment a third of the 139 GB disk (docs/DESIGN.md section 12.3). It was
#: 2_000_000 until the first full build (2026-10-17): Qdrant 1.12 does not seem to count sparse
#: vectors fully against it, so segments grew to about 2.5M passages (some 3.5-4 GB of sparse
#: data), and building one on the heap OOM-killed Qdrant even with one optimization at a time.
#: 400_000 was set live on that collection the same day, after which Qdrant's anonymous memory
#: oscillated around 2-3 GB instead of climbing.
_MAX_SEGMENT_KB: Final = 400_000

#: Segment optimizations (merges) the collection may run at once. Each merge of sparse segments
#: builds its inverted index on the heap, and that grows with the segments: during the first
#: full build on the mirror LXC (2026-10-17) three concurrent merges OOM-killed Qdrant at a 4 GB cap
#: (8.7M passages) and again at 6 GB (12.8M). One at a time is slower to settle but bounds the
#: heap by the largest single merge; set live on that collection through the API the same day.
_MAX_OPTIMIZATION_THREADS: Final = 1

#: Points per upsert call. Large enough to amortise the round trip, small enough that one
#: request stays a few MB (a passage's sparse vector is a few hundred index/value pairs).
_UPSERT_POINTS: Final = 256

#: Articles per unit of encoding work. A pool task has to cost far more than pickling it.
_BATCH_ARTICLES: Final = 64

#: Article batches between checkpoints; a crash repeats at most this many batches (idempotent).
_CHECKPOINT_EVERY: Final = 20

#: Pool tasks in flight per worker: enough to keep workers busy while the main process upserts.
_IN_FLIGHT_PER_WORKER: Final = 2

#: Tries per upsert before the build gives up, and seconds between them. Qdrant blocks writes
#: while its optimizer holds the segment locks; on the mirror LXC (2026-10-17), under its cgroup's
#: memory throttle, one upsert waited 144 s ("Trying to read-lock all collection segments is
#: taking a long time") and ended the build twice. An upsert is idempotent (deterministic point
#: ids), so a timed-out one is simply sent again; only transport failures are retried, never a
#: refusal (`UnexpectedResponse`), which is a real error.
_UPSERT_ATTEMPTS: Final = 3
_UPSERT_RETRY_SECONDS: Final = 30.0

StateReader = Callable[[Path], Any]
StateWriter = Callable[[Path, Any], None]


class IndexBuildError(BuildError):
    """The index cannot be built or swapped in as asked; the message says what to do."""


class _Point(NamedTuple):
    """One passage, encoded and ready to upsert. Plain types, so it pickles cheaply."""

    point_id: int
    indices: list[int]
    values: list[float]
    payload: dict[str, Any]


@dataclass(frozen=True)
class _BatchResult:
    """What encoding one batch of articles produced."""

    #: The highest page id in the batch; the checkpoint once the batch is upserted.
    last_page_id: int
    points: list[_Point]
    #: Passages dropped because the chunk index does not fit `PASSAGE_STRIDE`.
    over_stride: int
    #: Passages dropped because no term survived tokenising (nothing to match on).
    empty: int


@dataclass(frozen=True)
class _IndexState:
    """One collection's index state: its build checkpoint and what it currently reflects.

    Kept on local disk beside the build state (`index_state_file`). The build fills it in; from
    W3 on the monthly update moves `applied_snapshot` in the same file.
    """

    #: The tag of the snapshot the collection was first built from: the `<born>` of its name.
    born: str
    #: Every article with a page id at or below this is fully in the collection.
    last_page_id: int
    passages: int
    over_stride: int
    empty: int
    #: All articles are in; only the alias swap and cleanup may remain.
    complete: bool
    #: `chunker_fingerprint()` of the code that cut these passages; empty when unrecorded.
    chunker: str = ""
    #: The tag of the snapshot whose article store the passages currently reflect; empty until
    #: the build completes (and so nothing can be served from the collection yet).
    applied_snapshot: str = ""

    def as_json(self) -> str:
        """The index state file's contents."""
        return json.dumps(
            {
                "born": self.born,
                "last_page_id": self.last_page_id,
                "passages": self.passages,
                "over_stride": self.over_stride,
                "empty": self.empty,
                "complete": self.complete,
                "chunker": self.chunker,
                "applied_snapshot": self.applied_snapshot,
            }
        )


def collection_name(born: SnapshotMeta) -> str:
    """The Qdrant collection a full build of one snapshot creates: `wikipedia_lexical_<born>`.

    Args:
        born: The snapshot the collection is first built from. The name records only that birth;
            what the collection reflects later is `applied_snapshot`.
    """
    return f"wikipedia_lexical_{born.tag}"


def _born(collection: str) -> str | None:
    """The born tag in a collection's name, or None when this module did not create the name."""
    # `fullmatch`, so a name with a trailing newline (which `$` would let through) is not ours.
    found = _OWN_COLLECTION.fullmatch(collection)
    return found[1] if found else None


def chunker_fingerprint() -> str:
    """A short name for how the running code cuts articles into passages, e.g. `v1:512/50`.

    `passages.chunker_fingerprint` applied to this module's `CHUNKER_VERSION` and `CHUNK_CONFIG`
    (re-exports of the `passages` values), so patching them here, as the tests do, moves the
    fingerprint the build records and `serving` compares.
    """
    return _chunker_fingerprint(CHUNKER_VERSION, CHUNK_CONFIG)


def index_state_file(data_dir: Path, born: str) -> Path:
    """Where the index state of collection `wikipedia_lexical_<born>` lives.

    Args:
        data_dir: The service's local data directory.
        born: The collection's born tag (`SnapshotMeta.tag`).
    """
    return data_dir / "state" / f"index_lexical_{born}.json"


def _state_object(data_dir: Path, collection: str) -> dict[str, Any] | None:
    """A collection's index state file as a plain JSON object, or None; never logs or raises.

    Silent on purpose: `serving` reads this every few seconds, and it reports a missing or
    unreadable state once, itself, rather than have every look warn.
    """
    born = _born(collection)
    if born is None:
        return None
    try:
        raw = json.loads(index_state_file(data_dir, born).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def indexed_chunker(data_dir: Path, collection: str) -> str | None:
    """The chunker fingerprint that was recorded when `collection` was built.

    Args:
        data_dir: The service's local data directory.
        collection: A collection name this module created (`wikipedia_lexical_<born>`).

    Returns:
        The fingerprint, or None when it cannot be known: no index state file, an unreadable
        one, or one written before fingerprints existed. None is "no verdict", not "mismatch".
    """
    state = _state_object(data_dir, collection)
    value = None if state is None else state.get("chunker")
    return value if isinstance(value, str) and value else None


def applied_snapshot(data_dir: Path, collection: str) -> str | None:
    """The snapshot whose article store `collection` currently reflects, from its index state.

    This is what `serving` pairs the collection with. It is the collection's birth snapshot
    after a full build, and (from W3) the snapshot a monthly update last applied in place; the
    collection's name never says which.

    Args:
        data_dir: The service's local data directory.
        collection: A collection name this module created (`wikipedia_lexical_<born>`).

    Returns:
        The eight-digit snapshot tag, or None when it cannot be known: the name is not one this
        module creates, there is no index state file, it is unreadable, or the build has not
        completed. Without it there is no telling which store the passages were cut from, so
        callers treat None as a fault to report, not as "use the newest store".
    """
    state = _state_object(data_dir, collection)
    value = None if state is None else state.get("applied_snapshot")
    return value if isinstance(value, str) and _SNAPSHOT_TAG.fullmatch(value) else None


def run_index(
    config: MirrorConfig,
    store: ArticleSource,
    client: QdrantClient,
    *,
    workers: int | None = None,
    state_reader: StateReader | None = None,
    state_writer: StateWriter | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    batch_articles: int = _BATCH_ARTICLES,
    checkpoint_every: int = _CHECKPOINT_EVERY,
) -> int:
    """Index every article of a snapshot (a full build) and swap the alias onto the result.

    Creates `wikipedia_lexical_<tag>` (or resumes it from the index state at
    `data_dir/state/index_lexical_<tag>.json`), upserts every passage, records the snapshot as
    the collection's `applied_snapshot` in that state, then points the alias `wikipedia_lexical`
    at it and deletes the `wikipedia_lexical_*` collections that are neither the new one nor
    the one the alias pointed at before (the way back; when the alias already pointed at the new
    collection, the newest other one is kept instead). The article stores and index states of
    older snapshots whose collection was deleted go with them. The build state moves to
    `indexing`, then `serving` (`failed`, with the exception's class name, if this raises).

    Args:
        config: Service settings (`data_dir`, `lexical_dimensions`, `parse_workers`).
        store: The finished article store, read in page-id order.
        client: A Qdrant client for the loopback server.
        workers: Encoding processes; `None` means `config.parse_workers`. 1 encodes in this
            process (no pool), which is what tests use.
        state_reader: Reads the build state; defaults to `build.read_build_state`.
        state_writer: Writes it; defaults to `build.write_build_state`.
        clock: Supplies `updated_at` for the build state.
        batch_articles: Articles per encoding task.
        checkpoint_every: Task results between index-state and build-state writes.

    Returns:
        The number of passages in the collection.

    Raises:
        IndexBuildError: The store is empty, or the snapshot's collection already serves the
            alias but has no usable index state (a completed build whose state was lost, or
            one cut by a different chunker). Nothing is modified.
        BuildError: A new collection is needed and the disk is below `min_free_disk_gb`.
        Exception: Whatever Qdrant, the store or the encoder raises. The index state is intact,
            so running the command again resumes.
    """
    reader = state_reader or read_build_state
    writer = state_writer or write_build_state
    snapshot = store.snapshot
    name = collection_name(snapshot)
    state_path = index_state_file(config.data_dir, snapshot.tag)
    articles = store.count()
    if articles == 0:
        # Swapping an empty index in would make the service answer every question "no results".
        msg = f"the article store for {snapshot.date} is empty; nothing to index"
        raise IndexBuildError(msg)

    def publish(**changes: Any) -> None:
        """Record progress in the build state, which `status` and `/v1/wiki/health` read."""
        writer(
            config.data_dir,
            replace(reader(config.data_dir), updated_at=clock().isoformat(), **changes),
        )

    try:
        publish(phase="indexing", articles=articles, error="")
        state = _begin(config, client, name, snapshot, state_path)
        if not state.complete:
            state = _index_articles(
                config,
                store,
                client,
                name,
                state,
                state_path,
                workers=config.parse_workers if workers is None else workers,
                batch_articles=batch_articles,
                checkpoint_every=checkpoint_every,
                progress=lambda passages: publish(passages_indexed=passages),
            )
        kept = _swap_alias(client, name)
        _prune_local(config, snapshot.tag, kept)
        publish(phase="serving", passages_indexed=state.passages, articles=articles)
    except Exception as exc:
        # Recorded so `status` and the health route say why the previous snapshot is still the
        # one serving; `describe_failure` keeps a library's message (which can carry a URL) out.
        with contextlib.suppress(Exception):
            publish(phase="failed", error=f"index: {describe_failure(exc)}")
        raise
    if state.over_stride or state.empty:
        logger.warning(
            "index %s: %d passages beyond the id stride and %d with no terms were left out",
            name,
            state.over_stride,
            state.empty,
        )
    logger.info("index %s serving: %d passages from %d articles", name, state.passages, articles)
    return state.passages


def _begin(
    config: MirrorConfig,
    client: QdrantClient,
    name: str,
    snapshot: SnapshotMeta,
    state_path: Path,
) -> _IndexState:
    """Decide between resuming and starting over, and leave a collection ready to fill.

    An index state is trusted only if its collection still exists and it was written by the same
    chunker as the running code (two cuts of the same article must never share a collection). A
    collection without a usable state is the debris of a crash before the first checkpoint
    and is rebuilt - unless it is the one serving, which is never touched.

    Raises:
        IndexBuildError: The collection is the one serving and there is no usable index state to
            resume.
        BuildError: A collection must be created and the disk is below `min_free_disk_gb`.
    """
    state = _read_index_state(state_path, snapshot)
    if state is not None and state.chunker != chunker_fingerprint():
        logger.warning(
            "index %s: the index state was written by chunker %r and this is %r; not resuming it",
            name,
            state.chunker or "unknown",
            chunker_fingerprint(),
        )
        state = None
    exists = client.collection_exists(name)
    if state is not None and exists:
        logger.info("index %s: resuming after page id %d", name, state.last_page_id)
        return state
    if exists:
        if aliased_collection(client) == name:
            msg = (
                f"{name} already serves the alias {ALIAS!r} but has no usable index state "
                f"({state_path}: missing, unreadable or from another chunker); "
                "delete the collection by hand to rebuild it"
            )
            raise IndexBuildError(msg)
        client.delete_collection(name)
    ensure_free_space(config.data_dir, config.min_free_disk_gb, f"the collection {name}")
    _create_collection(client, name)
    return _IndexState(snapshot.tag, -1, 0, 0, 0, complete=False, chunker=chunker_fingerprint())


def _create_collection(client: QdrantClient, name: str) -> None:
    """Create the sparse-only collection and its payload index.

    The sparse index is on disk and the payload with it, so 13M passages do not have to fit
    the LXC's 8 GB of RAM (docs/DESIGN.md section 4). `page_id` is indexed because W3's diff and
    any per-article filter select on it, and that index is on disk too rather than resident
    across 13M points, on a box whose Qdrant is capped at 4 GB. On-disk integer payload indexes
    arrived in Qdrant 1.11 (its release notes: "On-disk support for Keyword, Integer,
    Datetime, Float and UUID indexes"); the server here is 1.12.4.

    Segments are capped at `_MAX_SEGMENT_KB` (module docstring) so the optimizer's rewrite of a
    segment never needs a third of the disk free.
    """
    client.create_collection(
        collection_name=name,
        vectors_config={},
        sparse_vectors_config={
            SPARSE_VECTOR: models.SparseVectorParams(
                index=models.SparseIndexParams(on_disk=True), modifier=models.Modifier.IDF
            )
        },
        on_disk_payload=True,
        optimizers_config=models.OptimizersConfigDiff(
            max_segment_size=_MAX_SEGMENT_KB,
            max_optimization_threads=_MAX_OPTIMIZATION_THREADS,
        ),
    )
    client.create_payload_index(
        collection_name=name,
        field_name="page_id",
        field_schema=models.IntegerIndexParams(type=models.IntegerIndexType.INTEGER, on_disk=True),
    )


def _index_articles(
    config: MirrorConfig,
    store: ArticleSource,
    client: QdrantClient,
    name: str,
    start: _IndexState,
    state_path: Path,
    *,
    workers: int,
    batch_articles: int,
    checkpoint_every: int,
    progress: Callable[[int], None],
) -> _IndexState:
    """Encode and upsert every article past the checkpoint; return the completed index state.

    The completed state records `applied_snapshot` as the collection's born snapshot: a full
    build puts exactly that snapshot's articles in.
    """
    passages, over_stride, empty = start.passages, start.over_stride, start.empty
    last_page_id = start.last_page_id
    batches = _article_batches(store.iter_articles(after=start.last_page_id), batch_articles)
    for done, result in enumerate(
        _encode_batches(batches, config.lexical_dimensions, workers), start=1
    ):
        for first in range(0, len(result.points), _UPSERT_POINTS):
            _upsert(
                client,
                name,
                [
                    models.PointStruct(
                        id=point.point_id,
                        vector={
                            SPARSE_VECTOR: models.SparseVector(
                                indices=point.indices, values=point.values
                            )
                        },
                        payload=point.payload,
                    )
                    for point in result.points[first : first + _UPSERT_POINTS]
                ],
            )
        passages += len(result.points)
        over_stride += result.over_stride
        empty += result.empty
        last_page_id = result.last_page_id
        if done % checkpoint_every == 0:
            _write_index_state(
                state_path,
                replace(
                    start,
                    last_page_id=last_page_id,
                    passages=passages,
                    over_stride=over_stride,
                    empty=empty,
                    complete=False,
                ),
            )
            progress(passages)
    final = replace(
        start,
        last_page_id=last_page_id,
        passages=passages,
        over_stride=over_stride,
        empty=empty,
        complete=True,
        applied_snapshot=start.born,
    )
    _write_index_state(state_path, final)
    progress(passages)
    return final


def _upsert(
    client: QdrantClient,
    name: str,
    points: list[models.PointStruct],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Upsert one request's points, retrying a transport failure (`_UPSERT_ATTEMPTS`).

    Args:
        client: The Qdrant client.
        name: The collection.
        points: The points of one request.
        sleep: Waits between tries; tests inject a recorder.

    Raises:
        ResponseHandlingException: Every try failed in transport (Qdrant unreachable, or
            blocked past the client's timeout each time). The checkpoint is intact.
        UnexpectedResponse: Qdrant refused the request; not retried.
    """
    for attempt in range(1, _UPSERT_ATTEMPTS + 1):
        try:
            client.upsert(collection_name=name, points=points)
            return
        except ResponseHandlingException:
            if attempt == _UPSERT_ATTEMPTS:
                raise
            logger.warning(
                "index %s: upsert try %d of %d failed in transport; retrying in %.0f s",
                name,
                attempt,
                _UPSERT_ATTEMPTS,
                _UPSERT_RETRY_SECONDS,
            )
            sleep(_UPSERT_RETRY_SECONDS)


def _article_batches(articles: Iterable[Article], size: int) -> Iterator[tuple[Article, ...]]:
    """Group articles into tasks of `size`, keeping page-id order."""
    batch: list[Article] = []
    for article in articles:
        batch.append(article)
        if len(batch) >= size:
            yield tuple(batch)
            batch = []
    if batch:
        yield tuple(batch)


def _encode_batches(
    batches: Iterable[tuple[Article, ...]], dimensions: int, workers: int
) -> Iterator[_BatchResult]:
    """Encode batches, in order, on `workers` processes (or inline when `workers` is 1)."""
    if workers <= 1:
        for batch in batches:
            yield _encode_batch(dimensions, batch)
        return
    # `spawn`, not the Linux default `fork`: this process holds Qdrant client threads, and
    # forking a threaded process can deadlock the child (and warns on Python 3.12).
    pool = ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context("spawn")
    )
    pending: deque[Future[_BatchResult]] = deque()
    try:
        for batch in batches:
            pending.append(pool.submit(_encode_batch, dimensions, batch))
            if len(pending) >= workers * _IN_FLIGHT_PER_WORKER:
                yield pending.popleft().result()
        while pending:
            yield pending.popleft().result()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def _encode_batch(dimensions: int, batch: tuple[Article, ...]) -> _BatchResult:
    """Encode one batch of articles. Module-level, so a pool worker can import and run it."""
    encoder = SparseEncoder(dimensions=dimensions)
    points: list[_Point] = []
    over_stride = empty = 0
    for article in batch:
        chunks, dropped = indexable_chunks(article)
        over_stride += dropped
        for chunk in chunks:
            weights = encoder.encode_document(chunk.indexed_text(article.title))
            if not weights:
                empty += 1
                continue
            points.append(
                _Point(
                    point_id=passage_point_id(article.page_id, chunk.index),
                    indices=list(weights.keys()),
                    values=list(weights.values()),
                    payload=passage_payload(article, chunk),
                )
            )
    return _BatchResult(batch[-1].page_id, points, over_stride, empty)


def indexable_chunks(article: Article) -> tuple[list[Chunk], int]:
    """The passages of an article that get a point, and how many were dropped for the stride.

    The one definition of "the passages of an article" shared by the lexical build and the
    semantic one (`wiki_mirror.embed`), so the two collections hold the same point ids: the
    chunker's cut (`CHUNK_CONFIG`) minus the chunks whose number does not fit
    `records.PASSAGE_STRIDE`, which a point id cannot encode.

    Args:
        article: The article, as the store holds it.

    Returns:
        The chunks in document order, and the number dropped for not fitting the stride.
    """
    kept: list[Chunk] = []
    over_stride = 0
    for chunk in chunk_text(article.text, CHUNK_CONFIG):
        if chunk.index >= PASSAGE_STRIDE:
            over_stride += 1
            continue
        kept.append(chunk)
    return kept, over_stride


def passage_payload(article: Article, chunk: Chunk) -> dict[str, Any]:
    """The payload of one passage's point: the six fields, never any text (module docstring).

    Shared with the semantic collection (`wiki_mirror.embed`), whose points must carry exactly
    the lexical collection's payload for the same passage.

    Args:
        article: The article the chunk was cut from.
        chunk: One of `indexable_chunks(article)`.
    """
    return {
        "page_id": article.page_id,
        "revision_id": article.revision_id,
        "chunk": chunk.index,
        "section": chunk.section_path,
        "disambiguation": article.is_disambiguation,
        "text_hash": article.text_hash,
    }


def aliased_collection(client: QdrantClient) -> str | None:
    """The collection the alias `wikipedia_lexical` points at now, or None if it does not exist."""
    for entry in client.get_aliases().aliases:
        if entry.alias_name == ALIAS:
            return str(entry.collection_name)
    return None


def _swap_alias(client: QdrantClient, name: str) -> frozenset[str]:
    """Point the alias at `name` in one atomic call, then delete the collections now unused.

    Delete and create travel in one `update_collection_aliases` call, so a search sees the old
    index or the new one and never neither. Deleting an alias that does not exist is an error
    on a real server, so the delete is only sent when there is a previous target. Cleanup keeps
    the new collection and the one just replaced (a quick way back), and touches only names
    this module creates (`wikipedia_lexical_<8 digits>`), never a collection somebody made by hand.

    When the alias already pointed at `name` (the same collection swapped in again after a
    resume), nothing was replaced, so the way back is the newest *other* collection of ours, which
    is kept instead: an idempotent re-run must not cost the rollback the first run left behind.

    Returns:
        The collections that remain of ours or were the alias's previous target, whose
        `applied_snapshot` article stores and index states `_prune_local` keeps.
    """
    previous = aliased_collection(client)
    operations: list[models.AliasOperations] = []
    if previous is not None:
        operations.append(
            models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=ALIAS))
        )
    operations.append(
        models.CreateAliasOperation(
            create_alias=models.CreateAlias(collection_name=name, alias_name=ALIAS)
        )
    )
    client.update_collection_aliases(change_aliases_operations=operations)
    ours = sorted(
        entry.name
        for entry in client.get_collections().collections
        if _born(entry.name) is not None
    )
    keep = {name}
    if previous is not None and previous != name:
        keep.add(previous)
    elif previous == name:
        # Eight-digit tags sort as text, so the last one is the newest-born.
        others = [collection for collection in ours if collection != name]
        keep.update(others[-1:])
    for collection in ours:
        if collection not in keep:
            logger.info("index: deleting superseded collection %s", collection)
            client.delete_collection(collection)
    return frozenset(keep)


def _prune_local(config: MirrorConfig, new_tag: str, kept: frozenset[str]) -> None:
    """Delete the article stores and index states of snapshots that have dropped out of the pair.

    Runs after the alias swap, so a failure to delete costs disk, never correctness: it is
    logged and the next build tries again.

    What a kept collection needs is its index state file (named by its born tag) and the article
    store of the snapshot it currently reflects (`applied_snapshot`, which after a full build is
    its born tag but from W3 on can be newer). A kept collection whose `applied_snapshot` cannot
    be read keeps the store of its born tag instead: pruning is the one place where guessing wrong
    deletes something, so the doubt is resolved towards keeping.

    Only files of snapshots *older* than the one just swapped in are candidates. A newer file is
    the resumable work of a build that has not finished (or of one run out of order), never
    something this swap superseded.

    Deleting a store the API still has open is safe on Linux: the open connection keeps its
    inode until it closes, and a finished store is one immutable file with no `-wal` or `-shm`
    beside it. In practice the store the API serves is the previous snapshot's, which is kept.

    Args:
        config: Service settings (`data_dir`, `snapshot_identifier`).
        new_tag: The tag of the snapshot just swapped in.
        kept: The collections that remain (the new one, and the way back).
    """
    born_tags = {tag for collection in kept if (tag := _born(collection)) is not None}
    store_tags = {
        applied_snapshot(config.data_dir, collection) or tag
        for collection in kept
        if (tag := _born(collection)) is not None
    }
    stores = re.compile(rf"^{re.escape(config.snapshot_identifier)}_(\d{{8}})\.db$")
    states = re.compile(r"^index_lexical_(\d{8})\.json$")
    for directory, pattern, keep_tags in (
        (config.data_dir / "articles", stores, store_tags),
        (config.data_dir / "state", states, born_tags),
    ):
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue  # no such directory yet: nothing was ever written there
        for entry in entries:
            found = pattern.match(entry.name)
            if found is None or found[1] >= new_tag or found[1] in keep_tags:
                continue
            try:
                entry.unlink()
            except OSError:
                logger.warning("index: could not delete the superseded file %s", entry.name)
            else:
                logger.info("index: deleted the superseded file %s", entry.name)


def _read_index_state(path: Path, snapshot: SnapshotMeta) -> _IndexState | None:
    """Load the index state, or None if there is none, it is unreadable, or it is another's.

    "Another's" means the file records a different born tag than the snapshot being built, which
    the file name rules out unless a file was copied by hand.

    An unreadable index state is a restart rather than an error: indexing is idempotent, so
    starting over is always safe, merely slower than trusting a file that may be torn.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        state = _IndexState(
            born=str(raw["born"]),
            last_page_id=int(raw["last_page_id"]),
            passages=int(raw["passages"]),
            over_stride=int(raw["over_stride"]),
            empty=int(raw["empty"]),
            complete=bool(raw["complete"]),
            chunker=str(raw.get("chunker", "")),
            applied_snapshot=str(raw.get("applied_snapshot") or ""),
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("index state %s is unreadable; starting the collection over", path)
        return None
    return state if state.born == snapshot.tag else None


def _write_index_state(path: Path, state: _IndexState) -> None:
    """Write the index state atomically (temp file, then rename) so a crash never tears it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(path.suffix + ".tmp")
    scratch.write_text(state.as_json(), encoding="utf-8")
    scratch.replace(path)
