"""Qualifies the Wikipedia golden-set generator: what it skips, what it rejects, what it writes.

``tools/wiki_golden.py`` turns random mirror articles into labelled questions, and the semantic
index is judged against them, so a bad label is a wrong number. Tested here: the article skip
rules, paragraph and evidence selection, every deterministic rejection rule (one test each),
the retry-then-reject flow against a fake model (bad JSON, a rule violation, a router that
cannot be repaired), the 404 skip, duplicate articles, and determinism - the same seed gives the
same file however the model's replies interleave. Finally the file shape and that stdout carries
counts only.

The mirror is ``tests/fixtures/wiki_eval_fakes.FakeMirror`` (real reply shapes). The model is a
fake chat object that answers from the prompt it is given, with a seeded number of event-loop
yields before replying to shuffle completion order without any wall-clock wait. All text is
invented.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.fixtures.wiki_eval_fakes import MIRROR_KEY, FakeMirror
from tools import wiki_golden
from tools.wiki_common import (
    Article,
    MirrorClient,
    MirrorServiceError,
    RouterContentError,
    RouterError,
)
from tools.wiki_golden import (
    GoldenOptions,
    assign_kinds,
    candidate_paragraphs,
    generate,
    skip_reason,
    trim_evidence,
    validate_question,
    write_cases,
)

TITLE = "Alpha Quartz Bridge"
EVIDENCE = (
    "The span opened to traffic in 1932 after four years of construction. Engineers chose "
    "weathering steel for the deck because it needed no paint, and the river pilings were "
    "sunk into bedrock forty metres below the waterline."
)


@pytest.fixture(autouse=True)
def mirror_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Export the mirror key the way a client's env file does."""
    monkeypatch.setenv("WIKI_MIRROR_API_KEY", MIRROR_KEY)


def make_text(seed: int) -> str:
    """Invented article text: a heading, then four 300-plus-character paragraphs."""
    rng = random.Random(seed)  # noqa: S311
    words = [
        "ferry", "lantern", "orchard", "copper", "meadow",
        "granite", "harbour", "timber", "kettle", "velvet",
    ]  # fmt: skip
    paragraphs = []
    for _ in range(4):
        sentences = [
            f"The {rng.choice(words)} {rng.choice(words)} stood {rng.randint(2, 99)} paces from "
            f"the {rng.choice(words)} {rng.choice(words)}."
            for _ in range(5)
        ]
        paragraphs.append(" ".join(sentences))
    return "## Overview\n\n" + "\n\n".join(paragraphs)


def make_mirror(missing: set[int] | None = None, ids: int = 8) -> FakeMirror:
    """A mirror with articles 1..ids (minus ``missing``) under distinct titles."""
    names = ["Alpha Quartz Bridge", "Beta Marble Tower", "Gamma Ember Lake", "Delta Frost Gate"]
    missing = missing or set()
    return FakeMirror(
        {
            pid: (f"{names[pid % 4]} {pid}", 1000 + pid, make_text(pid))
            for pid in range(1, ids + 1)
            if pid not in missing
        }
    )


class FakeChat:
    """A model that answers from the prompt, with optional scripted replies first.

    Attributes:
        script: Replies served before the prompt-driven ones; an exception is raised instead.
        calls: ``(model, messages, options)`` of every call.
        jitter: When set, the number of event-loop yields before each reply is drawn from it,
            which reorders completions between slots.
    """

    def __init__(self, script: list[dict[str, Any] | Exception] | None = None, jitter: int = 0):
        """Create the fake; ``jitter`` seeds the yield counts (0 for none)."""
        self.script = list(script or [])
        self.calls: list[tuple[str, list[dict[str, str]], dict[str, Any]]] = []
        self.jitter = random.Random(jitter) if jitter else None  # noqa: S311

    async def chat_json(
        self, model: str, messages: list[dict[str, str]], options: dict[str, Any]
    ) -> dict[str, Any]:
        """Answer one prompt."""
        self.calls.append((model, messages, options))
        if self.jitter is not None:
            for _ in range(self.jitter.randint(0, 6)):
                await asyncio.sleep(0)
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        user = messages[-1]["content"]
        title = re.search(r"Article title: (.*)\n", user).group(1)  # type: ignore[union-attr]
        evidence = user.split("Paragraph:\n", 1)[1].split("\n\n", 1)[0]
        if "Name the article's subject" in user:
            return {
                "question": f"What does the register say about {title} in this entry?",
                "answer": " ".join(evidence.split()[1:4]),
            }
        return {"question": "Which landmark is the subject of the quiet entry below?"}


def run(options: GoldenOptions, mirror: FakeMirror, chat: FakeChat):  # type: ignore[no-untyped-def]
    """Run the generator to completion."""
    return asyncio.run(generate(options, mirror.client(), chat))


def small(**overrides: Any) -> GoldenOptions:
    """Options for a tiny run over the fake mirror's eight ids."""
    values: dict[str, Any] = {"count": 6, "max_page_id": 8, "concurrency": 3}
    values.update(overrides)
    return GoldenOptions(**values)


# -- selection ---------------------------------------------------------------------------------


@pytest.mark.unit
def test_assign_kinds_matches_the_describe_share_and_is_reproducible() -> None:
    kinds = assign_kinds(20, 0.25, 7)

    assert kinds.count("describe") == 5
    assert kinds.count("fact") == 15
    assert kinds == assign_kinds(20, 0.25, 7)


@pytest.mark.unit
def test_skip_reason_disambiguation_title_is_skipped() -> None:
    article = Article(1, 1, "Mercury (disambiguation)", "x" * 2000)

    assert skip_reason(article) == "disambiguation_title"


@pytest.mark.unit
def test_skip_reason_may_refer_to_in_the_lead_is_skipped() -> None:
    article = Article(1, 1, "Mercury", "Mercury may refer to:\n- a planet\n" + "x" * 2000)

    assert skip_reason(article) == "may_refer_to"


@pytest.mark.unit
def test_skip_reason_may_refer_to_far_down_is_not_a_disambiguation() -> None:
    article = Article(1, 1, "Mercury", "x" * 400 + " may refer to: other things " + "y" * 800)

    assert skip_reason(article) is None


@pytest.mark.unit
def test_skip_reason_short_article_is_skipped() -> None:
    assert skip_reason(Article(1, 1, "Stub", "x" * 999)) == "too_short"


@pytest.mark.unit
def test_candidate_paragraphs_drops_headings_and_short_paragraphs() -> None:
    long_paragraph = "Word " * 50
    text = f"## Heading\n\nShort one.\n\n{long_paragraph}\n\n# Another\n## Sub\n\n{long_paragraph}"

    paragraphs = candidate_paragraphs(text)

    assert len(paragraphs) == 2
    assert all(len(p) >= 200 and "#" not in p for p in paragraphs)


@pytest.mark.unit
def test_candidate_paragraphs_heading_line_inside_a_block_is_dropped_but_body_kept() -> None:
    body = "Sentence about the harbour. " * 12

    paragraphs = candidate_paragraphs(f"## Harbour\n{body}")

    assert paragraphs == [" ".join(body.split())]


@pytest.mark.unit
def test_trim_evidence_short_paragraph_is_unchanged() -> None:
    assert trim_evidence(EVIDENCE) == EVIDENCE


@pytest.mark.unit
def test_trim_evidence_long_paragraph_ends_at_a_sentence_within_the_limit() -> None:
    paragraph = " ".join(f"Sentence {i} ends here." for i in range(80))

    trimmed = trim_evidence(paragraph)

    assert len(trimmed) <= 700
    assert trimmed.endswith(".")
    assert paragraph.startswith(trimmed)


@pytest.mark.unit
def test_trim_evidence_without_a_sentence_end_cuts_at_a_word_boundary() -> None:
    paragraph = " ".join(f"word{i}" for i in range(300))

    trimmed = trim_evidence(paragraph)

    assert len(trimmed) <= 700
    assert paragraph.startswith(trimmed)
    assert paragraph[len(trimmed)] == " "


# -- validation --------------------------------------------------------------------------------

GOOD_FACT = "When did the Alpha Quartz Bridge open to the public?"


@pytest.mark.unit
def test_validate_question_sound_fact_passes() -> None:
    assert validate_question("fact", GOOD_FACT, "1932", TITLE, EVIDENCE) is None


@pytest.mark.unit
def test_validate_question_sound_describe_passes() -> None:
    question = "Which crossing opened in 1932 with a steel deck?"

    assert validate_question("describe", question, None, TITLE, EVIDENCE) is None


@pytest.mark.unit
@pytest.mark.parametrize("question", [None, 5, ["a"]])
def test_validate_question_non_string_question_is_malformed(question: object) -> None:
    assert validate_question("fact", question, "1932", TITLE, EVIDENCE) == "malformed"


@pytest.mark.unit
def test_validate_question_fact_with_non_string_answer_is_malformed() -> None:
    assert validate_question("fact", GOOD_FACT, 1932, TITLE, EVIDENCE) == "malformed"


@pytest.mark.unit
@pytest.mark.parametrize("question", ["Why?", "x" * 301, "              "])
def test_validate_question_length_outside_15_to_300_is_rejected(question: str) -> None:
    assert validate_question("fact", question, "1932", TITLE, EVIDENCE) == "question_length"


@pytest.mark.unit
def test_validate_question_copying_a_five_word_run_is_rejected() -> None:
    question = "Why did engineers chose weathering steel for the deck of the bridge?"

    assert validate_question("fact", question, "1932", TITLE, EVIDENCE) == "copies_evidence"


@pytest.mark.unit
def test_validate_question_four_shared_words_in_a_row_is_allowed() -> None:
    question = "Which bridge chose weathering steel for decks?"

    assert validate_question("fact", question, "1932", TITLE, EVIDENCE) is None


@pytest.mark.unit
def test_validate_question_copy_check_ignores_case_and_spacing() -> None:
    question = "WHY  were the river   pilings were sunk into bedrock?"

    assert validate_question("fact", question, "1932", TITLE, EVIDENCE) == "copies_evidence"


@pytest.mark.unit
def test_validate_question_fact_answer_absent_from_evidence_is_rejected() -> None:
    assert (
        validate_question("fact", GOOD_FACT, "1957", TITLE, EVIDENCE) == "answer_not_in_evidence"
    )


@pytest.mark.unit
def test_validate_question_fact_answer_matching_ignores_case_and_whitespace() -> None:
    assert validate_question("fact", GOOD_FACT, "Weathering\n STEEL", TITLE, EVIDENCE) is None


@pytest.mark.unit
def test_validate_question_fact_answer_inside_the_question_is_rejected() -> None:
    question = "Was the weathering steel deck the reason the bridge needed no paint?"

    assert validate_question("fact", question, "weathering steel", TITLE, EVIDENCE) == (
        "answer_in_question"
    )


@pytest.mark.unit
def test_validate_question_fact_answer_over_eight_words_is_rejected() -> None:
    answer = "the span opened to traffic in 1932 after four"

    assert validate_question("fact", GOOD_FACT, answer, TITLE, EVIDENCE) == "answer_length"


@pytest.mark.unit
def test_validate_question_fact_empty_answer_is_rejected() -> None:
    assert validate_question("fact", GOOD_FACT, "  ", TITLE, EVIDENCE) == "answer_length"


@pytest.mark.unit
def test_validate_question_describe_with_a_title_word_is_rejected() -> None:
    question = "Which Quartz crossing opened in 1932 with a steel deck?"

    assert validate_question("describe", question, None, TITLE, EVIDENCE) == (
        "title_word_in_question"
    )


@pytest.mark.unit
def test_validate_question_describe_ignores_title_words_under_four_letters() -> None:
    question = "Which structure opened in 1932 using a steel deck?"

    assert validate_question("describe", question, None, "The Dam", EVIDENCE) is None


# -- generation --------------------------------------------------------------------------------


@pytest.mark.contract
def test_generate_writes_the_requested_cases_with_ids_in_order_and_kinds_mixed() -> None:
    cases, stats = run(small(count=8, describe_share=0.25), make_mirror(), FakeChat())

    assert [c.id for c in cases] == [f"wg-{i:04d}" for i in range(1, len(cases) + 1)]
    assert stats["written"] == len(cases) > 0
    assert {c.kind for c in cases} <= {"fact", "describe"}
    assert all(c.revision_id == 1000 + c.page_id for c in cases)


@pytest.mark.contract
def test_generate_describe_case_answer_is_the_title_and_fact_answer_is_in_the_evidence() -> None:
    cases, _ = run(small(count=8, describe_share=0.5), make_mirror(), FakeChat())

    for case in cases:
        if case.kind == "describe":
            assert case.answer == case.title
        else:
            assert case.answer.casefold() in case.evidence.casefold()
        assert len(case.evidence) <= 700


@pytest.mark.contract
def test_generate_missing_pages_are_counted_not_found_and_skipped() -> None:
    mirror = make_mirror(missing={1, 2, 3, 4})

    cases, stats = run(small(count=4), mirror, FakeChat())

    assert stats["not_found"] > 0
    assert all(c.page_id not in {1, 2, 3, 4} for c in cases)


@pytest.mark.contract
def test_generate_skips_disambiguation_may_refer_and_short_articles() -> None:
    mirror = make_mirror(ids=3)
    mirror.articles[4] = ("Foo (disambiguation)", 4, make_text(4))
    mirror.articles[5] = ("Foo", 5, "Foo may refer to:\n" + make_text(5))
    mirror.articles[6] = ("Stub", 6, "tiny")

    cases, stats = run(small(count=16, max_page_id=6), mirror, FakeChat())

    assert {c.page_id for c in cases} <= {1, 2, 3}
    assert stats["skipped_disambiguation_title"] > 0
    assert stats["skipped_may_refer_to"] > 0
    assert stats["skipped_too_short"] > 0


@pytest.mark.contract
def test_generate_bad_json_attempt_is_counted_and_asked_again() -> None:
    chat = FakeChat(script=[RouterContentError("not json")])

    cases, stats = run(small(count=1), make_mirror(), chat)

    assert stats["rejected_bad_json"] == 1
    assert len(cases) == 1


@pytest.mark.contract
def test_generate_rule_violation_is_counted_by_rule_and_retried() -> None:
    chat = FakeChat(script=[{"question": "Why?", "answer": "x"}])

    cases, stats = run(small(count=1, describe_share=0.0), make_mirror(), chat)

    assert stats["rejected_question_length"] == 1
    assert len(cases) == 1


@pytest.mark.contract
def test_generate_three_failed_attempts_move_on_to_another_article() -> None:
    chat = FakeChat(script=[{"question": "Why?", "answer": "x"}] * 3)

    cases, stats = run(small(count=1, describe_share=0.0), make_mirror(), chat)

    assert stats["rejected_question_length"] == 3
    assert stats["articles_drawn"] >= 2
    assert len(cases) == 1


@pytest.mark.contract
def test_generate_a_model_that_never_passes_exhausts_the_slot_and_writes_nothing() -> None:
    class Hopeless(FakeChat):
        async def chat_json(self, model, messages, options):  # type: ignore[no-untyped-def]
            return {"question": "Why?"}

    cases, stats = run(small(count=1, max_page_id=2), make_mirror(ids=2), Hopeless())

    assert cases == []
    assert stats["slots_exhausted"] == 1
    assert stats["written"] == 0


@pytest.mark.contract
def test_generate_router_failure_propagates_instead_of_thinning_the_set() -> None:
    chat = FakeChat(script=[RouterError("router answered 503 on all 5 attempts")])

    with pytest.raises(RouterError):
        run(small(count=2), make_mirror(), chat)


@pytest.mark.contract
def test_generate_page_whose_read_fails_after_retries_is_skipped_and_counted() -> None:
    async def no_sleep(seconds: float) -> None:
        return None

    mirror = make_mirror()
    mirror.fail_first = 3  # one page's three attempts all answer 500

    cases, stats = asyncio.run(
        generate(small(count=3), mirror.client(), FakeChat(), sleep=no_sleep)
    )

    assert stats["read_failures"] == 1
    assert len(cases) == 3


@pytest.mark.contract
def test_generate_dead_mirror_raises_after_consecutive_read_failures() -> None:
    async def no_sleep(seconds: float) -> None:
        return None

    mirror = make_mirror()
    mirror.fail_first = 100_000

    with pytest.raises(MirrorServiceError):
        asyncio.run(generate(small(count=2), mirror.client(), FakeChat(), sleep=no_sleep))


@pytest.mark.contract
def test_main_failure_output_is_the_status_never_the_mirrors_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        """A 422 whose detail echoes what was asked."""
        return httpx.Response(422, json={"error": "invalid_request", "detail": "SECRET QUERY"})

    class FakeRouterClient:
        """Stands in for ``RouterClient``."""

        def __init__(self, base_url: str) -> None:
            """Ignore the address."""

        async def __aenter__(self) -> FakeChat:
            return FakeChat()

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(wiki_golden, "RouterClient", FakeRouterClient)
    monkeypatch.setattr(
        wiki_golden,
        "MirrorClient",
        lambda **_: MirrorClient(
            base_url="http://m.test", transport=httpx.MockTransport(refuse)
        ),
    )

    code = wiki_golden.main(["--out", str(tmp_path / "g.jsonl"), "--count", "1"])

    err = capsys.readouterr().err
    assert code == 1
    assert "status 422" in err
    assert "SECRET" not in err


@pytest.mark.contract
def test_generate_two_slots_drawing_the_same_article_keep_only_the_first() -> None:
    cases, stats = run(small(count=3, max_page_id=1), make_mirror(ids=1), FakeChat())

    assert len(cases) == 1
    assert stats["duplicate_article"] == 2


@pytest.mark.contract
def test_generate_output_does_not_depend_on_the_order_replies_complete() -> None:
    first, _ = run(small(count=10), make_mirror(), FakeChat(jitter=1))
    second, _ = run(small(count=10), make_mirror(), FakeChat(jitter=2))
    plain, _ = run(small(count=10), make_mirror(), FakeChat())

    assert first == second == plain


@pytest.mark.contract
def test_generate_different_seeds_draw_different_articles() -> None:
    a, _ = run(small(count=6, seed=1), make_mirror(), FakeChat())
    b, _ = run(small(count=6, seed=2), make_mirror(), FakeChat())

    assert [c.page_id for c in a] != [c.page_id for c in b]


@pytest.mark.contract
def test_generate_asks_the_model_with_the_documented_sampling_options() -> None:
    chat = FakeChat()

    run(small(count=1, model="gemma3:12b"), make_mirror(), chat)

    model, _, options = chat.calls[0]
    assert model == "gemma3:12b"
    assert options["temperature"] == 0.3
    assert options["num_predict"] == 200
    assert isinstance(options["seed"], int)


# -- output ------------------------------------------------------------------------------------


@pytest.mark.unit
def test_write_cases_writes_jsonl_with_the_documented_fields_and_leaves_no_temp(
    tmp_path: Path,
) -> None:
    cases, _ = run(small(count=3), make_mirror(), FakeChat())
    out = tmp_path / "golden.jsonl"

    write_cases(out, cases)

    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [set(line) for line in lines] == [
        {"id", "kind", "question", "answer", "page_id", "revision_id", "title", "evidence"}
    ] * len(cases)
    assert [line["id"] for line in lines] == sorted(line["id"] for line in lines)
    assert [p.name for p in tmp_path.iterdir()] == ["golden.jsonl"]


@pytest.mark.contract
def test_main_prints_counts_only_and_writes_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mirror = make_mirror()
    chat = FakeChat()

    class FakeRouterClient:
        """Stands in for ``RouterClient`` with the fake model."""

        def __init__(self, base_url: str) -> None:
            """Ignore the address."""

        async def __aenter__(self) -> FakeChat:
            return chat

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(wiki_golden, "RouterClient", FakeRouterClient)
    monkeypatch.setattr(wiki_golden, "MirrorClient", lambda **_: mirror.client())
    out = tmp_path / "golden.jsonl"

    code = wiki_golden.main(
        ["--out", str(out), "--count", "4", "--max-page-id", "8", "--concurrency", "2"]
    )

    captured = capsys.readouterr()
    written = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert code == 0
    assert re.fullmatch(r"(?:[a-z_]+=\d+\n?)+", captured.out)
    assert f"written={len(written)}" in captured.out
    for case in written:
        assert case["title"] not in captured.out
        assert case["question"] not in captured.out
