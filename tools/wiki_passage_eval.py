r"""Score Wikipedia passage-length schemes against a golden set: lexical, dense and hybrid.

The Wikipedia mirror's keyword index cuts every article into ~720-character passages (41.5M of
them). A semantic index would embed every passage, and the cost - disk, RAM, embedding hours -
scales with the passage count. Packing consecutive pieces into passages of up to 1024, 1536 or
2048 characters cuts the count to 30.1M, 25.1M and 21.6M. This tool measures whether retrieval
quality holds, so the W2 plan can pick a length on evidence (docs/DESIGN.md section 12.5).

Usage (on a client box that reaches both the mirror and the router)::

    systemd-run -p MemoryMax=6G --wait --pipe .venv/bin/python tools/wiki_passage_eval.py \
        --golden /var/tmp/wiki_golden.jsonl

What it does, in order:

1. **Sample corpus**, the same for every scheme: the golden articles, plus each question's top
   ``--negatives-per-question`` articles from the live mirror search (hard negatives), plus
   uniform random articles up to ``--articles`` in all. Texts are cached in the work directory
   (gzip JSONL), so a re-run fetches nothing it already has.
2. **Per scheme**, cut every article with ``cut_passages(text, pack_chars=s)`` (0 is the live v1
   cut), embed the passages and the questions through the router's ``/api/embed``, and rank the
   questions in five modes: ``lexical`` (the mirror's BM25 with Qdrant's IDF
   modifier, over an inverted index), ``dense`` (exact cosine), ``dense_binary`` (sign bits,
   Hamming candidates, float rescore), ``dense_int8`` (per-dimension int8, dot-product
   candidates, float rescore) and ``hybrid`` (RRF, k=60, of lexical and exact dense).
3. **Collapse** each passage ranking (1000 deep, so v1's many passages per article cannot push the
   gold article out) to articles - an article ranks at its best passage, as the live search
   does - and score against the golden set: article hit@1, hit@5, MRR@10, then three
   answer-location metrics, all requiring the gold article first:

   * ``answer_in_passage@1``: its best passage holds the target. The target is the golden
     ``answer`` phrase for ``fact`` cases and the middle 120 characters of the evidence for
     ``describe`` cases (whose answer is the title). Longer passages hold more by length alone,
     so each scheme also reports the **chance baseline**: the average share of the gold
     article's passages that hold the target, which is what a random passage would score.
   * ``answer_in_snippet@1`` (``live``): the target is in the 320-character snippet the
     service would show, cut exactly as ``search._snippet`` does from the best passage.
   * ``answer_in_snippet@1`` (``focused``): the target is in the best 320-character window of
     the best passage, chosen by query-term overlap (what a snippet could show if it were
     chosen for the query).

   ``evidence_given_hit5`` keeps its name and uses the same target.
4. **Two views of every scheme and mode**: ``with_negatives`` (the whole corpus) and
   ``random_only``, which masks the passages of articles in the corpus only as hard negatives.
   The negatives come from the live v1 keyword ranker and so flatter packed schemes; the random
   view shows the effect. Both come from the same embeddings; only the candidate set changes.
5. **Paired comparison** against v1 (scheme 0, when it is run): per scheme, mode and view, the
   cases whose hit@5 and focused-snippet answer went miss to hit and hit to miss.

Per scheme also: passage count, mean passage length, the share of cases whose evidence is in no
passage of the gold article (a sanity number, expected ~0), the share of queries with fewer than
10 distinct articles in their top 100 passages, and the recall@10 of the binary and int8 modes
against exact dense.

Output: a table on stdout and ``report_<UTC timestamp>.json`` in the work directory with every
metric and per-case booleans keyed by case id.

Non-obvious constraints:

* **Embedding canary.** The vector cache is keyed by model and text, not by which Ollama build
  made the vector, and the embedding nodes may run different builds. Each run first embeds one
  fixed text 16 times concurrently (one request each, so it spreads over nodes) and stops when
  the vectors disagree, or disagree with the canary stored by the first run, below cosine
  0.9995. Delete ``<work_dir>/vectors`` and re-run after a node upgrade.
* **IDF comes from the random-fill articles only** (per scheme), applied to every passage: the
  hard negatives are chosen by lexical rank and would skew the document frequency of exactly
  the query terms.
* **Binary and int8 results are an upper bound on a small corpus**, not the quantization
  decision, which needs a Qdrant measurement at scale. They are labelled so in the outputs.

* **stdout carries metrics and counts only**: never a question, a title or article text. The
  report file holds case ids and booleans, not text. Texts live only in the work-directory caches.
* **No ``zstandard``, no scipy.** The client box has numpy and httpx. Passage cutting comes from
  ``wiki_mirror.passages``, which imports neither the mirror index nor its store.
* **16 requests in flight, batches of 8, is the embedding pool's measured knee** (docs/DESIGN.md
  section 12.5); more adds latency without throughput and would starve live traffic, so the
  tool refuses a larger ``--in-flight``.
* **Vector cache** keys are ``sha256(model + text)``, so schemes that share a passage (many
  short articles are one passage under every scheme) and re-runs embed it once. Delete the work
  directory when the mirror's snapshot changes: the article cache is not keyed by snapshot.
* **Run under ``systemd-run -p MemoryMax=6G``**: a scheme holds ~90K x 1024 float32 (370 MB) plus
  its quantised copies; they are released before the next scheme.

See docs/DESIGN.md section 12.5.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import math
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

import numpy as np
from numpy.typing import NDArray

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
    ReadResult,
    RouterClient,
    RouterError,
    Sleep,
    describe_failure,
    fold,
    new_request_id,
    page_id_stream,
    search_ids,
)
from tools.wiki_golden import GoldenCase  # noqa: E402
from wiki_mirror.passages import cut_passages  # noqa: E402
from wiki_mirror.text.sparse import SparseEncoder, tokenize  # noqa: E402

__all__ = [
    "MODES",
    "CanaryError",
    "CaseResult",
    "EvalOptions",
    "LexicalIndex",
    "VectorCache",
    "add_paired",
    "binary_rankings",
    "bm25_idf",
    "chance_baseline",
    "check_canary",
    "collapse_to_articles",
    "dense_rankings",
    "embed_texts",
    "evidence_in_passage",
    "few_articles_share",
    "focused_snippet",
    "int8_rankings",
    "judge_case",
    "live_snippet",
    "paired_counts",
    "rank_all_modes",
    "rescore",
    "rrf",
    "summarise",
    "target_in_text",
    "target_text",
    "top_k",
]

#: The retrieval modes, in report order.
MODES: Final = ("lexical", "dense", "dense_binary", "dense_int8", "hybrid")

#: Depth of every passage ranking before RRF and the collapse to articles. Deep, because a v1
#: article can fill dozens of passage slots and a shallow list would push the gold article out
#: by passage count alone, which is the thing being measured.
RANK_DEPTH: Final = 1000

#: How many of the top passages count when asking "did the query reach 10 distinct articles".
SHALLOW_DEPTH: Final = 100

#: The snippet budget in characters: the live service's ``_SNIPPET_CHARS``
#: (``wiki_mirror.search``, which cannot be imported here: it pulls in Qdrant).
SNIPPET_CHARS: Final = 320

#: Step between candidate windows of the focused snippet, characters (about a sentence's worth
#: of resolution; the window start then snaps forward to a word boundary).
WINDOW_STEP: Final = 40

#: The text embedded 16 times at the start of a run to detect nodes whose embeddings differ.
CANARY_TEXT: Final = (
    "Canary text for embedding drift: the quick brown fox jumps over the lazy dog near the "
    "riverbank at dawn."
)

#: Concurrent canary requests: one per request, so the router spreads them across nodes.
CANARY_COPIES: Final = 16

#: Minimum cosine between canary vectors (and against the stored one). Embeddings of one text
#: from the same build agree to ~1.0; a different Ollama build or quantization typically lands
#: well under this.
CANARY_MIN_COSINE: Final = 0.9995

VIEWS: Final = ("with_negatives", "random_only")

#: Modes carrying the "upper bound, small corpus" label.
QUANTISED_MODES: Final = ("dense_binary", "dense_int8")

#: Printed beside the quantised modes: a small corpus flatters them.
QUANTISED_NOTE: Final = (
    "upper bound, small corpus - not for the quantization decision "
    "(that needs a Qdrant measurement at scale)"
)

#: RRF constant: k=60, the value from the original Reciprocal Rank Fusion paper (Cormack et al.,
#: 2009), the common default.
RRF_K: Final = 60

#: Lexical hash space: the live keyword index's ``lexical_dimensions`` (docs/DESIGN.md section 12).
LEXICAL_DIMENSIONS: Final = 2**24

#: Quantile that clips each int8 dimension: the 99th percentile of its magnitudes, like Qdrant's
#: scalar-quantization ``quantile: 0.99`` (outliers would otherwise stretch the 255 levels).
INT8_QUANTILE: Final = 0.99

#: Largest in-flight embed count: the embedding pool's measured knee (docs/DESIGN.md section 12.5).
MAX_IN_FLIGHT: Final = 16

#: The mirror's search accepts 1..20 results per call (``SearchBody.limit``).
MAX_NEGATIVES: Final = 20

#: Concurrent mirror reads and searches. Modest: the mirror also serves live questions.
MIRROR_CONCURRENCY: Final = 8

#: Ids tried per step of the random fill, so the cache is flushed regularly.
_FILL_CHUNK: Final = 64

#: Random ids drawn per wanted article before the fill gives up (about 12 are needed on a real
#: mirror); stops a mirror that 404s everything from looping forever.
_FILL_DRAW_FACTOR: Final = 200

#: Width of the evidence core that must appear in a passage. A packed or split cut can trim the
#: evidence's edges, so only its middle is compared.
EVIDENCE_CORE_CHARS: Final = 120

#: Texts per embed cache shard flush: ``in_flight * batch * 16`` is typical (2048).
_SHARD_BATCHES: Final = 16

#: Articles fetched between cache flushes.
_FETCH_CHUNK: Final = 500

KINDS: Final = ("all", "fact", "describe")

_POPCOUNT: Final = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)

FloatMatrix = NDArray[np.float32]


@dataclass(frozen=True)
class EvalOptions:
    """Everything that shapes a run, apart from the clients and the paths."""

    schemes: tuple[int, ...] = (0, 1024, 1536, 2048)
    articles: int = 15_000
    negatives_per_question: int = 20
    seed: int = 20261019
    max_page_id: int = DEFAULT_MAX_PAGE_ID
    embed_model: str = "bge-m3:latest"
    in_flight: int = 16
    batch: int = 8
    binary_oversampling: float = 3.0
    int8_oversampling: float = 2.0


class Embedder(Protocol):
    """What the evaluation needs of the router: batch embedding (``RouterClient`` provides it)."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per input text."""
        ...


# ---------------------------------------------------------------------------------------------
# Golden set and corpus
# ---------------------------------------------------------------------------------------------


def load_cases(path: Path) -> list[GoldenCase]:
    """Read the golden JSONL written by ``tools/wiki_golden.py``.

    Args:
        path: The JSONL file.

    Returns:
        The cases, in file order.

    Raises:
        ValueError: A line is not a golden case, or an id repeats.
    """
    cases: list[GoldenCase] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            cases.append(GoldenCase(**json.loads(line)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path.name} line {number} is not a golden case") from exc
    if len({case.id for case in cases}) != len(cases):
        raise ValueError(f"{path.name} repeats a case id")
    return cases


class ArticleCache:
    """Fetched articles (and ids known to be absent) kept in a gzip JSONL file.

    Appends are gzip members, which concatenate, so a flush never rewrites the file and a run
    killed between flushes loses only the last chunk. A truncated last member is not repaired:
    delete the file if a crash left one.
    """

    def __init__(self, path: Path) -> None:
        """Load whatever the file already holds.

        Args:
            path: ``articles.jsonl.gz`` in the work directory (created on first flush).
        """
        self._path = path
        self.articles: dict[int, Article] = {}
        self.missing: set[int] = set()
        #: Ids whose read failed after retries this run. Treated as absent but never written to
        #: the file: a transient failure must not make a page missing for every later run.
        self.failed: set[int] = set()
        self._pending: list[str] = []
        if path.exists():
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    record = json.loads(line)
                    if record.get("missing"):
                        self.missing.add(int(record["page_id"]))
                    else:
                        self.articles[int(record["page_id"])] = Article(
                            page_id=int(record["page_id"]),
                            revision_id=int(record["revision_id"]),
                            title=str(record["title"]),
                            text=str(record["text"]),
                        )

    def known(self, page_id: int) -> bool:
        """Whether the id is cached, as an article, as absent, or as failed this run."""
        return page_id in self.articles or page_id in self.missing or page_id in self.failed

    def record(self, page_id: int, article: Article | None, *, failed: bool = False) -> None:
        """Remember a fetch result (None for a 404); written at the next ``flush``.

        Args:
            page_id: The page.
            article: The article, or None when there is none.
            failed: The read failed after retries (``article`` is None): remembered for this
                run only, not written.
        """
        if failed:
            self.failed.add(page_id)
        elif article is None:
            self.missing.add(page_id)
            self._pending.append(json.dumps({"page_id": page_id, "missing": True}))
        else:
            self.articles[page_id] = article
            self._pending.append(json.dumps(asdict(article), ensure_ascii=False))

    def flush(self) -> None:
        """Append the pending records as one gzip member."""
        if not self._pending:
            return
        with gzip.open(self._path, "at", encoding="utf-8") as handle:
            handle.write("\n".join(self._pending) + "\n")
        self._pending = []


async def _gather_bounded[T, R](
    items: Iterable[T], worker: Callable[[T], Awaitable[R]], limit: int
) -> list[R]:
    """Run ``worker`` over ``items`` with at most ``limit`` in flight; results keep item order."""
    gate = asyncio.Semaphore(limit)

    async def run(item: T) -> R:
        """One item under the gate."""
        async with gate:
            return await worker(item)

    return list(await asyncio.gather(*(run(item) for item in items)))


async def ensure_fetched(
    ids: Iterable[int], cache: ArticleCache, mirror: MirrorClient, guard: ReadGuard
) -> None:
    """Fetch every id the cache does not know, flushing after each chunk of ``_FETCH_CHUNK``.

    A page whose read fails after retries is recorded as absent for this run and counted in
    ``guard.failed``; the guard raises if failures never stop (a dead mirror).

    Args:
        ids: Page ids wanted.
        cache: Receives the results (articles, 404s and failures).
        mirror: The mirror client.
        guard: Shared across the run; counts and bounds read failures.

    Raises:
        MirrorUnavailableError: The mirror failed on many pages in a row.
        ArticleReadError: Many pages in a row could not be assembled.
    """
    todo = [pid for pid in dict.fromkeys(ids) if not cache.known(pid)]
    for start in range(0, len(todo), _FETCH_CHUNK):
        chunk = todo[start : start + _FETCH_CHUNK]

        async def fetch(pid: int) -> ReadResult:
            """One guarded article read."""
            return await guard.read(mirror, pid, request_id=new_request_id("wiki-eval-read"))

        for pid, result in zip(
            chunk, await _gather_bounded(chunk, fetch, MIRROR_CONCURRENCY), strict=True
        ):
            cache.record(pid, result.article, failed=result.failed)
        cache.flush()


async def find_negatives(
    cases: Sequence[GoldenCase], limit: int, path: Path, mirror: MirrorClient
) -> dict[str, list[int]]:
    """Each question's top ``limit`` articles from the live mirror search (hard negatives).

    Cached in ``path`` keyed by case id with a hash of the question and the limit, so a re-run
    searches only what changed.

    Args:
        cases: The golden cases.
        limit: Articles per question, 1..20.
        path: The JSON cache file.
        mirror: The mirror client.

    Returns:
        Case id to page ids, in the mirror's order.
    """
    cached: dict[str, Any] = json.loads(path.read_text("utf-8")) if path.exists() else {}

    def stamp(case: GoldenCase) -> str:
        """What a cached search was for: the question text and the depth asked."""
        return hashlib.sha256(f"{limit}\0{case.question}".encode()).hexdigest()

    todo = [c for c in cases if cached.get(c.id, {}).get("stamp") != stamp(c)]

    async def search(case: GoldenCase) -> list[int]:
        """One search."""
        return await search_ids(
            mirror, case.question, limit, request_id=new_request_id("wiki-eval-search")
        )

    found = await _gather_bounded(todo, search, MIRROR_CONCURRENCY)
    for case, ids in zip(todo, found, strict=True):
        cached[case.id] = {"stamp": stamp(case), "ids": ids}
    if todo:
        path.write_text(json.dumps(cached), encoding="utf-8")
    return {case.id: [int(i) for i in cached[case.id]["ids"]] for case in cases}


@dataclass
class Corpus:
    """The sample every scheme is scored on."""

    articles: dict[int, Article]
    #: Golden cases whose article the mirror no longer has (they stay in the metrics as misses).
    gold_missing: int = 0
    #: Golden cases whose stored revision differs from the fetched one (still used).
    revision_mismatches: int = 0
    #: Articles drawn by the uniform random id stream (a gold article or a hard negative the
    #: stream happened to hit counts too: it is a legitimate uniform draw). The IDF is computed
    #: from these only.
    random_ids: set[int] = field(default_factory=set)
    #: Articles in the corpus only as hard negatives: neither gold nor uniformly drawn. The
    #: ``random_only`` view masks their passages.
    negative_only: set[int] = field(default_factory=set)
    stats: dict[str, int] = field(default_factory=dict)


async def build_corpus(
    cases: Sequence[GoldenCase],
    options: EvalOptions,
    mirror: MirrorClient,
    work_dir: Path,
    *,
    sleep: Sleep = asyncio.sleep,
) -> Corpus:
    """Assemble the corpus: golden articles, hard negatives, then uniform random articles.

    A page whose read fails after retries is treated as absent for this run and counted in
    ``stats["read_failures"]``; it is not cached as missing, so the next run tries it again.

    Args:
        cases: The golden cases.
        options: Sample size, seed and negatives depth.
        mirror: The mirror client.
        work_dir: Where the article and search caches live.
        sleep: Awaited between mirror retries; tests inject a recorder.

    Returns:
        The corpus, articles keyed by page id. The random fill walks one seeded id stream in
        order, so the same seed gives the same sample whatever is already cached.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    cache = ArticleCache(work_dir / "articles.jsonl.gz")
    guard = ReadGuard(sleep=sleep)
    negatives = (
        await find_negatives(
            cases, options.negatives_per_question, work_dir / "negatives.json", mirror
        )
        if options.negatives_per_question > 0
        else {}
    )
    gold_ids = {c.page_id for c in cases}
    base_ids = [c.page_id for c in cases] + [pid for ids in negatives.values() for pid in ids]
    await ensure_fetched(base_ids, cache, mirror, guard)
    chosen = {pid: cache.articles[pid] for pid in dict.fromkeys(base_ids) if pid in cache.articles}

    wanted = options.articles - len(chosen)
    draws = 0
    random_ids: set[int] = set()
    stream = page_id_stream(f"{options.seed}:corpus", options.max_page_id)
    while len(chosen) < options.articles and draws < max(wanted, 1) * _FILL_DRAW_FACTOR:
        ids = [next(stream) for _ in range(_FILL_CHUNK)]
        draws += _FILL_CHUNK
        await ensure_fetched(ids, cache, mirror, guard)
        for pid in ids:  # in draw order, so the first ``wanted`` hits are the same every run
            if len(chosen) >= options.articles:
                break
            if pid in cache.articles:
                chosen.setdefault(pid, cache.articles[pid])
                random_ids.add(pid)

    corpus = Corpus(
        articles=dict(sorted(chosen.items())),
        random_ids=random_ids,
        negative_only=set(chosen) - gold_ids - random_ids,
    )
    for case in cases:
        article = corpus.articles.get(case.page_id)
        if article is None:
            corpus.gold_missing += 1
        elif article.revision_id != case.revision_id:
            corpus.revision_mismatches += 1
    corpus.stats = {
        "articles": len(corpus.articles),
        "base_articles": len(set(base_ids) & set(corpus.articles)),
        "random_articles": len(corpus.random_ids),
        "negative_only_articles": len(corpus.negative_only),
        "ids_drawn_for_fill": draws,
        "read_failures": guard.failed,
    }
    return corpus


# ---------------------------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------------------------


def normalise_rows(matrix: FloatMatrix) -> FloatMatrix:
    """L2-normalise every row (a zero row stays zero)."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return (matrix / np.where(norms == 0.0, 1.0, norms)).astype(np.float32)


class VectorCache:
    """Embeddings on disk, keyed by ``sha256(model + text)``, read back memory-mapped.

    Each ``add`` writes one shard: ``vec_NNNNNN.npy`` (float32 rows) and ``vec_NNNNNN.keys``
    (one hex key per line, written last, so a shard without keys is an unfinished write and is
    ignored). Shards are never rewritten.
    """

    def __init__(self, directory: Path, model: str) -> None:
        """Index the shards already on disk.

        Args:
            directory: The cache directory (created if missing).
            model: The embedding model; part of every key, so another model never collides.
        """
        directory.mkdir(parents=True, exist_ok=True)
        self._directory = directory
        self._model = model
        self._shards: list[NDArray[np.float32]] = []
        self._where: dict[str, tuple[int, int]] = {}
        for keys_file in sorted(directory.glob("vec_*.keys")):
            matrix_file = keys_file.with_suffix(".npy")
            if not matrix_file.exists():
                continue
            keys = keys_file.read_text(encoding="ascii").split()
            shard = np.load(matrix_file, mmap_mode="r")
            for row, key in enumerate(keys):
                self._where[key] = (len(self._shards), row)
            self._shards.append(shard)

    def key(self, text: str) -> str:
        """The cache key of a text under this cache's model."""
        return hashlib.sha256(f"{self._model}\0{text}".encode()).hexdigest()

    def has(self, key: str) -> bool:
        """Whether a vector is cached."""
        return key in self._where

    def add(self, keys: Sequence[str], vectors: FloatMatrix) -> None:
        """Write a shard of new vectors (rows aligned with ``keys``)."""
        # Highest existing number + 1 over both file kinds: counting files would reuse a number
        # if one were ever deleted, and an orphan .npy (a write that died before its keys) must
        # not be overwritten by a shard that then looks complete next to the wrong keys.
        numbers = [
            int(path.stem.removeprefix("vec_"))
            for path in self._directory.glob("vec_*")
            if path.suffix in {".npy", ".keys"} and path.stem.removeprefix("vec_").isdigit()
        ]
        stem = self._directory / f"vec_{max(numbers, default=-1) + 1:06d}"
        with stem.with_suffix(".tmp").open("wb") as handle:
            np.save(handle, vectors)
        stem.with_suffix(".tmp").replace(stem.with_suffix(".npy"))
        # Keys go last and arrive whole (temp then rename): a shard is complete only once its
        # keys file exists, and a half-written keys file must not pass for one.
        keys_tmp = stem.with_suffix(".keys.tmp")
        keys_tmp.write_text("\n".join(keys) + "\n", encoding="ascii")
        keys_tmp.replace(stem.with_suffix(".keys"))
        shard = np.load(stem.with_suffix(".npy"), mmap_mode="r")
        for row, key in enumerate(keys):
            self._where[key] = (len(self._shards), row)
        self._shards.append(shard)

    def gather(self, keys: Sequence[str]) -> FloatMatrix:
        """The cached vectors for ``keys``, as one in-memory float32 matrix in that order.

        Raises:
            KeyError: A key is not cached.
        """
        rows = [self._where[key] for key in keys]
        out = np.empty((len(rows), self._shards[0].shape[1] if self._shards else 0), np.float32)
        for position, (shard, row) in enumerate(rows):
            out[position] = self._shards[shard][row]
        return out


async def embed_texts(
    texts: Sequence[str],
    *,
    router: Embedder,
    cache: VectorCache,
    in_flight: int,
    batch: int,
) -> tuple[FloatMatrix, int]:
    """Embed texts through the router, reusing and extending the vector cache.

    Holds ``in_flight`` requests of ``batch`` texts each. Work proceeds in groups of
    ``in_flight * batch * 16`` texts, each written to the cache as a shard when it completes, so
    an interrupted run resumes from the last group.

    Args:
        texts: The texts, any length; duplicates are embedded once.
        router: Provides ``embed`` (one vector per input).
        cache: The vector cache; new vectors are added to it.
        in_flight: Concurrent requests.
        batch: Texts per request.

    Returns:
        The L2-normalised float32 vectors, one row per input text in order, and the number of
        texts newly embedded.

    Raises:
        RouterError: The router failed for good (a short reply included).
    """
    keys = [cache.key(text) for text in texts]
    pending: dict[str, str] = {}
    for key, text in zip(keys, texts, strict=True):
        if not cache.has(key) and key not in pending:
            pending[key] = text
    pending_keys = list(pending)

    gate = asyncio.Semaphore(in_flight)

    async def embed_batch(batch_keys: list[str]) -> FloatMatrix:
        """One request, under the in-flight gate."""
        async with gate:
            vectors = await router.embed([pending[key] for key in batch_keys])
        if len(vectors) != len(batch_keys):
            # A reply zipped against the wrong inputs would poison the cache for good.
            raise RouterError(f"embed returned {len(vectors)} vectors for {len(batch_keys)} inputs")
        return normalise_rows(np.asarray(vectors, dtype=np.float32))

    group = in_flight * batch * _SHARD_BATCHES
    for start in range(0, len(pending_keys), group):
        group_keys = pending_keys[start : start + group]
        batches = [group_keys[i : i + batch] for i in range(0, len(group_keys), batch)]
        results = await asyncio.gather(*(embed_batch(b) for b in batches))
        cache.add(group_keys, np.concatenate(results))
    return cache.gather(keys), len(pending_keys)


class CanaryError(RuntimeError):
    """The embedding pool's vectors differ across nodes, or from those the vector cache holds."""


async def check_canary(router: Embedder, directory: Path) -> dict[str, Any]:
    """Embed a fixed text on several nodes and compare, to catch mixed embedding builds.

    Sends ``CANARY_COPIES`` single-text requests at once (the router spreads concurrent
    requests over nodes) and takes the minimum pairwise cosine. The first run stores one canary
    vector in ``directory``; later runs also compare against it, which catches a node upgrade
    since the cache was made. Nothing is stored when the nodes already disagree.

    Args:
        router: Provides ``embed``.
        directory: The vector cache directory; the canary lives in ``canary.npy`` there.

    Returns:
        ``{"min_pairwise_cosine", "min_cosine_vs_stored" (None on the first run), "stored_now"}``.

    Raises:
        CanaryError: Any cosine is under ``CANARY_MIN_COSINE``.
        RouterError: A canary request failed for good.
    """
    replies = await asyncio.gather(*(router.embed([CANARY_TEXT]) for _ in range(CANARY_COPIES)))
    if any(len(reply) != 1 for reply in replies):
        raise RouterError("a canary embed reply did not hold exactly one vector")
    vectors = normalise_rows(np.asarray([reply[0] for reply in replies], dtype=np.float32))
    minimum = float((vectors @ vectors.T).min())
    directory.mkdir(parents=True, exist_ok=True)
    stored_file = directory / "canary.npy"
    against: float | None = None
    if stored_file.exists():
        stored = np.load(stored_file)
        against = float((vectors @ stored.astype(np.float32)).min())
    report: dict[str, Any] = {
        "min_pairwise_cosine": minimum,
        "min_cosine_vs_stored": against,
        "stored_now": False,
    }
    if minimum < CANARY_MIN_COSINE or (against is not None and against < CANARY_MIN_COSINE):
        raise CanaryError(
            "embeddings differ across nodes or since the cache was made; "
            f"delete {directory} (min pairwise cosine {minimum:.5f}"
            + (f", vs stored {against:.5f})" if against is not None else ")")
        )
    if against is None:
        np.save(stored_file, vectors[0])
        report["stored_now"] = True
    return report


# ---------------------------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------------------------


def top_k(scores: NDArray[Any], k: int) -> NDArray[np.int64]:
    """Indices of the ``k`` highest scores, best first; equal scores go to the lower index.

    A full ``lexsort`` rather than a partial one, so the tie rule is exact: the Hamming scores
    of the binary mode tie constantly, and a candidate cut that depended on partition order
    would make two runs disagree.
    """
    order = np.lexsort((np.arange(scores.shape[0]), -scores))
    return order[: max(0, k)].astype(np.int64)


def bm25_idf(containing: int, total: int) -> float:
    """Qdrant's IDF modifier: ``ln(1 + (N - n + 0.5) / (n + 0.5))``.

    This is the BM25 inverse document frequency (docs/DESIGN.md section 12.7).

    Args:
        containing: ``n``, the passages that contain the term.
        total: ``N``, all passages in the scheme.
    """
    return math.log(1.0 + (total - containing + 0.5) / (containing + 0.5))


class LexicalIndex:
    """An inverted index scoring passages the way the mirror's keyword index does.

    Documents and queries are encoded by the mirror's ``SparseEncoder`` (BM25 term-frequency
    saturation, hashed terms); the score is the sum over query terms of ``q_w * d_w * idf``,
    with ``idf`` computed over *this scheme's* passages, because the live collection computes it
    over its own points (``Modifier.IDF``).

    The statistic can be taken from a subset of the documents (``idf_from``): the evaluation
    uses the uniformly drawn articles only, because hard negatives are picked by lexical rank
    and would inflate the document frequency of exactly the query terms. Every document is
    still scored, with that idf.
    """

    def __init__(self, documents: Sequence[str], idf_from: Sequence[bool] | None = None) -> None:
        """Encode and index the documents.

        Args:
            documents: The passages' ``indexed_text``; a document's id is its position.
            idf_from: One flag per document; ``N`` and the per-term document frequency count
                only the flagged documents. None, or no flag set, counts every document.
        """
        self._encoder = SparseEncoder(dimensions=LEXICAL_DIMENSIONS)
        self._total = len(documents)
        terms: list[NDArray[np.int64]] = []
        owners: list[NDArray[np.int32]] = []
        weights: list[NDArray[np.float32]] = []
        for number, text in enumerate(documents):
            encoded = self._encoder.encode_document(text)
            terms.append(np.fromiter(encoded.keys(), dtype=np.int64, count=len(encoded)))
            weights.append(np.fromiter(encoded.values(), dtype=np.float32, count=len(encoded)))
            owners.append(np.full(len(encoded), number, dtype=np.int32))
        flat_terms = np.concatenate(terms) if terms else np.empty(0, dtype=np.int64)
        order = np.argsort(flat_terms, kind="stable")
        self._postings_terms = flat_terms[order]
        self._postings_docs = (np.concatenate(owners) if owners else np.empty(0, np.int32))[order]
        self._postings_weights = (
            np.concatenate(weights) if weights else np.empty(0, np.float32)
        )[order]
        unique, starts, counts = np.unique(
            self._postings_terms, return_index=True, return_counts=True
        )
        self._unique = unique
        self._starts = starts
        self._counts = counts
        counted = (
            np.asarray(idf_from, dtype=bool)
            if idf_from is not None and any(idf_from)
            else np.ones(self._total, dtype=bool)
        )
        total = int(counted.sum())
        # Documents with the term among the counted ones; reduceat sums each term's postings,
        # and ``starts`` is strictly increasing because the terms are unique.
        containing = (
            np.add.reduceat(counted[self._postings_docs].astype(np.int64), starts)
            if len(starts)
            else np.empty(0, dtype=np.int64)
        )
        self._idf = np.log1p((total - containing + 0.5) / (containing + 0.5))

    def search(
        self, query: str, k: int, allowed: Sequence[bool] | NDArray[np.bool_] | None = None
    ) -> list[int]:
        """The ``k`` best documents for a query, best first; documents scoring 0 are omitted.

        Args:
            query: The query text.
            k: How many documents to return at most.
            allowed: One flag per document; documents flagged False cannot be returned (the
                idf is unchanged, so this is a view of the same scores, not a new index).
        """
        scores = np.zeros(self._total, dtype=np.float64)
        for term, weight in self._encoder.encode_query(query).items():
            at = int(np.searchsorted(self._unique, term))
            if at >= len(self._unique) or self._unique[at] != term:
                continue
            start, count = int(self._starts[at]), int(self._counts[at])
            docs = self._postings_docs[start : start + count]
            scores[docs] += weight * self._idf[at] * self._postings_weights[start : start + count]
        if allowed is not None:
            scores[~np.asarray(allowed, dtype=bool)] = 0.0
        best = top_k(scores, k)
        return [int(i) for i in best if scores[i] > 0.0]


def dense_rankings(
    passages: FloatMatrix, queries: FloatMatrix, k: int, *, chunk: int = 64
) -> list[list[int]]:
    """Exact cosine ranking (the vectors are unit length, so a matrix product).

    Args:
        passages: Normalised passage vectors, one row each.
        queries: Normalised query vectors.
        k: Depth of each ranking.
        chunk: Queries scored per matrix product, bounding the score matrix's memory.

    Returns:
        Per query, passage indices best first.
    """
    rankings: list[list[int]] = []
    for start in range(0, queries.shape[0], chunk):
        scores = queries[start : start + chunk] @ passages.T
        rankings.extend([int(i) for i in top_k(row, k)] for row in scores)
    return rankings


def rescore(
    passages: FloatMatrix, query: FloatMatrix, candidates: Sequence[int], k: int
) -> list[int]:
    """Re-rank candidate passages by float cosine and keep the best ``k``.

    This is the step that makes a quantised first pass safe: the quantised score only has to
    get the right passages *into* the candidate set, not order them.

    Args:
        passages: Normalised passage vectors.
        query: One normalised query vector.
        candidates: Passage indices from the quantised pass.
        k: How many to keep.
    """
    if not candidates:
        return []
    chosen = np.asarray(candidates, dtype=np.int64)
    order = top_k(passages[chosen] @ query, k)
    return [int(chosen[i]) for i in order]


def binary_rankings(
    passages: FloatMatrix,
    queries: FloatMatrix,
    k: int,
    oversampling: float,
) -> list[list[int]]:
    """Binary-quantised search: sign bits, Hamming candidates, float rescore.

    Args:
        passages: Normalised passage vectors.
        queries: Normalised query vectors.
        k: Depth of each ranking.
        oversampling: Candidates taken are ``ceil(k * oversampling)`` (Qdrant's oversampling).

    Returns:
        Per query, passage indices best first.
    """
    p_bits = np.packbits(passages > 0.0, axis=1)
    q_bits = np.packbits(queries > 0.0, axis=1)
    pool = math.ceil(k * oversampling)
    rankings: list[list[int]] = []
    for number in range(queries.shape[0]):
        differing = _POPCOUNT[p_bits ^ q_bits[number]].sum(axis=1, dtype=np.int32)
        candidates = top_k(-differing, pool)  # fewest differing bits = highest similarity
        rankings.append(rescore(passages, queries[number], [int(c) for c in candidates], k))
    return rankings


def int8_rankings(
    passages: FloatMatrix,
    queries: FloatMatrix,
    k: int,
    oversampling: float,
    *,
    chunk: int = 64,
    block: int = 8192,
) -> list[list[int]]:
    """Int8-quantised search: per-dimension codes, dot-product candidates, float rescore.

    Each dimension is clipped at the ``INT8_QUANTILE`` of its magnitudes and mapped to
    ``-127..127`` (symmetric, so zero stays zero). Passages and queries share those scales. A
    dot product of raw codes would over-weight narrow dimensions (code units differ per
    dimension), so the candidate score is the dot product of the *dequantised* values: the
    stored side stays int8 and ``scale**2`` is folded into the query side. Only the candidate
    pass is approximate; the final order is the float cosine.

    Args:
        passages: Normalised passage vectors.
        queries: Normalised query vectors.
        k: Depth of each ranking.
        oversampling: Candidates taken are ``ceil(k * oversampling)``.
        chunk: Queries scored per block product.
        block: Passage rows converted from int8 per product, bounding the float copy.

    Returns:
        Per query, passage indices best first.
    """
    clip = np.empty(passages.shape[1], dtype=np.float32)
    for start in range(0, passages.shape[1], 128):  # column blocks bound the |x| temporary
        clip[start : start + 128] = np.quantile(
            np.abs(passages[:, start : start + 128]), INT8_QUANTILE, axis=0
        )
    scale = np.maximum(clip, 1e-9) / 127.0

    def encode(matrix: FloatMatrix) -> NDArray[np.int8]:
        """Round to codes in -127..127 on the per-dimension scale."""
        return np.clip(np.rint(matrix / scale), -127, 127).astype(np.int8)

    p_codes = encode(passages)
    q_weighted = encode(queries).astype(np.float32) * (scale * scale)
    pool = math.ceil(k * oversampling)
    rankings: list[list[int]] = []
    for start in range(0, queries.shape[0], chunk):
        weights = q_weighted[start : start + chunk]
        scores = np.empty((weights.shape[0], passages.shape[0]), dtype=np.float32)
        for lo in range(0, passages.shape[0], block):
            scores[:, lo : lo + block] = weights @ p_codes[lo : lo + block].astype(np.float32).T
        for offset, row in enumerate(scores):
            candidates = [int(c) for c in top_k(row, pool)]
            rankings.append(rescore(passages, queries[start + offset], candidates, k))
    return rankings


def rrf(rankings: Sequence[Sequence[int]], *, k: int = RRF_K, limit: int = RANK_DEPTH) -> list[int]:
    """Reciprocal Rank Fusion: ``score(d) = sum over lists of 1 / (k + rank)``, rank from 1.

    Written for bare passage indices: no stored points and no per-source weights.

    Args:
        rankings: Ranked lists of passage indices, best first.
        k: The RRF constant.
        limit: Length of the fused list.

    Returns:
        Fused passage indices, best first; equal scores go to the lower index.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda item: (-scores[item], item))[:limit]


def collapse_to_articles(ranking: Sequence[int], page_of: Sequence[int]) -> list[tuple[int, int]]:
    """Collapse a passage ranking to articles; an article ranks at its best passage.

    Args:
        ranking: Passage indices, best first.
        page_of: Page id of each passage, by passage index.

    Returns:
        ``(page_id, best_passage_index)`` per distinct article, in order of first appearance.
    """
    seen: set[int] = set()
    articles: list[tuple[int, int]] = []
    for passage in ranking:
        page = int(page_of[passage])
        if page not in seen:
            seen.add(page)
            articles.append((page, int(passage)))
    return articles


# ---------------------------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------------------------


def evidence_in_passage(passage: str, evidence: str) -> bool:
    """Whether a passage contains the evidence, tolerating trimmed edges.

    Both are casefolded and whitespace-collapsed. Packed and split cuts can shave the start or
    end of the evidence, so only its middle ``EVIDENCE_CORE_CHARS`` must be present (all of it
    when it is shorter).

    Args:
        passage: The passage body.
        evidence: The golden case's evidence paragraph.
    """
    return _core(evidence) in fold(passage)


def _core(evidence: str) -> str:
    """The folded middle ``EVIDENCE_CORE_CHARS`` of an evidence paragraph (all if shorter)."""
    folded = fold(evidence)
    if len(folded) > EVIDENCE_CORE_CHARS:
        begin = (len(folded) - EVIDENCE_CORE_CHARS) // 2
        folded = folded[begin : begin + EVIDENCE_CORE_CHARS]
    return folded


def target_text(case: GoldenCase) -> str:
    """The folded string a good answer location must contain.

    A ``fact`` case's target is the golden ``answer`` phrase: what a reader looks for in the
    passage or snippet. A ``describe`` case's answer is the article title, which says nothing
    about whether the right *content* was surfaced, so its target is the middle of the evidence.

    Args:
        case: The golden case.
    """
    return fold(case.answer) if case.kind == "fact" else _core(case.evidence)


def target_in_text(text: str, case: GoldenCase) -> bool:
    """Whether ``text`` (a passage or a snippet) contains the case's target."""
    target = target_text(case)
    return bool(target) and target in fold(text)


def _cut(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` characters at a word boundary, adding an ellipsis.

    Copied from ``wiki_mirror.search._cut``, which cannot be imported here (that
    module pulls in Qdrant and the zstandard-using index); keep the two in step.
    """
    if len(text) <= limit:
        return text
    head = text[:limit]
    if not text[limit].isspace():
        # The cut is inside a word: back up to the space before it, if there is one.
        head = head.rsplit(" ", 1)[0]
    return head.rstrip() + "…"


def live_snippet(passage: str) -> str:
    """The snippet the live service shows for a best passage: its first 320 characters.

    ``search._snippet`` whitespace-collapses the passage text and cuts it with ``_cut`` at
    ``_SNIPPET_CHARS`` (320). Reproduced for a packed passage too: the service would do the same
    to whatever the index calls a passage.

    Args:
        passage: The best passage's body.
    """
    return _cut(" ".join(passage.split()), SNIPPET_CHARS)


def focused_snippet(passage: str, question: str) -> str:
    """The 320-character window of a passage that overlaps the question's terms most.

    Windows start on word boundaries, about every ``WINDOW_STEP`` characters (plus one flush
    with the passage's end), and end at a word boundary within the budget. The score is the
    number of distinct question terms (the mirror's tokenizer, so stems and stop words match
    the keyword channel) found in the window; ties go to the earliest. This is what a snippet
    chosen for the query could show, as against the fixed head ``live_snippet`` takes.

    Args:
        passage: The best passage's body.
        question: The query text.
    """
    text = " ".join(passage.split())
    if len(text) <= SNIPPET_CHARS:
        return text
    starts = {0}
    for position in range(WINDOW_STEP, len(text), WINDOW_STEP):
        space = text.find(" ", position - 1)
        if space != -1:
            starts.add(space + 1)
    # The first word boundary from which the rest of the passage fits the budget.
    flush = text.find(" ", len(text) - SNIPPET_CHARS - 1)
    if flush != -1:
        starts.add(flush + 1)
    terms = set(tokenize(question))
    best_window, best_score = "", -1
    for start in sorted(starts):
        window = text[start : start + SNIPPET_CHARS]
        if start + SNIPPET_CHARS < len(text) and not text[start + SNIPPET_CHARS].isspace():
            window = window.rsplit(" ", 1)[0]  # do not end on a half word
        score = len(terms & set(tokenize(window)))
        if score > best_score:  # strictly greater: the earliest window keeps a tie
            best_window, best_score = window, score
    return best_window


@dataclass(frozen=True)
class CaseResult:
    """How one question fared in one mode."""

    #: 1-based rank of the gold article among the collapsed articles, None beyond the top 10.
    rank: int | None
    #: The gold article's best passage holds the target; None when the article is not top 5.
    target_in_best: bool | None
    #: Gold article first and the target is in the live-style snippet of its best passage.
    snippet_live: bool = False
    #: Gold article first and the target is in the query-focused snippet of its best passage.
    snippet_focused: bool = False

    @property
    def hit1(self) -> bool:
        """The gold article is first."""
        return self.rank == 1

    @property
    def hit5(self) -> bool:
        """The gold article is in the top five."""
        return self.rank is not None and self.rank <= 5

    @property
    def answer_in_passage1(self) -> bool:
        """The gold article is first and its best passage holds the target."""
        return self.hit1 and bool(self.target_in_best)

    @property
    def reciprocal_rank(self) -> float:
        """``1 / rank`` within the top 10, else 0."""
        return 1.0 / self.rank if self.rank is not None else 0.0


def judge_case(
    case: GoldenCase, articles: Sequence[tuple[int, int]], bodies: Sequence[str]
) -> CaseResult:
    """Score one ranked list of articles against its question.

    Args:
        case: The golden case.
        articles: ``collapse_to_articles`` output.
        bodies: Passage bodies by passage index (the target is looked for in these, not in
            the title-prefixed text that was embedded).
    """
    for position, (page, best) in enumerate(articles[:10], start=1):
        if page != case.page_id:
            continue
        found = target_in_text(bodies[best], case) if position <= 5 else None
        if position != 1:
            return CaseResult(rank=position, target_in_best=found)
        return CaseResult(
            rank=1,
            target_in_best=found,
            snippet_live=target_in_text(live_snippet(bodies[best]), case),
            snippet_focused=target_in_text(focused_snippet(bodies[best], case.question), case),
        )
    return CaseResult(rank=None, target_in_best=None)


def summarise(
    results: Mapping[str, CaseResult], kinds: Mapping[str, str]
) -> dict[str, dict[str, float | int | None]]:
    """Aggregate case results overall and per kind.

    Args:
        results: Case id to result for one scheme and mode.
        kinds: Case id to ``fact`` or ``describe``.

    Returns:
        ``{"all"|kind: {n, hit1, hit5, mrr10, answer_in_passage1, answer_in_snippet_live1,
        answer_in_snippet_focused1, evidence_given_hit5, n_hit5}}``; the rates are shares of
        ``n``, except ``evidence_given_hit5``, a share of ``n_hit5`` (None when no case hit the
        top five): among the cases with the gold article in the top five, how many have the
        target in its best passage. Kinds with no cases are omitted.
    """
    summary: dict[str, dict[str, float | int | None]] = {}
    for kind in KINDS:
        chosen = [r for cid, r in results.items() if kind == "all" or kinds[cid] == kind]
        if not chosen:
            continue
        hits = [r for r in chosen if r.hit5]
        n = len(chosen)
        summary[kind] = {
            "n": n,
            "hit1": sum(r.hit1 for r in chosen) / n,
            "hit5": len(hits) / n,
            "mrr10": sum(r.reciprocal_rank for r in chosen) / n,
            "answer_in_passage1": sum(r.answer_in_passage1 for r in chosen) / n,
            "answer_in_snippet_live1": sum(r.snippet_live for r in chosen) / n,
            "answer_in_snippet_focused1": sum(r.snippet_focused for r in chosen) / n,
            "evidence_given_hit5": (
                sum(bool(r.target_in_best) for r in hits) / len(hits) if hits else None
            ),
            "n_hit5": len(hits),
        }
    return summary


def recall_at(approx: Sequence[Sequence[int]], exact: Sequence[Sequence[int]], depth: int) -> float:
    """Mean overlap of two rankings' top ``depth`` passages, as a share of the exact top.

    Args:
        approx: Per query, the quantised mode's ranking.
        exact: Per query, the exact dense ranking.
        depth: How many to compare (10 for recall@10).
    """
    shares = [
        len(set(a[:depth]) & set(e[:depth])) / len(e[:depth])
        for a, e in zip(approx, exact, strict=True)
        if e
    ]
    return sum(shares) / len(shares) if shares else 0.0


# ---------------------------------------------------------------------------------------------
# One scheme, the report and the command line
# ---------------------------------------------------------------------------------------------


def rank_all_modes(
    vectors: FloatMatrix,
    question_vectors: FloatMatrix,
    lexical: LexicalIndex,
    cases: Sequence[GoldenCase],
    options: EvalOptions,
    mask: NDArray[np.bool_] | None,
) -> dict[str, list[list[int]]]:
    """Rank every question in every mode, over all passages or only those the mask allows.

    The dense modes search the allowed rows (``vectors[keep]``) and map the result back to
    corpus passage numbers; the lexical index applies the mask to the same scores, so its idf
    does not change between views. No embedding is redone.

    Args:
        vectors: Normalised passage vectors, one row per passage of the scheme.
        question_vectors: Normalised question vectors, in case order.
        lexical: The scheme's inverted index.
        cases: The golden cases.
        options: Oversampling settings.
        mask: One flag per passage, False to exclude it; None for no exclusion.

    Returns:
        Mode to, per question, passage numbers best first (``RANK_DEPTH`` deep).
    """
    keep = None if mask is None else np.flatnonzero(mask)
    searched = vectors if keep is None else vectors[keep]

    def back(rankings: list[list[int]]) -> list[list[int]]:
        """Translate row numbers of the searched matrix to corpus passage numbers."""
        if keep is None:
            return rankings
        return [[int(keep[i]) for i in ranking] for ranking in rankings]

    ranked: dict[str, list[list[int]]] = {
        "lexical": [lexical.search(case.question, RANK_DEPTH, mask) for case in cases],
        "dense": back(dense_rankings(searched, question_vectors, RANK_DEPTH)),
        "dense_binary": back(
            binary_rankings(searched, question_vectors, RANK_DEPTH, options.binary_oversampling)
        ),
        "dense_int8": back(
            int8_rankings(searched, question_vectors, RANK_DEPTH, options.int8_oversampling)
        ),
    }
    ranked["hybrid"] = [
        rrf([lex, dense]) for lex, dense in zip(ranked["lexical"], ranked["dense"], strict=True)
    ]
    return ranked


def chance_baseline(
    cases: Sequence[GoldenCase], spans: Mapping[int, tuple[int, int]], bodies: Sequence[str]
) -> dict[str, float]:
    """The ``answer_in_passage`` score of picking a random passage of the gold article.

    Longer passages hold the target more often just by being longer, so ``answer_in_passage@1``
    must be read against this: for each case, the share of the gold article's passages that
    hold the target, averaged (a gold article missing from the corpus scores 0).

    Args:
        cases: The golden cases.
        spans: Page id to the ``[begin, end)`` range of its passages in ``bodies``.
        bodies: Passage bodies of the scheme.

    Returns:
        Overall (``all``) and per kind.
    """
    shares: dict[str, list[float]] = {kind: [] for kind in KINDS}
    for case in cases:
        begin, end = spans.get(case.page_id, (0, 0))
        held = sum(target_in_text(body, case) for body in bodies[begin:end])
        share = held / (end - begin) if end > begin else 0.0
        shares["all"].append(share)
        shares[case.kind].append(share)
    return {kind: sum(values) / len(values) for kind, values in shares.items() if values}


def few_articles_share(
    rankings: Sequence[Sequence[int]],
    page_of: Sequence[int],
    *,
    depth: int = SHALLOW_DEPTH,
    need: int = 10,
) -> float:
    """The share of queries whose top ``depth`` passages span fewer than ``need`` articles.

    A high share means a mode's list is dominated by a few articles' passages (v1 cuts long
    articles into many), so a ranking read as articles is shallower than it looks.

    Args:
        rankings: Per query, passage numbers best first.
        page_of: Page id of each passage.
        depth: How many top passages to look at.
        need: Distinct articles wanted.
    """
    if not rankings:
        return 0.0
    short = sum(len({page_of[i] for i in ranking[:depth]}) < need for ranking in rankings)
    return short / len(rankings)


#: What the paired comparison counts: hit@5 and the focused-snippet answer.
PAIRED_KEYS: Final = ("hit5", "answer_in_snippet_focused1")


def paired_counts(
    base: Mapping[str, Mapping[str, Any]], other: Mapping[str, Mapping[str, Any]], key: str
) -> dict[str, int]:
    """Cases where a boolean went miss to hit (``gained``) or hit to miss (``lost``).

    Args:
        base: Case id to the baseline's per-case results.
        other: Case id to the compared scheme's per-case results.
        key: The boolean field to compare.
    """
    gained = sum(1 for cid, row in other.items() if row[key] and not base[cid][key])
    lost = sum(1 for cid, row in other.items() if base[cid][key] and not row[key])
    return {"gained": gained, "lost": lost}


def add_paired(schemes: dict[str, Any]) -> None:
    """Add each scheme's paired comparison against v1 (scheme ``0``) to its views, in place.

    Does nothing when v1 was not run. For every other scheme, view and mode, stores
    ``paired_vs_v1[mode][key] = {"gained", "lost"}`` for the keys in ``PAIRED_KEYS``.

    Args:
        schemes: The report's ``schemes`` mapping, keyed by ``str(pack_chars)``.
    """
    baseline = schemes.get("0")
    if baseline is None:
        return
    for name, scheme in schemes.items():
        if name == "0":
            continue
        for view, data in scheme["views"].items():
            base_cases = baseline["views"][view]["cases"]
            data["paired_vs_v1"] = {
                mode: {
                    key: paired_counts(
                        {cid: row[mode] for cid, row in base_cases.items()},
                        {cid: row[mode] for cid, row in data["cases"].items()},
                        key,
                    )
                    for key in PAIRED_KEYS
                }
                for mode in MODES
            }


async def evaluate_scheme(
    pack_chars: int,
    corpus: Corpus,
    cases: Sequence[GoldenCase],
    question_vectors: FloatMatrix,
    *,
    router: Embedder,
    cache: VectorCache,
    options: EvalOptions,
) -> dict[str, Any]:
    """Cut, embed and score the corpus under one passage scheme.

    Args:
        pack_chars: ``cut_passages`` target (0 for the live v1 cut).
        corpus: The shared sample.
        cases: The golden cases.
        question_vectors: Normalised vectors of the cases' questions, in case order.
        router: Provides ``embed``.
        cache: The vector cache.
        options: Oversampling, in-flight and batch settings.

    Returns:
        The scheme's report: passage counts and sizes, the sanity number, the chance baseline,
        and per view (``with_negatives``, ``random_only``) the recall of the quantised modes,
        the share of short rankings, the metrics per mode and kind, and per-case booleans keyed
        by case id. No text.
    """
    page_of: list[int] = []
    bodies: list[str] = []
    documents: list[str] = []
    spans: dict[int, tuple[int, int]] = {}
    for page, article in corpus.articles.items():
        begin = len(bodies)
        for passage in cut_passages(article.text, pack_chars=pack_chars):
            page_of.append(page)
            bodies.append(passage.text)
            documents.append(passage.indexed_text(article.title))
        spans[page] = (begin, len(bodies))

    vectors, embedded = await embed_texts(
        documents, router=router, cache=cache, in_flight=options.in_flight, batch=options.batch
    )
    print(f"scheme {pack_chars}: passages={len(bodies)} newly_embedded={embedded}")

    # IDF from the uniformly drawn articles only (see ``LexicalIndex``).
    lexical = LexicalIndex(documents, [page in corpus.random_ids for page in page_of])
    masks: dict[str, NDArray[np.bool_] | None] = {"with_negatives": None}
    random_only = np.array([page not in corpus.negative_only for page in page_of], dtype=bool)
    # With nothing to mask, the second view is the first: reuse it instead of ranking twice.
    masks["random_only"] = None if random_only.all() else random_only
    ranked: dict[str, dict[str, list[list[int]]]] = {}
    for view in VIEWS:
        if view == "random_only" and masks[view] is None:
            ranked[view] = ranked["with_negatives"]
        else:
            ranked[view] = rank_all_modes(
                vectors, question_vectors, lexical, cases, options, masks[view]
            )
    del vectors, lexical

    kinds = {case.id: case.kind for case in cases}
    views: dict[str, Any] = {}
    for view in VIEWS:
        mask = masks[view]
        metrics: dict[str, Any] = {}
        per_case: dict[str, dict[str, Any]] = {case.id: {} for case in cases}
        for mode in MODES:
            results = {
                case.id: judge_case(case, collapse_to_articles(ranking, page_of), bodies)
                for case, ranking in zip(cases, ranked[view][mode], strict=True)
            }
            metrics[mode] = summarise(results, kinds)
            for case_id, result in results.items():
                per_case[case_id][mode] = {
                    "rank": result.rank,
                    "hit1": result.hit1,
                    "hit5": result.hit5,
                    "answer_in_passage1": result.answer_in_passage1,
                    "answer_in_snippet_live1": result.snippet_live,
                    "answer_in_snippet_focused1": result.snippet_focused,
                    "target_in_best": result.target_in_best,
                }
        exact = ranked[view]["dense"]
        views[view] = {
            "passages": len(bodies) if mask is None else int(mask.sum()),
            "recall10_vs_exact": {
                "dense_binary": recall_at(ranked[view]["dense_binary"], exact, 10),
                "dense_int8": recall_at(ranked[view]["dense_int8"], exact, 10),
            },
            "few_articles_share": {
                mode: few_articles_share(ranked[view][mode], page_of) for mode in MODES
            },
            "metrics": metrics,
            "cases": per_case,
        }

    missing = 0
    for case in cases:
        begin, end = spans.get(case.page_id, (0, 0))
        if not any(evidence_in_passage(body, case.evidence) for body in bodies[begin:end]):
            missing += 1
    return {
        "pack_chars": pack_chars,
        "passages": len(bodies),
        "mean_passage_chars": sum(len(b) for b in bodies) / len(bodies) if bodies else 0.0,
        "evidence_missing_share": missing / len(cases) if cases else 0.0,
        "answer_in_passage_chance": chance_baseline(cases, spans, bodies),
        "views": views,
    }


def format_table(report: Mapping[str, Any]) -> str:
    """The stdout table: corpus counts, then per scheme its sizes and a row per mode and kind.

    Carries numbers only - no question, title or article text.
    """
    corpus = report["corpus"]
    canary = report["canary"]
    lines = [
        "corpus: " + " ".join(f"{key}={value}" for key, value in corpus.items()),
        f"canary: min_pairwise_cosine={canary['min_pairwise_cosine']:.5f} "
        f"vs_stored={_number(canary['min_cosine_vs_stored'], '.5f')}",
    ]
    for scheme in report["schemes"].values():
        chance = scheme["answer_in_passage_chance"]
        lines.append(
            f"\nscheme {scheme['pack_chars']}: passages={scheme['passages']} "
            f"mean_chars={scheme['mean_passage_chars']:.1f} "
            f"evidence_missing={scheme['evidence_missing_share']:.4f}"
        )
        lines.append(
            "  answer_in_passage chance (random passage of the gold article): "
            + " ".join(f"{kind}={value:.3f}" for kind, value in chance.items())
        )
        for view, data in scheme["views"].items():
            recall = data["recall10_vs_exact"]
            lines.append(
                f"  view {view}: passages={data['passages']} recall10 vs exact "
                f"binary*={recall['dense_binary']:.4f} int8*={recall['dense_int8']:.4f}"
            )
            lines.append(
                f"  {'mode':<14}{'kind':<9}{'n':>4}{'hit@1':>7}{'hit@5':>7}{'mrr10':>7}"
                f"{'ans@1':>7}{'snipL':>7}{'snipF':>7}{'a|h5':>7}{'<10art':>7}"
                f"  vs v1 hit@5 / snipF"
            )
            for mode in MODES:
                label = f"{mode}*" if mode in QUANTISED_MODES else mode
                for kind, row in data["metrics"][mode].items():
                    paired = ""
                    if kind == "all" and "paired_vs_v1" in data:
                        pair = data["paired_vs_v1"][mode]
                        paired = (
                            f"  +{pair['hit5']['gained']}/-{pair['hit5']['lost']}"
                            f" / +{pair['answer_in_snippet_focused1']['gained']}"
                            f"/-{pair['answer_in_snippet_focused1']['lost']}"
                        )
                    few = _number(data["few_articles_share"][mode], ".3f") if kind == "all" else ""
                    lines.append(
                        f"  {label:<14}{kind:<9}{row['n']:>4}{row['hit1']:>7.3f}"
                        f"{row['hit5']:>7.3f}{row['mrr10']:>7.3f}"
                        f"{row['answer_in_passage1']:>7.3f}{row['answer_in_snippet_live1']:>7.3f}"
                        f"{row['answer_in_snippet_focused1']:>7.3f}"
                        f"{_number(row['evidence_given_hit5'], '.3f'):>7}{few:>7}{paired}"
                    )
    lines.append(f"\n* {QUANTISED_NOTE}")
    lines.append(
        "ans@1: gold article first and its best passage holds the target; snipL / snipF: the "
        "target is in the live-style / query-focused 320-char snippet; a|h5: ans given hit@5; "
        "<10art: share of queries with fewer than 10 distinct articles in the top 100 passages."
    )
    return "\n".join(lines)


def _number(value: float | None, spec: str) -> str:
    """A number formatted for the table, or ``n/a`` when there is none."""
    return "n/a" if value is None else format(value, spec)


async def run(
    cases: Sequence[GoldenCase],
    options: EvalOptions,
    *,
    mirror: MirrorClient,
    router: Embedder,
    work_dir: Path,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[dict[str, Any], Path]:
    """Build the corpus, evaluate every scheme, and write the JSON report.

    Args:
        cases: The golden cases.
        options: The run's settings.
        mirror: The mirror client.
        router: Provides ``embed``.
        work_dir: Cache and report directory (created if missing).
        now: The clock for the report's file name; tests inject theirs.

    Returns:
        The report and the path it was written to.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    # First, before any fetching: a mixed-build embedding pool makes the whole run worthless.
    canary = await check_canary(router, work_dir / "vectors")
    print(f"canary: min_pairwise_cosine={canary['min_pairwise_cosine']:.5f}")
    corpus = await build_corpus(cases, options, mirror, work_dir)
    cache = VectorCache(work_dir / "vectors", options.embed_model)
    question_vectors, _ = await embed_texts(
        [case.question for case in cases],
        router=router,
        cache=cache,
        in_flight=options.in_flight,
        batch=options.batch,
    )
    schemes: dict[str, Any] = {}
    for pack_chars in options.schemes:
        schemes[str(pack_chars)] = await evaluate_scheme(
            pack_chars,
            corpus,
            cases,
            question_vectors,
            router=router,
            cache=cache,
            options=options,
        )
    add_paired(schemes)
    stamp = now().astimezone(UTC)
    report: dict[str, Any] = {
        "generated_at": stamp.isoformat(),
        "options": asdict(options),
        "canary": canary,
        "notes": {
            "dense_binary": QUANTISED_NOTE,
            "dense_int8": QUANTISED_NOTE,
            "random_only": "hard negatives masked; idf from random-fill articles in both views",
        },
        "corpus": {
            **corpus.stats,
            "golden_cases": len(cases),
            "gold_missing": corpus.gold_missing,
            "revision_mismatches": corpus.revision_mismatches,
        },
        "schemes": schemes,
    }
    path = work_dir / f"report_{stamp.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report, path


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description="Score Wikipedia passage schemes.")
    parser.add_argument("--golden", type=Path, required=True, help="golden JSONL")
    # /var/tmp survives a reboot (/tmp does not), which the multi-hour embedding cache needs.
    parser.add_argument("--work-dir", type=Path, default=Path("/var/tmp/wiki_eval"))  # noqa: S108
    parser.add_argument("--schemes", default="0,1024,1536,2048", help="pack_chars list; 0 = v1")
    parser.add_argument("--articles", type=int, default=15_000)
    parser.add_argument("--negatives-per-question", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20261019)
    parser.add_argument("--mirror", default=DEFAULT_MIRROR)
    parser.add_argument("--router", default=DEFAULT_ROUTER)
    parser.add_argument("--embed-model", default="bge-m3:latest")
    parser.add_argument("--in-flight", type=int, default=MAX_IN_FLIGHT)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--max-page-id", type=int, default=DEFAULT_MAX_PAGE_ID)
    parser.add_argument("--binary-oversampling", type=float, default=3.0)
    parser.add_argument("--int8-oversampling", type=float, default=2.0)
    args = parser.parse_args(argv)
    try:
        args.scheme_list = tuple(int(part) for part in args.schemes.split(",") if part.strip())
    except ValueError:
        parser.error("--schemes must be comma-separated integers")
    if not args.scheme_list or any(s < 0 for s in args.scheme_list):
        parser.error("--schemes needs at least one non-negative integer")
    if not 1 <= args.in_flight <= MAX_IN_FLIGHT:
        parser.error(f"--in-flight must be 1..{MAX_IN_FLIGHT} (the embedding pool's measured knee)")
    if args.batch < 1 or args.articles < 1 or args.max_page_id < 1:
        parser.error("--batch, --articles and --max-page-id must be positive")
    if not 0 <= args.negatives_per_question <= MAX_NEGATIVES:
        parser.error(f"--negatives-per-question must be 0..{MAX_NEGATIVES} (the search limit)")
    if args.binary_oversampling < 1.0 or args.int8_oversampling < 1.0:
        parser.error("oversampling must be at least 1.0")
    return args


async def _main(args: argparse.Namespace) -> int:
    """Wire the real clients, run, and print the table."""
    cases = load_cases(args.golden)
    options = EvalOptions(
        schemes=args.scheme_list,
        articles=args.articles,
        negatives_per_question=args.negatives_per_question,
        seed=args.seed,
        max_page_id=args.max_page_id,
        embed_model=args.embed_model,
        in_flight=args.in_flight,
        batch=args.batch,
        binary_oversampling=args.binary_oversampling,
        int8_oversampling=args.int8_oversampling,
    )
    mirror = MirrorClient(base_url=args.mirror, timeout_seconds=30.0)
    async with RouterClient(args.router, embed_model=args.embed_model) as router:
        try:
            report, path = await run(
                cases, options, mirror=mirror, router=router, work_dir=args.work_dir
            )
        except (MirrorServiceError, RouterError, ArticleReadError, CanaryError) as exc:
            # Status and error code only: a 422 body from the mirror can echo the question.
            print(f"stopped: {describe_failure(exc)}", file=sys.stderr)
            return 1
    print(format_table(report))
    print(f"\nreport: {path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: ``python tools/wiki_passage_eval.py --golden PATH``.

    Args:
        argv: Arguments; the process's when omitted.

    Returns:
        0 on success, 1 when the mirror or the router failed.
    """
    return asyncio.run(_main(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
