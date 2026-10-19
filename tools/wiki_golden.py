r"""Generate a golden set of questions with known answer articles from the Wikipedia mirror.

The W2 semantic index is judged by one number: the share of questions whose answer article is in
the top five of hybrid retrieval (done-check: at least 90%). That number is only as honest as the
questions. This tool draws random articles from the live mirror, picks a paragraph of each as the
*evidence*, and asks a local LLM (through an Ollama-compatible router) for a question that
paragraph answers.
The model proposes; deterministic rules dispose - a question that copies the paragraph, gives its
own answer away, or names the title in a "describe it" question is rejected, because a lexical
giveaway would measure string matching and not retrieval.

Usage::

    python tools/wiki_golden.py --out /var/tmp/wiki_golden.jsonl
    python tools/wiki_golden.py --out golden.jsonl --count 50 --describe-share 0.5

Two kinds are written, in the proportion ``--describe-share``:

* ``fact``: a question naming the article's subject whose answer is a short phrase copied from
  the evidence paragraph.
* ``describe``: a question describing the subject from the paragraph's facts without naming it,
  whose answer is the title. (This is the kind that needs semantic retrieval; a keyword index has
  nothing to match.)

Output is JSONL, one object per line, ids ``wg-0001``..., sorted by id, written atomically::

    {"id","kind","question","answer","page_id","revision_id","title","evidence"}

**Deterministic given the seed.** Every case slot is assigned its kind and its own random streams
(page ids, paragraph choice, the model's ``seed`` option) up front, so the order in which async
calls complete cannot change the output. Duplicate articles across slots are resolved by keeping
the lowest slot. The model itself may still vary between runs on different hardware.

**Page ids are sparse**: about 8.6% of the ids in [1, ``--max-page-id``] are articles, so drawing
a uniform integer and skipping the 404s yields a uniform sample of articles.

Non-obvious constraints:

* **stdout carries counts only** (articles drawn, 404s, skips by reason, rejects by rule,
  written). Questions, titles and text go to the output file and nowhere else, never a log.
* Meant to run on a client box with no ``zstandard``: nothing here imports the mirror's
  index, build or store modules.
* A router that stays down raises and stops the run (a half-sampled set silently skewed toward
  the slots that happened to finish would be worse than none); a model that returns bad JSON
  only rejects that attempt.

See docs/DESIGN.md section 12.5.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final, Protocol

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
# The checkout root, so a sibling script is imported as ``tools.<name>``.
sys.path.insert(0, str(_REPO_ROOT))

from tools.wiki_common import (  # noqa: E402
    DEFAULT_MAX_PAGE_ID,
    DEFAULT_MIRROR,
    DEFAULT_ROUTER,
    Article,
    ArticleReadError,
    MirrorClient,
    MirrorServiceError,
    ReadGuard,
    RouterClient,
    RouterContentError,
    RouterError,
    Sleep,
    describe_failure,
    fold,
    new_request_id,
    page_id_stream,
)

__all__ = [
    "GoldenCase",
    "GoldenOptions",
    "assign_kinds",
    "build_messages",
    "candidate_paragraphs",
    "generate",
    "skip_reason",
    "trim_evidence",
    "validate_question",
]

#: Articles under this many characters are skipped: too little to hold a paragraph worth asking
#: about, and mostly stubs (the brief's rule).
MIN_ARTICLE_CHARS: Final = 1_000

#: Disambiguation pages state "may refer to:" in their lead; looked for in this many leading
#: characters, so an article that merely says it later is not discarded.
_MAY_REFER_WINDOW: Final = 300

#: Shortest paragraph used as evidence; below this the paragraph is a caption or a one-liner.
MIN_PARAGRAPH_CHARS: Final = 200

#: Longest evidence kept, trimmed to a sentence end. Bounds the prompt and keeps the evidence
#: inside a single packed passage in the schemes the evaluation compares (1024 chars and up).
MAX_EVIDENCE_CHARS: Final = 700

#: Question length window, characters: shorter is a fragment, longer is a pasted paragraph.
QUESTION_MIN_CHARS: Final = 15
QUESTION_MAX_CHARS: Final = 300

#: A question must not share this many consecutive words with the evidence (a question that
#: quotes the passage matches by string overlap and measures nothing).
MAX_SHARED_WORDS: Final = 5

#: A fact answer is a short phrase: at most this many words.
MAX_ANSWER_WORDS: Final = 8

#: Title words at least this long may not appear in a describe question (shorter ones are
#: articles, prepositions and initials that any sentence contains).
MIN_TITLE_TOKEN_LETTERS: Final = 4

#: Model attempts per article before another article is drawn (the brief).
ATTEMPTS_PER_ARTICLE: Final = 3

#: Page ids one slot may draw before giving up. About one id in twelve is an article, so this
#: allows roughly fifty articles per slot; a mirror that answers 404 to everything cannot loop
#: forever.
MAX_IDS_PER_SLOT: Final = 600

#: Sampling settings from the brief: a little temperature for variety, 200 tokens is ample for
#: a question and a short answer.
TEMPERATURE: Final = 0.3
NUM_PREDICT: Final = 200

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_WORD = re.compile(r"\w+")
_LETTER_RUN = re.compile(r"[^\W\d_]+")
# A sentence end: terminal punctuation, optional closing quote or bracket, then space or end.
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*(?=\s|$)")

_SYSTEM = (
    "You write test questions for a Wikipedia search engine. "
    "Reply with one JSON object and nothing else."
)


class ChatRouter(Protocol):
    """What the generator needs of the router: a JSON chat call (``RouterClient`` provides it)."""

    async def chat_json(
        self, model: str, messages: list[dict[str, str]], options: dict[str, Any]
    ) -> dict[str, Any]:
        """Ask a model for a JSON object."""
        ...


@dataclass(frozen=True)
class GoldenCase:
    """One golden question and where its answer lives; the JSONL line, field for field."""

    id: str
    kind: str
    question: str
    answer: str
    page_id: int
    revision_id: int
    title: str
    evidence: str


@dataclass(frozen=True)
class GoldenOptions:
    """Everything that shapes a run, apart from the clients."""

    count: int = 300
    describe_share: float = 0.25
    seed: int = 20261019
    model: str = "gemma3:12b"
    concurrency: int = 4
    max_page_id: int = DEFAULT_MAX_PAGE_ID


@dataclass(frozen=True)
class _Draft:
    """A validated case before it has an id: the slot it came from orders the output."""

    slot: int
    kind: str
    question: str
    answer: str
    article: Article
    evidence: str


def assign_kinds(count: int, describe_share: float, seed: int) -> list[str]:
    """Decide up front which slots are ``describe`` and which ``fact``.

    Args:
        count: Number of case slots.
        describe_share: Share of ``describe`` slots, 0 to 1; rounded to a whole number of slots.
        seed: Shuffle seed, so the kinds are interleaved but reproducible.

    Returns:
        One kind per slot.
    """
    describe = round(count * describe_share)
    kinds = ["describe"] * describe + ["fact"] * (count - describe)
    random.Random(f"{seed}:kinds").shuffle(kinds)  # noqa: S311 - sampling, not security
    return kinds


def skip_reason(article: Article) -> str | None:
    """Why an article is not a candidate, or None when it is.

    Args:
        article: A whole article read from the mirror.

    Returns:
        ``disambiguation_title``, ``may_refer_to`` or ``too_short``; None if usable.
    """
    if article.title.strip().casefold().endswith("(disambiguation)"):
        return "disambiguation_title"
    if "may refer to:" in article.text[:_MAY_REFER_WINDOW].casefold():
        return "may_refer_to"
    if len(article.text) < MIN_ARTICLE_CHARS:
        return "too_short"
    return None


def candidate_paragraphs(text: str) -> list[str]:
    """The paragraphs of an article that can serve as evidence.

    Paragraphs are split on blank lines; lines starting with ``#`` (markdown headings) are
    dropped, whitespace is collapsed, and paragraphs under ``MIN_PARAGRAPH_CHARS`` are dropped.

    Args:
        text: The article text.

    Returns:
        The surviving paragraphs, in document order.
    """
    paragraphs: list[str] = []
    for block in _PARAGRAPH_BREAK.split(text):
        lines = [line for line in block.splitlines() if not line.lstrip().startswith("#")]
        paragraph = " ".join(" ".join(lines).split())
        if len(paragraph) >= MIN_PARAGRAPH_CHARS:
            paragraphs.append(paragraph)
    return paragraphs


def trim_evidence(paragraph: str) -> str:
    """Trim a paragraph to at most ``MAX_EVIDENCE_CHARS``, ending at a sentence end.

    A paragraph that fits is returned as is. Otherwise it is cut at the last sentence end that
    leaves at least ``MIN_PARAGRAPH_CHARS``; when there is none, at the last word boundary, so
    the evidence never ends mid-word.

    Args:
        paragraph: A whitespace-collapsed paragraph.

    Returns:
        The evidence text.
    """
    if len(paragraph) <= MAX_EVIDENCE_CHARS:
        return paragraph
    head = paragraph[:MAX_EVIDENCE_CHARS]
    ends = [m.end() for m in _SENTENCE_END.finditer(head) if m.end() >= MIN_PARAGRAPH_CHARS]
    if ends:
        return head[: ends[-1]]
    return head.rsplit(" ", 1)[0]


def build_messages(kind: str, title: str, evidence: str) -> list[dict[str, str]]:
    """The chat prompt that asks for one question about the evidence.

    Args:
        kind: ``fact`` or ``describe``.
        title: The article title (the model is told it; a ``describe`` question must avoid it).
        evidence: The paragraph the question has to be answerable from.

    Returns:
        System and user messages.
    """
    if kind == "describe":
        task = (
            "Write one question whose answer is the article's subject (its title). The "
            "question must describe the subject using facts from the paragraph, without "
            "naming it and without using any word of the title. Do not copy more than four "
            "consecutive words from the paragraph.\n"
            'Reply as JSON: {"question": "..."}'
        )
    else:
        task = (
            "Write one question a curious person would ask that this paragraph answers. Name "
            "the article's subject explicitly in the question (never 'he', 'she', 'it' or "
            "'they' for it). Do not copy more than four consecutive words from the paragraph. "
            "Give the answer as a short phrase of 1 to 8 words copied exactly from the "
            "paragraph.\n"
            'Reply as JSON: {"question": "...", "answer": "..."}'
        )
    user = f"Article title: {title}\n\nParagraph:\n{evidence}\n\n{task}"
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


def _shares_word_run(question: str, evidence: str, run: int) -> bool:
    """Whether ``question`` and ``evidence`` have ``run`` consecutive words in common."""
    q_words = _WORD.findall(fold(question))
    if len(q_words) < run:
        return False
    e_words = _WORD.findall(fold(evidence))
    seen = {tuple(e_words[i : i + run]) for i in range(len(e_words) - run + 1)}
    return any(tuple(q_words[i : i + run]) in seen for i in range(len(q_words) - run + 1))


def validate_question(
    kind: str, question: object, answer: object, title: str, evidence: str
) -> str | None:
    """Apply the deterministic rules to one model proposal.

    Args:
        kind: ``fact`` or ``describe``.
        question: What the model wrote under ``question`` (any JSON value).
        answer: What it wrote under ``answer``; ignored for ``describe`` (the answer is the title).
        title: The article title.
        evidence: The evidence paragraph.

    Returns:
        The name of the first rule violated, or None when the proposal is sound. Rule names:
        ``malformed``, ``question_length``, ``copies_evidence``, ``answer_length``,
        ``answer_not_in_evidence``, ``answer_in_question`` (fact) and ``title_word_in_question``
        (describe).
    """
    if not isinstance(question, str) or (kind == "fact" and not isinstance(answer, str)):
        return "malformed"
    if not QUESTION_MIN_CHARS <= len(question.strip()) <= QUESTION_MAX_CHARS:
        return "question_length"
    if _shares_word_run(question, evidence, MAX_SHARED_WORDS):
        return "copies_evidence"
    if kind == "describe":
        words = set(_WORD.findall(fold(question)))
        for token in _LETTER_RUN.findall(title.casefold()):
            if len(token) >= MIN_TITLE_TOKEN_LETTERS and token in words:
                return "title_word_in_question"
        return None
    assert isinstance(answer, str)  # narrowed by the malformed check above
    folded = fold(answer)
    if not folded or len(folded.split()) > MAX_ANSWER_WORDS:
        return "answer_length"
    if folded not in fold(evidence):
        return "answer_not_in_evidence"
    if folded in fold(question):
        return "answer_in_question"
    return None


async def _fill_slot(
    slot: int,
    kind: str,
    options: GoldenOptions,
    mirror: MirrorClient,
    router: ChatRouter,
    stats: Counter[str],
    guard: ReadGuard,
) -> _Draft | None:
    """Produce the validated case for one slot, or None when its id budget ran out.

    The slot owns its random streams, derived from the seed and the slot number alone, so the
    result does not depend on how the event loop interleaves slots. A page whose read fails
    after retries is skipped and counted (``read_failures``); ``guard`` stops the run if the
    failures never end.
    """
    ids = page_id_stream(f"{options.seed}:{slot}:ids", options.max_page_id)
    rng = random.Random(f"{options.seed}:{slot}:draw")  # noqa: S311 - sampling, not security
    for _ in range(MAX_IDS_PER_SLOT):
        read = await guard.read(
            mirror, next(ids), request_id=new_request_id(f"wiki-golden-{slot}")
        )
        if read.failed:
            stats["read_failures"] += 1
            continue
        article = read.article
        if article is None:
            stats["not_found"] += 1
            continue
        stats["articles_drawn"] += 1
        reason = skip_reason(article)
        if reason is not None:
            stats[f"skipped_{reason}"] += 1
            continue
        paragraphs = candidate_paragraphs(article.text)
        if not paragraphs:
            stats["skipped_no_paragraph"] += 1
            continue
        for _attempt in range(ATTEMPTS_PER_ARTICLE):
            evidence = trim_evidence(rng.choice(paragraphs))
            sampling = {
                "temperature": TEMPERATURE,
                "num_predict": NUM_PREDICT,
                # Pinned per slot and attempt so a re-run asks the model the same thing.
                "seed": rng.randrange(2**31),
            }
            try:
                reply = await router.chat_json(
                    options.model, build_messages(kind, article.title, evidence), sampling
                )
            except RouterContentError:
                stats["rejected_bad_json"] += 1
                continue
            rule = validate_question(
                kind, reply.get("question"), reply.get("answer"), article.title, evidence
            )
            if rule is not None:
                stats[f"rejected_{rule}"] += 1
                continue
            question = str(reply["question"]).strip()
            answer = article.title if kind == "describe" else str(reply["answer"]).strip()
            return _Draft(slot, kind, question, answer, article, evidence)
    stats["slots_exhausted"] += 1
    return None


async def generate(
    options: GoldenOptions,
    mirror: MirrorClient,
    router: ChatRouter,
    *,
    sleep: Sleep = asyncio.sleep,
) -> tuple[list[GoldenCase], Counter[str]]:
    """Generate the golden set.

    Args:
        options: Count, mix, seed, model and concurrency.
        mirror: The mirror client.
        router: The chat router.
        sleep: Awaited between mirror retries; tests inject a recorder.

    Returns:
        The cases, ids ``wg-0001``... in slot order, and the run's counters. Fewer than
        ``options.count`` come back when a slot ran out of its id budget or two slots drew the
        same article (the later slot is dropped, ``duplicate_article``).

    Raises:
        MirrorServiceError: The mirror failed on many pages in a row (one bad page is skipped).
        RouterError: The router failed for good (bad JSON from the model does not raise).
    """
    kinds = assign_kinds(options.count, options.describe_share, options.seed)
    stats: Counter[str] = Counter()
    gate = asyncio.Semaphore(max(1, options.concurrency))
    guard = ReadGuard(sleep=sleep)

    async def run(slot: int) -> _Draft | None:
        """One slot under the concurrency gate."""
        async with gate:
            return await _fill_slot(slot, kinds[slot], options, mirror, router, stats, guard)

    drafts = await asyncio.gather(*(run(slot) for slot in range(options.count)))
    cases: list[GoldenCase] = []
    seen: set[int] = set()
    for draft in drafts:  # gather preserves slot order whatever order the calls finished in
        if draft is None:
            continue
        if draft.article.page_id in seen:
            stats["duplicate_article"] += 1
            continue
        seen.add(draft.article.page_id)
        cases.append(
            GoldenCase(
                id=f"wg-{len(cases) + 1:04d}",
                kind=draft.kind,
                question=draft.question,
                answer=draft.answer,
                page_id=draft.article.page_id,
                revision_id=draft.article.revision_id,
                title=draft.article.title,
                evidence=draft.evidence,
            )
        )
    stats["written"] = len(cases)
    return cases, stats


def write_cases(path: Path, cases: Sequence[GoldenCase]) -> None:
    """Write the cases as JSONL atomically: a temp file beside the target, then a rename.

    Args:
        path: The output file; its directory must exist.
        cases: The cases, already in id order.
    """
    temp = path.with_name(path.name + ".tmp")
    lines = [json.dumps(asdict(case), ensure_ascii=False) for case in cases]
    temp.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    temp.replace(path)


def format_counts(stats: Mapping[str, int]) -> str:
    """Counters as ``key=value`` lines, sorted, for stdout (counts only, no text)."""
    return "\n".join(f"{key}={stats[key]}" for key in sorted(stats))


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description="Generate a Wikipedia golden question set.")
    parser.add_argument("--out", type=Path, required=True, help="JSONL file to write")
    parser.add_argument("--count", type=int, default=300)
    parser.add_argument("--describe-share", type=float, default=0.25)
    parser.add_argument("--seed", type=int, default=20261019)
    parser.add_argument("--mirror", default=DEFAULT_MIRROR)
    parser.add_argument("--router", default=DEFAULT_ROUTER)
    parser.add_argument("--model", default="gemma3:12b")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-page-id", type=int, default=DEFAULT_MAX_PAGE_ID)
    args = parser.parse_args(argv)
    if args.count < 1 or args.concurrency < 1 or args.max_page_id < 1:
        parser.error("--count, --concurrency and --max-page-id must be positive")
    if not 0.0 <= args.describe_share <= 1.0:
        parser.error("--describe-share must be between 0 and 1")
    if not args.out.parent.is_dir():
        parser.error("the directory of --out does not exist")
    return args


async def _run(args: argparse.Namespace) -> int:
    """Wire the real clients, generate, write, and print the counters."""
    options = GoldenOptions(
        count=args.count,
        describe_share=args.describe_share,
        seed=args.seed,
        model=args.model,
        concurrency=args.concurrency,
        max_page_id=args.max_page_id,
    )
    mirror = MirrorClient(base_url=args.mirror, timeout_seconds=30.0)
    async with RouterClient(args.router) as router:
        try:
            cases, stats = await generate(options, mirror, router)
        except (MirrorServiceError, RouterError, ArticleReadError) as exc:
            # Status and error code only: a 422 body from the mirror can echo the question.
            print(f"stopped: {describe_failure(exc)}", file=sys.stderr)
            return 1
    write_cases(args.out, cases)
    print(format_counts(stats))
    return 0 if cases else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: ``python tools/wiki_golden.py --out PATH``.

    Args:
        argv: Arguments; the process's when omitted.

    Returns:
        0 when at least one case was written, 1 otherwise.
    """
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
