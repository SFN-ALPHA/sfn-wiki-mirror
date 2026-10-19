"""The mirror's HTTP surface: search, windowed article reads, health and the MCP door.

Called over the LAN with a bearer key: by the primary client (the one holding
`WIKI_MIRROR_API_KEY`) on the REST routes, and by other agents (Claude Code, ...) through MCP at
`/mcp` (`mcp_server`), each with a key of its own. Everything else, the download, parse and
index stages, runs from the command line
(`wiki_mirror.__main__`), never from a route: the API process runs as a user with no
internet and no write access to the data.

Non-obvious constraints:

* **Every route needs a key, including unknown paths and `/mcp`.** Authentication is a pure ASGI
  middleware (`_Guard`), so an unauthenticated caller cannot even learn which paths exist, and it
  runs before request validation. The key is the primary key (`WIKI_MIRROR_API_KEY`, client
  `primary`) or one in the live clients file (`keys.ClientKeys`: hashed, compared in constant time,
  re-read when the file changes, so a key added or revoked works at once without a restart).
  The matched client name is on `request.state.client` and in every log line of the request.
  It is *not* `BaseHTTPMiddleware`: that wraps the response body in a second task and stream,
  which is the wrong thing to put in front of a transport that owns its own tasks and streaming.
* **`X-Request-ID` is accepted if it matches `^[A-Za-z0-9._:-]{1,64}$`**, otherwise replaced by
  a generated one, and either way echoed on the response and carried in every log line.
* **Query text is never logged.** Questions can be personal, so a search is logged as its
  length and result count only; the access log is off in `serve` for the same reason (it would
  print the query string of `GET /v1/wiki/article?title=...`).
* **Errors are `{"error": "<code>", "detail": "<short>"}`** and never carry the offending input.
  `503 not_ready` means there is no snapshot to serve yet (the first build has not finished; the
  service notices it by itself within `serving.ALIAS_RECHECK_SECONDS`, no restart).
* **The service follows the alias and the index state.** Each request reads the snapshot it holds
  a lease on (`serving.ServedSnapshot`), which opens the next snapshot's article store when a
  build moves the Qdrant alias, or when the aliased collection's `applied_snapshot` moves (W3's
  in-place monthly update), so new data reaches the API without a restart. If the new store
  cannot be opened, or the collection has no usable index state, the old one keeps serving and
  `/v1/wiki/health` says `degraded` (the log says why).
* **A mirror that stops refreshing shows on health.** `degraded` also means the snapshot being
  served is more than `STALE_AFTER_DAYS` old (a monthly build that has been failing quietly), its
  last build failed, or its index was cut by a different chunker than this code's. The body keeps
  its shape; the reason for `degraded` is in the log, once per change.
* **Health also carries the last code update** (`update`: when, from and to which commit, and
  the outcome: `updated`, `current`, `deferred`, `held`, `failed` or `rolled_back`), read from the
  file `deploy/update.sh` leaves. It is `null` when there is none, and it never changes
  the status.
* **Only HTTP is served.** `_Guard` refuses any other ASGI scope type (a websocket upgrade is
  closed with 1008) rather than let it reach a route; `lifespan` passes, since the server needs it.

See docs/DESIGN.md sections 6 and 12.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Annotated, Any, Final

from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from wiki_mirror.build import read_build_state
from wiki_mirror.config import MirrorConfig
from wiki_mirror.keys import ClientKeys
from wiki_mirror.mcp_server import MCP_PATH, build_mcp_door
from wiki_mirror.records import MAX_PAGE_ID
from wiki_mirror.search import reply_as_dict
from wiki_mirror.serving import ServedSnapshot, Serving
from wiki_mirror.update_record import read_update_record

__all__ = ["HealthSource", "SearchBody", "ServiceHealth", "build_service", "create_app"]

logger = logging.getLogger("wiki_mirror.api")

#: What a caller may send as `X-Request-ID`; anything else is replaced.
_REQUEST_ID: Final = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

#: Build phases in which a build is running and nothing may be served yet.
_BUILDING_PHASES: Final = frozenset({"downloading", "parsing", "indexing"})

#: Days past a snapshot's date after which health calls the mirror `degraded`. Snapshots are
#: monthly (docs/DESIGN.md section 5), so 40 days is a month plus a week and a half: room for one
#: late or retried build, but not for a build that has been failing for a whole cycle.
STALE_AFTER_DAYS: Final = 40

#: Produces the body of `GET /v1/wiki/health`.
HealthSource = Callable[[], dict[str, Any]]


class SearchBody(BaseModel):
    """The body of `POST /v1/wiki/search`."""

    model_config = ConfigDict(extra="forbid")

    #: Keywords or a question; the lexical encoder drops stop-words. Never logged.
    query: str = Field(min_length=1, max_length=500)
    #: Most articles to return.
    limit: int = Field(default=8, ge=1, le=20)


class ServiceHealth:
    """Works out the health report from the build state, the alias and the snapshot served."""

    def __init__(
        self,
        config: MirrorConfig,
        served: ServedSnapshot,
        client: QdrantClient,
        *,
        state_reader: Callable[[Any], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        stale_after_days: int = STALE_AFTER_DAYS,
    ) -> None:
        """Remember what to look at.

        Args:
            config: Service settings (`data_dir`, where the build state lives).
            served: The holder of the snapshot being served; asked, each time, where the alias
                points and whether the matching store is open.
            client: The Qdrant client, asked how many passages the alias holds.
            state_reader: Reads the build state; defaults to `build.read_build_state`.
            clock: The current time, UTC; tests inject theirs. Defaults to the system clock.
            stale_after_days: Age of the served snapshot, in days, past which the mirror is
                `degraded`.
        """
        self._config = config
        self._served = served
        self._client = client
        self._read_state = state_reader or read_build_state
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stale_after = stale_after_days
        self._stale_logged = False
        # Counting a 7M-row table on every health poll would be wasteful, and a store never
        # changes while it is open. Keyed by (collection, applied snapshot): one entry per store
        # ever served. The collection alone would go stale when an in-place update (W3) opens
        # next month's store under the same collection name.
        self._articles: dict[tuple[str, str], int] = {}

    def __call__(self) -> dict[str, Any]:
        """The health report; never raises (a health route that fails reports nothing)."""
        build = self._build()
        passages = 0
        counted = True
        with self._served.lease(fresh=True) as serving:
            collection = self._served.alias
            problem = self._served.problem
            if collection is not None:
                try:
                    passages = int(
                        self._client.count(collection_name=collection, exact=False).count
                    )
                except Exception:
                    # Deliberately broad: the client raises its own family of errors for a
                    # server that is down, restarting or refusing, and health must still
                    # answer, as "degraded".
                    logger.warning("health: Qdrant could not be asked", exc_info=True)
                    counted = False
            articles = self._article_count(serving)
            snapshot = serving.store.snapshot.date if serving is not None else None
            applied = serving.applied_snapshot if serving is not None else None
            stale = self._is_stale(snapshot)
            status = self._status(build, collection, serving, problem, counted, stale)
        return {
            "status": status,
            "snapshot": snapshot,
            "applied_snapshot": applied,
            "articles": articles,
            "passages": passages,
            "collection": collection,
            "build": build,
            # The last code update (update.sh), or null if none is recorded. Reported only: a
            # held or rolled-back update leaves the previous code serving, which is not a fault
            # of the mirror, and reading it can never fail health (update_record).
            "update": read_update_record(self._config.data_dir),
        }

    def _article_count(self, serving: Serving | None) -> int:
        """Articles in the open store, counted once per store."""
        if serving is None:
            return 0
        key = (serving.collection, serving.applied_snapshot)
        if key not in self._articles:
            self._articles[key] = serving.store.count()
        return self._articles[key]

    def _build(self) -> dict[str, Any]:
        """The build state as a plain mapping, or a stand-in if it cannot be read."""
        try:
            state = self._read_state(self._config.data_dir)
            return dict(state) if isinstance(state, Mapping) else asdict(state)
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("health: the build state is unreadable", exc_info=True)
            return {"phase": "unknown", "error": "the build state could not be read"}

    def _status(
        self,
        build: Mapping[str, Any],
        collection: str | None,
        serving: Serving | None,
        problem: str | None,
        counted: bool,
        stale: bool,
    ) -> str:
        """`ok`, `degraded`, `building` or `empty`; the reason for `degraded` is in the log."""
        phase = build.get("phase")
        if problem is not None or not counted:
            return "degraded"
        if collection is None:
            return "building" if phase in _BUILDING_PHASES else "empty"
        if serving is None or serving.collection != collection:
            # The holder recorded no problem yet is not serving what the alias names: the alias
            # moved a moment ago and the next look will catch up.
            return "degraded"
        return "degraded" if phase == "failed" or stale else "ok"

    def _is_stale(self, snapshot: str | None) -> bool:
        """Whether the snapshot being served is older than `stale_after_days`.

        Logs a warning when it first becomes stale and an info line when it stops being so, not
        on every poll. A date that cannot be read counts as stale: a store whose own record is
        garbled is not one to call healthy.
        """
        if snapshot is None:
            self._stale_logged = False
            return False
        age: int | None
        try:
            age = (self._clock().date() - date.fromisoformat(snapshot)).days
        except ValueError:
            age = None
        stale = age is None or age > self._stale_after
        if stale and not self._stale_logged:
            logger.warning(
                "health: the snapshot served (%s) is %s days old, past the %d-day limit; "
                "check the monthly build (`wiki-mirror status`)",
                snapshot,
                "an unreadable number of" if age is None else age,
                self._stale_after,
                extra={"component": "api", "event": "stale"},
            )
        elif not stale and self._stale_logged:
            logger.info("health: the snapshot served is fresh again", extra={"component": "api"})
        self._stale_logged = stale
        return stale


class _Guard:
    """Pure ASGI middleware: request id, authentication, and a JSON 500 for a crash.

    Gives every HTTP request an id, requires a key from `keys`, puts `request_id` and `client`
    on the request state, stamps `X-Request-ID` on whatever response goes out, and turns an
    exception that escaped a route into a logged JSON 500 so the caller never parses a
    traceback page.

    Only two scope types get past it: `http`, authenticated as above, and `lifespan`, which the
    server needs to start and stop the app (it carries no client). Any other type - a websocket,
    or one a later ASGI revision adds - is refused: this service has no such routes, and a scope
    type the guard does not understand is one it cannot authenticate, so it must not be passed on
    "untouched" to whatever might one day be mounted behind it.
    """

    def __init__(self, app: ASGIApp, keys: ClientKeys) -> None:
        """Wrap `app`; `keys` decides who is let in."""
        self.app = app
        self._keys = keys

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI call (see the class docstring)."""
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await _refuse_scope(scope, receive, send)
            return
        headers = Headers(scope=scope)
        supplied = headers.get("x-request-id", "")
        request_id = supplied if _REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex
        state: dict[str, Any] = scope.setdefault("state", {})
        state["request_id"] = request_id
        started = False

        async def stamped_send(message: Message) -> None:
            """Forward a message, adding the request id to the response head."""
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        client = self._client_of(headers.get("authorization", ""))
        if client is None:
            logger.warning(
                "unauthorized request=%s",
                request_id,
                extra={"component": "api", "event": "unauthorized", "request_id": request_id},
            )
            denied = _error(401, "unauthorized", "invalid service token")
            denied.headers["WWW-Authenticate"] = "Bearer"
            await denied(scope, receive, stamped_send)
            return
        state["client"] = client
        try:
            await self.app(scope, receive, stamped_send)
        except Exception:
            # The last line of defence: whatever escaped a route is logged with its request id
            # and answered as JSON, so the caller never parses a traceback page.
            logger.exception(
                "unhandled error request=%s client=%s",
                request_id,
                client,
                extra={
                    "component": "api",
                    "event": "error",
                    "request_id": request_id,
                    "client": client,
                },
            )
            if started:
                raise  # the head is already sent; the server drops the connection
            await _error(500, "internal", "unexpected error; see the service log")(
                scope, receive, stamped_send
            )

    def _client_of(self, authorization: str) -> str | None:
        """The client an `Authorization` header authenticates, or None."""
        scheme, _, token = authorization.partition(" ")
        # Checked even when the scheme is wrong, so the time taken does not depend on it.
        client = self._keys.authenticate(token.strip())
        return client if scheme.lower() == "bearer" else None


async def _refuse_scope(scope: Scope, receive: Receive, send: Send) -> None:
    """Refuse an ASGI scope that is neither `http` nor `lifespan`, and log that it happened.

    A websocket is closed with code 1008 (policy violation) - sent as `websocket.close` before
    accept, which servers answer as an HTTP 403 - and anything else is dropped without a reply,
    since no response is defined for a scope type this code does not know.
    """
    kind = str(scope["type"])
    logger.warning(
        "refused a %s connection",
        kind,
        extra={"component": "api", "event": "scope_refused", "scope_type": kind},
    )
    if kind == "websocket":
        await receive()  # the connect message; the close below answers it
        await send({"type": "websocket.close", "code": 1008})


def create_app(
    config: MirrorConfig,
    served: ServedSnapshot,
    health_source: HealthSource,
    *,
    keys: ClientKeys | None = None,
) -> FastAPI:
    """Build the FastAPI application: REST routes, the MCP door and the guard in front.

    Args:
        config: Service settings; `api_key` must be set.
        served: The snapshot being served. Routes borrow it per request (`lease()`); while it
            holds nothing, search and article answer 503 `not_ready` and health still answers.
        health_source: Produces the `/v1/wiki/health` body.
        keys: Who may call. Defaults to the primary key plus `config.clients_file`;
            tests inject one with their own clock.

    Returns:
        The application, ready for uvicorn. Its lifespan starts the MCP session manager, which
        can run once, so build one application per process.

    Raises:
        ValueError: `config.api_key` is not set (a service that answers without a key is not a
            service to start), or the clients file exists but is malformed
            (`keys.ClientKeyError`, a `ValueError`).
    """
    if config.api_key is None:
        msg = "the mirror's API needs WIKI_MIRROR_API_KEY"
        raise ValueError(msg)
    client_keys = keys or ClientKeys(config.clients_file, config.api_key.get_secret_value())
    door = build_mcp_door(config, served)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Run the MCP session manager for as long as the application runs."""
        async with door.running():
            yield

    app = FastAPI(
        title="Wikipedia Mirror Service",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(_Guard, keys=client_keys)
    # A route to the SDK's ASGI endpoint, not `mount`: a mount at `/mcp` redirects `/mcp` to
    # `/mcp/`, which an MCP client configured with the bare URL should never have to follow.
    app.router.routes.append(Route(MCP_PATH, endpoint=door.app, include_in_schema=False))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        """422 naming the failing fields and rules, never the values (a query can be personal)."""
        problems = [
            f"{'.'.join(str(part) for part in error['loc'][1:]) or error['loc'][0]}: {error['msg']}"
            for error in exc.errors()
        ]
        return _error(422, "invalid_request", "; ".join(problems))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Routing errors (unknown path, wrong method) in the service's error shape."""
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _error(exc.status_code, code, "no such route" if exc.status_code == 404 else code)

    @app.post("/v1/wiki/search")
    def search_route(body: SearchBody, request: Request) -> Response:
        """Keyword search: one result per article, best first."""
        request_id: str = request.state.request_id
        client: str = request.state.client
        try:
            with served.lease() as serving:
                if serving is None:
                    return _not_ready()
                reply = serving.search.search(body.query, body.limit)
        except (UnexpectedResponse, ResponseHandlingException):
            logger.exception(
                "the index refused the query request=%s client=%s",
                request_id,
                client,
                extra={
                    "component": "api",
                    "event": "index_error",
                    "request_id": request_id,
                    "client": client,
                },
            )
            return _error(503, "index_unavailable", "the passage index could not answer")
        logger.info(
            "search request=%s client=%s query_chars=%d results=%d",
            request_id,
            client,
            len(body.query),
            len(reply.results),
            extra={
                "component": "api",
                "event": "search",
                "request_id": request_id,
                "client": client,
                "query_chars": len(body.query),
                "results": len(reply.results),
            },
        )
        return JSONResponse(reply_as_dict(reply))

    @app.get("/v1/wiki/article")
    def article_route(
        request: Request,
        page_id: Annotated[int | None, Query(ge=0, le=MAX_PAGE_ID)] = None,
        title: Annotated[str | None, Query(min_length=1, max_length=300)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> Response:
        """One window of one article's text, by page id or by title."""
        request_id: str = request.state.request_id
        client: str = request.state.client
        if (page_id is None) == (title is None):
            return _error(422, "invalid_request", "give exactly one of page_id or title")
        with served.lease() as serving:
            if serving is None:
                return _not_ready()
            reply = serving.search.read(page_id=page_id, title=title, offset=offset)
        if reply is None:
            return _error(404, "not_found", "no article matches")
        logger.info(
            "article request=%s client=%s page_id=%d offset=%d",
            request_id,
            client,
            reply.page_id,
            reply.offset,
            extra={
                "component": "api",
                "event": "article",
                "request_id": request_id,
                "client": client,
                "page_id": reply.page_id,
            },
        )
        return JSONResponse(reply_as_dict(reply))

    @app.get("/v1/wiki/health")
    def health_route() -> Response:
        """Snapshot, counts and build state; `status` says whether to trust the service."""
        return JSONResponse(health_source())

    return app


def build_service(
    config: MirrorConfig,
    served: ServedSnapshot,
    client: QdrantClient,
    *,
    keys: ClientKeys | None = None,
    state_reader: Callable[[Any], Any] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Assemble the application over a snapshot holder and a Qdrant client (what `serve` runs).

    Args:
        config: Service settings, with the API key set.
        served: The snapshot being served; the caller owns it and closes it.
        client: The Qdrant client.
        keys: Who may call; see `create_app`.
        state_reader: Reads the build state; defaults to `build.read_build_state`.
        clock: The current time (UTC) for the staleness check of `/v1/wiki/health`; defaults to
            the system clock. Tests inject theirs so a fixed snapshot date does not age out.

    Raises:
        ValueError: As `create_app`.
    """
    health = ServiceHealth(config, served, client, state_reader=state_reader, clock=clock)
    return create_app(config, served, health, keys=keys)


def _error(status_code: int, code: str, detail: str) -> JSONResponse:
    """A JSON error in the service's shape."""
    return JSONResponse({"error": code, "detail": detail}, status_code=status_code)


def _not_ready() -> JSONResponse:
    """The 503 for a service that has no snapshot to serve yet."""
    return _error(503, "not_ready", "no snapshot is being served yet")
