"""Embeds one search question for the semantic and hybrid modes, synchronously, with a typed error.

Called by `search.WikiSearch` (through the callable `serving` pairs with the semantic collection)
when a request asks for `mode: "semantic"` or `"hybrid"`. It POSTs one text to an
Ollama-compatible embedding server's `/api/embed` and returns the one vector, checked by the same
rules as the bulk run (`embed.checked_vectors`). The API's routes are plain `def`, run in a thread
pool, so this is a blocking client; the bulk run's async `embed.EmbeddingClient` is the wrong tool
for them. See docs/DECISIONS.md D10 and docs/DESIGN.md section 12.5.

Non-obvious constraints:

* **The model and the vector length come from the semantic index's own state, not from the
  settings.** A question must be embedded by the model the passages were embedded with: another
  model gives vectors that are valid numbers of the right length, and a similarity that is
  silently meaningless. `MirrorConfig.embedding_model` is what the *next* embed run will use;
  `serving` reads the model that run recorded (`embed.semantic_index_info`) and passes it here.
* **No retries.** A search is interactive and a person waits on it; a retry would double the wait
  for a server that is already struggling. The caller (an agent or a person) can ask again, and
  the reply says plainly that it failed. The bulk client retries because a nightly run has
  15 minutes of budget and nobody waiting.
* **The timeout is split.** Connecting waits `QUERY_EMBED_CONNECT_SECONDS`, because the server is
  on the LAN and a refused or dead host should fail fast; the other phases (write, read, pool)
  wait `QUERY_EMBED_TIMEOUT_SECONDS`, for a cold model load. An unreachable or hung server must
  not hold a worker thread for long.
* **A bound and a breaker protect the thread pool.** The API's routes run in anyio's pool of 40
  threads, shared with lexical search and `/v1/wiki/health`, and one embedding can hold a thread
  for the whole timeout. At most `QUERY_EMBED_MAX_IN_FLIGHT` embeddings run at once (the next
  fails at once with `busy`), and after `QUERY_EMBED_BREAKER_FAILURES` timeouts or unreachable
  errors in a row, embedding pauses for `QUERY_EMBED_BREAKER_PAUSE_SECONDS` (`breaker_open`),
  then lets one trial call through. Both belong to one embedder: a new pairing, which `serving`
  makes when the semantic index changes, starts with a closed breaker.
* **The key is `EMBEDDING_API_KEY` from the API process's environment**, its own key (the API unit
  loads `mirror.env`; the embed run loads `embedding.env`). It goes only into the `Authorization`
  header, is read when the embedder is built, and is never in a message, a log line or an
  exception. A server that wants no key (a local Ollama) gets no header. A key that is not
  printable ASCII cannot be sent as a header (httpx would raise, and its traceback would quote the
  character), so such an embedder refuses every question with `not_configured`.
* **The question is sent as plain text**, the form the evaluation measured
  (`embed.passage_embedding_text`): a question has no title or heading to put in front of it.
* **Every failure is a `QueryEmbedError`** with a plain sentence and a short `kind`, so the API
  answers 503 and never 500. The sentence never holds the URL, a header, a response body or the
  question, so it is never an exception's own text: httpx's can carry the URL.
* **One `httpx.Client` per embedder, shared by every thread.** Building a client costs about
  3.4 ms of CPU with the GIL held, because it loads an SSL context from certifi even for an
  `http://` origin, which is more than the 25-70 ms embedding it serves can spare per search under
  load. `httpx.Client` is thread-safe and keeps connections alive. A replaced embedder (a
  pairing changes rarely) is left to the garbage collector: closing its client could cut off a
  request another thread is still making.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Final

import httpx

from wiki_mirror.config import MirrorConfig
from wiki_mirror.embed import EMBEDDING_API_KEY_ENV, EmbedError, checked_vectors

__all__ = [
    "QUERY_EMBED_BREAKER_FAILURES",
    "QUERY_EMBED_BREAKER_PAUSE_SECONDS",
    "QUERY_EMBED_CONNECT_SECONDS",
    "QUERY_EMBED_MAX_IN_FLIGHT",
    "QUERY_EMBED_TIMEOUT_SECONDS",
    "QueryEmbedError",
    "QueryEmbedder",
    "key_problem",
]

#: Longest to wait for the embedding server in the write, read and pool phases of a request,
#: seconds. An interactive search must not hang, and a cold model load, the slowest healthy
#: answer, took about 1.5 s on the reference box (2026-10-23; a warm embedding takes 25-70 ms).
#: 15 s is ten times the cold load: room for a busy server's queue, short enough that a dead one
#: fails the search visibly.
QUERY_EMBED_TIMEOUT_SECONDS: Final = 15.0

#: Longest to wait for a connection, seconds. The server is on the LAN, where a handshake takes a
#: millisecond, so a refused or dead host should fail in 3 s and not hold a thread for 15.
QUERY_EMBED_CONNECT_SECONDS: Final = 3.0

#: Most embeddings in flight at once. The API's routes share anyio's default pool of 40 threads
#: with lexical search and `/v1/wiki/health`; four leaves 36 for them when the embedding server
#: stalls, and four is ample for a mirror whose embedding takes 25-70 ms.
QUERY_EMBED_MAX_IN_FLIGHT: Final = 4

#: Timeouts or unreachable errors in a row that open the breaker. Three is one more than a single
#: transient blip (a dropped packet, a model reload) would produce.
QUERY_EMBED_BREAKER_FAILURES: Final = 3

#: How long an open breaker refuses, seconds: long enough to spare the pool while the server
#: restarts, short enough that semantic search is back soon after it is up (one trial call).
QUERY_EMBED_BREAKER_PAUSE_SECONDS: Final = 30.0

#: What a forwarded request id may be: 1 to 64 characters of alphanumerics and `- _ . :`, the
#: cluster's rule and the same as `api._REQUEST_ID`.
_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9._:-]{1,64}")

#: Statuses that mean the server refused the key.
_KEY_REFUSED: Final = frozenset({401, 403})

#: The failures that count towards the breaker: the server did not answer. `rejected`, `status`
#: and `bad_reply` mean it did, so it is up and a pause would not help.
_NO_ANSWER: Final = frozenset({"timeout", "unreachable"})

#: The sentence for a full embedder (`busy`).
_BUSY: Final = "the embedding server is busy with other searches; try again"

#: The sentence for a key httpx could not send (`not_configured`). It names the variable and never
#: its content.
_KEY_NOT_ASCII: Final = (
    f"{EMBEDDING_API_KEY_ENV} holds a character that is not printable ASCII, so it cannot be "
    "sent; fix it in the API's mirror.env"
)


def key_problem(key: str | None) -> str | None:
    """Why `key` cannot be sent as a bearer token, or None when it can (or there is none).

    Args:
        key: The `EMBEDDING_API_KEY` value, already trimmed; None or empty means no key.

    Returns:
        A fixed sentence that names the variable and never its content.
    """
    if key and not (key.isascii() and key.isprintable()):
        return _KEY_NOT_ASCII
    return None


class QueryEmbedError(Exception):
    """A question could not be embedded; the message is a plain sentence that holds no secret.

    Attributes:
        kind: A short machine word for logs and tests: `not_configured`, `unreachable`,
            `timeout`, `rejected` (the key was refused), `status` (another HTTP status),
            `bad_reply` (not JSON, undecodable, the wrong shape, length or non-finite numbers),
            `busy` (too many embeddings already in flight) or `breaker_open` (the server failed
            several times in a row and embedding is paused).
    """

    def __init__(self, kind: str, message: str) -> None:
        """Record the failure's kind and its sentence."""
        super().__init__(message)
        self.kind = kind


class QueryEmbedder:
    """Embeds one text at a time with the model and length the semantic index was built with."""

    def __init__(
        self,
        base_url: str,
        *,
        model: str,
        dimensions: int,
        api_key: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = QUERY_EMBED_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure the embedder and build its one HTTP client; nothing is sent until `embed`.

        Args:
            base_url: The server's origin (`MirrorConfig.embedding_url`).
            model: The model to ask for: the one recorded in the semantic index's state.
            dimensions: The length the returned vector must have: also from that state.
            api_key: Sent as `Authorization: Bearer`; None sends no `Authorization` header. A key
                that is not printable ASCII makes every `embed` raise `not_configured`.
            headers: Extra headers for every request (`MirrorConfig.query_embedding_headers`).
            timeout_seconds: The read, write and pool timeout; connecting waits
                `QUERY_EMBED_CONNECT_SECONDS` (or this, if shorter).
            transport: Test seam. Never set in production.
            clock: Seconds on a monotonic scale, for the breaker's pause. A test seam.
        """
        # httpx logs every request at INFO, one line per search; the URL never changes and the
        # API's own log line already says a search happened.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self._model = model
        self._dimensions = dimensions
        self._api_key = api_key
        self._headers = dict(headers or {})
        self._timeout = timeout_seconds
        self._connect_timeout = min(QUERY_EMBED_CONNECT_SECONDS, timeout_seconds)
        self._refusal = key_problem(api_key)
        # Never closed: see the module docstring ("One `httpx.Client` per embedder").
        self._http = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=self._connect_timeout),
            transport=transport,
        )
        self._clock = clock
        self._slots = threading.BoundedSemaphore(QUERY_EMBED_MAX_IN_FLIGHT)
        # The breaker. `_lock` guards the three fields below it; `_open_until` is None while
        # closed, and a trial call is allowed once the clock passes it.
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until: float | None = None
        self._trial_in_flight = False

    @classmethod
    def from_config(
        cls,
        config: MirrorConfig,
        environ: Mapping[str, str],
        *,
        model: str,
        dimensions: int,
        transport: httpx.BaseTransport | None = None,
    ) -> QueryEmbedder:
        """The embedder the settings describe, with the key from `environ`.

        Args:
            config: Service settings (`embedding_url`, `query_embedding_headers`).
            environ: The process environment; only `EMBEDDING_API_KEY` is read.
            model: The semantic index's model, from its state (not `config.embedding_model`).
            dimensions: The semantic index's vector length, from its state.
            transport: Test seam. Never set in production.

        Raises:
            QueryEmbedError: `embedding_url` is not set (kind `not_configured`). A key that is not
                printable ASCII does not raise here; the embedder it returns refuses every
                question, and `serving` reports the pairing as unavailable (`key_problem`).
        """
        if config.embedding_url is None:
            msg = "the embedding server is not configured (embedding_url in mirror.yaml)"
            raise QueryEmbedError("not_configured", msg)
        key = environ.get(EMBEDDING_API_KEY_ENV, "").strip()
        return cls(
            config.embedding_url,
            model=model,
            dimensions=dimensions,
            api_key=key or None,
            headers=config.query_embedding_headers,
            transport=transport,
        )

    def embed(self, text: str, request_id: str | None = None) -> list[float]:
        """Embed one question.

        Args:
            text: The question, as the person typed it (never logged here).
            request_id: The id of the search request, sent as `X-Request-ID` so one search can be
                followed from the mirror's log to the embedding server's. Used only if it
                matches the cluster's rule (`_REQUEST_ID`); otherwise, or when None, a generated
                `wiki-query-<hex>` id is sent.

        Returns:
            One vector of the index's length, every number finite.

        Raises:
            QueryEmbedError: Embedding is refused (`busy`, `breaker_open`, or a key that cannot
                be sent), the server could not be reached or timed out, refused the key, answered
                another status, or answered something that is not one valid vector. Nothing else
                escapes: the API turns every one of these into a 503.
        """
        if self._refusal is not None:
            raise QueryEmbedError("not_configured", self._refusal)
        # Non-blocking: a caller that would have to wait holds a pool thread for nothing.
        if not self._slots.acquire(blocking=False):
            raise QueryEmbedError("busy", _BUSY)
        try:
            return self._through_breaker(text, request_id)
        finally:
            self._slots.release()

    def _through_breaker(self, text: str, request_id: str | None) -> list[float]:
        """Ask the server if the breaker allows it, and tell the breaker how the call ended."""
        is_trial = self._admit()
        # None until the server's behaviour is known: a bug that raises something else must still
        # end a trial, or the breaker would refuse for good.
        no_answer: bool | None = None
        try:
            vector = self._vector_of(self._post(text, request_id))
            no_answer = False
            return vector
        except QueryEmbedError as exc:
            no_answer = exc.kind in _NO_ANSWER
            raise
        finally:
            self._settle(no_answer, is_trial)

    def _post(self, text: str, request_id: str | None) -> httpx.Response:
        """Send the one request, mapping every httpx failure to a `QueryEmbedError`."""
        try:
            return self._http.post(
                "/api/embed",
                json={"model": self._model, "input": [text]},
                headers=self._request_headers(request_id),
            )
        except httpx.TimeoutException as exc:
            # Connecting is allowed less time than the other phases (see the constants).
            connecting = isinstance(exc, httpx.ConnectTimeout)
            limit = self._connect_timeout if connecting else self._timeout
            msg = f"the embedding server did not answer within {limit:g} s"
            raise QueryEmbedError("timeout", msg) from None
        except httpx.DecodingError:
            # The server answered, with a compressed body that does not decompress. Not a
            # `TransportError`, so it needs its own clause; the exception text is not kept.
            msg = "the embedding server answered something that cannot be decoded"
            raise QueryEmbedError("bad_reply", msg) from None
        except httpx.HTTPError:
            # Connection refused, reset, DNS failure, a protocol error: the exception text can
            # carry the URL.
            msg = "the embedding server could not be reached"
            raise QueryEmbedError("unreachable", msg) from None

    def _admit(self) -> bool:
        """Let the call through the breaker, or refuse it.

        Returns:
            True when this call is the one trial after a pause, False when the breaker is closed.

        Raises:
            QueryEmbedError: `breaker_open`, while the pause lasts or while the trial is out.
        """
        with self._lock:
            if self._open_until is None:
                return False
            left = self._open_until - self._clock()
            if left > 0 or self._trial_in_flight:
                # Rounded up, and never "0 s left" while still refusing.
                msg = (
                    f"the embedding server failed {QUERY_EMBED_BREAKER_FAILURES} times in a row; "
                    f"semantic search pauses for {QUERY_EMBED_BREAKER_PAUSE_SECONDS:g} s, "
                    f"{max(1, math.ceil(left))} s left"
                )
                raise QueryEmbedError("breaker_open", msg)
            self._trial_in_flight = True
            return True

    def _settle(self, no_answer: bool | None, is_trial: bool) -> None:
        """Record how a call ended.

        Args:
            no_answer: True if the server did not answer (timeout, unreachable); False if it did,
                whatever it said, which closes the breaker: it is up. None when the call ended
                in an unexpected way and says nothing about the server.
            is_trial: Whether the call was `_admit`'s trial.
        """
        with self._lock:
            if is_trial:
                self._trial_in_flight = False
            if no_answer is None:
                return
            if not no_answer:
                self._failures = 0
                self._open_until = None
                return
            self._failures += 1
            # Open on the third in a row, and again on a failed trial. A straggler that was
            # admitted before the breaker opened does not extend a pause already running.
            if self._failures >= QUERY_EMBED_BREAKER_FAILURES and (
                self._open_until is None or is_trial
            ):
                self._open_until = self._clock() + QUERY_EMBED_BREAKER_PAUSE_SECONDS

    def _vector_of(self, response: httpx.Response) -> list[float]:
        """The one vector in a reply, or the error that describes why there is not one."""
        status = response.status_code
        if status in _KEY_REFUSED:
            msg = (
                "the embedding server refused the key "
                f"(check {EMBEDDING_API_KEY_ENV} in the API's mirror.env)"
            )
            raise QueryEmbedError("rejected", msg)
        if status != httpx.codes.OK:
            msg = f"the embedding server answered {status}"
            raise QueryEmbedError("status", msg)
        try:
            reply = response.json()
        except ValueError:
            msg = "the embedding server answered something that is not JSON"
            raise QueryEmbedError("bad_reply", msg) from None
        vectors = reply.get("embeddings") if isinstance(reply, dict) else None
        try:
            (vector,) = checked_vectors(vectors, 1, self._dimensions)
        except EmbedError as exc:
            # The shared check words its findings as counts and lengths, never a value.
            raise QueryEmbedError("bad_reply", str(exc)) from None
        return vector

    def _request_headers(self, request_id: str | None) -> dict[str, str]:
        """The headers of one request: the configured ones, a request id, the key.

        Args:
            request_id: The search request's id, or None. `api._Guard` has already validated one
                that came from a client, but the rule is checked here too: this is the boundary
                to another server, and a header value must never carry anything else.
        """
        headers = dict(self._headers)
        if request_id is not None and _REQUEST_ID.fullmatch(request_id):
            headers["X-Request-ID"] = request_id
        else:
            headers["X-Request-ID"] = f"wiki-query-{uuid.uuid4().hex[:12]}"
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers
