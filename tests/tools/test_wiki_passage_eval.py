"""Qualifies the Wikipedia passage-length evaluator: its retrieval modes, metrics and caches.

``tools/wiki_passage_eval.py`` decides, with numbers, whether packing passages to 1024-2048
characters keeps retrieval quality. A wrong IDF, a quantised mode that silently misranks or a
metric that counts the wrong thing would make that decision wrong, so each piece is checked on
its own tiny, hand-computable input: the IDF formula, RRF arithmetic, binary and int8 candidate
passes whose rescoring fixes what the quantised score gets wrong, article collapse, evidence
matching with trimmed edges, and the metrics on a hand-worked example. Then the caches (articles,
vectors) and the embedding loop's concurrency and short-reply refusal, the corpus builder against
the fake mirror, and one end-to-end run over invented articles with a bag-of-words embedder:
report shape, stdout carrying no text, and a re-run embedding nothing new.

The mirror is ``tests/fixtures/wiki_eval_fakes.FakeMirror``; the embedder is a deterministic
hashing bag-of-words, so no router, GPU or network is touched. All text is invented.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import zlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from tests.fixtures.wiki_eval_fakes import MIRROR_KEY, FakeMirror
from tools import wiki_passage_eval as wpe
from tools.wiki_common import (
    Article,
    MirrorClient,
    MirrorUnavailableError,
    RouterError,
    fold,
)
from tools.wiki_golden import GoldenCase
from tools.wiki_passage_eval import (
    ArticleCache,
    CanaryError,
    CaseResult,
    EvalOptions,
    LexicalIndex,
    VectorCache,
    add_paired,
    binary_rankings,
    bm25_idf,
    build_corpus,
    chance_baseline,
    check_canary,
    collapse_to_articles,
    dense_rankings,
    embed_texts,
    evidence_in_passage,
    few_articles_share,
    focused_snippet,
    format_table,
    int8_rankings,
    judge_case,
    live_snippet,
    load_cases,
    paired_counts,
    rank_all_modes,
    recall_at,
    rescore,
    rrf,
    summarise,
    target_in_text,
    target_text,
    top_k,
)
from wiki_mirror.text.sparse import SparseEncoder

DIM = 128


@pytest.fixture(autouse=True)
def mirror_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Export the mirror key the way a client's env file does."""
    monkeypatch.setenv("WIKI_MIRROR_API_KEY", MIRROR_KEY)


def unit_rows(rows: Sequence[Sequence[float]]) -> np.ndarray:
    """Float32 rows scaled to unit length."""
    matrix = np.asarray(rows, dtype=np.float32)
    return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


def random_unit(count: int, dim: int, seed: int) -> np.ndarray:
    """Seeded random unit vectors."""
    return unit_rows(np.random.default_rng(seed).normal(size=(count, dim)))


def make_case(
    case_id: str,
    kind: str,
    page_id: int,
    evidence: str = "unused",
    answer: str = "needle text",
    question: str = "a question",
) -> GoldenCase:
    """A golden case with invented text."""
    return GoldenCase(case_id, kind, question, answer, page_id, 1, "A Title", evidence)


# -- formulas ----------------------------------------------------------------------------------


@pytest.mark.unit
def test_bm25_idf_matches_the_qdrant_modifier_formula() -> None:
    assert bm25_idf(10, 100) == pytest.approx(math.log(1 + (100 - 10 + 0.5) / (10 + 0.5)))


@pytest.mark.unit
def test_bm25_idf_rarer_term_weighs_more_and_a_term_in_every_passage_is_still_positive() -> None:
    assert bm25_idf(1, 1000) > bm25_idf(500, 1000) > bm25_idf(1000, 1000) > 0


@pytest.mark.unit
def test_top_k_equal_scores_go_to_the_lower_index() -> None:
    assert list(top_k(np.array([1.0, 3.0, 3.0, 2.0, 3.0]), 4)) == [1, 2, 4, 3]


@pytest.mark.unit
def test_top_k_asking_for_more_than_exist_returns_all() -> None:
    assert list(top_k(np.array([0.5, 0.9]), 10)) == [1, 0]


@pytest.mark.unit
def test_rrf_item_in_both_lists_beats_an_item_topping_one() -> None:
    assert rrf([[10, 20], [30, 20]]) == [20, 10, 30]


@pytest.mark.unit
def test_rrf_scores_are_reciprocal_rank_with_k_60() -> None:
    # 6: 1/62 + 1/61; 5: 1/61 + 1/63; 7: 1/63 + 1/62, hence 6 > 5 > 7.
    assert rrf([[5, 6, 7], [6, 7, 5]]) == [6, 5, 7]


@pytest.mark.unit
def test_rrf_tie_goes_to_the_lower_index_and_limit_truncates() -> None:
    assert rrf([[1, 2, 3], [2, 1, 4]], limit=3) == [1, 2, 3]


@pytest.mark.unit
def test_collapse_to_articles_ranks_an_article_at_its_best_passage() -> None:
    page_of = [100, 100, 200, 300, 200]

    collapsed = collapse_to_articles([2, 0, 4, 3, 1], page_of)

    assert collapsed == [(200, 2), (100, 0), (300, 3)]


# -- evidence ----------------------------------------------------------------------------------

LONG_EVIDENCE = (
    "The span opened to traffic in 1932 after four years of construction. Engineers chose "
    "weathering steel for the deck because it needed no paint, and the river pilings were "
    "sunk into bedrock forty metres below the waterline."
)


@pytest.mark.unit
def test_evidence_in_passage_whole_paragraph_present_is_found() -> None:
    assert evidence_in_passage("Intro. " + LONG_EVIDENCE + " Outro.", LONG_EVIDENCE)


@pytest.mark.unit
def test_evidence_in_passage_trimmed_edges_still_match_on_the_middle() -> None:
    trimmed = LONG_EVIDENCE[40:-40]

    assert evidence_in_passage(trimmed, LONG_EVIDENCE)


@pytest.mark.unit
def test_evidence_in_passage_ignores_case_and_whitespace() -> None:
    mangled = LONG_EVIDENCE.upper().replace(" ", "  \n ")

    assert evidence_in_passage(mangled, LONG_EVIDENCE)


@pytest.mark.unit
def test_evidence_in_passage_different_text_is_not_found() -> None:
    assert not evidence_in_passage("A passage about orchards and ferries.", LONG_EVIDENCE)


@pytest.mark.unit
def test_evidence_in_passage_short_evidence_must_appear_whole() -> None:
    assert evidence_in_passage("x short sentence here y", "short sentence here")
    assert not evidence_in_passage("x short sentence y", "short sentence here")


# -- metrics -----------------------------------------------------------------------------------


@pytest.mark.unit
def test_target_text_fact_is_the_folded_answer_and_describe_is_the_evidence_core() -> None:
    fact = make_case("f", "fact", 1, LONG_EVIDENCE, answer="Weathering  STEEL")
    describe = make_case("d", "describe", 1, LONG_EVIDENCE, answer="Alpha Quartz Bridge")

    assert target_text(fact) == "weathering steel"
    assert len(target_text(describe)) == 120
    assert target_text(describe) in fold(LONG_EVIDENCE)


@pytest.mark.unit
def test_target_in_text_describe_case_ignores_the_title_answer() -> None:
    case = make_case("d", "describe", 1, LONG_EVIDENCE, answer="Alpha Quartz Bridge")

    assert not target_in_text("Alpha Quartz Bridge is a crossing.", case)
    assert target_in_text(LONG_EVIDENCE[10:-10], case)


@pytest.mark.unit
def test_live_snippet_is_the_service_cut_of_the_collapsed_passage() -> None:
    passage = ("alpha  beta\n" * 100).strip()

    snippet = live_snippet(passage)

    assert snippet.endswith("…")
    assert len(snippet) <= 321
    assert snippet.startswith("alpha beta alpha beta")
    assert live_snippet("short  text") == "short text"


@pytest.mark.unit
def test_live_snippet_cuts_inside_a_word_back_to_the_previous_space() -> None:
    passage = "word " * 63 + "extraordinarily long closing token"  # char 320 falls inside a word

    snippet = live_snippet(passage)

    assert snippet.endswith("word…")


@pytest.mark.unit
def test_focused_snippet_picks_the_window_with_the_most_query_terms() -> None:
    filler = "plain filler words about the weather " * 20
    passage = filler + "the zorblax survey measured quartz depth " + filler

    snippet = focused_snippet(passage, "zorblax quartz depth survey")

    assert "zorblax survey measured quartz depth" in snippet
    assert len(snippet) <= 320
    assert "zorblax" not in live_snippet(passage)


@pytest.mark.unit
def test_focused_snippet_tie_goes_to_the_earliest_window() -> None:
    passage = "alpha filler words " * 60

    snippet = focused_snippet(passage, "unmatched")

    assert passage.startswith(snippet)  # the window at offset 0
    assert len(snippet) > 250


@pytest.mark.unit
def test_focused_snippet_window_starts_on_a_word_boundary() -> None:
    words = [f"term{i}" for i in range(300)]
    passage = " ".join(words)

    snippet = focused_snippet(passage, "term250")

    at = passage.find(snippet)
    assert "term250" in snippet
    assert at == 0 or passage[at - 1] == " "  # begins on a word boundary
    end = at + len(snippet)
    assert end == len(passage) or passage[end] == " "  # and ends on one


@pytest.mark.unit
def test_focused_snippet_passage_within_the_budget_is_returned_whole() -> None:
    assert focused_snippet("a short  passage", "anything") == "a short passage"


@pytest.mark.unit
def test_focused_snippet_target_in_the_last_words_is_reachable() -> None:
    passage = "filler " * 150 + "final needle phrase here"

    snippet = focused_snippet(passage, "needle phrase")

    assert snippet.endswith("final needle phrase here")


@pytest.mark.unit
def test_judge_case_gold_first_with_target_counts_everything() -> None:
    case = make_case("c", "fact", 7)

    result = judge_case(case, [(7, 0), (8, 1)], ["hay needle text hay", "other"])

    assert (result.hit1, result.hit5, result.answer_in_passage1, result.reciprocal_rank) == (
        True,
        True,
        True,
        1.0,
    )
    assert result.snippet_live
    assert result.snippet_focused


@pytest.mark.unit
def test_judge_case_target_beyond_the_live_snippet_is_found_only_by_the_focused_one() -> None:
    case = make_case("c", "fact", 7, question="where is the needle text")
    passage = "filler words " * 60 + "needle text " + "filler words " * 5

    result = judge_case(case, [(7, 0)], [passage])

    assert result.answer_in_passage1
    assert not result.snippet_live
    assert result.snippet_focused


@pytest.mark.unit
def test_judge_case_gold_first_but_best_passage_lacks_the_target_is_not_answer_at_1() -> None:
    case = make_case("c", "fact", 7)

    result = judge_case(case, [(7, 0)], ["nothing here"])

    assert result.hit1
    assert not result.answer_in_passage1
    assert result.target_in_best is False
    assert not result.snippet_live


@pytest.mark.unit
def test_judge_case_gold_second_never_counts_the_snippets() -> None:
    case = make_case("c", "fact", 7)

    result = judge_case(case, [(8, 1), (7, 0)], ["needle text", "needle text"])

    assert result.rank == 2
    assert result.target_in_best is True
    assert not result.snippet_live
    assert not result.snippet_focused
    assert not result.answer_in_passage1


@pytest.mark.unit
def test_judge_case_gold_sixth_is_outside_top_5_but_inside_mrr10() -> None:
    case = make_case("c", "fact", 6, "e")
    articles = [(page, page) for page in range(1, 11)]

    result = judge_case(case, articles, ["e"] * 10)

    assert not result.hit5
    assert result.rank == 6
    assert result.reciprocal_rank == pytest.approx(1 / 6)
    assert result.target_in_best is None


@pytest.mark.unit
def test_judge_case_gold_beyond_rank_10_scores_zero() -> None:
    articles = [(page, 0) for page in range(1, 15)]

    result = judge_case(make_case("c", "fact", 12), articles, ["x"])

    assert result.rank is None
    assert result.reciprocal_rank == 0.0


@pytest.mark.unit
def test_summarise_hand_computed_example_overall_and_per_kind() -> None:
    results = {
        "a": CaseResult(rank=1, target_in_best=True, snippet_live=True, snippet_focused=True),
        "b": CaseResult(rank=3, target_in_best=False),
        "c": CaseResult(rank=7, target_in_best=None),
        "d": CaseResult(rank=None, target_in_best=None),
    }
    kinds = {"a": "fact", "b": "fact", "c": "describe", "d": "describe"}

    summary = summarise(results, kinds)

    overall = summary["all"]
    assert overall["n"] == 4
    assert overall["hit1"] == 0.25
    assert overall["hit5"] == 0.5
    assert overall["mrr10"] == pytest.approx((1 + 1 / 3 + 1 / 7 + 0) / 4)
    assert overall["answer_in_passage1"] == 0.25
    assert overall["answer_in_snippet_live1"] == 0.25
    assert overall["answer_in_snippet_focused1"] == 0.25
    assert overall["evidence_given_hit5"] == 0.5  # a has the target, b does not
    assert summary["fact"]["hit5"] == 1.0
    assert summary["describe"]["hit5"] == 0.0
    assert summary["describe"]["evidence_given_hit5"] is None


@pytest.mark.unit
def test_summarise_omits_a_kind_with_no_cases() -> None:
    summary = summarise({"a": CaseResult(rank=1, target_in_best=True)}, {"a": "fact"})

    assert set(summary) == {"all", "fact"}


@pytest.mark.unit
def test_chance_baseline_is_the_average_share_of_gold_passages_holding_the_target() -> None:
    cases = [
        make_case("a", "fact", 1),  # 1 of 4 passages holds it
        make_case("b", "fact", 2),  # 2 of 2 hold it
        make_case("c", "fact", 9),  # the gold article is not in the corpus
    ]
    bodies = ["needle text", "x", "y", "z", "needle text", "needle text"]
    spans = {1: (0, 4), 2: (4, 6)}

    chance = chance_baseline(cases, spans, bodies)

    assert chance["all"] == pytest.approx((0.25 + 1.0 + 0.0) / 3)
    assert chance["fact"] == chance["all"]
    assert "describe" not in chance


@pytest.mark.unit
def test_chance_baseline_longer_passages_raise_it_for_the_same_article() -> None:
    case = make_case("a", "fact", 1)
    short = ["needle text", "x", "y", "z"]
    packed = ["needle text x", "y z"]

    assert chance_baseline([case], {1: (0, 4)}, short)["all"] == 0.25
    assert chance_baseline([case], {1: (0, 2)}, packed)["all"] == 0.5


@pytest.mark.unit
def test_few_articles_share_counts_queries_under_ten_distinct_articles() -> None:
    page_of = [i // 3 for i in range(60)]  # three passages per article, twenty articles
    spread = list(range(0, 60, 3))[:10]  # ten passages, ten articles
    clumped = list(range(30))  # thirty passages, ten articles exactly
    narrow = [0, 1, 2, 3, 4, 5]  # two articles

    share = few_articles_share([spread, clumped, narrow, []], page_of)

    assert share == pytest.approx(0.5)  # narrow and the empty list


@pytest.mark.unit
def test_few_articles_share_only_looks_at_the_top_depth() -> None:
    page_of = [i // 3 for i in range(60)]

    assert few_articles_share([list(range(60))], page_of, depth=6) == 1.0
    assert few_articles_share([list(range(60))], page_of, depth=60) == 0.0


@pytest.mark.unit
def test_paired_counts_gained_and_lost_are_hand_counted() -> None:
    base = {"a": {"k": True}, "b": {"k": False}, "c": {"k": True}, "d": {"k": False}}
    other = {"a": {"k": False}, "b": {"k": True}, "c": {"k": True}, "d": {"k": True}}

    assert paired_counts(base, other, "k") == {"gained": 2, "lost": 1}


@pytest.mark.unit
def test_add_paired_compares_every_scheme_with_v1_per_view_and_mode() -> None:
    def scheme(hit5: bool, focused: bool) -> dict[str, Any]:
        row = {"hit5": hit5, "answer_in_snippet_focused1": focused}
        cases = {"wg-1": {mode: dict(row) for mode in wpe.MODES}}
        return {"views": {view: {"cases": cases} for view in wpe.VIEWS}}

    schemes = {"0": scheme(False, True), "1024": scheme(True, False)}

    add_paired(schemes)

    pair = schemes["1024"]["views"]["random_only"]["paired_vs_v1"]["hybrid"]
    assert pair["hit5"] == {"gained": 1, "lost": 0}
    assert pair["answer_in_snippet_focused1"] == {"gained": 0, "lost": 1}
    assert "paired_vs_v1" not in schemes["0"]["views"]["with_negatives"]


@pytest.mark.unit
def test_add_paired_without_v1_changes_nothing() -> None:
    schemes = {"1024": {"views": {"with_negatives": {"cases": {}}}}}

    add_paired(schemes)

    assert "paired_vs_v1" not in schemes["1024"]["views"]["with_negatives"]


@pytest.mark.unit
def test_recall_at_is_the_overlap_share_of_the_exact_top() -> None:
    exact = [[1, 2, 3, 4], [5, 6, 7, 8]]
    approx = [[1, 2, 9, 9], [5, 6, 7, 8]]

    assert recall_at(approx, exact, 4) == pytest.approx((0.5 + 1.0) / 2)


# -- lexical -----------------------------------------------------------------------------------


@pytest.mark.unit
def test_lexical_index_idf_per_term_uses_this_collections_counts() -> None:
    index = LexicalIndex(["alpha beta", "beta gamma", "gamma delta", "delta delta"])
    term = SparseEncoder(dimensions=2**24)._index_of("alpha")
    at = int(np.searchsorted(index._unique, term))

    assert index._idf[at] == pytest.approx(bm25_idf(1, 4))


@pytest.mark.unit
def test_lexical_index_rare_term_outranks_a_common_one() -> None:
    docs = ["rareword", "common filler", "common other", "common more", "common last"]

    assert LexicalIndex(docs).search("rareword common", 5)[0] == 0


@pytest.mark.unit
def test_lexical_index_document_matching_both_terms_beats_one_matching_one() -> None:
    docs = ["quartz bridge", "quartz tower", "marble lake"]

    assert LexicalIndex(docs).search("quartz bridge", 3) == [0, 1]


@pytest.mark.unit
def test_lexical_index_omits_documents_with_no_matching_term() -> None:
    assert LexicalIndex(["alpha", "beta"]).search("gamma", 5) == []


@pytest.mark.unit
def test_lexical_index_empty_query_or_corpus_returns_nothing() -> None:
    assert LexicalIndex(["alpha"]).search("", 5) == []
    assert LexicalIndex([]).search("alpha", 5) == []


@pytest.mark.unit
def test_lexical_index_idf_counts_only_the_flagged_documents() -> None:
    # "alpha" is in three documents but only one of the two counted ones: N=2, n=1.
    index = LexicalIndex(["alpha", "alpha", "alpha", "beta"], [False, False, True, True])
    term = SparseEncoder(dimensions=2**24)._index_of("alpha")
    at = int(np.searchsorted(index._unique, term))

    assert index._idf[at] == pytest.approx(bm25_idf(1, 2))


@pytest.mark.unit
def test_lexical_index_term_only_in_uncounted_documents_gets_the_rarest_weight() -> None:
    index = LexicalIndex(["onlyhere", "alpha", "alpha"], [False, True, True])
    term = SparseEncoder(dimensions=2**24)._index_of("onlyhere")
    at = int(np.searchsorted(index._unique, term))

    assert index._idf[at] == pytest.approx(bm25_idf(0, 2))
    assert index.search("onlyhere", 3) == [0]  # still scored, because every document is


@pytest.mark.unit
def test_lexical_index_no_flag_set_falls_back_to_counting_every_document() -> None:
    index = LexicalIndex(["alpha", "beta"], [False, False])
    term = SparseEncoder(dimensions=2**24)._index_of("alpha")
    at = int(np.searchsorted(index._unique, term))

    assert index._idf[at] == pytest.approx(bm25_idf(1, 2))


@pytest.mark.unit
def test_lexical_index_allowed_mask_excludes_documents_without_changing_the_rest() -> None:
    index = LexicalIndex(["quartz bridge", "quartz tower", "quartz bridge tower"])

    everyone = index.search("quartz bridge", 3)
    masked = index.search("quartz bridge", 3, allowed=[False, True, True])

    assert everyone[0] in (0, 2)
    assert 0 not in masked
    assert [d for d in everyone if d != 0] == masked


# -- dense and its quantised modes --------------------------------------------------------------


@pytest.mark.unit
def test_rank_all_modes_mask_never_returns_a_masked_passage_and_keeps_corpus_numbers() -> None:
    passages = random_unit(60, 16, 21)
    queries = random_unit(4, 16, 22)
    docs = [f"word{i} shared" for i in range(60)]
    cases = [make_case(f"c{i}", "fact", 1, question="shared word3") for i in range(4)]
    mask = np.array([i % 3 != 0 for i in range(60)])
    lexical = LexicalIndex(docs)

    ranked = rank_all_modes(passages, queries, lexical, cases, opts(), mask)

    assert set(ranked) == set(wpe.MODES)
    for mode, rankings in ranked.items():
        for ranking in rankings:
            assert ranking, mode
            assert all(mask[i] for i in ranking), mode
    full = rank_all_modes(passages, queries, lexical, cases, opts(), None)
    # The masked dense ranking is the full one with the masked passages removed.
    assert ranked["dense"][0] == [i for i in full["dense"][0] if mask[i]]


@pytest.mark.unit
def test_rank_all_modes_ranks_one_thousand_deep_when_the_corpus_allows() -> None:
    passages = random_unit(1500, 8, 23)
    queries = random_unit(1, 8, 24)
    lexical = LexicalIndex([f"shared w{i}" for i in range(1500)])
    cases = [make_case("c", "fact", 1, question="shared")]

    ranked = rank_all_modes(passages, queries, lexical, cases, opts(), None)

    assert {mode: len(r[0]) for mode, r in ranked.items() if mode != "hybrid"} == {
        "lexical": 1000,
        "dense": 1000,
        "dense_binary": 1000,
        "dense_int8": 1000,
    }
    assert len(ranked["hybrid"][0]) == 1000


@pytest.mark.unit
def test_rank_depth_is_a_thousand_and_the_shallow_check_a_hundred() -> None:
    assert wpe.RANK_DEPTH == 1000
    assert wpe.SHALLOW_DEPTH == 100


@pytest.mark.unit
def test_dense_rankings_orders_by_cosine() -> None:
    passages = unit_rows([[1, 0], [0.8, 0.6], [0, 1]])
    queries = unit_rows([[1, 0.1]])

    assert dense_rankings(passages, queries, 3) == [[0, 1, 2]]


@pytest.mark.unit
def test_dense_rankings_chunking_does_not_change_the_result() -> None:
    passages = random_unit(50, 16, 1)
    queries = random_unit(7, 16, 2)

    assert dense_rankings(passages, queries, 10, chunk=2) == dense_rankings(
        passages, queries, 10, chunk=64
    )


@pytest.mark.unit
def test_rescore_reorders_candidates_by_float_cosine_and_truncates() -> None:
    passages = unit_rows([[0, 1], [1, 0.1], [1, 0.5]])
    query = unit_rows([[1, 0]])[0]

    assert rescore(passages, query, [0, 2, 1], 2) == [1, 2]


@pytest.mark.unit
def test_binary_rankings_without_oversampling_misranks_on_tied_sign_bits() -> None:
    # Every positive vector has the same sign pattern as the query, so Hamming ties and the
    # lower index wins: index 0 is the worse match and comes back alone.
    passages = unit_rows([[0.5, 0.5, 0.5, 0.5], [1, 0.01, 0.01, 0.01], [-1, -1, -1, -1]])
    queries = unit_rows([[1, 0.01, 0.01, 0.01]])

    assert binary_rankings(passages, queries, 1, 1.0) == [[0]]


@pytest.mark.unit
def test_binary_rankings_oversampling_lets_the_float_rescore_fix_the_ranking() -> None:
    passages = unit_rows([[0.5, 0.5, 0.5, 0.5], [1, 0.01, 0.01, 0.01], [-1, -1, -1, -1]])
    queries = unit_rows([[1, 0.01, 0.01, 0.01]])

    assert binary_rankings(passages, queries, 1, 2.0) == [[1]]


@pytest.mark.unit
def test_binary_rankings_candidates_are_the_fewest_differing_bits() -> None:
    passages = unit_rows([[-1, -1, -1, -1], [1, 1, 1, -1], [1, 1, 1, 1]])
    queries = unit_rows([[1, 1, 1, 1]])

    assert binary_rankings(passages, queries, 2, 1.0) == [[2, 1]]


@pytest.mark.unit
def test_binary_rankings_with_every_passage_a_candidate_equals_exact_dense() -> None:
    passages = random_unit(120, 64, 3)
    queries = random_unit(6, 64, 4)

    assert binary_rankings(passages, queries, 10, 12.0) == dense_rankings(passages, queries, 10)


@pytest.mark.unit
def test_int8_rankings_with_every_passage_a_candidate_equals_exact_dense() -> None:
    passages = random_unit(120, 64, 5)
    queries = random_unit(6, 64, 6)

    assert int8_rankings(passages, queries, 10, 12.0) == dense_rankings(passages, queries, 10)


@pytest.mark.unit
def test_int8_rankings_at_the_default_oversampling_recovers_nearly_all_of_the_exact_top() -> None:
    passages = random_unit(400, 64, 7)
    queries = random_unit(8, 64, 8)
    exact = dense_rankings(passages, queries, 10)

    approx = int8_rankings(passages, queries, 10, 2.0, chunk=3, block=100)

    assert recall_at(approx, exact, 10) >= 0.9


@pytest.mark.unit
def test_int8_rankings_one_outlier_does_not_collapse_the_other_vectors() -> None:
    # A single huge value in one dimension must not stretch that dimension's 255 levels over
    # everyone (the 0.99 quantile clips it), or the near neighbours would quantise to the same
    # code and the candidate pass could not tell them apart.
    passages = random_unit(300, 8, 9)
    passages[0, 0] = 50.0
    passages = passages / np.linalg.norm(passages, axis=1, keepdims=True)
    queries = random_unit(5, 8, 10)
    exact = dense_rankings(passages, queries, 5)

    approx = int8_rankings(passages, queries, 5, 3.0)

    assert recall_at(approx, exact, 5) >= 0.9


# -- caches and embedding ------------------------------------------------------------------------


@pytest.mark.unit
def test_article_cache_round_trips_articles_and_absent_ids_across_flushes(tmp_path: Path) -> None:
    path = tmp_path / "articles.jsonl.gz"
    cache = ArticleCache(path)
    cache.record(1, Article(1, 10, "One", "text one"))
    cache.flush()
    cache.record(2, None)
    cache.record(3, Article(3, 30, "Three", "text three"))
    cache.flush()

    reloaded = ArticleCache(path)

    assert reloaded.articles[1].title == "One"
    assert reloaded.articles[3].text == "text three"
    assert reloaded.missing == {2}
    assert reloaded.known(2)
    assert not reloaded.known(4)


@pytest.mark.unit
def test_vector_cache_round_trip_persists_and_keys_by_model(tmp_path: Path) -> None:
    cache = VectorCache(tmp_path / "v", "model-a")
    keys = [cache.key("one"), cache.key("two")]
    cache.add(keys, unit_rows([[1, 0], [0, 1]]))

    reopened = VectorCache(tmp_path / "v", "model-a")
    other_model = VectorCache(tmp_path / "v", "model-b")

    assert reopened.has(keys[0])
    assert np.allclose(reopened.gather([keys[1], keys[0]]), [[0, 1], [1, 0]])
    assert not other_model.has(other_model.key("one"))


@pytest.mark.unit
def test_vector_cache_shard_without_its_keys_file_is_ignored(tmp_path: Path) -> None:
    directory = tmp_path / "v"
    cache = VectorCache(directory, "m")
    cache.add([cache.key("a")], unit_rows([[1, 0]]))
    np.save(directory / "vec_000009.npy", unit_rows([[0, 1]]))  # a write that died before keys

    assert len(VectorCache(directory, "m")._where) == 1


@pytest.mark.unit
def test_vector_cache_new_shard_number_is_the_highest_existing_plus_one(tmp_path: Path) -> None:
    directory = tmp_path / "v"
    cache = VectorCache(directory, "m")
    cache.add([cache.key("a")], unit_rows([[1, 0]]))
    cache.add([cache.key("b")], unit_rows([[0, 1]]))
    (directory / "vec_000000.keys").unlink()  # a hole: counting files would now reuse 1
    np.save(directory / "vec_000007.npy", unit_rows([[1, 1]]))  # an orphan from a dead write

    reopened = VectorCache(directory, "m")
    reopened.add([reopened.key("c")], unit_rows([[1, 0]]))

    assert sorted(p.name for p in directory.glob("vec_*.keys")) == [
        "vec_000001.keys",
        "vec_000008.keys",
    ]


@pytest.mark.unit
def test_vector_cache_add_leaves_no_temp_files_and_keys_arrive_whole(tmp_path: Path) -> None:
    directory = tmp_path / "v"
    cache = VectorCache(directory, "m")

    cache.add([cache.key("a"), cache.key("b")], unit_rows([[1, 0], [0, 1]]))

    assert sorted(p.name for p in directory.iterdir()) == ["vec_000000.keys", "vec_000000.npy"]
    assert (directory / "vec_000000.keys").read_text(encoding="ascii").split() == [
        cache.key("a"),
        cache.key("b"),
    ]


class HashEmbedder:
    """A deterministic hashing bag-of-words embedder that records how it was called."""

    def __init__(self, short_by: int = 0) -> None:
        """``short_by`` drops that many vectors from every reply."""
        self.batches: list[int] = []
        self.texts: list[str] = []
        self.current = 0
        self.peak = 0
        self.short_by = short_by

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One count vector per text, after yielding so requests overlap."""
        self.current += 1
        self.peak = max(self.peak, self.current)
        await asyncio.sleep(0)
        self.current -= 1
        self.batches.append(len(texts))
        self.texts.extend(texts)
        vectors = []
        for text in texts:
            vector = [0.0] * DIM
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                vector[zlib.crc32(word.encode()) % DIM] += 1.0
            vectors.append(vector)
        return vectors[: len(vectors) - self.short_by]


@pytest.mark.unit
def test_embed_texts_respects_batch_size_and_in_flight_and_aligns_rows_with_inputs(
    tmp_path: Path,
) -> None:
    texts = [f"text number {i} unique{i}" for i in range(40)]
    embedder = HashEmbedder()
    cache = VectorCache(tmp_path / "v", "m")

    matrix, new = asyncio.run(
        embed_texts(texts, router=embedder, cache=cache, in_flight=3, batch=4)
    )

    assert new == 40
    assert max(embedder.batches) == 4
    assert embedder.peak <= 3
    assert matrix.shape == (40, DIM)
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)
    direct = asyncio.run(HashEmbedder().embed([texts[17]]))[0]
    expected = np.asarray(direct, dtype=np.float32)
    assert np.allclose(matrix[17], expected / np.linalg.norm(expected), atol=1e-6)


@pytest.mark.unit
def test_embed_texts_duplicates_are_embedded_once_but_returned_for_every_position(
    tmp_path: Path,
) -> None:
    embedder = HashEmbedder()
    cache = VectorCache(tmp_path / "v", "m")

    matrix, new = asyncio.run(
        embed_texts(["same", "other", "same"], router=embedder, cache=cache, in_flight=2, batch=8)
    )

    assert new == 2
    assert sorted(embedder.texts) == ["other", "same"]
    assert np.allclose(matrix[0], matrix[2])


@pytest.mark.unit
def test_embed_texts_second_run_over_the_same_texts_calls_the_router_not_at_all(
    tmp_path: Path,
) -> None:
    texts = ["alpha words", "beta words"]
    cache = VectorCache(tmp_path / "v", "m")
    asyncio.run(embed_texts(texts, router=HashEmbedder(), cache=cache, in_flight=2, batch=2))
    second = HashEmbedder()

    reopened = VectorCache(tmp_path / "v", "m")
    _, new = asyncio.run(embed_texts(texts, router=second, cache=reopened, in_flight=2, batch=2))

    assert new == 0
    assert second.batches == []


@pytest.mark.unit
def test_embed_texts_short_reply_raises_and_caches_nothing(tmp_path: Path) -> None:
    cache = VectorCache(tmp_path / "v", "m")

    with pytest.raises(RouterError, match="vectors for"):
        asyncio.run(
            embed_texts(
                ["a b", "c d"], router=HashEmbedder(short_by=1), cache=cache, in_flight=1, batch=2
            )
        )

    assert not cache.has(cache.key("a b"))


# -- canary ------------------------------------------------------------------------------------


class NodeEmbedder:
    """An embedder whose answer depends on which 'node' (call number modulo) serves it."""

    def __init__(self, vectors: Sequence[Sequence[float]]) -> None:
        """One vector per node; calls rotate through them."""
        self.vectors = [list(v) for v in vectors]
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """The next node's vector for every text."""
        node = self.vectors[self.calls % len(self.vectors)]
        self.calls += 1
        await asyncio.sleep(0)
        return [node for _ in texts]


@pytest.mark.unit
def test_check_canary_agreeing_nodes_pass_store_the_canary_and_send_sixteen_requests(
    tmp_path: Path,
) -> None:
    embedder = NodeEmbedder([[1.0, 0.0, 0.0]])

    report = asyncio.run(check_canary(embedder, tmp_path / "vectors"))

    assert embedder.calls == 16
    assert report["min_pairwise_cosine"] == pytest.approx(1.0)
    assert report["min_cosine_vs_stored"] is None
    assert report["stored_now"] is True
    assert (tmp_path / "vectors" / "canary.npy").exists()


@pytest.mark.unit
def test_check_canary_second_run_compares_against_the_stored_vector(tmp_path: Path) -> None:
    directory = tmp_path / "vectors"
    asyncio.run(check_canary(NodeEmbedder([[1.0, 0.0]]), directory))

    report = asyncio.run(check_canary(NodeEmbedder([[1.0, 0.0]]), directory))

    assert report["min_cosine_vs_stored"] == pytest.approx(1.0)
    assert report["stored_now"] is False


@pytest.mark.unit
def test_check_canary_nodes_that_disagree_fail_with_the_delete_message_and_store_nothing(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "vectors"
    embedder = NodeEmbedder([[1.0, 0.0], [0.9, 0.436]])  # two builds, cosine about 0.9

    with pytest.raises(CanaryError, match="differ across nodes or since the cache was made") as e:
        asyncio.run(check_canary(embedder, directory))

    assert str(directory) in str(e.value)
    assert not (directory / "canary.npy").exists()


@pytest.mark.unit
def test_check_canary_threshold_is_cosine_0_9995() -> None:
    assert wpe.CANARY_MIN_COSINE == 0.9995


@pytest.mark.unit
@pytest.mark.parametrize(("cosine", "passes"), [(0.9990, False), (0.9999, True)])
def test_check_canary_cosine_either_side_of_the_threshold(
    tmp_path: Path, cosine: float, passes: bool
) -> None:
    other = [cosine, math.sqrt(1 - cosine**2)]
    embedder = NodeEmbedder([[1.0, 0.0], other])

    if passes:
        asyncio.run(check_canary(embedder, tmp_path / "v"))
    else:
        with pytest.raises(CanaryError):
            asyncio.run(check_canary(embedder, tmp_path / "v"))


@pytest.mark.unit
def test_check_canary_embeddings_changed_since_the_cache_was_made_fail(tmp_path: Path) -> None:
    directory = tmp_path / "vectors"
    asyncio.run(check_canary(NodeEmbedder([[1.0, 0.0]]), directory))

    with pytest.raises(CanaryError, match="since the cache was made"):
        asyncio.run(check_canary(NodeEmbedder([[0.0, 1.0]]), directory))


@pytest.mark.unit
def test_check_canary_short_reply_raises_router_error(tmp_path: Path) -> None:
    class Empty:
        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            return []

    with pytest.raises(RouterError):
        asyncio.run(check_canary(Empty(), tmp_path / "v"))


# -- corpus ------------------------------------------------------------------------------------


def corpus_mirror(count: int = 20) -> FakeMirror:
    """Articles 1..count, ids 1..count+10 existing as 404s beyond."""
    return FakeMirror(
        {
            pid: (f"Topic {pid}", 500 + pid, f"Text of article {pid}. " * 5)
            for pid in range(1, count + 1)
        }
    )


def opts(**overrides: Any) -> EvalOptions:
    """Evaluation options sized for the fake mirror."""
    values: dict[str, Any] = {
        "schemes": (0,),
        "articles": 12,
        "negatives_per_question": 3,
        "max_page_id": 30,
        "in_flight": 2,
        "batch": 2,
    }
    values.update(overrides)
    return EvalOptions(**values)


def golden(page_id: int, revision: int | None = None, case_id: str = "wg-0001") -> GoldenCase:
    """A case whose answer article is ``page_id``."""
    return GoldenCase(
        case_id, "fact", f"question {page_id}", "a", page_id, revision or 500 + page_id, "T", "e"
    )


@pytest.mark.contract
def test_build_corpus_holds_golden_negatives_and_random_articles_up_to_the_total(
    tmp_path: Path,
) -> None:
    mirror = corpus_mirror()
    mirror.search_results["question 5"] = [9, 10, 11]

    corpus = asyncio.run(build_corpus([golden(5)], opts(), mirror.client(), tmp_path))

    assert {5, 9, 10, 11} <= set(corpus.articles)
    assert len(corpus.articles) == 12
    assert list(corpus.articles) == sorted(corpus.articles)


@pytest.mark.contract
def test_build_corpus_same_seed_gives_the_same_sample_from_a_cold_or_a_warm_cache(
    tmp_path: Path,
) -> None:
    cold = asyncio.run(build_corpus([golden(5)], opts(), corpus_mirror().client(), tmp_path / "a"))
    first = asyncio.run(build_corpus([golden(5)], opts(), corpus_mirror().client(), tmp_path / "b"))
    warm_mirror = corpus_mirror()
    warm = asyncio.run(build_corpus([golden(5)], opts(), warm_mirror.client(), tmp_path / "b"))

    assert set(cold.articles) == set(first.articles) == set(warm.articles)
    assert warm_mirror.reads == []  # nothing re-fetched
    assert [r for r in warm_mirror.requests if r[1] == "/v1/wiki/search"] == []


@pytest.mark.contract
def test_build_corpus_counts_revision_mismatch_and_still_uses_the_article(tmp_path: Path) -> None:
    corpus = asyncio.run(
        build_corpus([golden(5, revision=1)], opts(), corpus_mirror().client(), tmp_path)
    )

    assert corpus.revision_mismatches == 1
    assert 5 in corpus.articles


@pytest.mark.contract
def test_build_corpus_golden_article_gone_from_the_mirror_is_counted_missing(
    tmp_path: Path,
) -> None:
    corpus = asyncio.run(build_corpus([golden(99)], opts(), corpus_mirror().client(), tmp_path))

    assert corpus.gold_missing == 1
    assert 99 not in corpus.articles


@pytest.mark.contract
def test_build_corpus_marks_negative_only_articles_and_keeps_random_hits_out_of_them(
    tmp_path: Path,
) -> None:
    mirror = corpus_mirror()
    mirror.search_results["question 5"] = [9, 10, 11]

    corpus = asyncio.run(build_corpus([golden(5)], opts(), mirror.client(), tmp_path))

    assert corpus.negative_only <= {9, 10, 11}
    assert 5 not in corpus.negative_only
    assert corpus.negative_only.isdisjoint(corpus.random_ids - {9, 10, 11})
    assert (corpus.negative_only | corpus.random_ids | {5}) >= set(corpus.articles)
    assert corpus.stats["negative_only_articles"] == len(corpus.negative_only)


@pytest.mark.contract
def test_build_corpus_a_page_whose_read_keeps_failing_is_skipped_counted_and_not_cached(
    tmp_path: Path,
) -> None:
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    mirror = corpus_mirror()
    mirror.fail_first = 3  # the first page's three attempts all answer 500
    options = opts(negatives_per_question=0, articles=1)

    corpus = asyncio.run(
        build_corpus([golden(5)], options, mirror.client(), tmp_path, sleep=record)
    )

    assert corpus.stats["read_failures"] == 1
    assert corpus.gold_missing == 1
    assert sleeps[:2] == [1.0, 2.0]
    cache = ArticleCache(tmp_path / "articles.jsonl.gz")
    assert not cache.known(5)  # a transient failure is not remembered as a missing page


@pytest.mark.contract
def test_build_corpus_a_dead_mirror_stops_the_run_instead_of_skipping_everything(
    tmp_path: Path,
) -> None:
    async def no_sleep(seconds: float) -> None:
        return None

    mirror = corpus_mirror()
    mirror.fail_first = 10_000

    options = opts(negatives_per_question=0)

    with pytest.raises(MirrorUnavailableError):
        asyncio.run(
            build_corpus([golden(5)], options, mirror.client(), tmp_path, sleep=no_sleep)
        )


@pytest.mark.contract
def test_build_corpus_fewer_articles_than_asked_for_stops_instead_of_looping(
    tmp_path: Path,
) -> None:
    mirror = corpus_mirror(count=3)

    corpus = asyncio.run(
        build_corpus([golden(1)], opts(articles=50, max_page_id=3), mirror.client(), tmp_path)
    )

    assert len(corpus.articles) == 3


# -- end to end --------------------------------------------------------------------------------


def make_world() -> tuple[FakeMirror, list[GoldenCase]]:
    """Six invented articles with four sections each, and one golden case per article."""
    articles: dict[int, tuple[str, int, str]] = {}
    cases: list[GoldenCase] = []
    for pid in range(1, 7):
        sections = []
        for s in range(4):
            marker = f"zorblax{pid}x{s}"  # in every sentence, so the evidence core is unique
            body = f"The {marker} measured {s * 7 + 3} units. " + (
                f"Survey of {marker} noted plain filler about weather. " * 5
            )
            sections.append(f"## Part {s}\n\n{body}")
        text = "\n\n".join(sections)
        articles[pid] = (f"Topic {pid}", 700 + pid, text)
        evidence = sections[pid % 4].split("\n\n", 1)[1]
        is_fact = pid % 2 == 1
        cases.append(
            GoldenCase(
                f"wg-{pid:04d}",
                "fact" if is_fact else "describe",
                f"How big was zorblax{pid}x{pid % 4}?",
                f"{(pid % 4) * 7 + 3} units" if is_fact else f"Topic {pid}",
                pid,
                700 + pid,
                f"Topic {pid}",
                evidence,
            )
        )
    mirror = FakeMirror(articles)
    for case in cases:
        mirror.search_results[case.question] = [case.page_id, (case.page_id % 6) + 1]
    return mirror, cases


def fixed_clock() -> datetime:
    """A frozen clock for the report's file name."""
    return datetime(2026, 10, 19, 12, 30, 5, tzinfo=UTC)


@pytest.mark.contract
def test_run_scores_every_scheme_and_mode_and_writes_a_report_without_text(
    tmp_path: Path,
) -> None:
    mirror, cases = make_world()
    options = opts(schemes=(0, 1024), articles=6, negatives_per_question=2, max_page_id=6)

    report, path = asyncio.run(
        wpe.run(
            cases,
            options,
            mirror=mirror.client(),
            router=HashEmbedder(),
            work_dir=tmp_path,
            now=fixed_clock,
        )
    )

    assert path.name == "report_20261019T123005Z.json"
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(json.dumps(report))
    assert set(report["schemes"]) == {"0", "1024"}
    v1, packed = report["schemes"]["0"], report["schemes"]["1024"]
    assert packed["passages"] < v1["passages"]
    assert packed["mean_passage_chars"] > v1["mean_passage_chars"]
    assert v1["evidence_missing_share"] == 0.0
    assert packed["evidence_missing_share"] == 0.0
    for scheme in (v1, packed):
        assert set(scheme["views"]) == {"with_negatives", "random_only"}
        for view in scheme["views"].values():
            assert set(view["metrics"]) == set(wpe.MODES)
            assert view["metrics"]["lexical"]["all"]["hit1"] == 1.0
            assert view["metrics"]["hybrid"]["all"]["hit5"] == 1.0
            assert view["metrics"]["dense_binary"]["all"]["n"] == 6
            assert view["recall10_vs_exact"]["dense_binary"] > 0.5
            assert set(view["cases"]) == {c.id for c in cases}
            assert isinstance(view["cases"]["wg-0001"]["lexical"]["hit5"], bool)
            assert set(view["few_articles_share"]) == set(wpe.MODES)
    lexical_v1 = v1["views"]["with_negatives"]["metrics"]["lexical"]["all"]
    lexical_packed = packed["views"]["with_negatives"]["metrics"]["lexical"]["all"]
    # Both cuts keep each section's target inside the passage the article ranks at. A v1 passage
    # fits the 320-character budget whole; a packed one does not, so its snippets can miss.
    assert lexical_v1["answer_in_passage1"] == 1.0
    assert lexical_packed["answer_in_passage1"] == 1.0
    assert lexical_v1["answer_in_snippet_live1"] == 1.0
    assert lexical_v1["answer_in_snippet_focused1"] == 1.0
    assert lexical_packed["answer_in_snippet_live1"] < 1.0
    assert lexical_packed["answer_in_snippet_focused1"] > 0.0
    # One of four sections holds the target in v1, one of two passages once packed.
    assert v1["answer_in_passage_chance"]["all"] == pytest.approx(0.25)
    assert packed["answer_in_passage_chance"]["all"] == pytest.approx(0.5)
    # The packed scheme's paired comparison against v1 is present; v1 has none.
    assert "paired_vs_v1" in packed["views"]["with_negatives"]
    assert "paired_vs_v1" not in v1["views"]["with_negatives"]
    assert report["corpus"]["golden_cases"] == 6
    assert report["corpus"]["revision_mismatches"] == 0
    assert report["canary"]["min_pairwise_cosine"] == pytest.approx(1.0)
    assert "upper bound" in report["notes"]["dense_binary"]


@pytest.mark.contract
def test_run_random_only_view_masks_hard_negatives_that_win_the_with_negatives_view(
    tmp_path: Path,
) -> None:
    mirror, cases = make_world()
    # A distractor repeats case 1's rare token, so the keyword ranker puts it first when it is
    # a candidate; it enters the corpus only as a hard negative of that question.
    distractor = "## Part 0\n\n" + "zorblax1x1 big " * 25
    mirror.articles[7] = ("Distractor", 707, distractor)
    mirror.search_results[cases[0].question] = [1, 7]
    options = opts(articles=7, negatives_per_question=2, max_page_id=6)

    report, _ = asyncio.run(
        wpe.run(
            cases,
            options,
            mirror=mirror.client(),
            router=HashEmbedder(),
            work_dir=tmp_path,
            now=fixed_clock,
        )
    )

    views = report["schemes"]["0"]["views"]
    with_negatives = views["with_negatives"]["cases"]["wg-0001"]["lexical"]
    random_only = views["random_only"]["cases"]["wg-0001"]["lexical"]
    assert report["corpus"]["negative_only_articles"] == 1
    assert with_negatives["rank"] == 2
    assert random_only["rank"] == 1
    assert views["random_only"]["passages"] < views["with_negatives"]["passages"]


@pytest.mark.contract
def test_run_report_and_table_carry_no_question_title_or_article_text(tmp_path: Path) -> None:
    mirror, cases = make_world()

    report, path = asyncio.run(
        wpe.run(
            cases,
            opts(articles=6, max_page_id=6),
            mirror=mirror.client(),
            router=HashEmbedder(),
            work_dir=tmp_path,
            now=fixed_clock,
        )
    )

    table = format_table(report)
    saved = path.read_text(encoding="utf-8")
    for case in cases:
        for secret in (case.question, case.title, case.evidence[:40]):
            assert secret not in table
            assert secret not in saved
    assert "hit@5" in table


@pytest.mark.contract
def test_format_table_labels_quantised_modes_shows_both_views_and_the_paired_counts(
    tmp_path: Path,
) -> None:
    mirror, cases = make_world()

    report, _ = asyncio.run(
        wpe.run(
            cases,
            opts(schemes=(0, 1024), articles=6, max_page_id=6),
            mirror=mirror.client(),
            router=HashEmbedder(),
            work_dir=tmp_path,
            now=fixed_clock,
        )
    )

    table = format_table(report)
    assert "dense_binary*" in table
    assert "dense_int8*" in table
    assert "not for the quantization decision" in table
    assert "view with_negatives" in table
    assert "view random_only" in table
    assert "chance" in table
    assert re.search(r"\+\d+/-\d+ / \+\d+/-\d+", table)  # a paired cell for scheme 1024
    assert "canary: min_pairwise_cosine=1.00000" in table


@pytest.mark.contract
def test_run_again_over_a_warm_work_dir_embeds_nothing_and_fetches_nothing(
    tmp_path: Path,
) -> None:
    mirror, cases = make_world()
    options = opts(articles=6, max_page_id=6)
    first = HashEmbedder()
    asyncio.run(
        wpe.run(cases, options, mirror=mirror.client(), router=first, work_dir=tmp_path)
    )
    mirror_again, _ = make_world()
    second = HashEmbedder()

    asyncio.run(
        wpe.run(cases, options, mirror=mirror_again.client(), router=second, work_dir=tmp_path)
    )

    assert first.texts
    # Only the canary goes to the router on a warm run, sixteen times, and nothing is re-fetched.
    assert second.texts == [wpe.CANARY_TEXT] * 16
    assert mirror_again.reads == []


@pytest.mark.contract
def test_run_stops_with_the_canary_error_when_nodes_disagree_before_touching_the_mirror(
    tmp_path: Path,
) -> None:
    mirror, cases = make_world()

    with pytest.raises(CanaryError):
        asyncio.run(
            wpe.run(
                cases,
                opts(articles=6, max_page_id=6),
                mirror=mirror.client(),
                router=NodeEmbedder([[1.0, 0.0], [0.0, 1.0]]),
                work_dir=tmp_path,
            )
        )

    assert mirror.requests == []


# -- golden loading and command line -----------------------------------------------------------


@pytest.mark.unit
def test_load_cases_reads_the_golden_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "g.jsonl"
    case = golden(5)
    path.write_text(json.dumps(case.__dict__) + "\n\n", encoding="utf-8")

    assert load_cases(path) == [case]


@pytest.mark.unit
def test_load_cases_malformed_line_raises_naming_the_line_not_its_content(tmp_path: Path) -> None:
    path = tmp_path / "g.jsonl"
    path.write_text('{"id": "wg-0001", "secret_question": "private"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="line 1") as caught:
        load_cases(path)

    assert "private" not in str(caught.value)


@pytest.mark.unit
def test_load_cases_repeated_id_raises(tmp_path: Path) -> None:
    path = tmp_path / "g.jsonl"
    line = json.dumps(golden(5).__dict__)
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="repeats"):
        load_cases(path)


def patch_clients(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response], router: Any
) -> None:
    """Make ``_main`` build a mirror client over ``handler`` and use ``router`` as the router."""

    class FakeRouterClient:
        """Stands in for ``RouterClient``."""

        def __init__(self, base_url: str, embed_model: str = "") -> None:
            """Ignore the address and model."""

        async def __aenter__(self) -> Any:
            return router

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(wpe, "RouterClient", FakeRouterClient)
    monkeypatch.setattr(
        wpe,
        "MirrorClient",
        lambda **_: MirrorClient(
            base_url="http://m.test", transport=httpx.MockTransport(handler)
        ),
    )


@pytest.mark.contract
def test_main_failure_output_is_the_status_never_the_mirrors_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    golden_file = tmp_path / "g.jsonl"
    golden_file.write_text(json.dumps(golden(5).__dict__) + "\n", encoding="utf-8")
    patch_clients(
        monkeypatch,
        lambda r: httpx.Response(422, json={"error": "invalid_request", "detail": "SECRET QUERY"}),
        HashEmbedder(),
    )

    code = wpe.main(["--golden", str(golden_file), "--work-dir", str(tmp_path / "w")])

    err = capsys.readouterr().err
    assert code == 1
    assert "status 422" in err
    assert "SECRET" not in err


@pytest.mark.contract
def test_main_canary_failure_exits_1_naming_the_directory_to_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    golden_file = tmp_path / "g.jsonl"
    golden_file.write_text(json.dumps(golden(5).__dict__) + "\n", encoding="utf-8")
    patch_clients(
        monkeypatch,
        lambda r: httpx.Response(500),
        NodeEmbedder([[1.0, 0.0], [0.0, 1.0]]),
    )
    work = tmp_path / "w"

    code = wpe.main(["--golden", str(golden_file), "--work-dir", str(work)])

    err = capsys.readouterr().err
    assert code == 1
    assert "embeddings differ across nodes or since the cache was made" in err
    assert f"delete {work / 'vectors'}" in err


@pytest.mark.contract
def test_main_prints_the_canary_minimum_cosine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mirror, cases = make_world()
    golden_file = tmp_path / "g.jsonl"
    golden_file.write_text(
        "".join(json.dumps(c.__dict__) + "\n" for c in cases), encoding="utf-8"
    )
    patch_clients(monkeypatch, lambda r: httpx.Response(500), HashEmbedder())
    monkeypatch.setattr(wpe, "MirrorClient", lambda **_: mirror.client())  # the fake mirror

    code = wpe.main(
        [
            "--golden", str(golden_file), "--work-dir", str(tmp_path / "w"),
            "--schemes", "0", "--articles", "6", "--max-page-id", "6",
            "--negatives-per-question", "2", "--in-flight", "2", "--batch", "2",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "canary: min_pairwise_cosine=1.00000" in out
    for case in cases:
        assert case.question not in out
        assert case.title not in out


@pytest.mark.unit
def test_parse_args_in_flight_above_the_cluster_knee_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        wpe._parse_args(["--golden", str(tmp_path / "g"), "--in-flight", "17"])


@pytest.mark.unit
def test_parse_args_negatives_above_the_search_limit_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        wpe._parse_args(
            ["--golden", str(tmp_path / "g"), "--negatives-per-question", "21"]
        )


@pytest.mark.unit
def test_parse_args_defaults_match_the_documented_run(tmp_path: Path) -> None:
    args = wpe._parse_args(["--golden", str(tmp_path / "g")])

    assert args.scheme_list == (0, 1024, 1536, 2048)
    assert (args.articles, args.in_flight, args.batch) == (15000, 16, 8)
    assert (args.binary_oversampling, args.int8_oversampling) == (3.0, 2.0)
