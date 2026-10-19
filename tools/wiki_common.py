"""Shared plumbing for the Wikipedia passage-length evaluation tools.

``tools/wiki_golden.py`` (writes the golden set) and ``tools/wiki_passage_eval.py`` (scores
passage schemes against it) both read articles from the mirror, and both call an
Ollama-compatible inference router. This module holds what they share: reading a whole article
from the mirror, mirror search, a small router client (``/api/embed`` and ``/api/chat``),
request ids, a uniform page-id sampler and the casefold-and-collapse text normalisation both
tools compare with.

Non-obvious constraints:

* **No ``zstandard`` and no ``qdrant_client``.** The tools are meant to run on a client box, which
  has numpy and httpx but neither, so nothing here imports ``wiki_mirror.index``,
  ``.build`` or ``.store``. The mirror is reached through ``MirrorClient`` below, a minimal
  client of its HTTP API: a bearer key, an ``X-Request-ID`` and typed errors, so this repository
  needs nothing from any client platform.
* **Keys come from the environment only** (``WIKI_MIRROR_API_KEY`` for the mirror,
  ``EMBEDDING_API_KEY`` for the embedding server) and are sent as
  bearer headers; nothing here prints, logs or puts one in an exception. Error messages carry
  status codes and router error *codes*, never a body that could quote a question or an article.
* **Retries are bounded and never blind.** A router 429, 502, 503 or 504 is retried after its
  ``Retry-After`` (capped at 10 s, so a misbehaving node cannot stall a run), a timeout after a
  short backoff; after ``attempts`` tries the error is raised. Sleep is injectable so tests never
  wait.
* **A page that cannot be read is skipped and counted, not fatal** (``ReadGuard``), until twenty
  in a row fail: then the mirror is down or refusing the key and the run stops.

See docs/DESIGN.md section 12.5.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final

import httpx

__all__ = [
    "DEFAULT_MAX_PAGE_ID",
    "DEFAULT_MIRROR",
    "DEFAULT_ROUTER",
    "MIRROR_KEY_ENV",
    "ROUTER_KEY_ENV",
    "Article",
    "ArticleReadError",
    "MirrorClient",
    "MirrorRequestError",
    "MirrorServiceError",
    "MirrorUnavailableError",
    "ReadGuard",
    "ReadResult",
    "RouterClient",
    "RouterContentError",
    "RouterError",
    "Sleep",
    "describe_failure",
    "fold",
    "new_request_id",
    "page_id_stream",
    "read_article",
    "search_ids",
]

#: The mirror's API (docs/DESIGN.md section 5): the port the service listens on, on this machine.
#: Nobody's network is built in; point the tools at a deployment with ``--mirror``.
DEFAULT_MIRROR: Final = "http://localhost:8300"

#: The inference router: an Ollama-compatible API in front of the embedding and chat nodes.
#: 11434 is Ollama's own port, so a server on this machine works unconfigured; for anything
#: else give ``--router``.
DEFAULT_ROUTER: Final = "http://localhost:11434"

#: Env var holding the router's bearer key.
ROUTER_KEY_ENV: Final = "EMBEDDING_API_KEY"

#: Env var holding the mirror's bearer key. Read at call time, so fixing a missing key needs no
#: restart and no long-lived object holds it.
MIRROR_KEY_ENV: Final = "WIKI_MIRROR_API_KEY"

#: The largest page id of snapshot 2026-10-01, measured. Ids are sparse (about 8.6% of the ids in
#: [1, max] are articles), so a uniform integer that hits an article is a uniform article.
DEFAULT_MAX_PAGE_ID: Final = 84_378_654

#: Longest ``X-Request-ID`` the router and the mirror accept (`wiki_mirror.api`,
#: ``_REQUEST_ID``).
_MAX_REQUEST_ID_CHARS: Final = 64

#: The longest wait honoured from a ``Retry-After``; also the backoff ceiling.
_MAX_RETRY_WAIT: Final = 10.0

#: Attempts for a mirror call that failed as unavailable (a timeout, a reset): a 15,000-article
#: fetch should not die on one dropped connection, but a refused key must not spin either.
_MIRROR_ATTEMPTS: Final = 3

#: Router statuses worth another try within the attempt budget: rate limited, bad gateway,
#: service unavailable (a node busy or restarting) and gateway timeout. Anything else is final.
_RETRY_STATUSES: Final = frozenset({429, 502, 503, 504})

_TOO_MANY_REQUESTS: Final = 429

#: Consecutive page reads that may fail (after their own retries) before the run stops. A single
#: bad page is skipped and counted; twenty in a row is a mirror that is down or refusing our key.
_MAX_CONSECUTIVE_READ_FAILURES: Final = 20

_WHITESPACE = re.compile(r"\s+")
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")

#: Sleep signature shared by the retry loops, so a test substitutes a recorder.
Sleep = Callable[[float], Awaitable[None]]


class MirrorServiceError(RuntimeError):
    """Base for every way a mirror call can fail."""


class MirrorUnavailableError(MirrorServiceError):
    """The mirror could not be used: unreachable, refused our key, or still building."""


class MirrorRequestError(MirrorServiceError):
    """The mirror understood the request and refused it - a 404 or a bad argument.

    Carries the status so a caller can tell "no such article" (404) from a malformed request
    without parsing prose.
    """

    def __init__(self, message: str, *, status: int = 0) -> None:
        """Keep the status beside the message."""
        super().__init__(message)
        self.status = status


@dataclass
class MirrorClient:
    """One authenticated call at a time to the mirror's HTTP API.

    A client per call rather than a pooled one: the tools' calls are not a hot path and a pool
    would need a lifecycle for no measured gain. See the module docstring.

    Attributes:
        base_url: The service origin, e.g. ``http://192.168.1.50:8300``.
        timeout_seconds: Per-call budget.
        transport: Test seam. Never set in production.
    """

    base_url: str
    timeout_seconds: float = 10.0
    transport: httpx.AsyncBaseTransport | None = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        request_id: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Make one authenticated call and return the decoded JSON body.

        Args:
            method: HTTP verb.
            path: Path beneath ``base_url``.
            request_id: Sent as ``X-Request-ID`` so one identifier spans both systems' logs.
            params: Query parameters.
            json_body: Request body, when there is one.

        Returns:
            The decoded JSON body.

        Raises:
            MirrorUnavailableError: No key configured, unreachable, timed out, the key
                refused (401), the mirror not ready (503), any other 5xx, or a body that is
                not JSON.
            MirrorRequestError: The service returned another 4xx (404 for no such article).
        """
        token = os.environ.get(MIRROR_KEY_ENV, "").strip()
        if not token:
            raise MirrorUnavailableError(
                f"{MIRROR_KEY_ENV} is not set, so the Wikipedia mirror cannot be authenticated."
            )
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=self.timeout_seconds, transport=self.transport
            ) as client:
                response = await client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Request-ID": request_id,
                    },
                )
        except httpx.TimeoutException as exc:
            raise MirrorUnavailableError(
                f"the Wikipedia mirror is unreachable: no answer within "
                f"{self.timeout_seconds:.0f}s"
            ) from exc
        except httpx.HTTPError as exc:
            # The exception text can carry the URL but not the key or the query body.
            raise MirrorUnavailableError(
                f"the Wikipedia mirror is unreachable at {self.base_url}: {type(exc).__name__}"
            ) from exc

        status = response.status_code
        if status == 401:
            raise MirrorUnavailableError(
                f"the Wikipedia mirror refused the key in {MIRROR_KEY_ENV} (401); the caller's "
                f"env file and the mirror's key disagree."
            )
        if status == 503:
            raise MirrorUnavailableError(
                "the Wikipedia mirror is still building and cannot answer yet "
                f"({_detail(response)})"
            )
        if 400 <= status < 500:
            raise MirrorRequestError(
                f"the Wikipedia mirror refused the request: {status} {_detail(response)}",
                status=status,
            )
        if status >= 500:
            raise MirrorUnavailableError(
                f"the Wikipedia mirror failed: {status} {_detail(response)}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise MirrorUnavailableError(
                "the Wikipedia mirror returned a body that is not JSON"
            ) from exc


def _detail(response: httpx.Response) -> str:
    """The service's ``detail`` (or ``error``) when it sent one, else a truncated body."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        for key in ("detail", "error"):
            if key in body:
                return str(body[key])[:200]
    return response.text[:200]


class ArticleReadError(RuntimeError):
    """The mirror answered an article read with something that cannot be assembled."""


class RouterError(RuntimeError):
    """A router call failed for good: a refusal, a malformed reply, or retries exhausted."""


class RouterContentError(RouterError):
    """The router answered, but what the model wrote is not a JSON object.

    Split from ``RouterError`` because it is the model's fault and not the infrastructure's: a
    caller that samples (the golden tool) rejects the attempt and asks again instead of
    stopping the run.
    """


@dataclass(frozen=True)
class Article:
    """One article read whole from the mirror.

    Attributes:
        page_id: The Wikipedia page id.
        revision_id: The revision the snapshot holds.
        title: The article title.
        text: The full article text, all read windows joined.
    """

    page_id: int
    revision_id: int
    title: str
    text: str


def fold(text: str) -> str:
    """Casefold ``text`` and collapse every whitespace run to one space.

    The comparison form both tools use for "does this passage contain that evidence": a packed
    or split cut can change line breaks, and a question's answer is matched regardless of case.

    Args:
        text: Any text.

    Returns:
        The folded, stripped text.
    """
    return _WHITESPACE.sub(" ", text.casefold()).strip()


def new_request_id(prefix: str) -> str:
    """A fresh ``X-Request-ID``: ``<prefix>-<12 hex>``, at most 64 safe characters.

    Args:
        prefix: A short label naming the tool and step; anything outside ``A-Za-z0-9_.:-`` is
            replaced with ``-`` and the prefix is cut so the whole id fits.

    Returns:
        A new identifier, unique per call.
    """
    suffix = uuid.uuid4().hex[:12]
    safe = _ID_UNSAFE.sub("-", prefix)[: _MAX_REQUEST_ID_CHARS - len(suffix) - 1]
    return f"{safe}-{suffix}"


def page_id_stream(seed: str | int, max_page_id: int) -> Iterator[int]:
    """Endless uniform page ids in ``[1, max_page_id]``, reproducible from ``seed``.

    Most ids are not articles (the mirror answers 404), so callers skip those; the articles that
    remain are a uniform sample of all articles.

    Args:
        seed: Anything ``random.Random`` accepts as a seed; strings are hashed stably.
        max_page_id: The largest id to draw.

    Yields:
        Page ids.
    """
    rng = random.Random(seed)  # noqa: S311 - sampling, not security
    while True:
        yield rng.randint(1, max_page_id)


async def _mirror_call(
    client: MirrorClient,
    method: str,
    path: str,
    *,
    request_id: str,
    params: dict[str, Any] | None,
    json_body: dict[str, Any] | None,
    sleep: Sleep,
) -> Any:
    """One mirror call, retried when the mirror was unavailable or told us to slow down.

    ``MirrorRequestError`` (a 404 or a 422) is an answer and propagates at once, except a 429
    (the client does not expose ``Retry-After``, so the doubling backoff stands in for it).
    ``MirrorUnavailableError`` covers a timeout, a reset and every 5xx (502 and 504 included);
    it is retried up to ``_MIRROR_ATTEMPTS`` times with a doubling wait, then raised.
    """
    for attempt in range(1, _MIRROR_ATTEMPTS + 1):
        try:
            return await client.request(
                method, path, request_id=request_id, params=params, json_body=json_body
            )
        except MirrorUnavailableError:
            if attempt == _MIRROR_ATTEMPTS:
                raise
            await sleep(_backoff(attempt))
        except MirrorRequestError as exc:
            if exc.status != _TOO_MANY_REQUESTS or attempt == _MIRROR_ATTEMPTS:
                raise
            await sleep(_backoff(attempt))
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises


async def read_article(
    client: MirrorClient,
    page_id: int,
    *,
    request_id: str,
    sleep: Sleep = asyncio.sleep,
) -> Article | None:
    """Read an article's full text by following the mirror's windows.

    ``GET /v1/wiki/article?page_id=N&offset=K`` returns one window; concatenating the windows by
    ``next_offset`` until it is None gives the stored text exactly (docs/DESIGN.md section 5).

    Args:
        client: The mirror client.
        page_id: The page to read.
        request_id: ``X-Request-ID`` sent on every window.
        sleep: Awaited between retries; tests inject a recorder.

    Returns:
        The article, or None when the mirror has no such page (a 404).

    Raises:
        MirrorUnavailableError: The mirror stayed unreachable or refused our key.
        ArticleReadError: The windows do not assemble: the offset did not advance, the revision
            changed between windows, or the joined length is not the reported ``total_chars``.
    """
    parts: list[str] = []
    offset = 0
    first: dict[str, Any] | None = None
    while True:
        try:
            reply = await _mirror_call(
                client,
                "GET",
                "/v1/wiki/article",
                request_id=request_id,
                params={"page_id": page_id, "offset": offset},
                json_body=None,
                sleep=sleep,
            )
        except MirrorRequestError as exc:
            if exc.status == 404:
                return None
            raise
        if first is None:
            first = reply
        elif reply["revision_id"] != first["revision_id"]:
            raise ArticleReadError(f"page {page_id}: the revision changed between windows")
        parts.append(reply["text"])
        next_offset = reply["next_offset"]
        if next_offset is None:
            break
        if next_offset <= offset:
            raise ArticleReadError(f"page {page_id}: the next offset did not advance")
        offset = next_offset
    text = "".join(parts)
    if len(text) != first["total_chars"]:
        raise ArticleReadError(f"page {page_id}: windows do not add up to total_chars")
    return Article(
        page_id=int(first["page_id"]),
        revision_id=int(first["revision_id"]),
        title=str(first["title"]),
        text=text,
    )


@dataclass(frozen=True)
class ReadResult:
    """The outcome of one guarded read: an article, a clean 404, or a failure that was skipped."""

    article: Article | None
    #: True when the read failed after its retries and the page was skipped (not a 404).
    failed: bool = False


@dataclass
class ReadGuard:
    """Turns a page whose read keeps failing into a counted skip, without hiding a dead mirror.

    One page that cannot be assembled, or whose server keeps answering 5xx, should not end a
    run that reads thousands. But a mirror that is down or refusing our key fails every page
    the same way, and skipping all of them would quietly yield an empty sample; after
    ``max_consecutive`` failures in a row the original error is raised.

    Attributes:
        max_consecutive: Failures in a row that stop the run.
        failed: Pages skipped so far.
        consecutive: Failures since the last success.
        sleep: Awaited between a read's retries; tests inject a recorder.
    """

    max_consecutive: int = _MAX_CONSECUTIVE_READ_FAILURES
    failed: int = 0
    consecutive: int = 0
    sleep: Sleep = asyncio.sleep

    async def read(
        self, client: MirrorClient, page_id: int, *, request_id: str
    ) -> ReadResult:
        """``read_article``, with ``ArticleReadError`` and persistent unavailability skipped.

        Args:
            client: The mirror client.
            page_id: The page to read.
            request_id: ``X-Request-ID`` for the windows.

        Returns:
            The article, or None for a 404, or ``failed=True`` for a skipped page.

        Raises:
            MirrorUnavailableError: ``max_consecutive`` reads in a row were unavailable.
            ArticleReadError: ``max_consecutive`` reads in a row could not be assembled.
        """
        try:
            article = await read_article(client, page_id, request_id=request_id, sleep=self.sleep)
        except (ArticleReadError, MirrorUnavailableError):
            self.failed += 1
            self.consecutive += 1
            if self.consecutive >= self.max_consecutive:
                raise
            return ReadResult(None, failed=True)
        self.consecutive = 0
        return ReadResult(article)


def describe_failure(exc: Exception) -> str:
    """A line for stderr that names a failure without quoting what the server sent back.

    The mirror's 4xx detail can echo the query (a 422 on a question), so for a
    mirror error only the class and the HTTP status appear; the router and article errors
    raised by this module carry statuses and error codes by construction.

    Args:
        exc: The error that stopped the run.
    """
    if isinstance(exc, MirrorRequestError):
        return f"{type(exc).__name__}: status {exc.status}"
    if isinstance(exc, MirrorServiceError):
        return type(exc).__name__
    return f"{type(exc).__name__}: {exc}"


async def search_ids(
    client: MirrorClient,
    query: str,
    limit: int,
    *,
    request_id: str,
    sleep: Sleep = asyncio.sleep,
) -> list[int]:
    """Page ids of the mirror's best articles for a query, best first.

    Args:
        client: The mirror client.
        query: The search text (a question, in these tools). Never logged.
        limit: Articles wanted; the API accepts 1 to 20.
        request_id: ``X-Request-ID`` for the call.
        sleep: Awaited between retries; tests inject a recorder.

    Returns:
        One page id per result, in the mirror's order.

    Raises:
        MirrorUnavailableError: The mirror stayed unreachable.
        MirrorRequestError: The mirror refused the request (for instance ``limit`` > 20).
    """
    reply = await _mirror_call(
        client,
        "POST",
        "/v1/wiki/search",
        request_id=request_id,
        params=None,
        json_body={"query": query, "limit": limit},
        sleep=sleep,
    )
    return [int(result["page_id"]) for result in reply["results"]]


class RouterClient:
    """Async client for the two router endpoints these tools use, with bounded retries.

    One pooled ``httpx.AsyncClient`` for the run; use it as an async context manager. The
    router speaks the Ollama API: ``/api/embed`` takes
    ``{"model", "input": [...]}`` and answers ``{"embeddings": [...]}``; ``/api/chat`` with
    ``stream: false`` answers ``{"message": {"content": ...}}``.

    The key is read from ``EMBEDDING_API_KEY`` when the first request is made, and is only
    ever placed in the ``Authorization`` header.
    """

    def __init__(
        self,
        base_url: str,
        *,
        embed_model: str = "bge-m3:latest",
        timeout_seconds: float = 120.0,
        attempts: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Configure the client; nothing is sent until the first call.

        Args:
            base_url: The router origin, e.g. ``http://192.168.1.60:8080``.
            embed_model: The model ``embed`` asks for.
            timeout_seconds: Per-request budget; a chat generation can take tens of seconds.
            attempts: Most tries per call (the first plus retries) for a retryable status or
                a timeout.
            transport: Test seam. Never set in production.
            sleep: Awaited between retries; tests inject a recorder so nothing really waits.
        """
        self._embed_model = embed_model
        self._attempts = max(1, attempts)
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=timeout_seconds, transport=transport
        )

    async def __aenter__(self) -> RouterClient:
        """Enter the context; the connection pool is already open."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed ``texts`` with the configured model, one vector per input, in order.

        Args:
            texts: One batch of inputs.

        Returns:
            The vectors.

        Raises:
            RouterError: The call failed for good, or the reply holds a different number of
                vectors than inputs (a short reply must never be zipped against the inputs).
        """
        reply = await self._post(
            "/api/embed",
            {"model": self._embed_model, "input": list(texts)},
            new_request_id("wiki-embed"),
        )
        vectors = reply.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            got = len(vectors) if isinstance(vectors, list) else "none"
            raise RouterError(f"embed returned {got} vectors for {len(texts)} inputs")
        return [[float(x) for x in vector] for vector in vectors]

    async def chat_json(
        self, model: str, messages: list[dict[str, str]], options: dict[str, Any]
    ) -> dict[str, Any]:
        """Ask ``model`` for a JSON reply and return it decoded.

        Args:
            model: The chat model name.
            messages: Chat messages (``role``, ``content``).
            options: Ollama options (temperature, ``num_predict``, ``seed``).

        Returns:
            The decoded JSON object the model produced.

        Raises:
            RouterContentError: The reply's content is not a JSON object (a ``RouterError``).
            RouterError: The call failed for good. No exception carries the model's output.
        """
        reply = await self._post(
            "/api/chat",
            {
                "model": model,
                "messages": messages,
                "stream": False,
                "format": "json",
                "options": options,
            },
            new_request_id("wiki-chat"),
        )
        content = (reply.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise RouterError("chat reply has no message content")
        try:
            decoded = json.loads(content)
        except ValueError as exc:
            raise RouterContentError("chat content is not JSON") from exc
        if not isinstance(decoded, dict):
            raise RouterContentError("chat content is not a JSON object")
        return decoded

    async def _post(self, path: str, body: dict[str, Any], request_id: str) -> dict[str, Any]:
        """POST with the key and request id, retrying 429/502/503/504 and a timeout in budget."""
        token = os.environ.get(ROUTER_KEY_ENV, "").strip()
        if not token:
            raise RouterError(f"{ROUTER_KEY_ENV} is not set, so the router cannot be reached")
        headers = {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}
        for attempt in range(1, self._attempts + 1):
            try:
                response = await self._http.post(path, json=body, headers=headers)
            except httpx.TransportError as exc:
                # Timeouts and dropped connections; the exception text can carry the URL only.
                if attempt == self._attempts:
                    raise RouterError(
                        f"router unreachable after {attempt} attempts: {type(exc).__name__}"
                    ) from exc
                await self._sleep(_backoff(attempt))
                continue
            if response.status_code in _RETRY_STATUSES:
                if attempt == self._attempts:
                    raise RouterError(
                        f"router answered {response.status_code} on all {attempt} attempts"
                    )
                await self._sleep(_retry_after(response, attempt))
                continue
            if response.status_code >= 400:
                raise RouterError(
                    f"router refused {path}: {response.status_code} {_error_code(response)}"
                )
            try:
                decoded = response.json()
            except ValueError as exc:
                raise RouterError(f"router reply for {path} is not JSON") from exc
            if not isinstance(decoded, dict):
                raise RouterError(f"router reply for {path} is not an object")
            return decoded
        raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises


def _backoff(attempt: int) -> float:
    """Doubling wait after a failed attempt (1, 2, 4 ... s), capped at the retry ceiling."""
    return min(_MAX_RETRY_WAIT, float(2 ** (attempt - 1)))


def _retry_after(response: httpx.Response, attempt: int) -> float:
    """The wait a 429/503 asks for (``Retry-After`` seconds), capped; doubling backoff if absent."""
    raw = response.headers.get("Retry-After")
    if raw is not None:
        try:
            return max(0.0, min(_MAX_RETRY_WAIT, float(raw)))
        except ValueError:
            pass  # An HTTP-date or junk: the router sends seconds, so fall back to backoff.
    return _backoff(attempt)


def _error_code(response: httpx.Response) -> str:
    """The router's machine-readable error ``code`` (never its free-text message)."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict) and isinstance(body.get("code"), str):
        return str(body["code"])[:60]
    return ""
