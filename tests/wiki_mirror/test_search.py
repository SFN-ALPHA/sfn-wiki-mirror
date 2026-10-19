"""Qualifies search and windowed reads: grouping, exact-title boost, ordering, snippets, windows.

`WikiSearch` runs against a real (in-memory) Qdrant index built by `run_index` from synthetic
articles, so ranking is the engine's own: the tests assert ordering rules and shapes, not exact
scores. Reads need no index and are plain unit tests over a fake article source. Failure paths
named here: an index hit the store lacks is dropped, and so is one whose `text_hash` says it was
cut from different text than the store holds (logged by page id only).

See docs/DESIGN.md sections 5, 6 and 12.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tests.fixtures.wiki_mirror_fakes import (
    FakeSource,
    index_articles,
    make_article,
    make_config,
    make_snapshot,
    memory_client,
)
from wiki_mirror.index import collection_name
from wiki_mirror.records import Article, text_hash
from wiki_mirror.search import WikiSearch, reply_as_dict

_FILLER = "The committee reviewed the proposal and recorded its findings in the minutes. "

_indexed = index_articles


def _corpus() -> list[Article]:
    """A few articles with distinctive vocabulary, a redirect and a disambiguation page."""
    return [
        make_article(
            1,
            "Photosynthesis",
            {
                "": f"Photosynthesis converts light into chemical energy. {_FILLER}",
                "Light reactions": f"Chlorophyll absorbs light in the thylakoid. {_FILLER}",
                "Calvin cycle": f"Chlorophyll is not used here but ATP is. {_FILLER}",
            },
        ),
        make_article(
            2,
            "Albert Einstein",
            {"": f"Physicist who developed relativity. {_FILLER}"},
            redirects=("Einstein",),
        ),
        make_article(3, "Zeppelin", {"": f"A zeppelin is a rigid airship. {_FILLER}"}),
        make_article(
            4,
            "Mercury (disambiguation)",
            {"": f"Mercury mercury mercury mercury may refer to the planet. {_FILLER}"},
            is_disambiguation=True,
        ),
        make_article(5, "Mercury (planet)", {"": f"Mercury is the closest planet. {_FILLER}"}),
    ]


@pytest.mark.contract
def test_search_reply_names_the_snapshot_and_the_lexical_mode(tmp_path: Path) -> None:
    search, _, _ = _indexed(tmp_path, _corpus())
    reply = search.search("zeppelin airship", 5)
    assert (reply.snapshot, reply.mode) == ("2026-10-01", "lexical")


@pytest.mark.contract
def test_search_result_carries_the_citation_fields_the_contract_names(tmp_path: Path) -> None:
    search, _, _ = _indexed(tmp_path, _corpus())
    result = search.search("zeppelin airship", 5).results[0]
    assert reply_as_dict(search.search("zeppelin airship", 5))["results"][0] == {
        "page_id": 3,
        "revision_id": 31,
        "title": "Zeppelin",
        "section": "",
        "snippet": result.snippet,
        "score": result.score,
        "url": "https://en.wikipedia.org/wiki/Zeppelin?oldid=31",
        "disambiguation": False,
    }


@pytest.mark.contract
def test_search_two_matching_passages_of_one_article_give_one_result(tmp_path: Path) -> None:
    search, _, _ = _indexed(tmp_path, _corpus())
    results = search.search("chlorophyll light", 8).results
    assert [r.page_id for r in results].count(1) == 1


@pytest.mark.contract
def test_search_result_section_is_the_best_passages_section(tmp_path: Path) -> None:
    search, _, _ = _indexed(tmp_path, _corpus())
    assert search.search("thylakoid", 5).results[0].section == "Light reactions"


@pytest.mark.contract
def test_search_exact_title_comes_first_even_with_a_weaker_score(tmp_path: Path) -> None:
    corpus = [
        *_corpus(),
        make_article(6, "Airships", {"": f"zeppelin zeppelin zeppelin zeppelin. {_FILLER}"}),
    ]
    search, _, _ = _indexed(tmp_path, corpus)
    assert search.search("Zeppelin", 5).results[0].title == "Zeppelin"


@pytest.mark.contract
def test_search_redirect_title_puts_the_target_first_with_its_lead_snippet(tmp_path: Path) -> None:
    search, _, _ = _indexed(tmp_path, _corpus())
    first = search.search("Einstein", 5).results[0]
    assert first.title == "Albert Einstein"
    assert first.snippet.startswith("Physicist who developed relativity.")


@pytest.mark.contract
def test_search_disambiguation_page_sorts_after_articles_despite_a_higher_score(
    tmp_path: Path,
) -> None:
    search, _, _ = _indexed(tmp_path, _corpus())
    results = search.search("mercury planet element", 5).results
    flags = [r.disambiguation for r in results]
    assert flags == sorted(flags)
    assert results[-1].title == "Mercury (disambiguation)"
    assert results[-1].score > results[0].score


@pytest.mark.contract
def test_search_limit_truncates_the_results(tmp_path: Path) -> None:
    search, _, _ = _indexed(tmp_path, _corpus())
    assert len(search.search("the committee proposal minutes", 2).results) == 2


@pytest.mark.contract
@pytest.mark.parametrize("query", ["", "   ", "the of and", "???"])
def test_search_query_with_no_searchable_term_returns_no_results(
    tmp_path: Path, query: str
) -> None:
    search, _, _ = _indexed(tmp_path, _corpus())
    assert search.search(query, 5).results == ()


@pytest.mark.contract
def test_search_query_matching_nothing_returns_no_results(tmp_path: Path) -> None:
    search, _, _ = _indexed(tmp_path, _corpus())
    assert search.search("xylophonist", 5).results == ()


@pytest.mark.contract
def test_search_index_hit_missing_from_the_store_is_dropped(tmp_path: Path) -> None:
    _, client, source = _indexed(tmp_path, _corpus())
    reduced = FakeSource([a for a in _corpus() if a.page_id != 3], source.snapshot)
    search = WikiSearch(
        make_config(tmp_path), reduced, client, collection=collection_name(source.snapshot)
    )
    assert search.search("zeppelin", 5).results == ()


def _revised_corpus() -> list[Article]:
    """The corpus with Zeppelin's text changed since the index was built: a different hash."""
    lead = f"A zeppelin is a rigid airship, revised. {_FILLER}"
    revised = make_article(3, "Zeppelin", {"": lead})
    return [revised if article.page_id == 3 else article for article in _corpus()]


@pytest.mark.contract
def test_search_hit_cut_from_different_text_than_the_store_holds_is_dropped(
    tmp_path: Path,
) -> None:
    _, client, source = _indexed(tmp_path, _corpus())
    stale = FakeSource(_revised_corpus(), source.snapshot)
    search = WikiSearch(
        make_config(tmp_path), stale, client, collection=collection_name(source.snapshot)
    )
    page_ids = [r.page_id for r in search.search("committee proposal minutes", 8).results]
    assert 3 not in page_ids
    assert {1, 2, 5} <= set(page_ids)


@pytest.mark.contract
def test_search_dropped_hit_is_logged_by_page_id_and_never_by_title_or_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _, client, source = _indexed(tmp_path, _corpus())
    stale = FakeSource(_revised_corpus(), source.snapshot)
    search = WikiSearch(
        make_config(tmp_path), stale, client, collection=collection_name(source.snapshot)
    )
    with caplog.at_level(logging.WARNING, logger="wiki_mirror.search"):
        search.search("zeppelin airship", 5)
    assert "page 3 has a passage cut from different text" in caplog.text
    assert "Zeppelin" not in caplog.text
    assert "rigid airship" not in caplog.text


@pytest.mark.contract
def test_search_queries_the_collection_it_was_given_not_one_derived_from_its_store(
    tmp_path: Path,
) -> None:
    _, client, source = _indexed(tmp_path, _corpus())
    # A store of another snapshot over the first snapshot's index: if the collection came from
    # `store.snapshot` the query would hit a collection that does not exist and raise.
    november = FakeSource(_corpus(), make_snapshot("2026-11-01"))
    search = WikiSearch(
        make_config(tmp_path), november, client, collection=collection_name(source.snapshot)
    )
    reply = search.search("zeppelin airship", 5)
    assert (reply.snapshot, reply.results[0].page_id) == ("2026-11-01", 3)


@pytest.mark.contract
def test_search_long_passage_snippet_is_cut_at_a_word_boundary_with_an_ellipsis(
    tmp_path: Path,
) -> None:
    words = " ".join(f"word{n}" for n in range(200))
    search, _, _ = _indexed(tmp_path, [make_article(7, "Long", {"": words})])
    snippet = search.search("word5", 5).results[0].snippet
    assert snippet.endswith("…")
    assert len(snippet) <= 321
    assert snippet[:-1].split(" ")[-1] in words.split(" ")


@pytest.mark.contract
def test_search_snippet_collapses_whitespace(tmp_path: Path) -> None:
    text = "Alpha\n\n\n   beta\tgamma  delta. " + _FILLER
    search, _, _ = _indexed(tmp_path, [make_article(7, "Spaced", {"": text})])
    assert search.search("gamma", 5).results[0].snippet.startswith("Alpha beta gamma delta.")


def _reader(text: str, tmp_path: Path, **config: object) -> WikiSearch:
    """A search over one article with exactly `text`; reads never touch the index."""
    article = Article(
        page_id=9,
        title="Window",
        revision_id=91,
        revision_time="2026-09-30T12:00:00Z",
        text=text,
        text_hash=text_hash(text),
    )
    settings = {"max_read_chars": 1000, **config}
    return WikiSearch(
        make_config(tmp_path, **settings),
        FakeSource([article]),
        memory_client(),
        collection="wikipedia_lexical_20261001",
    )


@pytest.mark.unit
def test_read_whole_short_article_returns_everything_and_no_next_offset(tmp_path: Path) -> None:
    reply = _reader("Short text.", tmp_path).read(page_id=9, title=None, offset=0)
    assert reply is not None
    assert (reply.text, reply.next_offset, reply.total_chars) == ("Short text.", None, 11)


@pytest.mark.unit
def test_read_reply_carries_the_citation_fields_the_contract_names(tmp_path: Path) -> None:
    reply = _reader("Short text.", tmp_path).read(page_id=9, title=None, offset=0)
    assert reply is not None
    assert reply_as_dict(reply) == {
        "page_id": 9,
        "revision_id": 91,
        "title": "Window",
        "snapshot": "2026-10-01",
        "revision_time": "2026-09-30T12:00:00Z",
        "url": "https://en.wikipedia.org/wiki/Window?oldid=91",
        "license": "CC BY-SA 4.0",
        "offset": 0,
        "next_offset": None,
        "total_chars": 11,
        "text": "Short text.",
    }


@pytest.mark.unit
def test_read_window_ends_on_a_paragraph_break_in_the_last_15_percent(tmp_path: Path) -> None:
    text = "a" * 900 + "\n\n" + "b" * 2000
    reply = _reader(text, tmp_path).read(page_id=9, title=None, offset=0)
    assert reply is not None
    assert (len(reply.text), reply.next_offset) == (902, 902)
    assert reply.text.endswith("\n\n")


@pytest.mark.unit
def test_read_window_falls_back_to_a_line_break_when_there_is_no_paragraph_break(
    tmp_path: Path,
) -> None:
    text = "a" * 900 + "\n" + "b" * 2000
    reply = _reader(text, tmp_path).read(page_id=9, title=None, offset=0)
    assert reply is not None
    assert reply.next_offset == 901


@pytest.mark.unit
def test_read_window_ignores_a_break_before_the_last_15_percent(tmp_path: Path) -> None:
    text = "a" * 400 + "\n\n" + "b" * 2000
    reply = _reader(text, tmp_path).read(page_id=9, title=None, offset=0)
    assert reply is not None
    assert reply.next_offset == 1000


@pytest.mark.unit
def test_read_windows_chain_to_the_whole_text(tmp_path: Path) -> None:
    text = ("word " * 120 + "\n\n") * 12
    search = _reader(text, tmp_path)
    collected, offset = "", 0
    while offset is not None:
        reply = search.read(page_id=9, title=None, offset=offset)
        assert reply is not None
        collected += reply.text
        offset = reply.next_offset
    assert collected == text


@pytest.mark.unit
def test_read_window_size_follows_max_read_chars(tmp_path: Path) -> None:
    reply = _reader("a" * 5000, tmp_path, max_read_chars=2000).read(
        page_id=9, title=None, offset=0
    )
    assert reply is not None
    assert (len(reply.text), reply.next_offset) == (2000, 2000)


@pytest.mark.unit
def test_read_offset_past_the_end_returns_an_empty_window_without_a_next_offset(
    tmp_path: Path,
) -> None:
    reply = _reader("Short text.", tmp_path).read(page_id=9, title=None, offset=500)
    assert reply is not None
    assert (reply.text, reply.next_offset, reply.offset) == ("", None, 500)


@pytest.mark.unit
def test_read_by_title_resolves_the_same_article(tmp_path: Path) -> None:
    reply = _reader("Short text.", tmp_path).read(page_id=None, title="window", offset=0)
    assert reply is not None
    assert reply.page_id == 9


@pytest.mark.unit
def test_read_unknown_article_returns_none(tmp_path: Path) -> None:
    search = _reader("Short text.", tmp_path)
    assert search.read(page_id=404, title=None, offset=0) is None
    assert search.read(page_id=None, title="Nothing here", offset=0) is None
