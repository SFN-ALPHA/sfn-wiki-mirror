"""Qualifies the article store: upsert rules, title resolution, resume state, read-only serving.

Schema 2 (docs/DESIGN.md section 12.3) is qualified here too: the `text_hash` column is stored as
given and read back as stored, an article whose hash is not its text's is refused, the parser
version recorded at creation survives a reopen, and a schema-1 file is refused.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from wiki_mirror.records import Article, SnapshotMeta, text_hash
from wiki_mirror.store import (
    ArticleStore,
    StoreError,
    snapshot_from_json,
    snapshot_to_json,
    store_path,
)

#: The parser version the tests create stores with; no test depends on its value except the ones
#: that pass their own.
_PARSER = 1


def _article(page_id: int, title: str, revision_id: int = 1, **extra: object) -> Article:
    """A small article; `extra` overrides any field (its hash follows the final text)."""
    fields: dict[str, object] = {
        "page_id": page_id,
        "title": title,
        "revision_id": revision_id,
        "revision_time": "2026-09-30T12:00:00Z",
        "text": f"Body of {title} at revision {revision_id}.",
    }
    fields.update(extra)
    fields.setdefault("text_hash", text_hash(str(fields["text"])))
    return Article(**fields)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path, snapshot: SnapshotMeta) -> Iterator[ArticleStore]:
    """A fresh writable store."""
    created = ArticleStore.create(store_path(tmp_path, snapshot), snapshot, parser_version=_PARSER)
    yield created
    created.close()


@pytest.mark.unit
def test_store_path_is_named_for_the_snapshot_date(tmp_path: Path, snapshot: SnapshotMeta) -> None:
    assert store_path(tmp_path, snapshot) == (
        tmp_path / "articles" / "enwiki_namespace_0_20261001.db"
    )


@pytest.mark.unit
def test_create_existing_path_raises(tmp_path: Path, snapshot: SnapshotMeta) -> None:
    path = store_path(tmp_path, snapshot)
    ArticleStore.create(path, snapshot, parser_version=_PARSER).close()
    with pytest.raises(FileExistsError):
        ArticleStore.create(path, snapshot, parser_version=_PARSER)


@pytest.mark.unit
def test_add_many_round_trips_every_field(store: ArticleStore) -> None:
    article = _article(
        7,
        "Mercury (planet)",
        9,
        abstract="The smallest planet.",
        categories=("Planets", "Solar System"),
        redirects=("Mercury planet",),
        is_disambiguation=True,
        license="CC BY-SA 4.0",
        text="Line one.\n\n## Orbit\n\nUnicode \N{EN DASH} \N{LATIN SMALL LETTER E WITH ACUTE}",
    )
    store.add_many([article])
    assert store.get(7) == article


@pytest.mark.unit
def test_add_many_higher_revision_replaces_the_lower(store: ArticleStore) -> None:
    store.add_many([_article(1, "Page", 5)])
    assert store.add_many([_article(1, "Page", 6)]) == 1
    loaded = store.get(1)
    assert loaded is not None
    assert loaded.revision_id == 6


@pytest.mark.unit
def test_add_many_lower_revision_is_ignored(store: ArticleStore) -> None:
    store.add_many([_article(1, "Page", 6)])
    assert store.add_many([_article(1, "Page", 5)]) == 0
    loaded = store.get(1)
    assert loaded is not None
    assert loaded.revision_id == 6


@pytest.mark.unit
def test_add_many_same_revision_replayed_changes_nothing(store: ArticleStore) -> None:
    batch = [_article(1, "Page", 5, redirects=("Alias",))]
    store.add_many(batch)
    assert store.add_many(batch) == 0
    assert store.count() == 1


@pytest.mark.unit
def test_add_many_newer_revision_replaces_the_redirects(store: ArticleStore) -> None:
    store.add_many([_article(1, "Page", 1, redirects=("Old alias",))])
    store.add_many([_article(1, "Page", 2, redirects=("New alias",))])
    assert store.by_title("old alias") is None
    found = store.by_title("new alias")
    assert found is not None
    assert found.page_id == 1


@pytest.mark.unit
def test_add_many_ignored_older_revision_keeps_the_redirects(store: ArticleStore) -> None:
    store.add_many([_article(1, "Page", 2, redirects=("Kept alias",))])
    store.add_many([_article(1, "Page", 1, redirects=("Other alias",))])
    assert store.by_title("kept alias") is not None
    assert store.by_title("other alias") is None


@pytest.mark.unit
def test_add_many_failing_batch_is_rolled_back_whole(store: ArticleStore) -> None:
    def batch() -> Iterator[Article]:
        yield _article(1, "Good")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        store.add_many(batch())
    assert store.count() == 0


@pytest.mark.unit
def test_by_title_exact_match_wins_over_case_variants(store: ArticleStore) -> None:
    store.add_many([_article(1, "AIDS"), _article(2, "Aids")])
    found = store.by_title("Aids")
    assert found is not None
    assert found.page_id == 2


@pytest.mark.unit
def test_by_title_underscores_and_first_letter_case_are_normalised(store: ArticleStore) -> None:
    store.add_many([_article(1, "Albert Einstein")])
    found = store.by_title("albert_Einstein")
    assert found is not None
    assert found.page_id == 1


@pytest.mark.unit
def test_by_title_case_insensitive_match_prefers_the_lowest_page_id(store: ArticleStore) -> None:
    store.add_many([_article(9, "Aids"), _article(3, "AIDS")])
    found = store.by_title("aiDs")
    assert found is not None
    assert found.page_id == 3


@pytest.mark.unit
def test_by_title_display_title_with_lower_first_letter_matches_folded(store: ArticleStore) -> None:
    store.add_many([_article(1, "iPhone")])
    found = store.by_title("iphone")
    assert found is not None
    assert found.title == "iPhone"


@pytest.mark.unit
def test_by_title_redirect_alias_resolves_to_its_target(store: ArticleStore) -> None:
    store.add_many([_article(1, "Albert Einstein", redirects=("Einstein", "A. Einstein"))])
    found = store.by_title("einstein")
    assert found is not None
    assert found.page_id == 1


@pytest.mark.unit
def test_by_title_real_title_beats_a_redirect_with_the_same_name(store: ArticleStore) -> None:
    planet = _article(2, "Mercury (planet)", redirects=("Mercury",))
    store.add_many([_article(1, "Mercury"), planet])
    found = store.by_title("Mercury")
    assert found is not None
    assert found.page_id == 1


@pytest.mark.unit
def test_by_title_unknown_or_blank_returns_none(store: ArticleStore) -> None:
    store.add_many([_article(1, "Something")])
    assert store.by_title("Nothing here") is None
    assert store.by_title("   ") is None


@pytest.mark.unit
def test_get_unknown_page_id_returns_none(store: ArticleStore) -> None:
    assert store.get(404) is None


@pytest.mark.unit
def test_iter_articles_streams_in_page_id_order_across_batches(store: ArticleStore) -> None:
    ids = [30, 10, 20, *range(100, 1200)]
    store.add_many([_article(page_id, f"T{page_id}") for page_id in ids])
    streamed = [article.page_id for article in store.iter_articles()]
    assert streamed == sorted(ids)


@pytest.mark.unit
def test_iter_articles_after_resumes_past_the_given_page_id(store: ArticleStore) -> None:
    store.add_many([_article(page_id, f"T{page_id}") for page_id in (1, 2, 3, 4)])
    assert [article.page_id for article in store.iter_articles(after=2)] == [3, 4]


@pytest.mark.unit
def test_iter_articles_after_the_last_page_yields_nothing(store: ArticleStore) -> None:
    store.add_many([_article(1, "T1")])
    assert list(store.iter_articles(after=1)) == []


@pytest.mark.unit
def test_chunks_done_round_trips_through_a_reopen(tmp_path: Path, snapshot: SnapshotMeta) -> None:
    path = store_path(tmp_path, snapshot)
    first = ArticleStore.create(path, snapshot, parser_version=_PARSER)
    first.mark_chunk_done("enwiki_namespace_0_chunk_0")
    first.mark_chunk_done("enwiki_namespace_0_chunk_0")
    first.close()
    second = ArticleStore.open(path, readonly=False)
    assert second.chunks_done() == frozenset({"enwiki_namespace_0_chunk_0"})
    second.close()


@pytest.mark.unit
def test_revision_map_lists_page_to_revision(store: ArticleStore) -> None:
    store.add_many([_article(1, "A", 11), _article(2, "B", 22)])
    assert store.revision_map() == {1: 11, 2: 22}


@pytest.mark.unit
def test_snapshot_survives_a_reopen(tmp_path: Path, snapshot: SnapshotMeta) -> None:
    path = store_path(tmp_path, snapshot)
    ArticleStore.create(path, snapshot, parser_version=_PARSER).close()
    reopened = ArticleStore.open(path, readonly=False)
    assert reopened.snapshot == snapshot
    reopened.close()


@pytest.mark.unit
def test_snapshot_json_round_trips(snapshot: SnapshotMeta) -> None:
    assert snapshot_from_json(snapshot_to_json(snapshot)) == snapshot


@pytest.mark.unit
def test_snapshot_from_json_missing_field_raises_value_error() -> None:
    with pytest.raises(ValueError, match="snapshot description"):
        snapshot_from_json('{"identifier": "x"}')


@pytest.mark.unit
def test_open_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="no article store"):
        ArticleStore.open(tmp_path / "nope.db")


@pytest.mark.unit
def test_open_file_that_is_not_a_store_raises(tmp_path: Path) -> None:
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all" * 50)
    with pytest.raises(StoreError):
        ArticleStore.open(junk, readonly=False)


@pytest.mark.unit
def test_open_other_schema_version_is_refused(tmp_path: Path, snapshot: SnapshotMeta) -> None:
    path = store_path(tmp_path, snapshot)
    ArticleStore.create(path, snapshot, parser_version=_PARSER).close()
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema'")
    finally:
        conn.close()
    with pytest.raises(StoreError, match="schema version"):
        ArticleStore.open(path, readonly=False)


_SCHEMA_1 = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE articles (
    page_id INTEGER PRIMARY KEY, title TEXT NOT NULL, title_fold TEXT NOT NULL,
    revision_id INTEGER NOT NULL, revision_time TEXT NOT NULL, categories TEXT NOT NULL,
    is_disambiguation INTEGER NOT NULL, license TEXT NOT NULL, abstract TEXT NOT NULL,
    text BLOB NOT NULL, chars INTEGER NOT NULL
);
CREATE TABLE redirects (
    alias_fold TEXT NOT NULL, page_id INTEGER NOT NULL, alias TEXT NOT NULL,
    PRIMARY KEY (alias_fold, page_id)
) WITHOUT ROWID;
"""


@pytest.mark.unit
@pytest.mark.parametrize("readonly", [True, False], ids=["readonly", "writable"])
def test_open_schema_1_store_is_refused_and_left_untouched(
    tmp_path: Path, snapshot: SnapshotMeta, readonly: bool
) -> None:
    # A real schema-1 layout (no text_hash column, no parser version), finished as a build would
    # leave it: nothing about it is damaged, it is simply the old format.
    path = store_path(tmp_path, snapshot)
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.executescript(_SCHEMA_1)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema', '1'), ('snapshot', ?),"
                " ('complete', '1')",
                (snapshot_to_json(snapshot),),
            )
    finally:
        conn.close()
    before = path.read_bytes()
    with pytest.raises(StoreError, match=r"schema version \('1'\)"):
        ArticleStore.open(path, readonly=readonly, allow_complete=True)
    assert path.read_bytes() == before


@pytest.mark.unit
def test_open_store_without_a_parser_version_row_is_refused(
    tmp_path: Path, snapshot: SnapshotMeta
) -> None:
    path = store_path(tmp_path, snapshot)
    ArticleStore.create(path, snapshot, parser_version=_PARSER).close()
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute("DELETE FROM meta WHERE key = 'parser_version'")
    finally:
        conn.close()
    with pytest.raises(StoreError, match="parser version"):
        ArticleStore.open(path, readonly=False)


# -- schema 2: the text hash and the parser version --------------------------------------------


@pytest.mark.unit
def test_add_many_round_trips_the_text_hash(store: ArticleStore) -> None:
    article = _article(7, "Hashed", text="Some text about a subject.")
    store.add_many([article])
    loaded = store.get(7)
    assert loaded is not None
    assert loaded.text_hash == text_hash("Some text about a subject.")


@pytest.mark.unit
def test_get_returns_the_hash_as_stored_without_recomputing_it(
    tmp_path: Path, snapshot: SnapshotMeta
) -> None:
    path = store_path(tmp_path, snapshot)
    built = ArticleStore.create(path, snapshot, parser_version=_PARSER)
    built.add_many([_article(7, "Hashed")])
    built.finalize()
    built.close()
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute("UPDATE articles SET text_hash = 12345 WHERE page_id = 7")
    finally:
        conn.close()
    served = ArticleStore.open(path, readonly=True)
    try:
        loaded = served.get(7)
        streamed = next(served.iter_articles())
    finally:
        served.close()
    assert loaded is not None
    assert (loaded.text_hash, streamed.text_hash) == (12345, 12345)


@pytest.mark.unit
def test_add_many_hash_that_is_not_the_texts_is_refused_and_the_batch_rolled_back(
    store: ArticleStore,
) -> None:
    wrong = _article(2, "Wrong", text_hash=text_hash("some other text"))
    with pytest.raises(StoreError, match="text hash"):
        store.add_many([_article(1, "Good"), wrong])
    assert store.count() == 0


@pytest.mark.unit
def test_add_many_newer_revision_replaces_the_text_hash(store: ArticleStore) -> None:
    store.add_many([_article(1, "Page", 1, text="first version")])
    store.add_many([_article(1, "Page", 2, text="second version")])
    loaded = store.get(1)
    assert loaded is not None
    assert loaded.text_hash == text_hash("second version")


@pytest.mark.unit
def test_create_records_the_parser_version_it_is_given(
    tmp_path: Path, snapshot: SnapshotMeta
) -> None:
    created = ArticleStore.create(store_path(tmp_path, snapshot), snapshot, parser_version=7)
    try:
        assert created.parser_version == 7
    finally:
        created.close()


@pytest.mark.unit
def test_parser_version_survives_a_reopen_for_writing_and_for_serving(
    tmp_path: Path, snapshot: SnapshotMeta
) -> None:
    path = store_path(tmp_path, snapshot)
    building = ArticleStore.create(path, snapshot, parser_version=7)
    building.close()
    resumed = ArticleStore.open(path, readonly=False)
    resumed.finalize()
    resumed.close()
    served = ArticleStore.open(path, readonly=True)
    try:
        assert served.parser_version == 7
    finally:
        served.close()


@pytest.mark.unit
def test_parser_version_is_read_only(store: ArticleStore) -> None:
    with pytest.raises(AttributeError):
        store.parser_version = 99  # type: ignore[misc]


# -- completeness and read-only serving --------------------------------------------------------


@pytest.mark.unit
def test_is_complete_false_until_finalized(store: ArticleStore) -> None:
    assert store.is_complete is False
    store.finalize()
    assert store.is_complete is True


@pytest.mark.unit
def test_open_readonly_incomplete_store_is_refused(tmp_path: Path, snapshot: SnapshotMeta) -> None:
    path = store_path(tmp_path, snapshot)
    building = ArticleStore.create(path, snapshot, parser_version=_PARSER)
    building.add_many([_article(1, "Half built")])
    building.close()
    with pytest.raises(StoreError, match="incomplete"):
        ArticleStore.open(path, readonly=True)


@pytest.mark.unit
def test_finalize_leaves_a_single_file_without_wal_siblings(
    tmp_path: Path, snapshot: SnapshotMeta
) -> None:
    path = store_path(tmp_path, snapshot)
    built = ArticleStore.create(path, snapshot, parser_version=_PARSER)
    built.add_many([_article(1, "Done")])
    built.finalize()
    built.close()
    assert sorted(entry.name for entry in path.parent.iterdir()) == [path.name]


@pytest.mark.unit
def test_finalized_store_opens_readonly_from_an_unwritable_directory(
    tmp_path: Path, snapshot: SnapshotMeta
) -> None:
    path = store_path(tmp_path, snapshot)
    built = ArticleStore.create(path, snapshot, parser_version=_PARSER)
    built.add_many([_article(1, "Served", redirects=("Alias",))])
    built.mark_chunk_done("enwiki_namespace_0_chunk_0")
    built.finalize()
    built.close()
    directory = path.parent
    original = stat.S_IMODE(directory.stat().st_mode)
    directory.chmod(0o555)
    try:
        # A root user ignores directory permissions, which would make this test vacuous.
        if os.access(directory, os.W_OK):
            pytest.skip("directory permissions are not enforced for this user")
        served = ArticleStore.open(path, readonly=True)
        try:
            assert served.is_complete is True
            found = served.by_title("alias")
            assert found is not None
            assert found.title == "Served"
            assert served.count() == 1
        finally:
            served.close()
        assert sorted(entry.name for entry in directory.iterdir()) == [path.name]
    finally:
        directory.chmod(original)


@pytest.mark.unit
def test_readonly_store_refuses_writes(tmp_path: Path, snapshot: SnapshotMeta) -> None:
    path = store_path(tmp_path, snapshot)
    built = ArticleStore.create(path, snapshot, parser_version=_PARSER)
    built.finalize()
    built.close()
    served = ArticleStore.open(path, readonly=True)
    try:
        with pytest.raises(StoreError, match="read-only"):
            served.add_many([_article(1, "Nope")])
        with pytest.raises(StoreError, match="read-only"):
            served.mark_chunk_done("c")
        with pytest.raises(StoreError, match="read-only"):
            served.finalize()
    finally:
        served.close()


@pytest.mark.unit
def test_readonly_connection_itself_refuses_a_raw_write(
    tmp_path: Path, snapshot: SnapshotMeta
) -> None:
    path = store_path(tmp_path, snapshot)
    built = ArticleStore.create(path, snapshot, parser_version=_PARSER)
    built.finalize()
    built.close()
    served = ArticleStore.open(path, readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            served._conn.execute("DELETE FROM chunks")
    finally:
        served.close()


@pytest.mark.unit
def test_open_writable_of_a_finished_store_is_refused(
    tmp_path: Path, snapshot: SnapshotMeta
) -> None:
    path = store_path(tmp_path, snapshot)
    built = ArticleStore.create(path, snapshot, parser_version=_PARSER)
    built.add_many([_article(1, "Served")])
    built.finalize()
    built.close()
    with pytest.raises(StoreError, match="finished store"):
        ArticleStore.open(path, readonly=False)


@pytest.mark.unit
def test_open_writable_of_a_finished_store_leaves_the_file_untouched(
    tmp_path: Path, snapshot: SnapshotMeta
) -> None:
    path = store_path(tmp_path, snapshot)
    built = ArticleStore.create(path, snapshot, parser_version=_PARSER)
    built.add_many([_article(1, "Served")])
    built.finalize()
    built.close()
    before = path.read_bytes()
    with pytest.raises(StoreError):
        ArticleStore.open(path, readonly=False)
    # The refusal comes before the switch back to WAL, which would rewrite the header and
    # leave -wal/-shm siblings beside a file the API may have open.
    assert path.read_bytes() == before
    assert sorted(entry.name for entry in path.parent.iterdir()) == [path.name]
    served = ArticleStore.open(path, readonly=True)
    try:
        assert served.is_complete is True
    finally:
        served.close()


@pytest.mark.unit
def test_open_writable_of_an_unfinished_store_still_resumes(
    tmp_path: Path, snapshot: SnapshotMeta
) -> None:
    path = store_path(tmp_path, snapshot)
    building = ArticleStore.create(path, snapshot, parser_version=_PARSER)
    building.add_many([_article(1, "Half built")])
    building.close()
    resumed = ArticleStore.open(path, readonly=False)
    try:
        assert resumed.count() == 1
    finally:
        resumed.close()


@pytest.mark.unit
def test_writing_to_a_finalized_store_withdraws_the_complete_flag(
    tmp_path: Path, snapshot: SnapshotMeta
) -> None:
    path = store_path(tmp_path, snapshot)
    built = ArticleStore.create(path, snapshot, parser_version=_PARSER)
    built.finalize()
    built.close()
    reopened = ArticleStore.open(path, readonly=False, allow_complete=True)
    assert reopened.is_complete is True
    reopened.add_many([_article(1, "Late edit")])
    assert reopened.is_complete is False
    reopened.close()
    with pytest.raises(StoreError, match="incomplete"):
        ArticleStore.open(path, readonly=True)


@pytest.mark.unit
def test_readonly_store_is_usable_from_several_threads(
    tmp_path: Path, snapshot: SnapshotMeta
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    path = store_path(tmp_path, snapshot)
    built = ArticleStore.create(path, snapshot, parser_version=_PARSER)
    built.add_many([_article(page_id, f"Title {page_id}") for page_id in range(1, 41)])
    built.finalize()
    built.close()
    served = ArticleStore.open(path, readonly=True)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            titles = list(pool.map(lambda n: served.get(n), range(1, 41)))
    finally:
        served.close()
    assert [article.page_id for article in titles if article] == list(range(1, 41))
