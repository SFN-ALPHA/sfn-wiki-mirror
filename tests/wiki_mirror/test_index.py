"""Qualifies the passage index: collection shape, point ids and payload, resume, alias swap.

`index.run_index` runs against Qdrant's in-memory local mode (real sparse-vector, IDF and alias
semantics, no server), an in-memory article source and a fake build-state store, so the tests
need neither R1's download/parse stages nor a Qdrant process. Failure paths named here: a crash
mid-index resumes without re-writing finished articles; an interrupted or unreadable index state
restarts safely; a serving collection is never rebuilt over; an empty store is refused.

The collection is named for its birth (`wikipedia_lexical_<born>`) and its index state file
records `applied_snapshot`, the snapshot it reflects (docs/DESIGN.md section 12.3): the tests
cover the names, when `applied_snapshot` is set and read, the payload's `text_hash`, the segment
cap, and which stores and state files a swap keeps.

See docs/DESIGN.md sections 5 and 12.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from httpx import Headers
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from tests.fixtures.wiki_mirror_fakes import (
    FakeSource,
    FakeStateStore,
    make_article,
    make_config,
    make_snapshot,
    memory_client,
)
from wiki_mirror import build, index
from wiki_mirror.build import BuildError
from wiki_mirror.index import (
    ALIAS,
    IndexBuildError,
    aliased_collection,
    collection_name,
    run_index,
)
from wiki_mirror.records import Article, passage_point_id
from wiki_mirror.text.sanitize import ChunkConfig

pytestmark = pytest.mark.contract

_FILLER = "The committee reviewed the proposal and recorded its findings in the minutes. "

#: Tiny batches, checkpointed every time, so a crash lands between two known checkpoints.
_PAIRS = {"batch_articles": 2, "checkpoint_every": 1}
_EVERY_ARTICLE = {"batch_articles": 1, "checkpoint_every": 1}


def _articles() -> list[Article]:
    """Three articles, one of them a disambiguation page, with two passages in the first."""
    return [
        make_article(
            10,
            "Alpha",
            {
                "": f"Alpha is a letter. {_FILLER}",
                "History": f"Alpha was first used long ago. {_FILLER}",
            },
        ),
        make_article(20, "Beta", is_disambiguation=True),
        make_article(30, "Gamma"),
    ]


def _run(
    tmp_path: Path, client: QdrantClient, source: FakeSource, fake: FakeStateStore, **kw: Any
) -> int:
    """Run the index with the fakes wired in and one worker (no process pool)."""
    return run_index(
        make_config(tmp_path),
        source,
        client,
        workers=1,
        state_reader=fake.read,
        state_writer=fake.write,
        **kw,
    )


def _state_file(tmp_path: Path, born: str = "20261001") -> Path:
    """Where the test run's index state of the collection born on tag `born` is (on-disk name)."""
    return tmp_path / "data" / "state" / f"index_lexical_{born}.json"


def _all_points(client: QdrantClient, name: str) -> list[Any]:
    """Every point of a collection, with payload, ordered by id."""
    points, _ = client.scroll(name, limit=10_000, with_payload=True)
    return sorted(points, key=lambda point: int(point.id))


def test_collection_name_snapshot_date_is_tagged_without_dashes() -> None:
    assert collection_name(make_snapshot("2026-10-01")) == "wikipedia_lexical_20261001"


def test_run_index_articles_get_deterministic_point_ids(tmp_path: Path) -> None:
    client = memory_client()
    _run(tmp_path, client, FakeSource(_articles()), FakeStateStore())
    ids = [int(point.id) for point in _all_points(client, "wikipedia_lexical_20261001")]
    assert ids == [
        passage_point_id(10, 0),
        passage_point_id(10, 1),
        passage_point_id(20, 0),
        passage_point_id(30, 0),
    ]


def test_run_index_payload_holds_exactly_the_six_fields_and_no_text(tmp_path: Path) -> None:
    client = memory_client()
    articles = _articles()
    _run(tmp_path, client, FakeSource(articles), FakeStateStore())
    points = {int(p.id): p.payload for p in _all_points(client, "wikipedia_lexical_20261001")}
    assert points[passage_point_id(10, 1)] == {
        "page_id": 10,
        "revision_id": 101,
        "chunk": 1,
        "section": "History",
        "disambiguation": False,
        "text_hash": articles[0].text_hash,
    }
    assert points[passage_point_id(20, 0)]["disambiguation"] is True


def test_run_index_every_passage_carries_the_text_hash_of_its_own_article(tmp_path: Path) -> None:
    client = memory_client()
    articles = _articles()
    _run(tmp_path, client, FakeSource(articles), FakeStateStore())
    hashes = {article.page_id: article.text_hash for article in articles}
    points = _all_points(client, "wikipedia_lexical_20261001")
    assert {p.payload["text_hash"] == hashes[p.payload["page_id"]] for p in points} == {True}
    assert len(set(hashes.values())) == 3  # three different texts, so the check above has teeth


def test_run_index_collection_has_idf_sparse_vector_and_no_dense_vector(tmp_path: Path) -> None:
    client = memory_client()
    _run(tmp_path, client, FakeSource(_articles()), FakeStateStore())
    params = client.get_collection("wikipedia_lexical_20261001").config.params
    assert params.sparse_vectors is not None
    assert params.sparse_vectors["lexical"].modifier == models.Modifier.IDF
    assert not params.vectors


def test_run_index_returns_the_passage_count(tmp_path: Path) -> None:
    assert _run(tmp_path, memory_client(), FakeSource(_articles()), FakeStateStore()) == 4


def test_run_index_points_the_alias_at_the_new_collection(tmp_path: Path) -> None:
    client = memory_client()
    _run(tmp_path, client, FakeSource(_articles()), FakeStateStore())
    assert aliased_collection(client) == "wikipedia_lexical_20261001"
    assert ALIAS == "wikipedia_lexical"


def test_run_index_new_snapshot_keeps_the_previous_and_deletes_older(tmp_path: Path) -> None:
    client = memory_client()
    for date in ("2026-08-01", "2026-09-01", "2026-10-01"):
        _run(tmp_path, client, FakeSource(_articles(), make_snapshot(date)), FakeStateStore())
    names = {c.name for c in client.get_collections().collections}
    assert names == {"wikipedia_lexical_20260901", "wikipedia_lexical_20261001"}
    assert aliased_collection(client) == "wikipedia_lexical_20261001"


def test_run_index_cleanup_leaves_collections_it_did_not_create(tmp_path: Path) -> None:
    client = memory_client()
    dense = models.VectorParams(size=2, distance=models.Distance.COSINE)
    client.create_collection("scratch", vectors_config=dense)
    _run(tmp_path, client, FakeSource(_articles()), FakeStateStore())
    assert "scratch" in {c.name for c in client.get_collections().collections}


def test_run_index_old_snapshot_named_collections_and_alias_are_not_ours(tmp_path: Path) -> None:
    client = memory_client()
    client.create_collection(
        "wikipedia_20260801",
        vectors_config={},
        sparse_vectors_config={"lexical": models.SparseVectorParams()},
    )
    client.update_collection_aliases(
        change_aliases_operations=[
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(
                    collection_name="wikipedia_20260801", alias_name="wikipedia"
                )
            )
        ]
    )
    assert aliased_collection(client) is None
    _run(tmp_path, client, FakeSource(_articles()), FakeStateStore())
    assert "wikipedia_20260801" in {c.name for c in client.get_collections().collections}
    assert aliased_collection(client) == "wikipedia_lexical_20261001"


def test_run_index_collection_is_created_with_the_segment_size_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    created: list[dict[str, Any]] = []
    real = client.create_collection

    def spy(**kwargs: Any) -> Any:
        created.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(client, "create_collection", spy)
    _run(tmp_path, client, FakeSource(_articles()), FakeStateStore())
    assert len(created) == 1
    # Qdrant's unit is kilobytes. Small enough that building one segment fits Qdrant's heap, and
    # that a segment rewrite never needs a third of the disk.
    assert created[0]["optimizers_config"].max_segment_size == 400_000


def test_run_index_collection_is_created_with_one_optimization_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    created: list[dict[str, Any]] = []
    real = client.create_collection

    def spy(**kwargs: Any) -> Any:
        created.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(client, "create_collection", spy)
    _run(tmp_path, client, FakeSource(_articles()), FakeStateStore())
    # Concurrent merges OOM-killed Qdrant twice during the first full build.
    assert created[0]["optimizers_config"].max_optimization_threads == 1


# -- the index state: what a collection reflects ---------------------------------------------------


def test_run_index_completed_build_records_its_born_snapshot_as_applied(tmp_path: Path) -> None:
    _run(tmp_path, memory_client(), FakeSource(_articles()), FakeStateStore())
    state = json.loads(_state_file(tmp_path).read_text(encoding="utf-8"))
    assert (state["born"], state["applied_snapshot"], state["complete"]) == (
        "20261001",
        "20261001",
        True,
    )


def test_applied_snapshot_reads_the_snapshot_a_completed_build_applied(tmp_path: Path) -> None:
    _run(tmp_path, memory_client(), FakeSource(_articles()), FakeStateStore())
    found = index.applied_snapshot(tmp_path / "data", "wikipedia_lexical_20261001")
    assert found == "20261001"


def test_applied_snapshot_follows_the_state_file_not_the_collection_name(tmp_path: Path) -> None:
    # What W3's in-place update will do: the name stays, the state moves.
    _run(tmp_path, memory_client(), FakeSource(_articles()), FakeStateStore())
    path = _state_file(tmp_path)
    moved = json.loads(path.read_text(encoding="utf-8"))
    moved["applied_snapshot"] = "20261101"
    path.write_text(json.dumps(moved), encoding="utf-8")
    assert index.applied_snapshot(tmp_path / "data", "wikipedia_lexical_20261001") == "20261101"


def test_applied_snapshot_of_an_unfinished_build_is_none(tmp_path: Path) -> None:
    articles = [make_article(page_id, f"Title{page_id}") for page_id in (1, 2, 3, 4)]
    crashing = _CrashingSource(articles, after=3)
    with pytest.raises(_CrashError):
        _run(tmp_path, memory_client(), crashing, FakeStateStore(), **_PAIRS)
    state = json.loads(_state_file(tmp_path).read_text(encoding="utf-8"))
    assert state["complete"] is False
    assert index.applied_snapshot(tmp_path / "data", "wikipedia_lexical_20261001") is None


@pytest.mark.parametrize(
    "contents",
    [
        None,
        "{not json",
        "[]",
        '{"born": "20261001"}',
        '{"applied_snapshot": 20261001}',
        '{"applied_snapshot": ""}',
        '{"applied_snapshot": "2026-10-01"}',
        '{"applied_snapshot": "20261001\\n"}',
    ],
    ids=[
        "no-file",
        "garbage",
        "not-an-object",
        "no-key",
        "not-a-string",
        "empty",
        "a-date-not-a-tag",
        "trailing-newline",
    ],
)
def test_applied_snapshot_without_a_usable_record_is_none(
    tmp_path: Path, contents: str | None
) -> None:
    if contents is not None:
        path = _state_file(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(contents, encoding="utf-8")
    assert index.applied_snapshot(tmp_path / "data", "wikipedia_lexical_20261001") is None


@pytest.mark.parametrize(
    "collection",
    ["scratch", "wikipedia_2026", "wikipedia_20261001", "wikipedia_lexical_2026"],
    ids=["foreign", "short-tag", "old-style-name", "short-born"],
)
def test_applied_snapshot_of_a_name_this_module_did_not_create_is_none(
    tmp_path: Path, collection: str
) -> None:
    _run(tmp_path, memory_client(), FakeSource(_articles()), FakeStateStore())
    assert index.applied_snapshot(tmp_path / "data", collection) is None


def test_run_index_build_state_moves_from_indexing_to_serving(tmp_path: Path) -> None:
    fake = FakeStateStore()
    _run(tmp_path, memory_client(), FakeSource(_articles()), fake)
    assert fake.phases == ["idle", "indexing", "serving"]
    assert (fake.state.passages_indexed, fake.state.articles) == (4, 3)


def test_run_index_failure_records_only_the_exception_class(tmp_path: Path) -> None:
    class Exploding(FakeSource):
        def iter_articles(self, after: int | None = None) -> Iterator[Article]:
            raise RuntimeError("secret detail that must not be recorded")

    fake = FakeStateStore()
    with pytest.raises(RuntimeError):
        _run(tmp_path, memory_client(), Exploding(_articles()), fake)
    assert fake.state.phase == "failed"
    assert fake.state.error == "index: RuntimeError"


def test_run_index_empty_store_is_refused_before_anything_is_created(tmp_path: Path) -> None:
    client = memory_client()
    with pytest.raises(IndexBuildError, match="empty"):
        _run(tmp_path, client, FakeSource([]), FakeStateStore())
    assert client.get_collections().collections == []


class _CrashError(Exception):
    """Raised by the source to simulate a crash part-way through the index."""


class _CrashingSource(FakeSource):
    """A source that fails after yielding a number of articles."""

    def __init__(self, articles: list[Article], after: int) -> None:
        super().__init__(articles)
        self._after = after

    def iter_articles(self, after: int | None = None) -> Iterator[Article]:
        for number, article in enumerate(super().iter_articles(after)):
            if number == self._after:
                raise _CrashError
            yield article


def _record_upserts(client: QdrantClient, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the page id of every point the client is asked to upsert."""
    seen: list[int] = []
    real = client.upsert

    def spy(collection_name: str, points: list[models.PointStruct], **kw: Any) -> Any:
        seen.extend(int(point.id) // 10_000 for point in points)
        return real(collection_name=collection_name, points=points, **kw)

    monkeypatch.setattr(client, "upsert", spy)
    return seen


def test_run_index_resumes_after_the_checkpoint_without_rewriting_finished_articles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    articles = [make_article(page_id, f"Title{page_id}") for page_id in (1, 2, 3, 4, 5, 6)]
    with pytest.raises(_CrashError):
        _run(tmp_path, client, _CrashingSource(articles, after=5), FakeStateStore(), **_PAIRS)
    seen = _record_upserts(client, monkeypatch)
    total = _run(tmp_path, client, FakeSource(articles), FakeStateStore(), **_PAIRS)
    assert sorted(set(seen)) == [5, 6]
    assert total == 6
    assert len(_all_points(client, "wikipedia_lexical_20261001")) == 6


def test_run_index_resume_asks_the_store_to_seek_past_the_checkpoint(tmp_path: Path) -> None:
    asked: list[int | None] = []

    class Spy(FakeSource):
        def iter_articles(self, after: int | None = None) -> Iterator[Article]:
            asked.append(after)
            return super().iter_articles(after)

    client = memory_client()
    articles = [make_article(page_id, f"Title{page_id}") for page_id in (1, 2, 3, 4, 5, 6)]
    with pytest.raises(_CrashError):
        _run(tmp_path, client, _CrashingSource(articles, after=5), FakeStateStore(), **_PAIRS)
    _run(tmp_path, client, Spy(articles), FakeStateStore(), **_PAIRS)
    assert asked == [4]  # the last page id of the last checkpointed pair of articles


def test_run_index_resumed_run_equals_an_uninterrupted_run(tmp_path: Path) -> None:
    articles = [make_article(page_id, f"Title{page_id}") for page_id in range(1, 8)]
    whole = memory_client()
    _run(tmp_path / "a", whole, FakeSource(articles), FakeStateStore())
    resumed = memory_client()
    crashing = _CrashingSource(articles, after=4)
    with pytest.raises(_CrashError):
        _run(tmp_path / "b", resumed, crashing, FakeStateStore(), **_PAIRS)
    _run(tmp_path / "b", resumed, FakeSource(articles), FakeStateStore(), **_PAIRS)
    assert [(p.id, p.payload) for p in _all_points(resumed, "wikipedia_lexical_20261001")] == [
        (p.id, p.payload) for p in _all_points(whole, "wikipedia_lexical_20261001")
    ]


def test_run_index_unreadable_index_state_starts_the_collection_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    crashing = _CrashingSource(_articles(), after=2)
    with pytest.raises(_CrashError):
        _run(tmp_path, client, crashing, FakeStateStore(), **_EVERY_ARTICLE)
    _state_file(tmp_path).write_text("{not json", encoding="utf-8")
    seen = _record_upserts(client, monkeypatch)
    assert _run(tmp_path, client, FakeSource(_articles()), FakeStateStore()) == 4
    assert 10 in seen


def test_run_index_serving_collection_without_an_index_state_is_not_rebuilt(
    tmp_path: Path,
) -> None:
    client = memory_client()
    _run(tmp_path, client, FakeSource(_articles()), FakeStateStore())
    _state_file(tmp_path).unlink()
    with pytest.raises(IndexBuildError, match="already serves"):
        _run(tmp_path, client, FakeSource(_articles()), FakeStateStore())
    assert len(_all_points(client, "wikipedia_lexical_20261001")) == 4


def test_run_index_completed_index_state_only_repeats_the_alias_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    _run(tmp_path, client, FakeSource(_articles()), FakeStateStore())
    seen = _record_upserts(client, monkeypatch)
    assert _run(tmp_path, client, FakeSource(_articles()), FakeStateStore()) == 4
    assert seen == []


def test_run_index_chunks_past_the_stride_are_skipped_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index, "PASSAGE_STRIDE", 2)
    sections = {"": f"Lead. {_FILLER}", "One": _FILLER, "Two": _FILLER, "Three": _FILLER}
    source = FakeSource([make_article(10, "Many", sections)])
    total = _run(tmp_path, memory_client(), source, FakeStateStore())
    state = json.loads(_state_file(tmp_path).read_text(encoding="utf-8"))
    assert (total, state["over_stride"]) == (2, 2)


def test_run_index_chunk_with_no_searchable_terms_is_skipped_and_counted(tmp_path: Path) -> None:
    # The title and heading are stop-words too: they are part of the indexed text.
    sections = {"": f"Real text. {_FILLER}", "And": "the of and the of and the of and the of and"}
    source = FakeSource([make_article(10, "The", sections)])
    total = _run(tmp_path, memory_client(), source, FakeStateStore())
    state = json.loads(_state_file(tmp_path).read_text(encoding="utf-8"))
    assert (total, state["empty"]) == (1, 1)


def test_run_index_process_pool_produces_the_same_points_as_one_process(tmp_path: Path) -> None:
    articles = [make_article(page_id, f"Title{page_id}") for page_id in range(1, 6)]
    single = memory_client()
    _run(tmp_path / "a", single, FakeSource(articles), FakeStateStore())
    pooled = memory_client()
    run_index(
        make_config(tmp_path / "b"),
        FakeSource(articles),
        pooled,
        workers=2,
        batch_articles=2,
        state_reader=FakeStateStore().read,
        state_writer=FakeStateStore().write,
    )
    assert [(p.id, p.payload) for p in _all_points(pooled, "wikipedia_lexical_20261001")] == [
        (p.id, p.payload) for p in _all_points(single, "wikipedia_lexical_20261001")
    ]


# -- the chunker is fingerprinted ----------------------------------------------------------------


def test_chunker_fingerprint_follows_the_version_and_the_chunk_sizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = index.chunker_fingerprint()
    monkeypatch.setattr(index, "CHUNKER_VERSION", index.CHUNKER_VERSION + 1)
    bumped = index.chunker_fingerprint()
    monkeypatch.setattr(index, "CHUNK_CONFIG", ChunkConfig(max_tokens=256, overlap_tokens=50))
    resized = index.chunker_fingerprint()
    assert len({original, bumped, resized}) == 3


def test_run_index_records_the_chunker_in_the_index_state(tmp_path: Path) -> None:
    _run(tmp_path, memory_client(), FakeSource(_articles()), FakeStateStore())
    recorded = json.loads(_state_file(tmp_path).read_text(encoding="utf-8"))["chunker"]
    assert recorded == index.chunker_fingerprint()
    found = index.indexed_chunker(tmp_path / "data", "wikipedia_lexical_20261001")
    assert found == index.chunker_fingerprint()


@pytest.mark.parametrize(
    "contents",
    [None, "{not json", "[]", '{"born": "20261001"}', '{"chunker": 7}', '{"chunker": ""}'],
    ids=["no-file", "garbage", "not-an-object", "no-key", "not-a-string", "empty"],
)
def test_indexed_chunker_without_a_usable_record_is_none_not_a_mismatch(
    tmp_path: Path, contents: str | None
) -> None:
    if contents is not None:
        path = _state_file(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(contents, encoding="utf-8")
    assert index.indexed_chunker(tmp_path / "data", "wikipedia_lexical_20261001") is None


def test_run_index_encodes_with_the_chunk_config_it_fingerprints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = " ".join(f"Sentence {n} here." for n in range(400))
    long_article = make_article(10, "Long", {"": text})
    whole = _run(tmp_path / "a", memory_client(), FakeSource([long_article]), FakeStateStore())
    monkeypatch.setattr(index, "CHUNK_CONFIG", ChunkConfig(max_tokens=64, overlap_tokens=8))
    small = _run(tmp_path / "b", memory_client(), FakeSource([long_article]), FakeStateStore())
    assert small > whole


def test_run_index_state_from_another_chunker_is_not_resumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    articles = [make_article(page_id, f"Title{page_id}") for page_id in (1, 2, 3, 4, 5, 6)]
    with pytest.raises(_CrashError):
        _run(tmp_path, client, _CrashingSource(articles, after=5), FakeStateStore(), **_PAIRS)
    monkeypatch.setattr(index, "CHUNKER_VERSION", index.CHUNKER_VERSION + 1)
    seen = _record_upserts(client, monkeypatch)
    assert _run(tmp_path, client, FakeSource(articles), FakeStateStore(), **_PAIRS) == 6
    # Not "pages 5 and 6 only": two cuts of one article must never share a collection.
    assert sorted(set(seen)) == [1, 2, 3, 4, 5, 6]
    recorded = json.loads(_state_file(tmp_path).read_text(encoding="utf-8"))["chunker"]
    assert recorded == index.chunker_fingerprint()


def test_run_index_state_without_a_chunker_record_is_not_resumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    articles = [make_article(page_id, f"Title{page_id}") for page_id in (1, 2, 3, 4, 5, 6)]
    with pytest.raises(_CrashError):
        _run(tmp_path, client, _CrashingSource(articles, after=5), FakeStateStore(), **_PAIRS)
    path = _state_file(tmp_path)
    old = json.loads(path.read_text(encoding="utf-8"))
    del old["chunker"]
    path.write_text(json.dumps(old), encoding="utf-8")
    seen = _record_upserts(client, monkeypatch)
    _run(tmp_path, client, FakeSource(articles), FakeStateStore(), **_PAIRS)
    assert 1 in seen


def test_run_index_serving_collection_cut_by_another_chunker_is_not_rebuilt_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    _run(tmp_path, client, FakeSource(_articles()), FakeStateStore())
    monkeypatch.setattr(index, "CHUNKER_VERSION", index.CHUNKER_VERSION + 1)
    with pytest.raises(IndexBuildError, match="another chunker"):
        _run(tmp_path, client, FakeSource(_articles()), FakeStateStore())
    assert len(_all_points(client, "wikipedia_lexical_20261001")) == 4


# -- the page_id payload index lives on disk -----------------------------------------------------


def test_run_index_page_id_payload_index_is_an_on_disk_integer_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    created: list[dict[str, Any]] = []
    real = client.create_payload_index

    def spy(**kwargs: Any) -> Any:
        created.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(client, "create_payload_index", spy)
    _run(tmp_path, client, FakeSource(_articles()), FakeStateStore())
    assert [call["field_name"] for call in created] == ["page_id"]
    schema = created[0]["field_schema"]
    # What goes over the wire to the server: an integer index with `on_disk` and nothing else.
    assert schema.model_dump(exclude_unset=True, exclude_none=True, mode="json") == {
        "type": "integer",
        "on_disk": True,
    }


# -- the free-disk floor -------------------------------------------------------------------------


class _Usage(NamedTuple):
    """What `shutil.disk_usage` returns."""

    total: int
    used: int
    free: int


def _disk(monkeypatch: pytest.MonkeyPatch, free_gb: float) -> None:
    """Make the disk report `free_gb` GB free."""
    gb = 1024**3
    monkeypatch.setattr(
        build.shutil, "disk_usage", lambda _path: _Usage(100 * gb, 0, int(free_gb * gb))
    )


def test_run_index_below_the_disk_floor_refuses_before_creating_the_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disk(monkeypatch, 2)
    client = memory_client()
    fake = FakeStateStore()
    with pytest.raises(BuildError, match="not enough free disk to create the collection"):
        run_index(
            make_config(tmp_path, min_free_disk_gb=20),
            FakeSource(_articles()),
            client,
            workers=1,
            state_reader=fake.read,
            state_writer=fake.write,
        )
    assert client.get_collections().collections == []
    assert fake.state.phase == "failed"
    # A BuildError's text is meant to be read; this one tells the operator what to free.
    assert "free disk" in fake.state.error


def test_run_index_resuming_a_collection_is_not_blocked_by_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    articles = [make_article(page_id, f"Title{page_id}") for page_id in (1, 2, 3, 4)]
    with pytest.raises(_CrashError):
        _run(tmp_path, client, _CrashingSource(articles, after=3), FakeStateStore(), **_PAIRS)
    _disk(monkeypatch, 2)
    fake = FakeStateStore()
    total = run_index(
        make_config(tmp_path, min_free_disk_gb=20),
        FakeSource(articles),
        client,
        workers=1,
        state_reader=fake.read,
        state_writer=fake.write,
        **_PAIRS,
    )
    assert total == 4


# -- local files follow the collections ----------------------------------------------------------

_STORE = "enwiki_namespace_0_{tag}.db"


def _touch_store(tmp_path: Path, tag: str, name: str = _STORE) -> Path:
    """Put a placeholder article-store file in the test's `articles/` directory."""
    path = tmp_path / "data" / "articles" / name.format(tag=tag)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"placeholder")
    return path


def _index_snapshot(tmp_path: Path, client: QdrantClient, date: str) -> int:
    """Index the three test articles as the snapshot of `date`."""
    return _run(tmp_path, client, FakeSource(_articles(), make_snapshot(date)), FakeStateStore())


def test_run_index_swap_deletes_the_stores_and_states_that_fell_out_of_the_pair(
    tmp_path: Path,
) -> None:
    client = memory_client()
    for tag, date in (("20260801", "2026-08-01"), ("20260901", "2026-09-01")):
        _touch_store(tmp_path, tag)
        _index_snapshot(tmp_path, client, date)
    _touch_store(tmp_path, "20261001")
    _index_snapshot(tmp_path, client, "2026-10-01")
    stores = sorted(p.name for p in (tmp_path / "data" / "articles").iterdir())
    states = sorted(p.name for p in (tmp_path / "data" / "state").glob("index_lexical_*.json"))
    assert stores == [_STORE.format(tag="20260901"), _STORE.format(tag="20261001")]
    assert states == ["index_lexical_20260901.json", "index_lexical_20261001.json"]


def test_run_index_swap_never_touches_a_newer_store_or_files_it_does_not_own(
    tmp_path: Path,
) -> None:
    client = memory_client()
    keepers = [
        _touch_store(tmp_path, "20261101"),  # a build in progress, newer than this swap
        _touch_store(tmp_path, "20260801", "other_namespace_0_{tag}.db"),  # another identifier
        _touch_store(tmp_path, "20260801", "enwiki_namespace_0_{tag}.db.creating"),
        _touch_store(tmp_path, "x", "notes.txt"),
    ]
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True)
    (state / "index_lexical_20261101.json").write_text("{}", encoding="utf-8")
    (state / "build.json").write_text("{}", encoding="utf-8")
    (state / "index_lexical_abc.json").write_text("{}", encoding="utf-8")
    _index_snapshot(tmp_path, client, "2026-10-01")
    assert all(path.exists() for path in keepers)
    assert {p.name for p in state.iterdir()} >= {
        "index_lexical_20261101.json",
        "build.json",
        "index_lexical_abc.json",
    }


def test_run_index_swap_keeps_the_previous_snapshots_store_for_the_way_back(
    tmp_path: Path,
) -> None:
    client = memory_client()
    previous = _touch_store(tmp_path, "20260901")
    _index_snapshot(tmp_path, client, "2026-09-01")
    _index_snapshot(tmp_path, client, "2026-10-01")
    assert previous.exists()
    assert _state_file(tmp_path, "20260901").exists()


def test_run_index_swap_keeps_the_store_a_kept_collection_currently_reflects(
    tmp_path: Path,
) -> None:
    client = memory_client()
    _index_snapshot(tmp_path, client, "2026-08-01")
    _index_snapshot(tmp_path, client, "2026-09-01")
    # What a W3 in-place update leaves: the September collection now reflects a later snapshot,
    # so that store is the one it needs, and its own born store (September) no longer is.
    path = _state_file(tmp_path, "20260901")
    moved = json.loads(path.read_text(encoding="utf-8"))
    moved["applied_snapshot"] = "20260915"
    path.write_text(json.dumps(moved), encoding="utf-8")
    for tag in ("20260801", "20260901", "20260915"):
        _touch_store(tmp_path, tag)
    _touch_store(tmp_path, "20261001")
    _index_snapshot(tmp_path, client, "2026-10-01")
    stores = sorted(p.name for p in (tmp_path / "data" / "articles").iterdir())
    states = sorted(p.name for p in (tmp_path / "data" / "state").glob("index_lexical_*.json"))
    assert stores == [_STORE.format(tag="20260915"), _STORE.format(tag="20261001")]
    assert states == ["index_lexical_20260901.json", "index_lexical_20261001.json"]


def test_run_index_swap_kept_collection_with_an_unreadable_state_keeps_its_born_store(
    tmp_path: Path,
) -> None:
    client = memory_client()
    _index_snapshot(tmp_path, client, "2026-09-01")
    previous = _touch_store(tmp_path, "20260901")
    _state_file(tmp_path, "20260901").write_text("{not json", encoding="utf-8")
    _touch_store(tmp_path, "20261001")
    _index_snapshot(tmp_path, client, "2026-10-01")
    # Doubt about what the way-back collection reflects is settled towards keeping its store.
    assert previous.exists()


def test_run_index_swap_repeated_keeps_the_newest_other_collection_as_the_way_back(
    tmp_path: Path,
) -> None:
    client = memory_client()
    _index_snapshot(tmp_path, client, "2026-09-01")
    _index_snapshot(tmp_path, client, "2026-10-01")
    # An older leftover of ours, and the stores that go with each collection.
    client.create_collection(
        "wikipedia_lexical_20260801",
        vectors_config={},
        sparse_vectors_config={"lexical": models.SparseVectorParams()},
    )
    for tag in ("20260801", "20260901", "20261001"):
        _touch_store(tmp_path, tag)
    # The same snapshot swapped in again: nothing was replaced, so the previous alias target is
    # the collection itself. The rollback it left behind (September) must survive; August goes.
    _index_snapshot(tmp_path, client, "2026-10-01")
    names = {c.name for c in client.get_collections().collections}
    assert names == {"wikipedia_lexical_20260901", "wikipedia_lexical_20261001"}
    assert aliased_collection(client) == "wikipedia_lexical_20261001"
    stores = sorted(p.name for p in (tmp_path / "data" / "articles").iterdir())
    assert stores == [_STORE.format(tag="20260901"), _STORE.format(tag="20261001")]


def test_run_index_swap_repeated_with_no_other_collection_keeps_just_the_one(
    tmp_path: Path,
) -> None:
    client = memory_client()
    _index_snapshot(tmp_path, client, "2026-10-01")
    _index_snapshot(tmp_path, client, "2026-10-01")
    names = {c.name for c in client.get_collections().collections}
    assert names == {"wikipedia_lexical_20261001"}


def test_run_index_a_file_that_cannot_be_deleted_is_logged_and_the_build_still_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = memory_client()
    _index_snapshot(tmp_path, client, "2026-08-01")
    _index_snapshot(tmp_path, client, "2026-09-01")
    stuck = _touch_store(tmp_path, "20260801")
    real_unlink = Path.unlink

    def unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == stuck:
            raise PermissionError(13, "denied")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    fake = FakeStateStore()
    with caplog.at_level("WARNING", logger="wiki_mirror.index"):
        _run(tmp_path, client, FakeSource(_articles(), make_snapshot("2026-10-01")), fake)
    assert fake.state.phase == "serving"
    assert stuck.exists()
    assert "could not delete the superseded file" in caplog.text


# -- upsert retries ---------------------------------------------------------------------------


class _FlakyUpserts:
    """A client whose first `failures` upserts raise `error`, then succeed."""

    def __init__(self, failures: int, error: Exception) -> None:
        self.calls = 0
        self._failures = failures
        self._error = error

    def upsert(self, **_: Any) -> None:
        self.calls += 1
        if self.calls <= self._failures:
            raise self._error


def _timed_out() -> ResponseHandlingException:
    """The exception the client raises when Qdrant does not answer within its timeout."""
    return ResponseHandlingException(TimeoutError("timed out"))


def test_upsert_after_a_transport_timeout_retries_and_succeeds() -> None:
    client = _FlakyUpserts(failures=2, error=_timed_out())
    waits: list[float] = []
    index._upsert(client, "c", [], sleep=waits.append)  # type: ignore[arg-type]
    assert client.calls == 3
    assert len(waits) == 2


def test_upsert_failing_in_transport_every_try_raises_after_the_last() -> None:
    client = _FlakyUpserts(failures=99, error=_timed_out())
    with pytest.raises(ResponseHandlingException):
        index._upsert(client, "c", [], sleep=lambda _s: None)  # type: ignore[arg-type]
    assert client.calls == index._UPSERT_ATTEMPTS


def test_upsert_refused_by_qdrant_is_not_retried() -> None:
    refusal = UnexpectedResponse(400, "Bad Request", b"{}", Headers())
    client = _FlakyUpserts(failures=99, error=refusal)
    with pytest.raises(UnexpectedResponse):
        index._upsert(client, "c", [], sleep=lambda _s: None)  # type: ignore[arg-type]
    assert client.calls == 1
