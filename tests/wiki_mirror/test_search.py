"""Qualifies search and windowed reads: grouping, exact-title boost, ordering, snippets, windows.

`WikiSearch` runs against a real (in-memory) Qdrant index built by `run_index` from synthetic
articles, so ranking is the engine's own: the tests assert ordering rules and shapes, not exact
scores. Reads need no index and are plain unit tests over a fake article source. Failure paths
named here: an index hit the store lacks is dropped, and so is one whose `text_hash` says it was
cut from different text than the store holds (logged by page id only).

The semantic and hybrid modes run over the same corpus with a semantic collection built by the
real `run_embed` and a fake model whose vectors count topic words (`tests/fixtures`), so which
passage a question lands nearest is plain to see. Their failure paths: a search with no semantic
index paired, and a question that could not be embedded (a refused key, a timeout), both
`SemanticUnavailableError` and never keyword results under another mode's name. Reciprocal rank
fusion is checked on hand-made rankings: the arithmetic, the tie order, and that one point id
with two `text_hash` values is never merged.

See docs/DESIGN.md sections 5, 6 and 12 and docs/DECISIONS.md D10.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
from qdrant_client import models

from tests.fixtures.wiki_mirror_fakes import (
    EMBEDDING_URL,
    TOPIC_DIMS,
    FakeEmbeddingServer,
    FakeSource,
    FakeStateStore,
    add_semantic_index,
    index_articles,
    make_article,
    make_config,
    make_snapshot,
    memory_client,
    topic_vector,
)
from wiki_mirror.index import collection_name, run_index
from wiki_mirror.query_embed import QueryEmbedder
from wiki_mirror.records import Article, text_hash
from wiki_mirror.search import (
    SearchMode,
    SemanticBinding,
    SemanticUnavailableError,
    WikiSearch,
    _fuse,
    _Hit,
    reply_as_dict,
)

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


# -- semantic and hybrid ----------------------------------------------------------------------

_MODES: list[SearchMode] = ["lexical", "semantic", "hybrid"]

#: A question that shares no word with the article it means: the fake model maps it to "airship".
_FLYING_MACHINE = "flying machine"


def _paired(
    tmp_path: Path,
    articles: list[Article],
    answers: dict[str, list[float]] | None = None,
    *,
    store: list[Article] | None = None,
) -> tuple[WikiSearch, FakeEmbeddingServer]:
    """A search paired with a semantic index, and the fake embedding server it asks.

    Args:
        tmp_path: Where the index states are written.
        articles: What both indexes are built from.
        answers: Questions the fake model maps to a chosen vector instead of its topic vector.
        store: What the search's article store holds, when it should differ from the indexes.
    """
    settings = make_config(tmp_path, embedding_url=EMBEDDING_URL, embedding_dimensions=TOPIC_DIMS)
    client = memory_client()
    state = FakeStateStore()
    indexed = FakeSource(articles)
    run_index(
        settings, indexed, client, workers=1, state_reader=state.read, state_writer=state.write
    )
    collection = add_semantic_index(settings, client, articles)
    server = FakeEmbeddingServer(answers)
    embedder = QueryEmbedder.from_config(
        settings, {}, model="bge-m3", dimensions=TOPIC_DIMS, transport=server.transport
    )
    search = WikiSearch(
        settings,
        FakeSource(store if store is not None else articles),
        client,
        collection=collection_name(indexed.snapshot),
    )
    search.pair_semantic(SemanticBinding(collection, embedder.embed))
    return search, server


def _corpus_with_airships() -> list[Article]:
    """The corpus plus an article that repeats the title word, to outscore the exact title."""
    return [
        *_corpus(),
        make_article(6, "Airships", {"": f"zeppelin zeppelin zeppelin zeppelin. {_FILLER}"}),
    ]


def _hit(page_id: int, text_hash_: int = 7) -> _Hit:
    """A passage hit for the fusion tests; only its page id and `text_hash` matter."""
    return _Hit(
        page_id=page_id,
        chunk=0,
        section="",
        score=0.0,
        disambiguation=False,
        text_hash=text_hash_,
    )


@pytest.mark.contract
@pytest.mark.parametrize("mode", _MODES)
def test_search_reply_names_the_mode_that_answered(tmp_path: Path, mode: SearchMode) -> None:
    search, _ = _paired(tmp_path, _corpus())
    reply = search.search("airship", 5, mode)
    assert (reply.snapshot, reply.mode) == ("2026-10-01", mode)


@pytest.mark.contract
def test_search_default_mode_is_lexical_and_never_asks_the_embedding_server(
    tmp_path: Path,
) -> None:
    search, server = _paired(tmp_path, _corpus())
    assert search.search("zeppelin airship", 5) == search.search("zeppelin airship", 5, "lexical")
    assert server.requests == []


@pytest.mark.contract
def test_search_semantic_result_has_the_same_shape_and_fields_as_lexical_for_the_same_article(
    tmp_path: Path,
) -> None:
    search, _ = _paired(tmp_path, _corpus())
    lexical = reply_as_dict(search.search("airship", 5, "lexical"))["results"][0]
    semantic = reply_as_dict(search.search("airship", 5, "semantic"))["results"][0]
    assert semantic.keys() == lexical.keys()
    assert {k: v for k, v in semantic.items() if k != "score"} == {
        k: v for k, v in lexical.items() if k != "score"
    }
    assert semantic["title"] == "Zeppelin"


@pytest.mark.contract
def test_search_semantic_finds_the_article_a_question_without_its_words_means(
    tmp_path: Path,
) -> None:
    search, _ = _paired(tmp_path, _corpus(), {_FLYING_MACHINE: topic_vector("airship")})
    assert search.search(_FLYING_MACHINE, 5, "lexical").results == ()
    assert search.search(_FLYING_MACHINE, 5, "semantic").results[0].title == "Zeppelin"


@pytest.mark.contract
def test_search_semantic_score_is_the_cosine_similarity_of_the_best_passage(
    tmp_path: Path,
) -> None:
    search, _ = _paired(tmp_path, _corpus(), {_FLYING_MACHINE: topic_vector("airship")})
    top = search.search(_FLYING_MACHINE, 5, "semantic").results[0]
    # The passage embeds as "Zeppelin\n<text>": one "airship", the question the same words, so
    # the vectors point the same way (float16 storage costs a few decimals).
    assert top.score == pytest.approx(1.0, abs=1e-3)


@pytest.mark.contract
def test_search_semantic_two_matching_passages_of_one_article_give_one_result(
    tmp_path: Path,
) -> None:
    search, _ = _paired(tmp_path, _corpus())
    results = search.search("chlorophyll", 8, "semantic").results
    assert [r.page_id for r in results].count(1) == 1
    assert results[0].title == "Photosynthesis"


@pytest.mark.contract
def test_search_semantic_embeds_the_question_as_plain_text(tmp_path: Path) -> None:
    search, server = _paired(tmp_path, _corpus())
    search.search("  airship  ", 5, "semantic")
    (request,) = server.requests
    assert request.url.path == "/api/embed"
    assert b'"input":["airship"]' in request.content.replace(b" ", b"")


@pytest.mark.contract
def test_search_dense_query_sends_quantization_params_and_the_keyword_query_sends_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    search, _ = _paired(tmp_path, _corpus())
    client = search._client
    calls: list[dict[str, object]] = []
    real = client.query_points

    def spy(*args: object, **kwargs: object) -> object:
        calls.append(kwargs)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(client, "query_points", spy)
    search.search("airship", 5, "hybrid")
    (sparse,) = [c for c in calls if c["using"] is not None]
    (dense,) = [c for c in calls if c["using"] is None]
    assert sparse["search_params"] is None
    params = dense["search_params"]
    assert isinstance(params, models.SearchParams)
    assert params.quantization == models.QuantizationSearchParams(rescore=True, oversampling=1.0)


@pytest.mark.contract
@pytest.mark.parametrize("mode", _MODES)
def test_search_exact_title_comes_first_in_every_mode(tmp_path: Path, mode: SearchMode) -> None:
    # The fake model sends "Zeppelin" towards the planets, so by score Mercury beats the article
    # whose title is the whole query; by the rule, the article still comes first.
    search, _ = _paired(
        tmp_path, _corpus_with_airships(), {"Zeppelin": topic_vector("planet")}
    )
    assert search.search("Zeppelin", 5, mode).results[0].title == "Zeppelin"


@pytest.mark.contract
@pytest.mark.parametrize("mode", _MODES)
def test_search_disambiguation_page_sorts_after_articles_in_every_mode(
    tmp_path: Path, mode: SearchMode
) -> None:
    query = "mercury planet element"
    search, _ = _paired(tmp_path, _corpus(), {query: topic_vector("planet")})
    results = search.search(query, 5, mode).results
    flags = [r.disambiguation for r in results]
    assert flags == sorted(flags)
    assert results[-1].title == "Mercury (disambiguation)"


@pytest.mark.contract
def test_search_semantic_disambiguation_page_sorts_last_despite_the_higher_similarity(
    tmp_path: Path,
) -> None:
    query = "mercury planet element"
    search, _ = _paired(tmp_path, _corpus(), {query: topic_vector("planet")})
    results = search.search(query, 5, "semantic").results
    assert results[-1].title == "Mercury (disambiguation)"
    assert results[-1].score > results[0].score


@pytest.mark.contract
def test_search_semantic_limit_truncates_the_results(tmp_path: Path) -> None:
    search, _ = _paired(tmp_path, _corpus())
    assert len(search.search("airship", 2, "semantic").results) == 2


@pytest.mark.contract
@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_search_blank_question_returns_no_results_without_asking_the_embedding_server(
    tmp_path: Path, mode: SearchMode
) -> None:
    search, server = _paired(tmp_path, _corpus())
    assert search.search("   ", 5, mode).results == ()
    assert server.requests == []


@pytest.mark.contract
def test_search_hybrid_score_is_the_sum_of_reciprocal_rank_shares(tmp_path: Path) -> None:
    # "airship": the Zeppelin passage is first in the keyword list (the only match) and first in
    # the semantic list, so it scores 1/(60+1) twice.
    search, _ = _paired(tmp_path, _corpus())
    top = search.search("airship", 5, "hybrid").results[0]
    assert top.title == "Zeppelin"
    assert top.score == pytest.approx(2 / 61)


@pytest.mark.contract
def test_search_hybrid_includes_an_article_only_one_list_finds(tmp_path: Path) -> None:
    search, _ = _paired(tmp_path, _corpus(), {"rigid zeppelin": topic_vector("planet")})
    titles = [r.title for r in search.search("rigid zeppelin", 5, "hybrid").results]
    assert "Zeppelin" in titles  # the keyword list
    assert "Mercury (planet)" in titles  # the semantic list


@pytest.mark.contract
def test_search_hybrid_with_no_searchable_term_answers_from_the_semantic_list_alone(
    tmp_path: Path,
) -> None:
    search, _ = _paired(tmp_path, _corpus(), {"???": topic_vector("airship")})
    assert search.search("???", 5, "lexical").results == ()
    reply = search.search("???", 5, "hybrid")
    assert reply.mode == "hybrid"
    assert reply.results[0].title == "Zeppelin"
    assert reply.results[0].score == pytest.approx(1 / 61)


@pytest.mark.contract
@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_search_semantic_hit_cut_from_different_text_than_the_store_holds_is_dropped(
    tmp_path: Path, mode: SearchMode
) -> None:
    search, _ = _paired(tmp_path, _corpus(), store=_revised_corpus())
    page_ids = [r.page_id for r in search.search("airship", 8, mode).results]
    assert 3 not in page_ids
    assert {1, 2, 5} & set(page_ids)


@pytest.mark.unit
def test_fuse_adds_the_reciprocal_rank_shares_of_a_passage_in_both_lists() -> None:
    lexical = {10: _hit(1), 20: _hit(2)}
    semantic = {20: _hit(2), 30: _hit(3)}
    fused = {hit.page_id: hit.score for hit in _fuse(lexical, semantic)}
    assert fused[2] == pytest.approx(1 / 62 + 1 / 61)  # rank 2 in the first, rank 1 in the second
    assert fused[1] == pytest.approx(1 / 61)
    assert fused[3] == pytest.approx(1 / 62)


@pytest.mark.unit
def test_fuse_orders_by_the_fused_score_best_first() -> None:
    fused = _fuse({10: _hit(1), 20: _hit(2)}, {20: _hit(2), 30: _hit(3)})
    assert [hit.page_id for hit in fused] == [2, 1, 3]


@pytest.mark.unit
def test_fuse_equal_scores_keep_the_keyword_list_first() -> None:
    fused = _fuse({10: _hit(1)}, {30: _hit(3)})
    assert [hit.page_id for hit in fused] == [1, 3]


@pytest.mark.unit
def test_fuse_same_point_id_with_different_text_hashes_is_two_passages_never_merged() -> None:
    fused = _fuse({10: _hit(1, text_hash_=111)}, {10: _hit(1, text_hash_=222)})
    assert sorted(hit.text_hash for hit in fused) == [111, 222]
    assert [hit.score for hit in fused] == [pytest.approx(1 / 61)] * 2


@pytest.mark.unit
def test_fuse_with_an_empty_keyword_list_is_the_semantic_list_alone() -> None:
    fused = _fuse({}, {30: _hit(3), 40: _hit(4)})
    assert [(hit.page_id, hit.score) for hit in fused] == [
        (3, pytest.approx(1 / 61)),
        (4, pytest.approx(1 / 62)),
    ]


@pytest.mark.contract
@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_search_semantic_mode_without_a_paired_index_raises_with_the_reason(
    tmp_path: Path, mode: SearchMode
) -> None:
    search, _, _ = _indexed(tmp_path, _corpus())
    search.unpair_semantic("there is no semantic index yet")
    with pytest.raises(SemanticUnavailableError) as raised:
        search.search("airship", 5, mode)
    assert raised.value.reason == "there is no semantic index yet"
    assert raised.value.kind == "unpaired"


@pytest.mark.contract
def test_search_before_the_semantic_index_is_checked_refuses_semantic_and_serves_lexical(
    tmp_path: Path,
) -> None:
    search, _, _ = _indexed(tmp_path, _corpus())
    with pytest.raises(SemanticUnavailableError, match="has not been checked"):
        search.search("airship", 5, "semantic")
    assert search.search("zeppelin", 5, "lexical").results[0].title == "Zeppelin"


@pytest.mark.contract
def test_search_modes_list_what_can_be_used_now(tmp_path: Path) -> None:
    unpaired, _, _ = _indexed(tmp_path / "a", _corpus())
    paired, _ = _paired(tmp_path / "b", _corpus())
    assert (unpaired.modes, unpaired.semantic_reason is None) == (("lexical",), False)
    assert (paired.modes, paired.semantic_reason) == (("lexical", "semantic", "hybrid"), None)


@pytest.mark.contract
@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_search_question_that_cannot_be_embedded_raises_and_never_falls_back_to_keywords(
    tmp_path: Path, mode: SearchMode
) -> None:
    search, server = _paired(tmp_path, _corpus())
    server.reply = httpx.Response(401, text="no")
    with pytest.raises(SemanticUnavailableError) as raised:
        search.search("zeppelin airship", 5, mode)  # keywords that lexical would find
    assert raised.value.kind == "rejected"
    assert "zeppelin" not in raised.value.reason


@pytest.mark.contract
def test_search_embedding_timeout_raises_with_the_timeout_kind(tmp_path: Path) -> None:
    search, server = _paired(tmp_path, _corpus())
    server.reply = httpx.ReadTimeout("slow")
    with pytest.raises(SemanticUnavailableError) as raised:
        search.search("airship", 5, "semantic")
    assert raised.value.kind == "timeout"
