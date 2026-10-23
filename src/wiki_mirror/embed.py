"""The semantic index (W2): every passage embedded and loaded into its own Qdrant collection.

The `embed` stage (`wiki_mirror.__main__`), run by hand or by `wiki-mirror-embed.service` after a
build has finished. It reads the article store that the live lexical collection serves, cuts each
article into the same passages the lexical build cut (`index.indexable_chunks`), asks an
Ollama-compatible embedding server for one vector per passage and upserts them into
`wikipedia_semantic_<born>`, where `<born>` is the tag of that store's snapshot. When every passage
is in, HNSW is switched on and the alias `wikipedia_semantic` is pointed at the collection. The
API's semantic and hybrid search modes read it (`serving`, `search`, docs/DECISIONS.md D10), with a
question embedded by `query_embed` using the model this stage recorded in the state.

Non-obvious constraints:

* **A second collection beside the lexical one, with the same point ids and payload.**
  `index.passage_payload` and `records.passage_point_id` are shared, so a later fusion joins the
  two result lists on the point id. The lexical build skips a passage with no searchable term; this
  one skips only a passage with no text at all, so the semantic collection can hold a few passages
  the lexical one does not. Neither stage deletes the other's collection.
* **The embedded text is `passage_embedding_text`** (title, heading and passage, as the evaluation
  measured, dense hit@5 0.953), recorded in the state as `TEXT_FORM`. The query side must embed
  its question as plain text, the form the evaluation used, and anything that changes the passage
  form is a new `TEXT_FORM` and so a new collection.
* **One vector per input, or the run stops.** A server that answers fewer vectors than texts (a
  known failure mode of batching routers), a vector of the wrong length or a non-finite number
  raises `EmbedError`. Nothing is padded, zipped short or skipped: a vector on the wrong point is
  worse than no vector, because search would return it with full confidence.
* **Windows commit in submission order.** Articles are grouped into windows and each window's
  requests are sent as soon as the semaphore admits them (`embedding_concurrency` in flight in
  all, across windows), so replies arrive in any order. A window is upserted only when it and every
  window before it is fully embedded, and the checkpoint (`last_page_id`) is the last page id of the
  last upserted window. So "every article at or below `last_page_id` is fully in Qdrant" holds,
  exactly as in `index.py`, and a crash repeats only the windows that were still in flight.
  Memory is bounded: at most `_WINDOWS_QUEUED` windows exist at once.
* **Retry transient failures, never refusals.** 429, 500, 502, 503 and 504 (honouring `Retry-After`,
  capped), timeouts and connection errors are retried with exponential backoff for up to
  `_RETRY_BUDGET_SECONDS` per request, then raised: the systemd unit restarts the stage and it
  resumes from the checkpoint. Any other status is raised at once with a fixed message. A message
  or log line never carries the key, a header, the URL, a response body or any text.
* **The key is not a setting.** `EMBEDDING_API_KEY` is read from the environment (systemd loads it
  from a file only the embed unit's user can read) and goes only into the `Authorization` header,
  built per request. A server without authentication needs none.
* **The collection is loaded without HNSW and indexed at the end.** Building the graph while 41
  million points arrive costs far more than building it once per segment afterwards: the collection
  is created with `m=0` and `_finish` sets `SEMANTIC_HNSW`. Qdrant then builds the graphs in the
  background, `SEMANTIC_OPTIMIZATION_THREADS` segments at a time, and that outlives this stage; it
  is logged, not waited for.
* **Everything is on disk, and these constants are the one definition of that layout.** The
  float16 vectors, the HNSW graph (`SEMANTIC_HNSW`, `on_disk=True`) and the binary-quantized copy
  (`SEMANTIC_QUANTIZATION`, `always_ram=False`) all live on disk, so the mirror's pinned RAM stays
  under 10 GB (the operator's decision of 2026-10-23, docs/DECISIONS.md D8): the page cache keeps
  the hot parts in spare RAM and gives them back under pressure, so nothing is OOM-killed. This
  stage creates and finishes the collection with them, and the maintenance job (`maintain.py`)
  keeps the live collection equal to them.
* **Resuming is checked, not assumed, twice.** The state file records the chunker, model,
  dimensions and text form; any difference from the running configuration refuses the resume
  (mixing two models' vectors in one collection ruins it silently). Then the contents are checked
  against Qdrant: the passages at or below `last_page_id` are counted and must equal the state's
  count. A power loss can cost Qdrant the tail of its write-ahead log while the state file
  survives, and resuming on trust would skip the lost windows for good; on a mismatch the
  checkpoint steps back to just before the highest article still present (`_verified_checkpoint`)
  and a warning says so. A collection with no usable state is never deleted automatically: two
  days of vectors are not discarded on a guess.
* **Segments are capped** (`SEMANTIC_MAX_SEGMENT_KB`, 10 GB) and optimized
  `SEMANTIC_OPTIMIZATION_THREADS` at a time, for the reasons in `index.py`; the numbers here are
  the dense collection's own. The cap is large because every segment is searched and rescored
  separately: fewer segments, fewer reads per query (docs/DECISIONS.md D9). The maintenance job
  keeps the live collection equal to it.
* **Local state, no build state.** Progress lives in `state/index_semantic_<born>.json`, written
  atomically after every window. This stage never writes `build.json`: the index being served is
  fine while an embedding run is failing, and `/v1/wiki/health` must not say otherwise. It does
  hold the build lock, so a monthly build waits for it.

See docs/DESIGN.md sections 5 and 12.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Protocol

import httpx
from qdrant_client import QdrantClient, models

from wiki_mirror.build import BuildError, ensure_free_space
from wiki_mirror.config import MirrorConfig
from wiki_mirror.index import (
    _upsert,
    aliased_collection,
    applied_snapshot,
    chunker_fingerprint,
    indexable_chunks,
    indexed_chunker,
    passage_payload,
)
from wiki_mirror.records import Article, ArticleSource, passage_point_id
from wiki_mirror.store import ArticleStore, StoreError
from wiki_mirror.text.sanitize import Chunk

__all__ = [
    "ALIAS",
    "EMBEDDING_API_KEY_ENV",
    "SEGMENT_DISK_FACTOR",
    "SEMANTIC_HNSW",
    "SEMANTIC_MAX_SEGMENT_KB",
    "SEMANTIC_OPTIMIZATION_THREADS",
    "SEMANTIC_QUANTIZATION",
    "TEXT_FORM",
    "EmbedError",
    "Embedder",
    "EmbeddingClient",
    "SemanticIndexInfo",
    "checked_vectors",
    "collection_name",
    "open_source_store",
    "passage_embedding_text",
    "run_embed",
    "semantic_index_complete",
    "semantic_index_info",
    "semantic_state_file",
]

logger = logging.getLogger("wiki_mirror.embed")

#: The name semantic and hybrid search follow (`serving`). An alias, like the lexical one.
ALIAS: Final = "wikipedia_semantic"

#: `wikipedia_semantic_<born>`, where the group is the born tag (`collection_name`).
_OWN_COLLECTION: Final = re.compile(r"wikipedia_semantic_(\d{8})")

#: The environment variable holding the embedding server's key, if it wants one.
EMBEDDING_API_KEY_ENV: Final = "EMBEDDING_API_KEY"

#: Names the form `passage_embedding_text` produces; recorded in the state so a resume (and,
#: later, the query side) can tell what the vectors were made from.
TEXT_FORM: Final = "indexed_text/title+heading+text/v1"

#: Articles per window: the unit that is committed together. About 400 passages on the real
#: corpus, so a crash repeats at most a couple of minutes of work at the measured rate.
_WINDOW_ARTICLES: Final = 64

#: Windows that may exist (requests created, not yet upserted) at once. Enough that the
#: semaphore never runs dry while the oldest window finishes or its upsert runs, few enough that
#: the vectors held (about 32 KB per passage as Python floats) stay a few tens of MB.
_WINDOWS_QUEUED: Final = 4

#: Points per upsert call: a 1024-dimension vector is about 10 KB of JSON, so a request stays
#: a little over 1 MB.
_UPSERT_POINTS: Final = 128

#: Seconds between progress lines.
_PROGRESS_SECONDS: Final = 60.0

#: Largest segment Qdrant may build, in its "KB" (1024 bytes: `BYTES_IN_KB` in Qdrant v1.12.4,
#: `lib/segment/src/common/mod.rs`; it counts vector data, which for this collection is 2 KB a
#: point as float16). 10 GB, some 5.0 million passages: the optimizer
#: needs free disk the size of the segment it rewrites, and the HNSW build of one segment holds
#: its graph on the heap, so a bound is wanted; but each segment is searched separately.
#: Measured on the reference box on 2026-10-23 (Qdrant 1.12.4, limit 10), the earlier 1 GB cap
#: left 85 segments, and a first-time query caused about 1,370 major page faults and took about
#: 460 ms; with rescoring off it caused about 130 and took about 200 ms. Qdrant rescores per
#: segment, each of the 85 re-reading its own top 10 from the float16 originals (85 x 10 x about
#: 1.5 pages), and those 85 GB are never all cached. Fewer, larger segments are expected to cut
#: that roughly eight-fold, to be measured after the merge (D9). Qdrant 1.12.4
#: (`merge_optimizer.rs`, `check_condition`) merges a collection that
#: has more segments than `default_segment_number` (0 = automatic, the CPU count clamped to
#: 2..8) by taking its smallest segments, at least three at a time, while their cumulative
#: vector size stays under this value: about 9 one-GB segments a merge, ending at 10 or 11
#: segments. The costs are transient: a merge writes the new segment before it deletes the old
#: ones, and `SEMANTIC_OPTIMIZATION_THREADS` merges run at once (`SEGMENT_DISK_FACTOR` for the
#: room), and the new segment's graph is built on the heap (about 251 bytes a point, 1.26 GB for
#: a 5.0M-point segment). docs/DECISIONS.md D9. The maintenance job keeps the live collection
#: equal to this value, as a rebuilding change.
SEMANTIC_MAX_SEGMENT_KB: Final = 10_000_000

#: How much larger a segment is on disk than the vector data the cap counts: the graph, the
#: binary copy, the payload and the id tracker come on top. Measured on the reference box on
#: 2026-10-23, with `du --apparent-size --block-size=1K` (KiB): the largest segment of the
#: 1,000,000 KB cap was 1,264,748 KiB, a ratio of 1.265, so 1.27.
#: `maintain.py` multiplies the cap, and the merges running at once, by this to know the disk
#: room a rebuild needs (docs/DECISIONS.md D9).
SEGMENT_DISK_FACTOR: Final = 1.27

#: Segment optimizations (merges, graph builds, quantization) at once, for the semantic collection:
#: two (the lexical collection keeps one, `index._MAX_OPTIMIZATION_THREADS`). Two are safe because
#: each graph build holds only its own segment's graph on the heap (about 120 MB for a
#: 470,000-point segment), and they halve the time a rebuild takes. The 2026-10-23 out-of-memory
#: kill happened with one thread, on the in-RAM graph layout this replaced. docs/DECISIONS.md D8.
SEMANTIC_OPTIMIZATION_THREADS: Final = 2

#: HNSW once the load is done: Qdrant's own defaults (`m=16`, `ef_construct=100`), the setting
#: the evaluation's recall numbers assume. The graph is **on disk** (`on_disk=True`): the in-RAM
#: graph ran Qdrant out of its 20G cap on 2026-10-23, and the operator decided that day to keep
#: the mirror's pinned RAM under 10 GB, leaving the hot parts to the page cache, which gives its
#: RAM back under pressure (docs/DECISIONS.md D8). Semantic search latency on this layout was
#: measured once the rebuild had finished (D8), and the cost that D9 attacks with the segment cap
#: was measured on it. The maintenance job keeps the live collection equal to this value.
SEMANTIC_HNSW: Final = models.HnswConfigDiff(m=16, ef_construct=100, on_disk=True)

#: The binary-quantized copy of the vectors: held on disk too (`always_ram=False`), for the same
#: reason as the graph (docs/DECISIONS.md D8). Binary quantization is what lets 85 GB of float16
#: vectors be searched from a 32 GB host: the quantized copy is scanned and only the best
#: candidates are rescored from the full vectors. The maintenance job keeps the live collection
#: equal to this value.
SEMANTIC_QUANTIZATION: Final = models.BinaryQuantization(
    binary=models.BinaryQuantizationConfig(always_ram=False)
)

#: Longest a single request is retried before the stage gives up, seconds. The systemd unit
#: restarts the stage 15 minutes later and it resumes, so a longer wait here buys nothing.
_RETRY_BUDGET_SECONDS: Final = 900.0

#: First and largest backoff between retries, seconds; doubled each time. No jitter: the
#: server's `Retry-After` staggers a router's replies, and a herd of 16 retrying together is
#: answered one by one.
_BACKOFF_FIRST_SECONDS: Final = 1.0
_BACKOFF_MAX_SECONDS: Final = 60.0

#: Longest `Retry-After` honoured, seconds.
_RETRY_AFTER_MAX_SECONDS: Final = 300.0

#: Per-request timeout, seconds. A batch of eight passages takes about a second on an idle
#: server and many seconds when its queue is full.
_REQUEST_TIMEOUT_SECONDS: Final = 120.0

#: Server-side limit, seconds, for the resume check's exact count (`_count_points`). Qdrant's
#: default is 60 s; the count took 26 s on the 41M-point lexical collection, too close.
_COUNT_TIMEOUT_SECONDS: Final = 600

#: Statuses that mean "not now": rate limit, server error, bad gateway, unavailable, gateway
#: timeout. 500 is included because Ollama answers it while a model runner restarts or reloads;
#: an embedding request is idempotent, and a 500 that persists still ends the run when the retry
#: budget runs out.
_RETRYABLE_STATUSES: Final = frozenset({429, 500, 502, 503, 504})

#: What the operator is told about the refusals that have a known remedy.
_REFUSALS: Final = {
    400: "the request was refused as malformed (is the model name right?)",
    401: f"the key was refused (check {EMBEDDING_API_KEY_ENV})",
    403: f"the key was refused (check {EMBEDDING_API_KEY_ENV})",
    404: "no such endpoint or model (check embedding_url and embedding_model)",
}


class EmbedError(BuildError):
    """The embedding stage cannot continue; the message says what to do and holds no secret."""


class Embedder(Protocol):
    """What the stage needs of an embedding server: one vector per text, in order."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed one batch of texts; raise `EmbedError` rather than return a wrong shape."""
        ...


def collection_name(born: str) -> str:
    """The collection a full embedding run creates: `wikipedia_semantic_<born>`.

    Args:
        born: The tag of the snapshot whose articles are embedded (`SnapshotMeta.tag`).
    """
    return f"wikipedia_semantic_{born}"


def semantic_state_file(data_dir: Path, born: str) -> Path:
    """Where the state of collection `wikipedia_semantic_<born>` lives.

    Args:
        data_dir: The service's local data directory.
        born: The collection's born tag.
    """
    return data_dir / "state" / f"index_semantic_{born}.json"


def semantic_index_complete(data_dir: Path, collection: str, *, quiet: bool = False) -> bool:
    """Whether an embedding run has finished loading `collection` (its state says `complete`).

    The maintenance job asks this before it changes a semantic collection's settings: while a run
    is still loading, HNSW is off on purpose (`_create_collection`), and "fixing" it would build
    the graph under 41 million arriving points. Health asks it on every poll.

    Args:
        data_dir: The service's local data directory.
        collection: A collection name, normally `wikipedia_semantic_<born>`.
        quiet: Do not log an unreadable state file. Health sets it, because it must not write a
            log line per poll; the maintenance job leaves it off, so the problem is logged once
            per 15-minute run.

    Returns:
        True only when `collection` is a name this module creates and its state file reads
        `"complete": true`. A name that is not ours, an absent state and an unreadable or
        another snapshot's state are all False, so the caller waits rather than guesses.
    """
    found = _OWN_COLLECTION.fullmatch(collection)
    if found is None:
        return False
    born = found[1]
    state = _read_state(semantic_state_file(data_dir, born), born, quiet=quiet)
    return state is not None and state.complete


@dataclass(frozen=True)
class SemanticIndexInfo:
    """What a semantic collection's state file says about the vectors in it.

    The query side (`serving`, `query_embed`) needs these facts and no more: a question must be
    embedded by the model and at the length the passages were, and the index must reflect the
    snapshot being served.
    """

    #: The embedding run finished: every passage is in, HNSW is on, the alias points here.
    complete: bool
    #: The model the passages were embedded with (as the server named it, e.g. `bge-m3:latest`).
    model: str
    #: The length of every vector in the collection.
    dimensions: int
    #: The snapshot tag the vectors reflect; empty until the run completes.
    applied_snapshot: str
    #: `index.chunker_fingerprint()` of the code that cut the passages.
    chunker: str


def semantic_index_info(
    data_dir: Path, collection: str, *, quiet: bool = False
) -> SemanticIndexInfo | None:
    """What the state file of semantic collection `collection` records, or None.

    Args:
        data_dir: The service's local data directory.
        collection: A collection name, normally `wikipedia_semantic_<born>`.
        quiet: Do not log an unreadable state file (serving looks again every few seconds).

    Returns:
        The recorded facts; None for a name this module does not create, an absent or unreadable
        state, and another collection's state. The caller cannot tell those apart, and does not
        need to: none of them is a state anyone can pair a query with.
    """
    found = _OWN_COLLECTION.fullmatch(collection)
    if found is None:
        return None
    born = found[1]
    state = _read_state(semantic_state_file(data_dir, born), born, quiet=quiet)
    if state is None:
        return None
    return SemanticIndexInfo(
        complete=state.complete,
        model=state.model,
        dimensions=state.dimensions,
        applied_snapshot=state.applied_snapshot,
        chunker=state.chunker,
    )


def passage_embedding_text(article: Article, chunk: Chunk) -> str:
    """The text a passage is embedded as: its article title, heading and body (`TEXT_FORM`).

    This is `Chunk.indexed_text`, the one definition of what a passage is indexed under
    (`text.indexed_text.compose_indexed_text`): the title and the section heading are put in front
    of the passage so one deep in an article is still near its subject. It is exactly the form
    `tools/wiki_passage_eval.py` embedded when it measured dense retrieval, so the quality
    measured is the quality served. A question is embedded as plain text on the query side; it
    has no title. Changing this function without changing `TEXT_FORM` mixes two kinds of vector
    in one collection.

    Args:
        article: The article the passage was cut from (only its title is used).
        chunk: One of `index.indexable_chunks(article)`.
    """
    return chunk.indexed_text(article.title)


# -- the embedding client -------------------------------------------------------------------


class EmbeddingClient:
    """Async client for an Ollama-compatible `/api/embed`, with validation and bounded retries.

    One pooled `httpx.AsyncClient`; use it as an async context manager. The key lives only in
    this object's private state and in the `Authorization` header of each request.
    """

    def __init__(
        self,
        base_url: str,
        *,
        model: str,
        dimensions: int,
        api_key: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = _REQUEST_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure the client; nothing is sent until the first `embed`.

        Args:
            base_url: The server's origin (`MirrorConfig.embedding_url`).
            model: The model to ask for.
            dimensions: The length every returned vector must have.
            api_key: Sent as `Authorization: Bearer`; None sends no `Authorization` header.
            headers: Extra headers for every request (`MirrorConfig.embedding_headers`).
            timeout_seconds: Per-request timeout.
            transport: Test seam. Never set in production.
            sleep: Awaited between retries; tests inject a recorder.
            clock: Monotonic seconds, for the retry budget; tests inject theirs.
        """
        # httpx logs every request at INFO: millions of lines over a full run, none of them
        # useful (the URL never changes). Warnings and errors still show.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self._model = model
        self._dimensions = dimensions
        self._api_key = api_key
        self._headers = dict(headers or {})
        self._sleep = sleep
        self._clock = clock
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=timeout_seconds, transport=transport
        )

    @classmethod
    def from_config(
        cls,
        config: MirrorConfig,
        environ: Mapping[str, str],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> EmbeddingClient:
        """The client the settings describe, with the key from `environ`.

        Args:
            config: Service settings (`embedding_*`).
            environ: The process environment; only `EMBEDDING_API_KEY` is read.
            transport: Test seam. Never set in production.

        Raises:
            EmbedError: `embedding_url` is not set.
        """
        if config.embedding_url is None:
            msg = "embedding_url is not set in mirror.yaml"
            raise EmbedError(msg)
        key = environ.get(EMBEDDING_API_KEY_ENV, "").strip()
        return cls(
            config.embedding_url,
            model=config.embedding_model,
            dimensions=config.embedding_dimensions,
            api_key=key or None,
            headers=config.embedding_headers,
            transport=transport,
        )

    async def __aenter__(self) -> EmbeddingClient:
        """Enter the context; the connection pool is already open."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed `texts` with the configured model: one vector per input, in order.

        Args:
            texts: One batch of inputs (never logged).

        Returns:
            The vectors, each `dimensions` finite floats.

        Raises:
            EmbedError: The server refused the request, kept failing for
                `_RETRY_BUDGET_SECONDS`, or answered something other than exactly one valid
                vector per text.
        """
        response = await self._post({"model": self._model, "input": list(texts)})
        try:
            reply = response.json()
        except ValueError:
            msg = "the embedding server answered something that is not JSON"
            raise EmbedError(msg) from None
        vectors = reply.get("embeddings") if isinstance(reply, dict) else None
        return checked_vectors(vectors, len(texts), self._dimensions)

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        """POST one request, retrying what is transient (the module docstring).

        Raises:
            EmbedError: A refusal, or the retry budget ran out. The message is fixed text and
                numbers: no URL, header, body or input.
        """
        started = self._clock()
        attempt = 0
        while True:
            attempt += 1
            hint: float | None = None
            try:
                response = await self._http.post(
                    "/api/embed", json=body, headers=self._request_headers()
                )
            except httpx.TimeoutException:
                problem = "timed out"
            except httpx.TransportError as exc:
                problem = f"failed in transport ({type(exc).__name__})"
            else:
                status = response.status_code
                if status == httpx.codes.OK:
                    return response
                if status not in _RETRYABLE_STATUSES:
                    detail = _REFUSALS.get(status, "unexpected answer")
                    msg = f"the embedding server answered {status}: {detail}"
                    raise EmbedError(msg)
                problem = f"answered {status}"
                hint = _retry_after_seconds(response)
            delay = (
                max(_BACKOFF_FIRST_SECONDS, min(hint, _RETRY_AFTER_MAX_SECONDS))
                if hint is not None
                else min(_BACKOFF_FIRST_SECONDS * 2 ** (attempt - 1), _BACKOFF_MAX_SECONDS)
            )
            waited = self._clock() - started
            if waited + delay > _RETRY_BUDGET_SECONDS:
                msg = (
                    f"the embedding server kept failing for {waited:.0f} s ({problem}); "
                    f"gave up after {attempt} tries"
                )
                raise EmbedError(msg)
            logger.warning(
                "embedding request %s (try %d); retrying in %.0f s", problem, attempt, delay
            )
            await self._sleep(delay)

    def _request_headers(self) -> dict[str, str]:
        """The headers of one request: the configured ones, a fresh request id, the key."""
        headers = dict(self._headers)
        # 64 characters of alphanumerics and `- _ . :` is the cluster's request-id rule.
        headers["X-Request-ID"] = f"wiki-embed-{uuid.uuid4().hex[:12]}"
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """The `Retry-After` of a reply in seconds, or None when absent or not a plain number.

    An HTTP-date form is not parsed: the servers this talks to send seconds, and the exponential
    backoff covers the rest.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def checked_vectors(vectors: object, count: int, dimensions: int) -> list[list[float]]:
    """Return `vectors` as floats if there are exactly `count` of `dimensions` finite numbers.

    The one definition of a valid `/api/embed` reply, shared by the bulk client above and the
    API's query embedding (`query_embed`): a vector on the wrong point is as bad for a question
    as for a passage.

    Raises:
        EmbedError: Anything else. The message holds counts only, never a value.
    """
    if not isinstance(vectors, list) or len(vectors) != count:
        got = len(vectors) if isinstance(vectors, list) else "none"
        msg = f"the embedding server returned {got} vectors for {count} inputs"
        raise EmbedError(msg)
    checked: list[list[float]] = []
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dimensions:
            msg = f"the embedding server returned a vector that is not {dimensions} numbers long"
            raise EmbedError(msg)
        numbers: list[float] = []
        for position, number in enumerate(vector):
            try:
                if isinstance(number, bool) or not isinstance(number, int | float):
                    # A string would convert too; a bool is a number to Python, not to JSON.
                    raise TypeError
                # Converted before the finiteness check: a JSON integer of 400 digits is a valid
                # Python int that `math.isfinite` cannot take (OverflowError), and one too big
                # for a float is as bad as infinity.
                value = float(number)
            except (OverflowError, TypeError):
                value = math.nan
            if not math.isfinite(value):
                msg = (
                    "the embedding server returned a vector with a non-finite or other "
                    f"non-number at position {position}"
                )
                raise EmbedError(msg)
            numbers.append(value)
        checked.append(numbers)
    return checked


# -- the stage ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _EmbedState:
    """One semantic collection's state: its checkpoint and what its vectors are made of.

    Kept on local disk (`semantic_state_file`) and rewritten atomically after every window.
    """

    #: The tag of the snapshot whose articles are embedded: the `<born>` of the collection's name.
    born: str
    #: Every article with a page id at or below this is fully in the collection.
    last_page_id: int
    articles: int
    passages: int
    over_stride: int
    #: Passages with no text at all, left out.
    empty: int
    #: Every article is in, HNSW is on and the alias points here.
    complete: bool
    #: `index.chunker_fingerprint()` of the code that cut the passages.
    chunker: str
    model: str
    dimensions: int
    text_form: str
    #: The snapshot the vectors reflect; empty until the run completes.
    applied_snapshot: str = ""

    def as_json(self) -> str:
        """The state file's contents."""
        return json.dumps(
            {
                "born": self.born,
                "last_page_id": self.last_page_id,
                "articles": self.articles,
                "passages": self.passages,
                "over_stride": self.over_stride,
                "empty": self.empty,
                "complete": self.complete,
                "chunker": self.chunker,
                "model": self.model,
                "dimensions": self.dimensions,
                "text_form": self.text_form,
                "applied_snapshot": self.applied_snapshot,
            }
        )


@dataclass
class _Window:
    """A group of articles whose passages are being embedded; committed whole, in order."""

    #: The highest page id in the window: the checkpoint once it is upserted.
    last_page_id: int
    articles: int
    over_stride: int
    empty: int
    #: Point id and payload of each embedded passage, in the order of the requests' texts.
    point_ids: list[int]
    payloads: list[dict[str, Any]]
    #: One task per request of `embedding_batch` texts, in order.
    tasks: list[asyncio.Task[list[list[float]]]]


def open_source_store(config: MirrorConfig, client: QdrantClient) -> ArticleStore:
    """Open the article store that the live lexical collection serves.

    The semantic collection is built from exactly the text the lexical one was built from, so
    that a passage number means the same cut in both and a later fusion joins them.

    Args:
        config: Service settings.
        client: A Qdrant client for the loopback server.

    Returns:
        The store, read-only; the caller closes it.

    Raises:
        EmbedError: There is no lexical collection, it has no completed index state, it was cut
            by another chunker than the running code's, or its store is missing.
    """
    collection = aliased_collection(client)
    if collection is None:
        msg = "there is no lexical collection to embed from; run a build first (build-now)"
        raise EmbedError(msg)
    tag = applied_snapshot(config.data_dir, collection)
    if tag is None:
        msg = f"{collection} has no completed index state, so the snapshot to embed is unknown"
        raise EmbedError(msg)
    cut = indexed_chunker(config.data_dir, collection)
    if cut is not None and cut != chunker_fingerprint():
        msg = (
            f"{collection} was cut by chunker {cut!r} and this code cuts with "
            f"{chunker_fingerprint()!r}; the passages would not match, rebuild the index first"
        )
        raise EmbedError(msg)
    # Imported here, not at the top, because `serving` follows the semantic alias and so imports
    # this module: a top-level import in both directions cannot be resolved.
    from wiki_mirror.serving import open_store_for_snapshot

    try:
        return open_store_for_snapshot(config, tag)
    except StoreError as exc:
        msg = f"cannot open the article store for the lexical collection: {exc}"
        raise EmbedError(msg) from exc


async def run_embed(
    config: MirrorConfig,
    store: ArticleSource,
    client: QdrantClient,
    embedder: Embedder,
    *,
    clock: Callable[[], float] = time.monotonic,
    window_articles: int = _WINDOW_ARTICLES,
    windows_queued: int = _WINDOWS_QUEUED,
    progress_seconds: float = _PROGRESS_SECONDS,
) -> int:
    """Embed every passage of a store into `wikipedia_semantic_<born>` (creating or resuming it).

    Creates the collection (or resumes it from `state/index_semantic_<born>.json`), embeds and
    upserts every article past the checkpoint, then switches HNSW on, points the alias
    `wikipedia_semantic` at the collection and records the run complete. A complete state is
    left alone.

    Args:
        config: Service settings (`data_dir`, `min_free_disk_gb`, `embedding_*`).
        store: The article store to embed, read in page-id order.
        client: A Qdrant client for the loopback server.
        embedder: Provides one vector per text; closed by the caller.
        clock: Monotonic seconds, for progress lines.
        window_articles: Articles per window (the commit unit).
        windows_queued: Windows that may be in progress at once.
        progress_seconds: Seconds between progress lines.

    Returns:
        The number of passages in the collection.

    Raises:
        EmbedError: The store is empty; a resume would mix vectors made differently (another
            chunker, model, dimension or text form); the collection exists without a usable
            state; or the embedding server failed for good.
        BuildError: A new collection is needed and the disk is below `min_free_disk_gb`.
        Exception: Whatever Qdrant or the store raises. The state is intact: run again to resume.
    """
    snapshot = store.snapshot
    name = collection_name(snapshot.tag)
    state_path = semantic_state_file(config.data_dir, snapshot.tag)
    total = store.count()
    if total == 0:
        msg = f"the article store for {snapshot.date} is empty; nothing to embed"
        raise EmbedError(msg)
    state = _begin(config, client, name, snapshot.tag, state_path)
    if state.complete:
        logger.info("embed %s: already complete (%d passages); nothing to do", name, state.passages)
        return state.passages
    state = await _embed_articles(
        config,
        store,
        client,
        embedder,
        name,
        state,
        state_path,
        total=total,
        clock=clock,
        window_articles=window_articles,
        windows_queued=windows_queued,
        progress_seconds=progress_seconds,
    )
    _finish(client, name)
    done = replace(state, complete=True, applied_snapshot=state.born)
    _write_state(state_path, done)
    if done.over_stride or done.empty:
        logger.warning(
            "embed %s: %d passages beyond the id stride and %d with no text were left out",
            name,
            done.over_stride,
            done.empty,
        )
    logger.info(
        "embed %s complete: %d passages from %d articles; Qdrant now builds the HNSW graphs in the "
        "background, which outlasts this run; watch the collection's status for green",
        name,
        done.passages,
        done.articles,
    )
    return done.passages


def _begin(
    config: MirrorConfig, client: QdrantClient, name: str, born: str, state_path: Path
) -> _EmbedState:
    """Resume the collection from its state, or create it and write the first state.

    Raises:
        EmbedError: The collection exists but its state is missing, unreadable or another's
            (never deleted automatically), or the state records a different chunker, model,
            dimension or text form than this run's.
        BuildError: A collection must be created and the disk is below `min_free_disk_gb`.
    """
    fresh = _EmbedState(
        born=born,
        last_page_id=-1,
        articles=0,
        passages=0,
        over_stride=0,
        empty=0,
        complete=False,
        chunker=chunker_fingerprint(),
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
        text_form=TEXT_FORM,
    )
    state = _read_state(state_path, born)
    exists = client.collection_exists(name)
    if exists and state is None:
        msg = (
            f"{name} exists but has no usable state ({state_path.name} is missing, unreadable "
            "or another snapshot's); delete the collection by hand to start again"
        )
        raise EmbedError(msg)
    if state is not None and exists:
        _refuse_unless_same_vectors(state, fresh, name, state_path)
        if state.complete:
            # A finished run never rescans: nothing will be embedded, so nothing needs checking.
            return state
        checked = _verified_checkpoint(client, name, state)
        if checked != state:
            # Before any embedding, so a crash right after this still resumes from the true
            # position rather than from the lost one.
            _write_state(state_path, checked)
        logger.info("embed %s: resuming after page id %d", name, checked.last_page_id)
        return checked
    if state is not None:
        # Deleted by hand: the checkpoint no longer describes anything, so the run starts over.
        logger.warning("embed %s: the collection is gone; starting it over", name)
    ensure_free_space(config.data_dir, config.min_free_disk_gb, f"the collection {name}")
    _create_collection(client, name, config.embedding_dimensions)
    # Written at once, so a crash after this point leaves a collection that can be resumed
    # (without a state it would be refused, above).
    _write_state(state_path, fresh)
    previous = _alias_target(client)
    logger.info(
        "embed %s: created; the alias %s points at %s until it completes",
        name,
        ALIAS,
        previous or "nothing",
    )
    return fresh


def _refuse_unless_same_vectors(
    state: _EmbedState, wanted: _EmbedState, name: str, state_path: Path
) -> None:
    """Raise if the state's vectors were made differently from what this run would make.

    Raises:
        EmbedError: Naming each difference (these values are settings, not secrets).
    """
    differences = [
        f"{label} {had!r} -> {now!r}"
        for label, had, now in (
            ("chunker", state.chunker, wanted.chunker),
            ("model", state.model, wanted.model),
            ("dimensions", state.dimensions, wanted.dimensions),
            ("text form", state.text_form, wanted.text_form),
        )
        if had != now
    ]
    if differences:
        msg = (
            f"{name} was started with different settings ({'; '.join(differences)}) and its "
            f"vectors would not mix; to start over delete the collection and {state_path.name}"
        )
        raise EmbedError(msg)


def _verified_checkpoint(client: QdrantClient, name: str, state: _EmbedState) -> _EmbedState:
    """Return `state` if Qdrant holds what its checkpoint claims, else a state stepped back.

    The checkpoint is written only after Qdrant confirmed the upsert, but a power loss or host
    crash can lose the tail of Qdrant's write-ahead log while the state file survives; resuming
    would then skip the lost windows silently, and search would never find those articles. So the
    number of passages at or below `last_page_id` is counted and compared with `state.passages`.

    On a mismatch (fewer points, or more) the checkpoint moves to just before the highest page id
    that is present at or below it. Qdrant's log loses a suffix, and windows are upserted in
    page-id order, so everything below that article is intact; the article itself may have been
    cut off mid-way, so it is embedded again (its point ids are deterministic, so what survived is
    simply overwritten). Points beyond the checkpoint are not a mismatch: they are the windows
    that were upserted before a crash and not yet recorded, and the run repeats them harmlessly.

    Cost: one exact filtered count at resume, about half a minute on 41 million points; it runs
    only on resume, never for a new or a finished run. The step-back (a mismatch only) adds one
    ordered scroll (instant on the live server) and one more count.

    Args:
        client: A Qdrant client for the loopback server.
        name: The semantic collection, which exists.
        state: The state read from disk, not complete.

    Returns:
        `state` itself when the count matches; otherwise a copy with `last_page_id`, `articles`
        and `passages` corrected. The caller writes it to disk.
    """
    held = _count_points(client, name, state.last_page_id)
    if held == state.passages:
        logger.info(
            "embed %s: checkpoint verified, %d passages at or below page id %d",
            name,
            held,
            state.last_page_id,
        )
        return state
    highest = _highest_page_id(client, name, state.last_page_id)
    if highest is None:
        # Nothing at or below the checkpoint survived: start the collection's content over.
        # over_stride and empty only describe windows that were committed, and none is.
        corrected = replace(state, last_page_id=-1, articles=0, passages=0, over_stride=0, empty=0)
    else:
        # Article `highest` is re-embedded, as it may be partial. Page ids need not be dense:
        # the checkpoint is a bound ("at or below"), not the id of an article.
        before = highest - 1
        passages = _count_points(client, name, before)
        corrected = replace(
            state,
            last_page_id=before,
            # In proportion to the passages kept: the article count feeds only the ETA, and an
            # exact one would filter on `chunk`, which has no payload index; measured on the live
            # 41M-point lexical collection (2026-10-19) that count hit Qdrant's 60 s server-side
            # timeout, which would fail every resume after a loss.
            articles=round(state.articles * passages / state.passages) if state.passages else 0,
            passages=passages,
        )
        # over_stride and empty are left as they were: they cannot be recounted without cutting
        # the articles again, and the windows now repeated will add theirs a second time. They
        # feed only the closing warning, never the checkpoint.
    logger.warning(
        "embed %s: the checkpoint at page id %d (%d passages) does not match Qdrant, which holds "
        "%d passages at or below it; resuming after page id %d (%d passages)",
        name,
        state.last_page_id,
        state.passages,
        held,
        corrected.last_page_id,
        corrected.passages,
    )
    return corrected


def _count_points(client: QdrantClient, name: str, last_page_id: int) -> int:
    """Exact number of points whose `page_id` is at or below `last_page_id`.

    It uses the `page_id` payload index. Measured on the live 41M-point lexical collection
    (2026-10-19), the same count took 26 s; Qdrant's own server-side limit is 60 s, so the count
    asks for `_COUNT_TIMEOUT_SECONDS` explicitly rather than fail a resume on a busy box.

    Args:
        client: A Qdrant client.
        name: The collection.
        last_page_id: The inclusive bound.
    """
    return client.count(
        collection_name=name,
        count_filter=models.Filter(
            must=[models.FieldCondition(key="page_id", range=models.Range(lte=last_page_id))]
        ),
        exact=True,
        timeout=_COUNT_TIMEOUT_SECONDS,
    ).count


def _highest_page_id(client: QdrantClient, name: str, last_page_id: int) -> int | None:
    """The highest `page_id` held at or below `last_page_id`, or None if there is no such point.

    One ordered scroll (`order_by` on the integer payload index `_create_collection` makes, which
    Qdrant has supported since 1.8; production runs 1.12): descending, one point, payload limited
    to `page_id`. `start_from` begins the walk at the checkpoint rather than at the highest
    page id of the tail beyond it, and the filter keeps the bound exact. A binary search with
    exact counts was the alternative; it would cost some 25 counts over 41 million points where
    this reads one index entry.
    """
    points, _ = client.scroll(
        collection_name=name,
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(key="page_id", range=models.Range(lte=last_page_id)),
            ]
        ),
        order_by=models.OrderBy(
            key="page_id", direction=models.Direction.DESC, start_from=last_page_id
        ),
        limit=1,
        with_payload=["page_id"],
        with_vectors=False,
    )
    if not points or points[0].payload is None:
        return None
    return int(points[0].payload["page_id"])


def _create_collection(client: QdrantClient, name: str, dimensions: int) -> None:
    """Create the dense collection (HNSW off for the load) and the `page_id` payload index.

    One unnamed vector: cosine, float16 on disk, binary-quantized per `SEMANTIC_QUANTIZATION`
    (the copy on disk too). `m=0` turns HNSW off until `_finish` sets `SEMANTIC_HNSW`; the
    optimizer runs `SEMANTIC_OPTIMIZATION_THREADS` threads. The payload index is the same on-disk
    integer index as the lexical collection's, for W3's per-article selection.
    """
    client.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(
            size=dimensions,
            distance=models.Distance.COSINE,
            datatype=models.Datatype.FLOAT16,
            on_disk=True,
        ),
        hnsw_config=models.HnswConfigDiff(m=0),
        quantization_config=SEMANTIC_QUANTIZATION,
        on_disk_payload=True,
        optimizers_config=models.OptimizersConfigDiff(
            max_segment_size=SEMANTIC_MAX_SEGMENT_KB,
            max_optimization_threads=SEMANTIC_OPTIMIZATION_THREADS,
        ),
    )
    client.create_payload_index(
        collection_name=name,
        field_name="page_id",
        field_schema=models.IntegerIndexParams(type=models.IntegerIndexType.INTEGER, on_disk=True),
    )


async def _embed_articles(
    config: MirrorConfig,
    store: ArticleSource,
    client: QdrantClient,
    embedder: Embedder,
    name: str,
    start: _EmbedState,
    state_path: Path,
    *,
    total: int,
    clock: Callable[[], float],
    window_articles: int,
    windows_queued: int,
    progress_seconds: float,
) -> _EmbedState:
    """Embed and upsert every article past the checkpoint; return the state, not yet complete.

    The loop is the per-article pass over `iter_articles(after=<checkpoint>)`; a later mode that
    skips articles whose `text_hash` is unchanged slots in between the iterator and
    `_start_window`.
    """
    semaphore = asyncio.Semaphore(config.embedding_concurrency)
    pending: deque[_Window] = deque()
    state = start
    began = clock()
    progress = _Progress(clock, began, progress_seconds, start.articles, start.passages)
    windows = _article_windows(store.iter_articles(after=start.last_page_id), window_articles)
    try:
        for articles in windows:
            pending.append(
                _start_window(articles, embedder, semaphore, config.embedding_batch)
            )
            while len(pending) >= windows_queued:
                state = await _commit(client, name, pending[0], state, state_path)
                pending.popleft()
                progress.report(name, state, total)
        while pending:
            state = await _commit(client, name, pending[0], state, state_path)
            pending.popleft()
            progress.report(name, state, total)
    finally:
        # A failure leaves requests in flight for windows that will never be committed: stop
        # them, and collect their exceptions so none is reported as never retrieved.
        tasks = [task for window in pending for task in window.tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return state


def _article_windows(articles: Iterable[Article], size: int) -> Iterator[tuple[Article, ...]]:
    """Group articles into windows of `size`, keeping page-id order."""
    window: list[Article] = []
    for article in articles:
        window.append(article)
        if len(window) >= size:
            yield tuple(window)
            window = []
    if window:
        yield tuple(window)


def _start_window(
    articles: tuple[Article, ...],
    embedder: Embedder,
    semaphore: asyncio.Semaphore,
    batch: int,
) -> _Window:
    """Cut a window's articles into passages and start one request task per `batch` texts.

    The passages are cut exactly as the lexical build cuts them (`index.indexable_chunks`).
    The tasks wait on the shared semaphore, so creating a window does not send anything
    beyond `embedding_concurrency` requests; they are created in order, and the semaphore
    admits them in that order, so the oldest window's requests go first.
    """
    point_ids: list[int] = []
    payloads: list[dict[str, Any]] = []
    texts: list[str] = []
    over_stride = empty = 0
    for article in articles:
        chunks, dropped = indexable_chunks(article)
        over_stride += dropped
        for chunk in chunks:
            text = passage_embedding_text(article, chunk)
            if not text.strip():
                empty += 1
                continue
            point_ids.append(passage_point_id(article.page_id, chunk.index))
            payloads.append(passage_payload(article, chunk))
            texts.append(text)
    tasks = [
        asyncio.create_task(_embed_batch(embedder, semaphore, texts[first : first + batch]))
        for first in range(0, len(texts), batch)
    ]
    return _Window(
        last_page_id=articles[-1].page_id,
        articles=len(articles),
        over_stride=over_stride,
        empty=empty,
        point_ids=point_ids,
        payloads=payloads,
        tasks=tasks,
    )


async def _embed_batch(
    embedder: Embedder, semaphore: asyncio.Semaphore, texts: list[str]
) -> list[list[float]]:
    """Embed one request's texts once the semaphore admits it."""
    async with semaphore:
        return await embedder.embed(texts)


async def _commit(
    client: QdrantClient, name: str, window: _Window, state: _EmbedState, state_path: Path
) -> _EmbedState:
    """Wait for one window's vectors, upsert them, and move the checkpoint past it.

    Called for the oldest window only, which is what keeps the checkpoint honest. The state is
    written after the upsert returns (Qdrant has the points), never before.

    Raises:
        EmbedError: A request failed for good, or the vectors do not match the passages; nothing
            of this window has been upserted.
    """
    replies = await asyncio.gather(*window.tasks)
    vectors = [vector for reply in replies for vector in reply]
    if len(vectors) != len(window.point_ids):
        # The client checks every reply; this guards a custom embedder, because zipping short
        # would put vectors on the wrong points.
        msg = f"got {len(vectors)} vectors for {len(window.point_ids)} passages"
        raise EmbedError(msg)
    for first in range(0, len(vectors), _UPSERT_POINTS):
        points = [
            models.PointStruct(id=point_id, vector=vector, payload=payload)
            for point_id, vector, payload in zip(
                window.point_ids[first : first + _UPSERT_POINTS],
                vectors[first : first + _UPSERT_POINTS],
                window.payloads[first : first + _UPSERT_POINTS],
                strict=True,
            )
        ]
        # In a thread: an upsert can wait a minute on Qdrant's optimizer, and the requests of
        # the windows behind this one must keep flowing meanwhile.
        await asyncio.to_thread(_upsert, client, name, points)
    state = replace(
        state,
        last_page_id=window.last_page_id,
        articles=state.articles + window.articles,
        passages=state.passages + len(vectors),
        over_stride=state.over_stride + window.over_stride,
        empty=state.empty + window.empty,
    )
    _write_state(state_path, state)
    return state


def _finish(client: QdrantClient, name: str) -> None:
    """Turn HNSW on (`SEMANTIC_HNSW`) for the finished collection and point the alias at it.

    Both are idempotent, so a run that died between them and the final state write simply
    repeats them. Qdrant builds the graphs in the background; this does not wait for that.
    """
    client.update_collection(collection_name=name, hnsw_config=SEMANTIC_HNSW)
    previous = _alias_target(client)
    operations: list[models.AliasOperations] = []
    if previous is not None:
        operations.append(
            models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=ALIAS))
        )
    operations.append(
        models.CreateAliasOperation(
            create_alias=models.CreateAlias(collection_name=name, alias_name=ALIAS)
        )
    )
    client.update_collection_aliases(change_aliases_operations=operations)
    logger.info("embed %s: HNSW switched on; alias %s now points here", name, ALIAS)


def _alias_target(client: QdrantClient) -> str | None:
    """The collection the alias `wikipedia_semantic` points at now, or None."""
    for entry in client.get_aliases().aliases:
        if entry.alias_name == ALIAS:
            return str(entry.collection_name)
    return None


class _Progress:
    """Writes a progress line at most every `interval` seconds: counts and rates, never text."""

    def __init__(
        self,
        clock: Callable[[], float],
        began: float,
        interval: float,
        articles: int,
        passages: int,
    ) -> None:
        """Start measuring from the run's beginning.

        Args:
            clock: Monotonic seconds.
            began: The clock at the start of this run.
            interval: Seconds between lines.
            articles: Articles already done when this run began (a resume).
            passages: Passages already done when this run began.
        """
        self._clock = clock
        self._began = began
        self._interval = interval
        self._articles0 = articles
        self._passages0 = passages
        self._last_time = began
        self._last_passages = passages

    def report(self, name: str, state: _EmbedState, total: int) -> None:
        """Log a line if the interval has passed: done so far, rates, and the time left."""
        now = self._clock()
        if now - self._last_time < self._interval:
            return
        recent = (state.passages - self._last_passages) / (now - self._last_time)
        elapsed = now - self._began
        overall = (state.passages - self._passages0) / elapsed
        article_rate = (state.articles - self._articles0) / elapsed
        left = (
            _duration((total - state.articles) / article_rate) if article_rate > 0 else "unknown"
        )
        logger.info(
            "embed %s: %d of %d articles (%.1f%%), %d passages; %.0f passages/s over the last "
            "%.0f s, %.0f overall; about %s left",
            name,
            state.articles,
            total,
            100 * state.articles / total,
            state.passages,
            recent,
            now - self._last_time,
            overall,
            left,
        )
        self._last_time = now
        self._last_passages = state.passages


def _duration(seconds: float) -> str:
    """A rough duration for a progress line: `1d 3h`, `5h 12m` or `7m`."""
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _read_state(path: Path, born: str, *, quiet: bool = False) -> _EmbedState | None:
    """Load the state, or None if there is none, it is unreadable, or it is another's.

    An unreadable state is logged and reported as none; `_begin` then refuses to touch a
    collection that exists without one, rather than discard it. `quiet` suppresses that log line
    for callers that look on every poll (`semantic_index_complete`).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        state = _EmbedState(
            born=str(raw["born"]),
            last_page_id=int(raw["last_page_id"]),
            articles=int(raw["articles"]),
            passages=int(raw["passages"]),
            over_stride=int(raw["over_stride"]),
            empty=int(raw["empty"]),
            complete=bool(raw["complete"]),
            chunker=str(raw["chunker"]),
            model=str(raw["model"]),
            dimensions=int(raw["dimensions"]),
            text_form=str(raw["text_form"]),
            applied_snapshot=str(raw.get("applied_snapshot") or ""),
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        if not quiet:
            logger.warning("embed state %s is unreadable", path)
        return None
    return state if state.born == born else None


def _write_state(path: Path, state: _EmbedState) -> None:
    """Write the state atomically (temp file, then rename) so a crash never tears it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(path.suffix + ".tmp")
    scratch.write_text(state.as_json(), encoding="utf-8")
    scratch.replace(path)
