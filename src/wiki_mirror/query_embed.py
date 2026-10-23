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
* **The timeout is `QUERY_EMBED_TIMEOUT_SECONDS`, applied to each phase** (connect, write, read)
  as httpx does: an unreachable or hung server must not hold a worker thread for long.
* **The key is `EMBEDDING_API_KEY` from the API process's environment**, its own key (the API unit
  loads `mirror.env`; the embed run loads `embedding.env`). It goes only into the `Authorization`
  header, is read when the embedder is built, and is never in a message, a log line or an
  exception. A server that wants no key (a local Ollama) gets no header.
* **The question is sent as plain text**, the form the evaluation measured
  (`embed.passage_embedding_text`): a question has no title or heading to put in front of it.
* **Every failure is a `QueryEmbedError`** with a plain sentence and a short `kind`. The sentence
  never holds the URL, a header, a response body or the question.
* **A connection per call.** An `httpx.Client` is opened and closed around each request: a LAN
  handshake is a millisecond against an embedding that takes 25-70 ms, and a stateless embedder
  needs no lifecycle for `serving` to manage.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping
from typing import Final

import httpx

from wiki_mirror.config import MirrorConfig
from wiki_mirror.embed import EMBEDDING_API_KEY_ENV, EmbedError, checked_vectors

__all__ = ["QUERY_EMBED_TIMEOUT_SECONDS", "QueryEmbedError", "QueryEmbedder"]

#: Longest to wait for the embedding server in each phase of a request, seconds. An interactive
#: search must not hang, and a cold model load, the slowest healthy answer, took about 1.5 s on
#: the reference box (2026-10-23; a warm embedding takes 25-70 ms). 15 s is ten times the cold
#: load: room for a busy server's queue, short enough that a dead one fails the search visibly.
QUERY_EMBED_TIMEOUT_SECONDS: Final = 15.0

#: What a forwarded request id may be: 1 to 64 characters of alphanumerics and `- _ . :`, the
#: cluster's rule and the same as `api._REQUEST_ID`.
_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9._:-]{1,64}")

#: Statuses that mean the server refused the key.
_KEY_REFUSED: Final = frozenset({401, 403})


class QueryEmbedError(Exception):
    """A question could not be embedded; the message is a plain sentence that holds no secret.

    Attributes:
        kind: A short machine word for logs and tests: `not_configured`, `unreachable`,
            `timeout`, `rejected` (the key was refused), `status` (another HTTP status) or
            `bad_reply` (not JSON, the wrong shape, length or non-finite numbers).
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
    ) -> None:
        """Configure the embedder; nothing is sent until the first `embed`.

        Args:
            base_url: The server's origin (`MirrorConfig.embedding_url`).
            model: The model to ask for: the one recorded in the semantic index's state.
            dimensions: The length the returned vector must have: also from that state.
            api_key: Sent as `Authorization: Bearer`; None sends no `Authorization` header.
            headers: Extra headers for every request (`MirrorConfig.query_embedding_headers`).
            timeout_seconds: Per-phase timeout.
            transport: Test seam. Never set in production.
        """
        # httpx logs every request at INFO, one line per search; the URL never changes and the
        # API's own log line already says a search happened.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self._base_url = base_url
        self._model = model
        self._dimensions = dimensions
        self._api_key = api_key
        self._headers = dict(headers or {})
        self._timeout = timeout_seconds
        self._transport = transport

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
            QueryEmbedError: `embedding_url` is not set (kind `not_configured`).
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
            QueryEmbedError: The server could not be reached or timed out, refused the key,
                answered another status, or answered something that is not one valid vector.
        """
        try:
            with httpx.Client(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as http:
                response = http.post(
                    "/api/embed",
                    json={"model": self._model, "input": [text]},
                    headers=self._request_headers(request_id),
                )
        except httpx.TimeoutException:
            msg = f"the embedding server did not answer within {self._timeout:g} s"
            raise QueryEmbedError("timeout", msg) from None
        except httpx.TransportError:
            # Connection refused, reset, DNS failure: the exception text can carry the URL.
            msg = "the embedding server could not be reached"
            raise QueryEmbedError("unreachable", msg) from None
        return self._vector_of(response)

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
