"""Qualifies the download and parse stages and the build-state file, with a fake Enterprise client.

Chunks are tiny real `.tar.gz` archives built from the fixture records, so the whole path runs:
download (fake) -> NAS directory -> parse -> spool -> article store -> finalize. The parse stage
runs in-process (`parse_workers=1`) except for one test that starts a real process pool.
"""

from __future__ import annotations

import gzip
import io
import json
import subprocess
import sys
import tarfile
import threading
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from wiki_mirror import build
from wiki_mirror.build import (
    BuildError,
    BuildLockedError,
    BuildState,
    build_lock,
    describe_failure,
    ensure_free_space,
    latest_downloaded_snapshot,
    read_build_state,
    read_snapshot_meta,
    run_download,
    run_parse,
    snapshot_dir,
    write_build_state,
)
from wiki_mirror.config import MirrorConfig
from wiki_mirror.enterprise import ChunkBudget, EnterpriseError
from wiki_mirror.parse import PARSER_VERSION
from wiki_mirror.records import Article, SnapshotMeta, text_hash
from wiki_mirror.store import ArticleStore

Records = dict[str, dict[str, object]]

CHUNK_0 = "enwiki_namespace_0_chunk_0"
CHUNK_1 = "enwiki_namespace_0_chunk_1"


def _write_chunk(path: Path, records: list[dict[str, object]]) -> Path:
    """Write a chunk archive holding the records as one NDJSON member."""
    payload = ("\n".join(json.dumps(record) for record in records) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("chunk.ndjson")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return path


@pytest.fixture
def config(tmp_path: Path) -> MirrorConfig:
    """Settings that keep everything under tmp_path and parse in-process."""
    return MirrorConfig(
        snapshot_dir=tmp_path / "nas",
        data_dir=tmp_path / "data",
        parse_workers=1,
        download_concurrency=2,
        retain_snapshots=2,
        # The floor reads the real disk; its own tests fake `shutil.disk_usage`.
        min_free_disk_gb=0,
    )


@pytest.fixture
def chunk_files(wme_records: Records, tmp_path: Path) -> dict[str, bytes]:
    """Two chunks' archive bytes: four records each, the second also holding a newer duplicate."""
    records = list(wme_records.values())
    newer = json.loads(json.dumps(records[0]))
    newer["version"]["identifier"] = records[0]["version"]["identifier"] + 1  # type: ignore[index,operator]
    scratch = tmp_path / "scratch"
    first = _write_chunk(scratch / "0.tar.gz", records[:4])
    second = _write_chunk(scratch / "1.tar.gz", [*records[4:], newer])
    return {CHUNK_0: first.read_bytes(), CHUNK_1: second.read_bytes()}


class FakeClient:
    """Stands in for `EnterpriseClient`: describes a snapshot and 'downloads' stored bytes."""

    def __init__(self, snapshot: SnapshotMeta, chunks: dict[str, bytes]) -> None:
        self.meta = snapshot
        self.chunks = chunks
        self.downloaded: list[str] = []
        self.budgets: list[ChunkBudget | None] = []
        self.fail_on: str | None = None
        self._lock = threading.Lock()

    def snapshot(self, identifier: str) -> SnapshotMeta:
        """The canned snapshot."""
        assert identifier == self.meta.identifier
        return self.meta

    def download_chunk(
        self, identifier: str, chunk_id: str, dest: Path, *, budget: ChunkBudget | None = None
    ) -> Path:
        """Write the canned bytes, or fail for the chunk set to fail."""
        with self._lock:
            self.downloaded.append(chunk_id)
            self.budgets.append(budget)
        if chunk_id == self.fail_on:
            msg = f"chunk {chunk_id}: HTTP 503 after 4 attempts"
            raise EnterpriseError(msg)
        dest.write_bytes(self.chunks[chunk_id])
        return dest


@pytest.fixture
def client(snapshot: SnapshotMeta, chunk_files: dict[str, bytes]) -> FakeClient:
    """A fake client for the two-chunk snapshot."""
    return FakeClient(snapshot, chunk_files)


def _downloaded(config: MirrorConfig, client: FakeClient) -> SnapshotMeta:
    """Run the download stage (the parse tests start from its output)."""
    return run_download(config, client)  # type: ignore[arg-type]


# -- build state -------------------------------------------------------------------------------


@pytest.mark.unit
def test_read_build_state_without_a_file_is_idle_and_empty(tmp_path: Path) -> None:
    assert read_build_state(tmp_path) == BuildState()


@pytest.mark.unit
def test_write_build_state_round_trips_and_stamps_the_time(tmp_path: Path) -> None:
    now = datetime(2026, 10, 16, 12, 30, 5, tzinfo=UTC)
    state = BuildState(snapshot_date="2026-10-01", phase="parsing", chunks_total=435, articles=9)
    write_build_state(tmp_path, state, utc_now=lambda: now)
    loaded = read_build_state(tmp_path)
    assert loaded.updated_at == "2026-10-16T12:30:05Z"
    assert (loaded.snapshot_date, loaded.phase, loaded.chunks_total, loaded.articles) == (
        "2026-10-01",
        "parsing",
        435,
        9,
    )


@pytest.mark.unit
def test_write_build_state_leaves_no_temporary_file(tmp_path: Path) -> None:
    write_build_state(tmp_path, BuildState(phase="downloading"))
    assert [entry.name for entry in (tmp_path / "state").iterdir()] == ["build.json"]


@pytest.mark.unit
def test_write_build_state_failed_write_keeps_the_previous_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_build_state(tmp_path, BuildState(phase="downloading", chunks_total=7))

    def refuse(self: Path, target: Any) -> Path:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        write_build_state(tmp_path, BuildState(phase="failed"))
    monkeypatch.undo()
    assert read_build_state(tmp_path).phase == "downloading"


@pytest.mark.unit
def test_read_build_state_corrupt_file_reports_idle_with_an_error(tmp_path: Path) -> None:
    path = tmp_path / "state" / "build.json"
    path.parent.mkdir()
    path.write_text("{truncated", encoding="utf-8")
    state = read_build_state(tmp_path)
    assert state.phase == "idle"
    assert "unreadable" in state.error


@pytest.mark.unit
def test_read_build_state_unknown_phase_reports_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "state" / "build.json"
    path.parent.mkdir()
    path.write_text('{"phase": "exploding"}', encoding="utf-8")
    assert "unreadable" in read_build_state(tmp_path).error


@pytest.mark.unit
def test_read_build_state_ignores_fields_it_does_not_know(tmp_path: Path) -> None:
    path = tmp_path / "state" / "build.json"
    path.parent.mkdir()
    path.write_text('{"phase": "serving", "future_field": 1, "articles": 3}', encoding="utf-8")
    state = read_build_state(tmp_path)
    assert (state.phase, state.articles) == ("serving", 3)


@pytest.mark.unit
def test_build_state_unknown_phase_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown build phase"):
        BuildState(phase="exploding")


# -- download ----------------------------------------------------------------------------------


@pytest.mark.contract
def test_run_download_fetches_every_chunk_into_the_snapshot_directory(
    config: MirrorConfig, client: FakeClient
) -> None:
    snapshot = _downloaded(config, client)
    directory = snapshot_dir(config, snapshot)
    assert directory.name == "enwiki_namespace_0_20261001"
    assert sorted(client.downloaded) == [CHUNK_0, CHUNK_1]
    assert (directory / f"{CHUNK_0}.tar.gz").read_bytes() == client.chunks[CHUNK_0]


@pytest.mark.contract
def test_run_download_hands_the_client_a_budget_with_the_configured_limit(
    config: MirrorConfig, client: FakeClient
) -> None:
    _downloaded(config, client)
    assert all(isinstance(budget, ChunkBudget) for budget in client.budgets)


@pytest.mark.contract
def test_run_download_records_progress_and_finishes_idle(
    config: MirrorConfig, client: FakeClient
) -> None:
    _downloaded(config, client)
    state = read_build_state(config.data_dir)
    assert (state.phase, state.chunks_total, state.chunks_downloaded) == ("idle", 2, 2)
    assert state.snapshot_date == "2026-10-01"


@pytest.mark.contract
def test_run_download_writes_the_snapshot_description_beside_the_chunks(
    config: MirrorConfig, client: FakeClient, snapshot: SnapshotMeta
) -> None:
    _downloaded(config, client)
    directory = snapshot_dir(config, snapshot)
    assert read_snapshot_meta(directory) == snapshot
    assert read_snapshot_meta(directory / "snapshot.json") == snapshot
    assert not (directory / "snapshot.json.tmp").exists()


@pytest.mark.contract
def test_run_download_failed_chunk_raises_marks_failed_and_writes_no_description(
    config: MirrorConfig, client: FakeClient, snapshot: SnapshotMeta
) -> None:
    client.fail_on = CHUNK_1
    with pytest.raises(BuildError, match=CHUNK_1):
        _downloaded(config, client)
    state = read_build_state(config.data_dir)
    assert state.phase == "failed"
    assert "HTTP 503" in state.error
    assert not (snapshot_dir(config, snapshot) / "snapshot.json").exists()


@pytest.mark.contract
def test_run_download_prunes_old_snapshots_but_keeps_the_newest_and_strangers(
    config: MirrorConfig, client: FakeClient
) -> None:
    for name in ("enwiki_namespace_0_20260801", "enwiki_namespace_0_20260901", "notes"):
        (config.snapshot_dir / name).mkdir(parents=True)
    _downloaded(config, client)
    names = sorted(entry.name for entry in config.snapshot_dir.iterdir())
    assert names == ["enwiki_namespace_0_20260901", "enwiki_namespace_0_20261001", "notes"]


@pytest.mark.contract
def test_run_download_never_prunes_the_snapshot_being_built(
    config: MirrorConfig, client: FakeClient
) -> None:
    # Two newer directories exist, so retain_snapshots=2 would drop the current (older) one.
    for name in ("enwiki_namespace_0_20261101", "enwiki_namespace_0_20261201"):
        (config.snapshot_dir / name).mkdir(parents=True)
    _downloaded(config, client)
    assert (config.snapshot_dir / "enwiki_namespace_0_20261001").is_dir()


@pytest.mark.unit
def test_read_snapshot_meta_missing_file_raises_build_error(tmp_path: Path) -> None:
    with pytest.raises(BuildError, match="snapshot description"):
        read_snapshot_meta(tmp_path / "nothing")


@pytest.mark.unit
def test_read_snapshot_meta_garbage_raises_build_error(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text('{"identifier": "x"}', encoding="utf-8")
    with pytest.raises(BuildError, match="snapshot description"):
        read_snapshot_meta(path)


@pytest.mark.unit
def test_latest_downloaded_snapshot_none_when_the_nas_is_empty(config: MirrorConfig) -> None:
    assert latest_downloaded_snapshot(config) is None


@pytest.mark.contract
def test_latest_downloaded_snapshot_finds_the_complete_download(
    config: MirrorConfig, client: FakeClient, snapshot: SnapshotMeta
) -> None:
    _downloaded(config, client)
    assert latest_downloaded_snapshot(config) == snapshot


@pytest.mark.contract
def test_latest_downloaded_snapshot_skips_a_newer_incomplete_download(
    config: MirrorConfig, client: FakeClient, snapshot: SnapshotMeta
) -> None:
    _downloaded(config, client)
    newer = SnapshotMeta(snapshot.identifier, "2026-11-01", 1, (CHUNK_0,))
    newer_dir = config.snapshot_dir / "enwiki_namespace_0_20261101"
    newer_dir.mkdir()
    (newer_dir / "snapshot.json").write_text(
        build.snapshot_to_json(newer), encoding="utf-8"
    )  # described, but its chunk never arrived
    (config.snapshot_dir / "enwiki_namespace_0_20261201").mkdir()  # no description at all
    assert latest_downloaded_snapshot(config) == snapshot


# -- parse -------------------------------------------------------------------------------------


@pytest.mark.contract
def test_run_parse_builds_a_complete_store_from_the_downloaded_chunks(
    config: MirrorConfig, client: FakeClient
) -> None:
    snapshot = _downloaded(config, client)
    path = run_parse(config, snapshot)
    store = ArticleStore.open(path, readonly=True)
    try:
        assert store.is_complete
        assert store.count() == 8
        assert store.chunks_done() == frozenset({CHUNK_0, CHUNK_1})
        found = store.by_title("Acoustic Sessions")
        assert found is not None
        assert found.is_disambiguation
    finally:
        store.close()


@pytest.mark.contract
def test_run_parse_records_the_parser_version_in_the_store(
    config: MirrorConfig, client: FakeClient
) -> None:
    path = run_parse(config, _downloaded(config, client))
    store = ArticleStore.open(path, readonly=True)
    try:
        assert store.parser_version == PARSER_VERSION
    finally:
        store.close()


@pytest.mark.contract
def test_run_parse_stores_every_article_with_the_hash_of_its_text(
    config: MirrorConfig, client: FakeClient
) -> None:
    path = run_parse(config, _downloaded(config, client))
    store = ArticleStore.open(path, readonly=True)
    try:
        articles = list(store.iter_articles())
    finally:
        store.close()
    assert len(articles) == 8
    assert [a.text_hash == text_hash(a.text) for a in articles] == [True] * 8


@pytest.mark.contract
def test_run_parse_resuming_a_store_begun_by_another_parser_version_is_refused(
    config: MirrorConfig, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _downloaded(config, client)
    second = snapshot_dir(config, snapshot) / f"{CHUNK_1}.tar.gz"
    good = second.read_bytes()
    second.write_bytes(good[:100])  # the second chunk fails, leaving an unfinished store
    with pytest.raises(BuildError, match=CHUNK_1):
        run_parse(config, snapshot)
    second.write_bytes(good)
    monkeypatch.setattr(build, "PARSER_VERSION", PARSER_VERSION + 1)
    with pytest.raises(BuildError, match=r"parser version \d+ and this code is \d+"):
        run_parse(config, snapshot)
    assert read_build_state(config.data_dir).phase == "failed"


@pytest.mark.unit
def test_article_spool_round_trip_keeps_the_text_hash() -> None:
    article = Article(
        page_id=1,
        title="A",
        revision_id=2,
        revision_time="2026-09-30T12:00:00Z",
        text="Some text.",
        text_hash=text_hash("Some text."),
    )
    assert build._article_from_dict(build._article_dict(article)) == article


@pytest.mark.contract
def test_run_parse_keeps_the_highest_revision_across_chunks(
    config: MirrorConfig, client: FakeClient, wme_records: Records
) -> None:
    first = next(iter(wme_records.values()))
    path = run_parse(config, _downloaded(config, client))
    store = ArticleStore.open(path)
    try:
        page = store.get(int(first["identifier"]))  # type: ignore[call-overload]
    finally:
        store.close()
    assert page is not None
    assert page.revision_id == first["version"]["identifier"] + 1  # type: ignore[index,operator]


@pytest.mark.contract
def test_run_parse_records_progress_and_finishes_idle(
    config: MirrorConfig, client: FakeClient
) -> None:
    run_parse(config, _downloaded(config, client))
    state = read_build_state(config.data_dir)
    assert (state.phase, state.chunks_total, state.chunks_parsed, state.articles) == (
        "idle",
        2,
        2,
        8,
    )


@pytest.mark.contract
def test_run_parse_leaves_no_spool_files_behind(
    config: MirrorConfig, client: FakeClient
) -> None:
    run_parse(config, _downloaded(config, client))
    assert list((config.data_dir / "tmp").rglob("*.*")) == []


@pytest.mark.contract
def test_run_parse_resumes_skipping_chunks_already_done(
    config: MirrorConfig,
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _downloaded(config, client)
    second = snapshot_dir(config, snapshot) / f"{CHUNK_1}.tar.gz"
    good = second.read_bytes()
    second.write_bytes(good[:100])  # a truncated archive: the second chunk will fail
    with pytest.raises(BuildError, match=CHUNK_1):
        run_parse(config, snapshot)
    failed = read_build_state(config.data_dir)
    assert failed.phase == "failed"
    assert failed.chunks_parsed == 1

    second.write_bytes(good)
    parsed: list[str] = []
    real = build.iter_chunk_articles

    def spy(path: Path) -> Any:
        parsed.append(path.name)
        return real(path)

    monkeypatch.setattr(build, "iter_chunk_articles", spy)
    run_parse(config, snapshot)
    assert parsed == [f"{CHUNK_1}.tar.gz"]
    assert read_build_state(config.data_dir).phase == "idle"


@pytest.mark.contract
def test_run_parse_a_finished_store_is_returned_without_being_reopened_for_writing(
    config: MirrorConfig, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _downloaded(config, client)
    path = run_parse(config, snapshot)
    before = path.stat().st_mtime_ns

    def refuse(*_args: object, **_kwargs: object) -> None:
        msg = "must not parse again"
        raise AssertionError(msg)

    monkeypatch.setattr(build, "iter_chunk_articles", refuse)
    assert run_parse(config, snapshot) == path
    assert path.stat().st_mtime_ns == before


@pytest.mark.contract
def test_run_parse_missing_chunk_file_raises_and_names_the_remedy(
    config: MirrorConfig, client: FakeClient
) -> None:
    snapshot = _downloaded(config, client)
    (snapshot_dir(config, snapshot) / f"{CHUNK_0}.tar.gz").unlink()
    with pytest.raises(BuildError, match="run download"):
        run_parse(config, snapshot)
    assert read_build_state(config.data_dir).phase == "failed"


@pytest.mark.contract
def test_run_parse_store_of_another_snapshot_is_refused(
    config: MirrorConfig, client: FakeClient, snapshot: SnapshotMeta
) -> None:
    run_parse(config, _downloaded(config, client))
    # Same file name, different recorded date: someone copied a store into place.
    other = SnapshotMeta(snapshot.identifier, "2026-10-02", 1, snapshot.chunk_ids)
    path = build.store_path(config.data_dir, snapshot)
    path.replace(build.store_path(config.data_dir, other))
    (config.snapshot_dir / "enwiki_namespace_0_20261002").mkdir()
    with pytest.raises(BuildError, match=r"different snapshot|not downloaded"):
        run_parse(config, other)


@pytest.mark.contract
def test_run_parse_with_a_process_pool_matches_the_in_process_result(
    config: MirrorConfig, client: FakeClient
) -> None:
    pooled = config.model_copy(update={"parse_workers": 2})
    path = run_parse(pooled, _downloaded(pooled, client))
    store = ArticleStore.open(path)
    try:
        assert store.count() == 8
        assert store.chunks_done() == frozenset({CHUNK_0, CHUNK_1})
    finally:
        store.close()


# -- corrupt chunks are deleted so the next download fetches them again ----------------------


def _failing_spool(chunk: str, problem: Exception) -> Any:
    """A stand-in for `build._spool_chunk` that raises `problem` for one chunk, else parses."""
    real = build._spool_chunk

    def spool(source: str, spool_file: str) -> int:
        if Path(source).name.startswith(chunk):
            raise problem
        return real(source, spool_file)

    return spool


@pytest.mark.contract
@pytest.mark.parametrize(
    "problem",
    [
        EOFError("Compressed file ended before the end-of-stream marker was reached"),
        gzip.BadGzipFile("Not a gzipped file"),
        zlib.error("Error -3 while decompressing data"),
        tarfile.ReadError("unexpected end of data"),
    ],
    ids=["eof", "bad-gzip", "zlib", "tar-read"],
)
def test_run_parse_corrupt_chunk_is_deleted_with_its_sidecar_and_the_stage_fails(
    config: MirrorConfig,
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    problem: Exception,
) -> None:
    snapshot = _downloaded(config, client)
    broken = snapshot_dir(config, snapshot) / f"{CHUNK_1}.tar.gz"
    sidecar = broken.with_name(broken.name + ".etag")
    sidecar.write_text('"abc"', encoding="utf-8")
    monkeypatch.setattr(build, "_spool_chunk", _failing_spool(CHUNK_1, problem))
    with pytest.raises(BuildError, match="corrupt files deleted"):
        run_parse(config, snapshot)
    assert not broken.exists()
    assert not sidecar.exists()
    assert (snapshot_dir(config, snapshot) / f"{CHUNK_0}.tar.gz").is_file()
    assert read_build_state(config.data_dir).phase == "failed"


@pytest.mark.contract
def test_run_parse_failure_that_is_not_a_corrupt_archive_leaves_the_chunk_alone(
    config: MirrorConfig, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _downloaded(config, client)
    chunk = snapshot_dir(config, snapshot) / f"{CHUNK_1}.tar.gz"
    monkeypatch.setattr(build, "_spool_chunk", _failing_spool(CHUNK_1, ValueError("parser bug")))
    with pytest.raises(BuildError) as raised:
        run_parse(config, snapshot)
    assert chunk.is_file()
    assert "deleted" not in str(raised.value)


@pytest.mark.contract
def test_run_parse_truncated_real_archive_is_deleted_and_the_next_download_refetches_it(
    config: MirrorConfig, client: FakeClient
) -> None:
    snapshot = _downloaded(config, client)
    broken = snapshot_dir(config, snapshot) / f"{CHUNK_1}.tar.gz"
    broken.write_bytes(broken.read_bytes()[:100])
    with pytest.raises(BuildError, match="download again"):
        run_parse(config, snapshot)
    assert not broken.exists()
    # Without the chunk the snapshot no longer counts as completely downloaded...
    assert latest_downloaded_snapshot(config) is None
    # ...and the next download fetches just that chunk again, after which parse succeeds.
    client.downloaded.clear()
    run_download(config, client)  # type: ignore[arg-type]
    assert broken.is_file()
    assert run_parse(config, snapshot).is_file()


@pytest.mark.contract
def test_run_parse_corrupt_chunk_in_a_process_pool_is_deleted_too(
    config: MirrorConfig, client: FakeClient
) -> None:
    pooled = config.model_copy(update={"parse_workers": 2})
    snapshot = _downloaded(pooled, client)
    broken = snapshot_dir(pooled, snapshot) / f"{CHUNK_1}.tar.gz"
    broken.write_bytes(broken.read_bytes()[:100])
    with pytest.raises(BuildError, match="corrupt files deleted"):
        run_parse(pooled, snapshot)
    assert not broken.exists()


# -- a finished store is never reopened for writing ---------------------------------------------


@pytest.mark.contract
def test_run_parse_finished_store_for_a_different_chunk_list_fails_loudly_and_is_untouched(
    config: MirrorConfig, client: FakeClient
) -> None:
    snapshot = _downloaded(config, client)
    path = run_parse(config, snapshot)
    before = path.read_bytes()
    relisted = SnapshotMeta(
        snapshot.identifier,
        snapshot.date,
        snapshot.record_count,
        (*snapshot.chunk_ids, "enwiki_namespace_0_chunk_2"),
    )
    with pytest.raises(BuildError, match="different chunk list"):
        run_parse(config, relisted)
    assert path.read_bytes() == before
    assert sorted(entry.name for entry in path.parent.iterdir()) == [path.name]
    assert read_build_state(config.data_dir).phase == "failed"


# -- one build at a time ------------------------------------------------------------------------


@pytest.mark.unit
def test_build_lock_a_second_holder_is_refused_with_a_clear_message(tmp_path: Path) -> None:
    with (
        build_lock(tmp_path),
        pytest.raises(BuildLockedError, match="a build is already running"),
        build_lock(tmp_path),
    ):
        pytest.fail("the second holder must not get in")


@pytest.mark.unit
def test_build_lock_refusal_is_a_build_error_and_writes_no_state(tmp_path: Path) -> None:
    with build_lock(tmp_path), pytest.raises(BuildError), build_lock(tmp_path):
        pytest.fail("the second holder must not get in")
    assert read_build_state(tmp_path) == BuildState()


@pytest.mark.unit
def test_build_lock_is_free_again_after_the_block(tmp_path: Path) -> None:
    with build_lock(tmp_path):
        pass
    with build_lock(tmp_path):
        pass


@pytest.mark.unit
def test_build_lock_is_released_when_the_block_raises(tmp_path: Path) -> None:
    def fail_while_holding() -> None:
        with build_lock(tmp_path):
            msg = "boom"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        fail_while_holding()
    with build_lock(tmp_path):
        pass


@pytest.mark.unit
def test_build_lock_is_held_by_another_process_until_it_lets_go(tmp_path: Path) -> None:
    # The kernel's lock, not a marker file: a second process is refused while this one holds it,
    # and a dead holder leaves nothing to clear.
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from wiki_mirror.build import build_lock\n"
        "with build_lock(Path(sys.argv[1])):\n"
        "    print('locked', flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    holder = subprocess.Popen(  # noqa: S603 - fixed argv, this interpreter, a script above
        [sys.executable, "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(BuildLockedError), build_lock(tmp_path):
            pytest.fail("the other process holds the lock")
    finally:
        holder.communicate("\n", timeout=30)
    with build_lock(tmp_path):
        pass


# -- the free-disk floor -----------------------------------------------------------------------


class _Usage(NamedTuple):
    """What `shutil.disk_usage` returns."""

    total: int
    used: int
    free: int


def _disk(monkeypatch: pytest.MonkeyPatch, free_gb: float) -> None:
    """Make `build.shutil.disk_usage` report `free_gb` GB free."""
    gb = 1024**3
    monkeypatch.setattr(
        build.shutil, "disk_usage", lambda _path: _Usage(100 * gb, 0, int(free_gb * gb))
    )


@pytest.mark.unit
def test_ensure_free_space_below_the_floor_raises_naming_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disk(monkeypatch, 3.5)
    with pytest.raises(BuildError, match=r"3\.5 GB free.*floor is 20 GB"):
        ensure_free_space(tmp_path, 20, "a new article store")


@pytest.mark.unit
def test_ensure_free_space_exactly_at_the_floor_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disk(monkeypatch, 20)
    ensure_free_space(tmp_path, 20, "a new article store")


@pytest.mark.unit
def test_ensure_free_space_between_the_floor_and_the_room_raises_naming_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disk(monkeypatch, 30)
    with pytest.raises(BuildError) as raised:
        ensure_free_space(tmp_path, 20, "the rewritten segments of a collection", room_gb=25)
    message = str(raised.value)
    assert "30.0 GB free" in message
    assert "25 GB of room" in message
    assert "floor of 20 GB (min_free_disk_gb)" in message
    assert "45 GB in all" in message


@pytest.mark.unit
def test_ensure_free_space_exactly_at_the_floor_plus_the_room_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disk(monkeypatch, 45)
    ensure_free_space(tmp_path, 20, "the rewritten segments of a collection", room_gb=25)


@pytest.mark.unit
def test_ensure_free_space_below_the_floor_with_room_asked_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disk(monkeypatch, 3.5)
    with pytest.raises(BuildError, match=r"3\.5 GB free.*25 GB of room"):
        ensure_free_space(tmp_path, 20, "the rewritten segments of a collection", room_gb=25)


@pytest.mark.unit
def test_ensure_free_space_without_room_asked_does_not_mention_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disk(monkeypatch, 3.5)
    with pytest.raises(BuildError) as raised:
        ensure_free_space(tmp_path, 20, "a new article store")
    assert "room" not in str(raised.value)


@pytest.mark.unit
def test_ensure_free_space_with_the_floor_off_ignores_the_room_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disk(monkeypatch, 1)
    ensure_free_space(tmp_path, 0, "anything", room_gb=25)


@pytest.mark.unit
def test_ensure_free_space_with_the_floor_off_never_looks_at_the_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_path: Path) -> None:
        msg = "must not look"
        raise AssertionError(msg)

    monkeypatch.setattr(build.shutil, "disk_usage", refuse)
    ensure_free_space(tmp_path, 0, "anything")


@pytest.mark.unit
def test_ensure_free_space_judges_a_missing_directory_by_its_nearest_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []

    def usage(path: Path) -> _Usage:
        seen.append(path)
        return _Usage(0, 0, 50 * 1024**3)

    monkeypatch.setattr(build.shutil, "disk_usage", usage)
    ensure_free_space(tmp_path / "not" / "yet" / "made", 20, "a new article store")
    assert seen == [tmp_path]


@pytest.mark.contract
def test_run_parse_below_the_disk_floor_refuses_before_creating_the_store(
    config: MirrorConfig, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    guarded = config.model_copy(update={"min_free_disk_gb": 20})
    snapshot = _downloaded(guarded, client)
    _disk(monkeypatch, 1)
    with pytest.raises(BuildError, match="not enough free disk to create a new article store"):
        run_parse(guarded, snapshot)
    assert not build.store_path(guarded.data_dir, snapshot).exists()
    state = read_build_state(guarded.data_dir)
    assert state.phase == "failed"
    assert "free disk" in state.error


@pytest.mark.contract
def test_run_parse_resuming_an_existing_store_is_not_blocked_by_the_floor(
    config: MirrorConfig, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    guarded = config.model_copy(update={"min_free_disk_gb": 20})
    snapshot = _downloaded(guarded, client)
    second = snapshot_dir(guarded, snapshot) / f"{CHUNK_1}.tar.gz"
    good = second.read_bytes()
    second.write_bytes(good[:100])
    # Plenty free for the first attempt, so it fails on the damaged chunk after creating the
    # store, not on the floor of whatever disk runs the suite (CI's has under 20 GB).
    _disk(monkeypatch, 100)
    with pytest.raises(BuildError, match="chunk"):
        run_parse(guarded, snapshot)
    second.write_bytes(good)
    # The floor guards the start of a big write; a store already on disk is finished, not started.
    _disk(monkeypatch, 1)
    assert run_parse(guarded, snapshot).is_file()


# -- what the state file may say about a failure ---------------------------------------------


@pytest.mark.unit
def test_describe_failure_keeps_a_build_errors_text_and_only_names_other_classes() -> None:
    assert describe_failure(BuildError("2 chunks missing")) == "BuildError: 2 chunks missing"
    leaky = EnterpriseError("GET https://example.test/x?token=SECRET failed")
    assert describe_failure(leaky) == "EnterpriseError"
    assert describe_failure(OSError("/etc/secret")) == "OSError"
