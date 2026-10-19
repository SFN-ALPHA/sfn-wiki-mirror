"""Qualifies the snapshot holder: it follows the Qdrant alias to a new store without a restart.

Runs real finalized article stores under `tmp_path` against Qdrant's in-memory mode, with an
injected clock, so "the alias moved, and after the interval the new snapshot serves and the old
store is closed" is checked end to end without a server or a sleep. Failure paths named here:
nothing built yet, an alias whose store is missing or incomplete, an aliased collection whose
index state is missing, unreadable or unfinished, Qdrant not answering, an alias that
disappears, a store file whose name lies, a request still reading a snapshot while it is
swapped out (its store must stay open until the request ends), and a swap racing worker threads.

The served pair is (collection, applied snapshot) (docs/DESIGN.md section 12.3): a recheck that
finds the same alias target but a different `applied_snapshot` in the index state must swap the
new store in, which is how W3's in-place monthly update will reach the API.

See docs/DESIGN.md sections 5, 6 and 12 (docs/DECISIONS.md D2).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from qdrant_client import QdrantClient, models

from tests.fixtures.clock import FrozenClock
from tests.fixtures.wiki_mirror_fakes import (
    build_snapshot,
    index_without_store,
    make_article,
    make_config,
    make_snapshot,
    memory_client,
    write_store,
)
from wiki_mirror import index, serving
from wiki_mirror.config import MirrorConfig
from wiki_mirror.index import ALIAS, aliased_collection
from wiki_mirror.records import Article, SnapshotMeta
from wiki_mirror.serving import (
    ALIAS_RECHECK_SECONDS,
    ServedSnapshot,
    open_store_for_snapshot,
)
from wiki_mirror.store import StoreError

pytestmark = pytest.mark.contract

_OCTOBER = make_snapshot("2026-10-01")
_NOVEMBER = make_snapshot("2026-11-01")


def _articles(label: str) -> list[Article]:
    """Two articles whose text names the snapshot they belong to."""
    return [
        make_article(1, "Mercury", {"": f"Mercury is a planet, in the {label} snapshot."}),
        make_article(2, "Venus", {"": f"Venus is a planet, in the {label} snapshot."}),
    ]


#: Every holder the running test made; `_close_holders` closes them so no store connection
#: outlives its test (an unclosed SQLite connection is a ResourceWarning, which the suite
#: turns into an error).
_OPEN_HOLDERS: list[ServedSnapshot] = []


@pytest.fixture(autouse=True)
def _close_holders() -> Iterator[None]:
    """Close the holders a test made, whether it passed or not."""
    yield
    while _OPEN_HOLDERS:
        _OPEN_HOLDERS.pop().close()


def _holder(
    tmp_path: Path, clock: FrozenClock
) -> tuple[ServedSnapshot, QdrantClient, MirrorConfig]:
    """A holder over an empty Qdrant and an empty data directory."""
    config = make_config(tmp_path)
    client = memory_client()
    served = ServedSnapshot(config, client, clock=clock.monotonic)
    _OPEN_HOLDERS.append(served)
    return served, client, config


def _date_served(served: ServedSnapshot) -> str | None:
    """The snapshot date being served right now, or None; takes (and ends) a lease."""
    with served.lease() as current:
        return None if current is None else current.store.snapshot.date


def _point_alias_at(client: QdrantClient, collection: str) -> None:
    """Move the alias in one atomic call, as the end of a build does."""
    client.update_collection_aliases(
        change_aliases_operations=[
            models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=ALIAS)),
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(collection_name=collection, alias_name=ALIAS)
            ),
        ]
    )


def _is_closed(store: object) -> bool:
    """Whether the store's connection has been closed (using it raises)."""
    try:
        store.count()  # type: ignore[attr-defined]
    except sqlite3.ProgrammingError:
        return True
    return False


# -- starting up ------------------------------------------------------------------------------


def test_lease_with_no_alias_yields_nothing_and_is_not_a_problem(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, _, _ = _holder(tmp_path, frozen_clock)
    assert _date_served(served) is None
    assert (served.alias, served.problem) == (None, None)


def test_lease_opens_the_store_the_alias_points_at(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    with served.lease() as current:
        assert current is not None
        reply = current.search.search("mercury planet", 5)
    assert reply.snapshot == "2026-10-01"
    assert (served.alias, served.problem) == ("wikipedia_lexical_20261001", None)


def test_first_build_is_served_after_the_interval_by_the_same_holder(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    assert _date_served(served) is None
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    frozen_clock.advance(ALIAS_RECHECK_SECONDS - 1)
    assert _date_served(served) is None
    frozen_clock.advance(2)
    assert _date_served(served) == "2026-10-01"


def test_refresh_opens_the_store_now_whatever_the_interval(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    assert _date_served(served) is None
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    served.refresh()
    assert _date_served(served) == "2026-10-01"


# -- following the alias ----------------------------------------------------------------------


def test_alias_moving_to_a_new_snapshot_is_served_after_the_interval_and_the_old_store_closed(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    with served.lease() as current:
        assert current is not None
        october_store = current.store
    build_snapshot(config, client, _articles("november"), _NOVEMBER)
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    with served.lease() as current:
        assert current is not None
        reply = current.search.search("mercury planet", 5)
    assert reply.snapshot == "2026-11-01"
    assert "november" in reply.results[0].snippet
    assert _is_closed(october_store)


def test_requests_inside_the_interval_do_not_ask_qdrant_again(
    tmp_path: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    asked: list[int] = []

    def spy(qdrant: QdrantClient) -> str | None:
        asked.append(1)
        return aliased_collection(qdrant)

    monkeypatch.setattr(serving, "aliased_collection", spy)
    for _ in range(5):
        _date_served(served)
        frozen_clock.advance(ALIAS_RECHECK_SECONDS / 10)
    assert len(asked) == 1


def test_fresh_lease_looks_at_the_alias_whatever_the_interval(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    assert _date_served(served) == "2026-10-01"
    build_snapshot(config, client, _articles("november"), _NOVEMBER)
    with served.lease(fresh=True) as current:
        assert current is not None
        assert current.store.snapshot.date == "2026-11-01"


def test_lease_taken_before_a_swap_keeps_its_store_open_until_it_ends(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    with served.lease() as in_flight:
        assert in_flight is not None
        build_snapshot(config, client, _articles("november"), _NOVEMBER)
        frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
        assert _date_served(served) == "2026-11-01"
        assert in_flight.store.count() == 2  # the request that began on October finishes on it
        assert not _is_closed(in_flight.store)
    assert _is_closed(in_flight.store)


def test_alias_flipping_back_is_followed_too(tmp_path: Path, frozen_clock: FrozenClock) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    build_snapshot(config, client, _articles("november"), _NOVEMBER)
    assert _date_served(served) == "2026-11-01"
    _point_alias_at(client, "wikipedia_lexical_20261001")
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    assert _date_served(served) == "2026-10-01"


def test_store_is_matched_by_the_snapshot_it_records_not_by_its_file_name(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    path = write_store(config, _articles("november"), _NOVEMBER)
    path.rename(path.with_name("enwiki_namespace_0_renamed.db"))
    index_without_store(config, client, _articles("november"), _NOVEMBER)
    assert _date_served(served) == "2026-11-01"


# -- when the new snapshot cannot be served ---------------------------------------------------


def _index_without_a_complete_store(
    config: MirrorConfig, client: QdrantClient, snapshot: SnapshotMeta, *, store: bool
) -> None:
    """Index a snapshot into Qdrant while its article store is missing or unfinished."""
    if store:
        write_store(config, _articles("unfinished"), snapshot, finalize=False)
    index_without_store(config, client, _articles("unfinished"), snapshot)


def test_new_store_incomplete_keeps_the_old_snapshot_and_reports_the_problem(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    assert _date_served(served) == "2026-10-01"
    _index_without_a_complete_store(config, client, _NOVEMBER, store=True)
    with caplog.at_level(logging.WARNING, logger="wiki_mirror.serving"):
        for _ in range(3):
            frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
            assert _date_served(served) == "2026-10-01"
    assert served.problem is not None
    assert "wikipedia_lexical_20261101" in served.problem
    warnings = [r for r in caplog.records if r.name == "wiki_mirror.serving"]
    assert len(warnings) == 1


def test_new_store_missing_keeps_the_old_snapshot_and_reports_the_problem(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    assert _date_served(served) == "2026-10-01"
    _index_without_a_complete_store(config, client, _NOVEMBER, store=False)
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    assert _date_served(served) == "2026-10-01"
    assert served.problem is not None


def test_problem_clears_when_the_new_store_appears(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    _date_served(served)
    _index_without_a_complete_store(config, client, _NOVEMBER, store=False)
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    _date_served(served)
    assert served.problem is not None
    write_store(config, _articles("november"), _NOVEMBER)
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    assert _date_served(served) == "2026-11-01"
    assert served.problem is None


def test_alias_whose_store_is_missing_at_start_serves_nothing_and_says_why(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    _index_without_a_complete_store(config, client, _OCTOBER, store=False)
    assert _date_served(served) is None
    assert served.problem is not None
    assert served.alias == "wikipedia_lexical_20261001"


def test_qdrant_not_answering_keeps_the_snapshot_and_recovers_when_it_answers(
    tmp_path: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    assert _date_served(served) == "2026-10-01"

    def down(_: QdrantClient) -> str:
        raise ConnectionError

    with monkeypatch.context() as broken:
        broken.setattr(serving, "aliased_collection", down)
        frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
        assert _date_served(served) == "2026-10-01"
        assert served.problem is not None
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    assert _date_served(served) == "2026-10-01"
    assert served.problem is None


def test_alias_disappearing_keeps_the_snapshot_and_reports_the_problem(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    assert _date_served(served) == "2026-10-01"
    client.update_collection_aliases(
        change_aliases_operations=[
            models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=ALIAS))
        ]
    )
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    assert _date_served(served) == "2026-10-01"
    assert served.problem is not None


# -- closing ----------------------------------------------------------------------------------


def test_close_closes_the_open_store_and_later_leases_yield_nothing(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    with served.lease() as current:
        assert current is not None
        store = current.store
    served.close()
    assert _is_closed(store)
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    assert _date_served(served) is None


def test_close_with_a_request_in_flight_closes_the_store_when_it_ends(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    with served.lease() as current:
        assert current is not None
        served.close()
        assert not _is_closed(current.store)
    assert _is_closed(current.store)


# -- the store lookup -------------------------------------------------------------------------


def test_open_store_for_snapshot_no_store_names_how_many_files_were_passed_over(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    write_store(config, _articles("october"), _OCTOBER, finalize=False)
    with pytest.raises(StoreError, match=r"snapshot 20261101.*1 files, 1 unusable"):
        open_store_for_snapshot(config, "20261101")


def test_open_store_for_snapshot_skips_an_unfinished_store_to_find_the_right_one(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    write_store(config, _articles("october"), _OCTOBER)
    write_store(config, _articles("november"), _NOVEMBER, finalize=False)
    store = open_store_for_snapshot(config, "20261001")
    try:
        assert store.snapshot.date == "2026-10-01"
    finally:
        store.close()


# -- the window between the alias moving and the next look ------------------------------------


def test_snapshot_still_served_after_the_alias_moves_answers_wholly_from_its_own_index(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    assert _date_served(served) == "2026-10-01"
    # November's index holds nothing the October store has: a search that followed the alias
    # instead of the store would find only pages the open store lacks, and answer with nothing.
    mars = [make_article(3, "Mars", {"": "Mars is a planet, in the november snapshot."})]
    build_snapshot(config, client, mars, _NOVEMBER)
    assert aliased_collection(client) == "wikipedia_lexical_20261101"
    with served.lease() as current:  # inside the interval: still the October snapshot
        assert current is not None
        reply = current.search.search("mercury planet", 5)
    assert reply.snapshot == "2026-10-01"
    assert [result.title for result in reply.results] == ["Mercury", "Venus"]
    assert "october" in reply.results[0].snippet


# -- an index cut by another chunker ---------------------------------------------------------


def _record_chunker(config: MirrorConfig, tag: str, value: str | None) -> None:
    """Rewrite the chunker fingerprint in an index state, named by born tag (None removes it)."""
    path = _state_file(config, tag)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if value is None:
        del raw["chunker"]
    else:
        raw["chunker"] = value
    path.write_text(json.dumps(raw), encoding="utf-8")


def test_index_cut_by_the_running_chunker_is_not_a_problem(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    assert _date_served(served) == "2026-10-01"
    assert served.problem is None


def test_index_cut_by_another_chunker_is_served_but_reported_and_logged(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    _record_chunker(config, "20261001", "v0:1/1")
    with caplog.at_level(logging.WARNING, logger="wiki_mirror.serving"):
        assert _date_served(served) == "2026-10-01"  # degraded, not down
    assert served.problem is not None
    assert "chunker" in served.problem
    assert "v0:1/1" in caplog.text
    assert "rebuild the index" in caplog.text


def test_chunker_problem_outlasts_looks_at_the_alias_that_change_nothing(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    _record_chunker(config, "20261001", "v0:1/1")
    assert _date_served(served) == "2026-10-01"
    for _ in range(3):
        frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
        assert _date_served(served) == "2026-10-01"
        assert served.problem is not None


def test_chunker_problem_is_logged_once_not_on_every_look(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    _record_chunker(config, "20261001", "v0:1/1")
    with caplog.at_level(logging.WARNING, logger="wiki_mirror.serving"):
        for _ in range(3):
            assert _date_served(served) == "2026-10-01"
            frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    assert caplog.text.count("snippets and passage numbers may not line up") == 1


def test_chunker_problem_clears_when_a_correctly_cut_snapshot_is_swapped_in(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    _record_chunker(config, "20261001", "v0:1/1")
    assert _date_served(served) == "2026-10-01"
    assert served.problem is not None
    build_snapshot(config, client, _articles("november"), _NOVEMBER)
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    assert _date_served(served) == "2026-11-01"
    assert served.problem is None


def test_running_code_that_changed_its_chunker_since_the_build_is_reported(
    tmp_path: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    monkeypatch.setattr(index, "CHUNKER_VERSION", index.CHUNKER_VERSION + 1)
    assert _date_served(served) == "2026-10-01"
    assert served.problem is not None


def test_index_with_no_chunker_record_has_no_verdict_and_is_not_degraded(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    _record_chunker(config, "20261001", None)
    with caplog.at_level(logging.INFO, logger="wiki_mirror.serving"):
        assert _date_served(served) == "2026-10-01"
    assert served.problem is None
    assert "no chunker fingerprint is recorded" in caplog.text


# -- the pair is (collection, applied snapshot) ---------------------------------------------------


def _state_file(config: MirrorConfig, born: str) -> Path:
    """The index state of the collection born on tag `born` (the on-disk name)."""
    return config.data_dir / "state" / f"index_lexical_{born}.json"


def _apply_snapshot(config: MirrorConfig, born: str, applied: str) -> None:
    """Move `applied_snapshot` in a collection's index state, as W3's in-place update will."""
    path = _state_file(config, born)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["applied_snapshot"] = applied
    path.write_text(json.dumps(raw), encoding="utf-8")


def test_applied_snapshot_moving_under_an_unchanged_alias_swaps_in_the_new_store(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    with served.lease() as current:
        assert current is not None
        october_store = current.store
    write_store(config, _articles("november"), _NOVEMBER)
    _apply_snapshot(config, "20261001", "20261101")
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    with served.lease() as current:
        assert current is not None
        pair = (current.collection, current.applied_snapshot, current.store.snapshot.date)
    assert pair == ("wikipedia_lexical_20261001", "20261101", "2026-11-01")
    assert aliased_collection(client) == "wikipedia_lexical_20261001"  # the alias never moved
    assert _is_closed(october_store)


def test_unchanged_alias_and_applied_snapshot_keep_the_open_store(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    with served.lease() as current:
        assert current is not None
        first = current.store
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    with served.lease() as current:
        assert current is not None
        assert current.store is first


def test_applied_snapshot_moving_to_a_store_that_is_not_there_keeps_the_old_one_and_says_so(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    assert _date_served(served) == "2026-10-01"
    _apply_snapshot(config, "20261001", "20261101")
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    assert _date_served(served) == "2026-10-01"
    assert served.problem is not None
    assert "snapshot 20261101" in served.problem


def test_applied_snapshot_moving_back_is_followed_too(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    write_store(config, _articles("november"), _NOVEMBER)
    _apply_snapshot(config, "20261001", "20261101")
    assert _date_served(served) == "2026-11-01"
    _apply_snapshot(config, "20261001", "20261001")
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    assert _date_served(served) == "2026-10-01"


# -- an aliased collection without a usable index state -------------------------------------------


def _remove_state(config: MirrorConfig, born: str) -> None:
    """Delete a collection's index state file."""
    _state_file(config, born).unlink()


def test_alias_whose_index_state_is_missing_at_start_serves_nothing_and_says_what_to_do(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    _remove_state(config, "20261001")
    assert _date_served(served) is None
    assert served.alias == "wikipedia_lexical_20261001"
    assert served.problem is not None
    assert "wikipedia_lexical_20261001 has no usable index state" in served.problem
    assert "wiki-mirror build-now" in served.problem


def test_index_state_that_goes_missing_keeps_the_old_snapshot_and_reports_the_problem(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    assert _date_served(served) == "2026-10-01"
    _remove_state(config, "20261001")
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    assert _date_served(served) == "2026-10-01"
    assert served.problem is not None
    assert "index state" in served.problem


@pytest.mark.parametrize(
    "contents",
    ["{not json", "[]", '{"applied_snapshot": ""}', '{"applied_snapshot": "2026-10-01"}'],
    ids=["garbage", "not-an-object", "unfinished", "not-a-tag"],
)
def test_index_state_that_is_unreadable_or_unfinished_is_a_problem_not_a_guess(
    tmp_path: Path, frozen_clock: FrozenClock, contents: str
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    _state_file(config, "20261001").write_text(contents, encoding="utf-8")
    # The October store is right there, and the newest: still not served on a guess.
    assert _date_served(served) is None
    assert served.problem is not None


def test_index_state_problem_is_logged_once_not_on_every_look(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    _remove_state(config, "20261001")
    with caplog.at_level(logging.WARNING, logger="wiki_mirror.serving"):
        for _ in range(3):
            _date_served(served)
            frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    assert caplog.text.count("has no usable index state") == 1


def test_index_state_problem_clears_when_the_state_is_restored(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    saved = _state_file(config, "20261001").read_bytes()
    _remove_state(config, "20261001")
    assert _date_served(served) is None
    _state_file(config, "20261001").write_bytes(saved)
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    assert _date_served(served) == "2026-10-01"
    assert served.problem is None


def test_alias_pointing_at_a_collection_this_service_did_not_build_is_a_problem(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    client.create_collection(
        "scratch",
        vectors_config={},
        sparse_vectors_config={"lexical": models.SparseVectorParams()},
    )
    _point_alias_at(client, "scratch")
    assert _date_served(served) is None
    assert served.problem is not None
    assert "scratch has no usable index state" in served.problem


# -- threads ----------------------------------------------------------------------------------


def test_swaps_racing_reader_threads_never_close_a_store_under_a_reader(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = _holder(tmp_path, frozen_clock)
    build_snapshot(config, client, _articles("october"), _OCTOBER)
    build_snapshot(config, client, _articles("november"), _NOVEMBER)
    assert _date_served(served) == "2026-11-01"

    def read_many(_: int) -> int:
        seen = 0
        for _ in range(300):
            with served.lease() as current:
                assert current is not None
                assert current.store.count() == 2
                seen += 1
        return seen

    with ThreadPoolExecutor(max_workers=4) as pool:
        readers = [pool.submit(read_many, n) for n in range(4)]
        for target in ("wikipedia_lexical_20261001", "wikipedia_lexical_20261101") * 3:
            _point_alias_at(client, target)
            with served.lease(fresh=True):
                pass
        assert [reader.result() for reader in readers] == [300] * 4
