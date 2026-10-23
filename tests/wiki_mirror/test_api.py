"""Qualifies the mirror's HTTP API: auth, request ids, validation, errors, shapes, health.

Runs the FastAPI app in-process through `TestClient` over a real finalized store and an
in-memory index (search and article routes) or a stub search (error paths), so the contract with
the primary client (JSON shapes, status codes, `X-Request-ID`) is checked without a server.
Failure paths named here: no key, a wrong key and a revoked key (401), a bad request id
(replaced), unknown fields and out-of-range values (422), no snapshot yet (503 not_ready),
Qdrant refusing (503 index_unavailable), a crash (JSON 500), a malformed clients file (refused at
start, ignored at runtime), the alias moving or its store being unusable, and every health state.
The search modes: an unknown mode (422), and `semantic` or `hybrid` with no usable semantic index,
after the monthly build, with the embedding server failing, timing out or refusing the key (503
semantic_unavailable, never keyword results, never the key or the query), and health's
`search_modes` and `searchable_reason`. One test drives the route through every embedding failure
that logs (busy, rejected, bad_reply, unreachable, breaker_open), with the key and the question in
the fake server's own error text and every logger at DEBUG, and finds neither in a log line or a
reply.

See docs/DESIGN.md section 6.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse
from starlette.testclient import WebSocketDisconnect

from tests.fixtures.clock import FrozenClock
from tests.fixtures.wiki_mirror_fakes import (
    API_KEY,
    FakeEmbeddingServer,
    FakeState,
    FakeStateStore,
    StubServed,
    build_snapshot,
    index_without_store,
    make_article,
    make_config,
    make_snapshot,
    memory_client,
    served_over,
    write_store,
)
from wiki_mirror.api import (
    MAINTENANCE_STALE_MINUTES,
    QDRANT_CRASH_REPORT_HOURS,
    STALE_AFTER_DAYS,
    ServiceHealth,
    build_service,
    create_app,
)
from wiki_mirror.config import MirrorConfig
from wiki_mirror.keys import (
    RECHECK_SECONDS,
    ClientKeyError,
    ClientKeys,
    add_client,
    revoke_client,
)
from wiki_mirror.records import MAX_PAGE_ID, Article
from wiki_mirror.search import SearchReply, SemanticUnavailableError
from wiki_mirror.serving import ALIAS_RECHECK_SECONDS, ServedSnapshot

pytestmark = pytest.mark.contract

_AUTH = {"Authorization": f"Bearer {API_KEY}"}
_FILLER = "The committee reviewed the proposal and recorded its findings in the minutes. "

#: Builds an app over a real store and index; arguments are `MirrorConfig` overrides.
AppFactory = Callable[..., tuple[TestClient, MirrorConfig]]


def _now() -> datetime:
    """The health clock of every test that is not about staleness.

    Between the dates of the test snapshots (2026-10-01 and 2026-11-01), so neither is past the
    staleness limit and the tests do not age out as the real calendar moves on.
    """
    return datetime(2026, 11, 5, tzinfo=UTC)


def _articles(label: str = "") -> list[Article]:
    """Two articles: one with a long body (for paged reads) and one short."""
    long_body = "\n\n".join(f"Paragraph {n} about zeppelins. {_FILLER * 4}" for n in range(30))
    return [
        make_article(3, "Zeppelin", {"": long_body}),
        make_article(5, "Mercury", {"": f"Mercury is a planet. {label} {_FILLER}"}),
    ]


@pytest.fixture
def app_factory(tmp_path: Path, frozen_clock: FrozenClock) -> Iterator[AppFactory]:
    """Makes apps over `_articles` and closes every snapshot holder they used."""
    holders: list[ServedSnapshot] = []

    def make(**config: object) -> tuple[TestClient, MirrorConfig]:
        served, client, settings = served_over(
            tmp_path, _articles(), clock=frozen_clock.monotonic, **config
        )
        holders.append(served)
        health = ServiceHealth(
            settings, served, client, state_reader=FakeStateStore().read, clock=_now
        )
        return TestClient(create_app(settings, served, health)), settings

    yield make
    for holder in holders:
        holder.close()


class _StubSearch:
    """A search that raises what it is given, to drive the error paths."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def search(
        self, query: str, limit: int, mode: str = "lexical", *, request_id: str | None = None
    ) -> SearchReply:
        raise self._error


def _stub_app(config: MirrorConfig, error: Exception) -> TestClient:
    """An app whose search raises `error`."""
    return TestClient(create_app(config, StubServed(_StubSearch(error)), lambda: {"status": "ok"}))  # type: ignore[arg-type]


def _empty_app(config: MirrorConfig, health: dict[str, Any] | None = None) -> TestClient:
    """An app with no snapshot to serve."""
    return TestClient(create_app(config, StubServed(None), lambda: health or {}))  # type: ignore[arg-type]


# -- authentication ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/wiki/search"),
        ("get", "/v1/wiki/article?page_id=3"),
        ("get", "/v1/wiki/health"),
        ("get", "/v1/wiki/nothing-here"),
        ("post", "/mcp"),
        ("get", "/mcp"),
    ],
)
def test_api_request_without_a_key_is_401_on_every_route(
    app_factory: AppFactory, method: str, path: str
) -> None:
    client, _ = app_factory()
    body = {"query": "zeppelin"} if method == "post" else None
    response = client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_api_wrong_key_is_401_and_does_not_echo_either_key(app_factory: AppFactory) -> None:
    client, _ = app_factory()
    response = client.get("/v1/wiki/health", headers={"Authorization": "Bearer " + "x" * 40})
    assert response.status_code == 401
    assert API_KEY not in response.text
    assert "x" * 40 not in response.text


def test_api_key_with_a_wrong_scheme_is_401(app_factory: AppFactory) -> None:
    client, _ = app_factory()
    response = client.get("/v1/wiki/health", headers={"Authorization": f"Basic {API_KEY}"})
    assert response.status_code == 401


def test_api_empty_bearer_token_is_401(app_factory: AppFactory) -> None:
    client, _ = app_factory()
    assert client.get("/v1/wiki/health", headers={"Authorization": "Bearer "}).status_code == 401


def test_api_bad_key_beats_a_bad_body_so_validation_never_leaks_to_strangers(
    app_factory: AppFactory,
) -> None:
    client, _ = app_factory()
    assert client.post("/v1/wiki/search", json={"bogus": 1}).status_code == 401


def test_create_app_without_an_api_key_refuses(tmp_path: Path) -> None:
    config = make_config(tmp_path, api_key=None)
    with pytest.raises(ValueError, match="WIKI_MIRROR_API_KEY"):
        create_app(config, StubServed(None), lambda: {})  # type: ignore[arg-type]


def test_api_unknown_path_with_a_key_is_the_json_404(app_factory: AppFactory) -> None:
    client, _ = app_factory()
    response = client.get("/v1/wiki/nothing-here", headers=_AUTH)
    assert (response.status_code, response.json()["error"]) == (404, "not_found")


# -- per-agent keys ---------------------------------------------------------------------------


def _keyed_app(
    tmp_path: Path, frozen_clock: FrozenClock
) -> tuple[TestClient, Path, ServedSnapshot]:
    """An app whose keys come from a clients file in `tmp_path`, on the test clock."""
    served, client, settings = served_over(
        tmp_path, _articles(), clock=frozen_clock.monotonic, clients_file=tmp_path / "clients.json"
    )
    keys = ClientKeys(settings.clients_file, API_KEY, clock=frozen_clock.monotonic)
    health = ServiceHealth(settings, served, client, state_reader=FakeStateStore().read, clock=_now)
    app = create_app(settings, served, health, keys=keys)
    return TestClient(app), settings.clients_file, served


def test_api_listed_agent_key_is_accepted(tmp_path: Path, frozen_clock: FrozenClock) -> None:
    path = tmp_path / "clients.json"
    key = add_client(path, "claude-code")
    client, _, served = _keyed_app(tmp_path, frozen_clock)
    response = client.get("/v1/wiki/health", headers={"Authorization": f"Bearer {key}"})
    served.close()
    assert response.status_code == 200


def test_api_search_logs_the_name_of_the_agent_that_asked(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    key = add_client(tmp_path / "clients.json", "claude-code")
    client, _, served = _keyed_app(tmp_path, frozen_clock)
    with caplog.at_level(logging.INFO, logger="wiki_mirror.api"):
        client.post(
            "/v1/wiki/search",
            json={"query": "zeppelin"},
            headers={"Authorization": f"Bearer {key}", "X-Request-ID": "req-5"},
        )
        client.post("/v1/wiki/search", json={"query": "zeppelin"}, headers=_AUTH)
    served.close()
    assert "search request=req-5 client=claude-code" in caplog.text
    assert "client=primary" in caplog.text


def test_api_revoked_agent_key_is_401_without_restarting(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    path = tmp_path / "clients.json"
    key = add_client(path, "worker")
    client, _, served = _keyed_app(tmp_path, frozen_clock)
    headers = {"Authorization": f"Bearer {key}"}
    assert client.get("/v1/wiki/health", headers=headers).status_code == 200
    revoke_client(path, "worker")
    frozen_clock.advance(RECHECK_SECONDS + 1)
    after = client.get("/v1/wiki/health", headers=headers)
    served.close()
    assert after.status_code == 401


def test_api_agent_key_added_while_running_is_accepted_without_restarting(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    client, path, served = _keyed_app(tmp_path, frozen_clock)
    key = add_client(path, "worker")
    headers = {"Authorization": f"Bearer {key}"}
    assert client.get("/v1/wiki/health", headers=headers).status_code == 401
    frozen_clock.advance(RECHECK_SECONDS + 1)
    after = client.get("/v1/wiki/health", headers=headers)
    served.close()
    assert after.status_code == 200


def test_api_malformed_clients_file_at_runtime_keeps_the_last_good_keys(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    path = tmp_path / "clients.json"
    key = add_client(path, "worker")
    client, _, served = _keyed_app(tmp_path, frozen_clock)
    path.write_text("{not json", encoding="utf-8")
    os.utime(path, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    frozen_clock.advance(RECHECK_SECONDS + 1)
    headers = {"Authorization": f"Bearer {key}"}
    codes = (
        client.get("/v1/wiki/health", headers=headers).status_code,
        client.get("/v1/wiki/health", headers=_AUTH).status_code,
    )
    served.close()
    assert codes == (200, 200)


def test_create_app_with_a_malformed_clients_file_refuses_to_start(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    path.write_text("{not json", encoding="utf-8")
    config = make_config(tmp_path, clients_file=path)
    with pytest.raises(ClientKeyError):
        create_app(config, StubServed(None), lambda: {})  # type: ignore[arg-type]


# -- request ids ------------------------------------------------------------------------------


def test_api_valid_request_id_is_echoed(app_factory: AppFactory) -> None:
    client, _ = app_factory()
    response = client.get("/v1/wiki/health", headers={**_AUTH, "X-Request-ID": "turn-42:a.b_c"})
    assert response.headers["X-Request-ID"] == "turn-42:a.b_c"


@pytest.mark.parametrize("bad", ["has space", "x" * 65, "semi;colon", "slash/id"])
def test_api_malformed_request_id_is_replaced_with_a_generated_one(
    app_factory: AppFactory, bad: str
) -> None:
    client, _ = app_factory()
    response = client.get("/v1/wiki/health", headers={**_AUTH, "X-Request-ID": bad})
    assert response.headers["X-Request-ID"] != bad
    assert len(response.headers["X-Request-ID"]) == 32


def test_api_missing_request_id_is_generated_even_on_a_401(app_factory: AppFactory) -> None:
    client, _ = app_factory()
    response = client.get("/v1/wiki/health")
    assert len(response.headers["X-Request-ID"]) == 32


# -- search -----------------------------------------------------------------------------------


def test_api_search_returns_the_contract_shape(app_factory: AppFactory) -> None:
    client, _ = app_factory()
    response = client.post(
        "/v1/wiki/search", json={"query": "mercury planet", "limit": 3}, headers=_AUTH
    )
    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"snapshot", "mode", "results"}
    assert set(body["results"][0]) == {
        "page_id",
        "revision_id",
        "title",
        "section",
        "snippet",
        "score",
        "url",
        "disambiguation",
    }
    assert body["results"][0]["url"] == "https://en.wikipedia.org/wiki/Mercury?oldid=51"


def test_api_search_limit_defaults_to_eight(tmp_path: Path, frozen_clock: FrozenClock) -> None:
    articles = [make_article(n, f"Topic{n}") for n in range(1, 13)]
    served, client, settings = served_over(tmp_path, articles, clock=frozen_clock.monotonic)
    app = build_service(settings, served, client, state_reader=FakeStateStore().read, clock=_now)
    response = TestClient(app).post(
        "/v1/wiki/search", json={"query": "described article subject"}, headers=_AUTH
    )
    served.close()
    assert len(response.json()["results"]) == 8


@pytest.mark.parametrize(
    "body",
    [
        {"query": "x", "surprise": True},
        {"query": "x", "mode": "fuzzy"},
        {"query": "x", "mode": None},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": 21},
        {"query": ""},
        {"query": "q" * 501},
        {"limit": 3},
    ],
)
def test_api_search_invalid_body_is_422_in_the_error_shape(
    app_factory: AppFactory, body: dict[str, Any]
) -> None:
    client, _ = app_factory()
    response = client.post("/v1/wiki/search", json=body, headers=_AUTH)
    assert response.status_code == 422
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "invalid_request"


def test_api_search_422_detail_never_repeats_the_query_text(app_factory: AppFactory) -> None:
    client, _ = app_factory()
    secret = "my private medical question " * 30
    response = client.post("/v1/wiki/search", json={"query": secret}, headers=_AUTH)
    assert response.status_code == 422
    assert "private" not in response.text


def test_api_search_without_a_snapshot_is_503_not_ready(tmp_path: Path) -> None:
    client = _empty_app(make_config(tmp_path), {"status": "empty"})
    response = client.post("/v1/wiki/search", json={"query": "zeppelin"}, headers=_AUTH)
    assert (response.status_code, response.json()["error"]) == (503, "not_ready")


def test_api_search_when_qdrant_refuses_is_503_index_unavailable(tmp_path: Path) -> None:
    error = UnexpectedResponse(404, "Not Found", b"alias missing", {})  # type: ignore[arg-type]
    client = _stub_app(make_config(tmp_path), error)
    response = client.post("/v1/wiki/search", json={"query": "zeppelin"}, headers=_AUTH)
    assert (response.status_code, response.json()["error"]) == (503, "index_unavailable")


def test_api_search_unexpected_crash_is_a_json_500_with_the_request_id(tmp_path: Path) -> None:
    client = _stub_app(make_config(tmp_path), RuntimeError("boom"))
    response = client.post(
        "/v1/wiki/search", json={"query": "zeppelin"}, headers={**_AUTH, "X-Request-ID": "r-1"}
    )
    assert response.status_code == 500
    assert response.json()["error"] == "internal"
    assert response.headers["X-Request-ID"] == "r-1"
    assert "boom" not in response.text


def test_api_search_logs_the_query_length_and_never_the_query(
    app_factory: AppFactory, caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = app_factory()
    with caplog.at_level(logging.INFO, logger="wiki_mirror.api"):
        client.post(
            "/v1/wiki/search",
            json={"query": "zeppelin secretword"},
            headers={**_AUTH, "X-Request-ID": "req-77"},
        )
    assert "query_chars=19" in caplog.text
    assert "request=req-77" in caplog.text
    assert "secretword" not in caplog.text
    assert "mode=lexical" in caplog.text


# -- search modes -----------------------------------------------------------------------------

_SEARCH_RESULT_FIELDS = {
    "page_id",
    "revision_id",
    "title",
    "section",
    "snippet",
    "score",
    "url",
    "disambiguation",
}


@dataclass
class SemanticApp:
    """An app whose snapshot has a semantic index, and the fake server that embeds questions."""

    client: TestClient
    server: FakeEmbeddingServer
    served: ServedSnapshot
    qdrant: QdrantClient
    config: MirrorConfig
    clock: FrozenClock


@pytest.fixture
def semantic_app(tmp_path: Path, frozen_clock: FrozenClock) -> Iterator[SemanticApp]:
    """An app over `_articles` with a paired semantic index; its holder is closed afterwards."""
    server = FakeEmbeddingServer()
    served, qdrant, settings = served_over(
        tmp_path, _articles(), clock=frozen_clock.monotonic, semantic=server
    )
    health = ServiceHealth(settings, served, qdrant, state_reader=FakeStateStore().read, clock=_now)
    app = TestClient(create_app(settings, served, health))
    yield SemanticApp(app, server, served, qdrant, settings, frozen_clock)
    served.close()


def _search(app: SemanticApp | TestClient, **body: object) -> Any:
    """POST a search with the primary key."""
    client = app.client if isinstance(app, SemanticApp) else app
    return client.post("/v1/wiki/search", json=body, headers=_AUTH)


def test_api_search_without_a_mode_is_lexical(app_factory: AppFactory) -> None:
    client, _ = app_factory()
    assert _search(client, query="mercury planet").json()["mode"] == "lexical"


@pytest.mark.parametrize("mode", ["lexical", "semantic", "hybrid"])
def test_api_search_every_mode_returns_the_same_shape_and_names_itself(
    semantic_app: SemanticApp, mode: str
) -> None:
    response = _search(semantic_app, query="mercury planet", mode=mode)
    body = response.json()
    assert response.status_code == 200
    assert (set(body), body["mode"]) == ({"snapshot", "mode", "results"}, mode)
    assert set(body["results"][0]) == _SEARCH_RESULT_FIELDS
    assert body["results"][0]["title"] == "Mercury"
    assert body["results"][0]["page_id"] == 5


def test_api_search_semantic_asks_the_embedding_server_and_lexical_does_not(
    semantic_app: SemanticApp,
) -> None:
    _search(semantic_app, query="mercury planet", mode="lexical")
    assert semantic_app.server.requests == []
    _search(semantic_app, query="mercury planet", mode="semantic")
    assert len(semantic_app.server.requests) == 1


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_api_search_semantic_mode_without_a_semantic_index_is_503_and_never_keyword_results(
    app_factory: AppFactory, mode: str
) -> None:
    client, _ = app_factory()
    response = _search(client, query="mercury planet", mode=mode)
    assert response.status_code == 503
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "semantic_unavailable"
    assert "no semantic index yet" in response.json()["detail"]


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_api_search_after_the_monthly_build_is_503_until_the_semantic_catch_up(
    semantic_app: SemanticApp, mode: str
) -> None:
    november = make_snapshot("2026-11-01")
    build_snapshot(semantic_app.config, semantic_app.qdrant, _articles("november"), november)
    semantic_app.clock.advance(ALIAS_RECHECK_SECONDS + 1)
    semantic = _search(semantic_app, query="mercury planet", mode=mode)
    lexical = _search(semantic_app, query="mercury planet")
    assert semantic.status_code == 503
    assert semantic.json()["error"] == "semantic_unavailable"
    assert "of snapshot 20261001" in semantic.json()["detail"]
    assert "of 20261101" in semantic.json()["detail"]
    assert (lexical.status_code, lexical.json()["snapshot"]) == (200, "2026-11-01")


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_api_search_embedding_server_failing_is_503_semantic_unavailable_with_its_reason(
    semantic_app: SemanticApp, mode: str
) -> None:
    semantic_app.server.reply = httpx.Response(500, text="the router's own error page")
    response = _search(semantic_app, query="mercury planet", mode=mode)
    assert response.status_code == 503
    assert response.json() == {
        "error": "semantic_unavailable",
        "detail": "the embedding server answered 500",
    }


def test_api_search_embedding_server_not_answering_is_503_naming_the_timeout(
    semantic_app: SemanticApp,
) -> None:
    semantic_app.server.reply = httpx.ReadTimeout("slow")
    response = _search(semantic_app, query="mercury planet", mode="semantic")
    assert response.status_code == 503
    assert "did not answer within" in response.json()["detail"]


def test_api_search_refused_embedding_key_is_503_without_the_key_or_the_query(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    key = "sk-sentinel-0123456789abcdef"
    server = FakeEmbeddingServer()
    server.reply = httpx.Response(401, text=f"bad key {key} for my private question")
    # The holder reads the key from its environment, as the service does from mirror.env.
    served, qdrant, settings = served_over(
        tmp_path,
        _articles(),
        clock=frozen_clock.monotonic,
        semantic=server,
        environ={"EMBEDDING_API_KEY": key},
    )
    health = ServiceHealth(settings, served, qdrant, state_reader=FakeStateStore().read, clock=_now)
    try:
        client = TestClient(create_app(settings, served, health))
        response = _search(client, query="my private question", mode="hybrid")
    finally:
        served.close()
    assert response.status_code == 503
    assert response.json()["error"] == "semantic_unavailable"
    assert key not in response.text
    assert "private" not in response.text


def test_api_search_embedding_failures_log_neither_the_key_nor_the_query(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    key = "sk-sentinel-0123456789abcdef"
    question = "my private question"
    # Every failure the fake server produces carries both in its own text, so a path that echoed
    # an exception or a body into a log line or a reply would show them.
    secret = f"{key} {question}"
    server = FakeEmbeddingServer()
    answering = server.transport
    gate, blocking, arrived = threading.Event(), threading.Event(), threading.Semaphore(0)

    def handler(request: httpx.Request) -> httpx.Response:
        if blocking.is_set():
            arrived.release()
            assert gate.wait(5), "the test never opened the gate"
        return answering.handle_request(request)  # type: ignore[return-value]

    server.transport = httpx.MockTransport(handler)
    served, qdrant, settings = served_over(
        tmp_path,
        _articles(),
        clock=frozen_clock.monotonic,
        semantic=server,
        environ={"EMBEDDING_API_KEY": key},
    )
    health = ServiceHealth(settings, served, qdrant, state_reader=FakeStateStore().read, clock=_now)
    app = create_app(settings, served, health)
    replies: list[httpx.Response] = []

    def ask() -> httpx.Response:
        reply = _search(TestClient(app), query=question, mode="hybrid")
        replies.append(reply)
        return reply

    try:
        # A lexical search first: the holder's first look pairs the semantic index and builds the
        # embedder, which turns the `httpx` logger down to WARNING.
        _search(TestClient(app), query=question)
        with (
            caplog.at_level(logging.DEBUG),
            caplog.at_level(logging.DEBUG, logger="httpx"),
            caplog.at_level(logging.DEBUG, logger="httpcore"),
        ):
            # busy: four searches held inside the embedding server, a fifth turned away.
            blocking.set()
            held = [threading.Thread(target=ask) for _ in range(4)]
            for thread in held:
                thread.start()
            try:
                for _ in held:
                    assert arrived.acquire(timeout=5)
                busy = ask()
            finally:
                blocking.clear()
                gate.set()
                for thread in held:
                    thread.join(5)
            server.reply = httpx.Response(401, text=secret)
            rejected = ask()
            server.reply = httpx.Response(200, text=secret)
            bad_reply = ask()
            server.reply = httpx.ConnectError(f"refused: {secret}")
            unreachable = [ask() for _ in range(3)]
            breaker_open = ask()
    finally:
        served.close()
    for reply in (busy, rejected, bad_reply, *unreachable, breaker_open):
        assert reply.status_code == 503
        assert reply.json()["error"] == "semantic_unavailable"
    assert "busy with other searches" in busy.json()["detail"]
    assert breaker_open.json()["detail"].startswith("the embedding server failed 3 times")
    # Each failure path logged its kind, so the checks below saw log lines from every one.
    for kind in ("busy", "rejected", "bad_reply", "unreachable", "breaker_open"):
        assert f"kind={kind}" in caplog.text
    assert key not in caplog.text
    assert question not in caplog.text
    assert not any(key in reply.text or question in reply.text for reply in replies)


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_api_search_forwards_the_request_id_to_the_embedding_server(
    semantic_app: SemanticApp, mode: str
) -> None:
    semantic_app.client.post(
        "/v1/wiki/search",
        json={"query": "mercury planet", "mode": mode},
        headers={**_AUTH, "X-Request-ID": "turn-42:a.b_c"},
    )
    (request,) = semantic_app.server.requests
    assert request.headers["x-request-id"] == "turn-42:a.b_c"


def test_api_search_replaced_request_id_is_the_one_forwarded_to_the_embedding_server(
    semantic_app: SemanticApp,
) -> None:
    response = semantic_app.client.post(
        "/v1/wiki/search",
        json={"query": "mercury planet", "mode": "semantic"},
        headers={**_AUTH, "X-Request-ID": "has space"},
    )
    (request,) = semantic_app.server.requests
    assert request.headers["x-request-id"] == response.headers["X-Request-ID"]
    assert request.headers["x-request-id"] != "has space"


def test_api_search_semantic_failure_is_logged_with_its_kind_and_never_the_query(
    semantic_app: SemanticApp, caplog: pytest.LogCaptureFixture
) -> None:
    semantic_app.server.reply = httpx.Response(403, text="no")
    with caplog.at_level(logging.WARNING, logger="wiki_mirror.api"):
        _search(semantic_app, query="a very private question", mode="semantic")
    assert "kind=rejected" in caplog.text
    assert "mode=semantic" in caplog.text
    assert "private" not in caplog.text


def test_api_search_semantic_success_is_logged_with_its_mode_and_never_the_query(
    semantic_app: SemanticApp, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="wiki_mirror.api"):
        _search(semantic_app, query="a very private question", mode="hybrid")
    assert "mode=hybrid" in caplog.text
    assert "query_chars=23" in caplog.text
    assert "private" not in caplog.text


def test_api_search_semantic_when_qdrant_refuses_is_503_index_unavailable_not_semantic(
    tmp_path: Path,
) -> None:
    error = UnexpectedResponse(404, "Not Found", b"collection missing", {})  # type: ignore[arg-type]
    client = _stub_app(make_config(tmp_path), error)
    response = _search(client, query="zeppelin", mode="semantic")
    assert (response.status_code, response.json()["error"]) == (503, "index_unavailable")


def test_api_search_semantic_unavailable_from_the_search_is_503_with_its_reason(
    tmp_path: Path,
) -> None:
    error = SemanticUnavailableError("the stand-in reason", "unpaired")
    client = _stub_app(make_config(tmp_path), error)
    response = _search(client, query="zeppelin", mode="semantic")
    assert response.status_code == 503
    assert response.json() == {"error": "semantic_unavailable", "detail": "the stand-in reason"}


# -- article ----------------------------------------------------------------------------------


def test_api_article_by_page_id_returns_the_first_window(app_factory: AppFactory) -> None:
    client, _ = app_factory(max_read_chars=1000)
    body = client.get("/v1/wiki/article?page_id=3&offset=0", headers=_AUTH).json()
    assert body["title"] == "Zeppelin"
    assert (body["offset"], len(body["text"])) == (0, body["next_offset"])
    assert body["total_chars"] > 1000
    assert set(body) == {
        "page_id",
        "revision_id",
        "title",
        "snapshot",
        "revision_time",
        "url",
        "license",
        "offset",
        "next_offset",
        "total_chars",
        "text",
    }


def test_api_article_by_title_with_underscores_resolves(app_factory: AppFactory) -> None:
    client, _ = app_factory()
    response = client.get("/v1/wiki/article?title=mercury", headers=_AUTH)
    assert response.json()["page_id"] == 5


def test_api_article_second_window_starts_at_the_next_offset(app_factory: AppFactory) -> None:
    client, _ = app_factory(max_read_chars=1000)
    first = client.get("/v1/wiki/article?page_id=3", headers=_AUTH).json()
    second = client.get(
        f"/v1/wiki/article?page_id=3&offset={first['next_offset']}", headers=_AUTH
    ).json()
    assert second["offset"] == first["next_offset"]


def test_api_article_unknown_page_is_404_not_found(app_factory: AppFactory) -> None:
    client, _ = app_factory()
    response = client.get("/v1/wiki/article?page_id=999", headers=_AUTH)
    assert (response.status_code, response.json()["error"]) == (404, "not_found")


@pytest.mark.parametrize(
    "query", ["", "?page_id=3&title=Zeppelin", "?page_id=abc", "?page_id=3&offset=-1", "?title="]
)
def test_api_article_bad_selector_is_422(app_factory: AppFactory, query: str) -> None:
    client, _ = app_factory()
    response = client.get(f"/v1/wiki/article{query}", headers=_AUTH)
    assert (response.status_code, response.json()["error"]) == (422, "invalid_request")


def test_api_article_without_a_snapshot_is_503_not_ready(tmp_path: Path) -> None:
    client = _empty_app(make_config(tmp_path))
    response = client.get("/v1/wiki/article?page_id=3", headers=_AUTH)
    assert (response.status_code, response.json()["error"]) == (503, "not_ready")


# -- following the alias ----------------------------------------------------------------------


def test_api_started_with_nothing_built_serves_the_first_build_without_a_new_app(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    config = make_config(tmp_path)
    qdrant = memory_client()
    served = ServedSnapshot(config, qdrant, clock=frozen_clock.monotonic)
    app = TestClient(
        build_service(config, served, qdrant, state_reader=FakeStateStore().read, clock=_now)
    )
    search = {"query": "mercury planet"}
    assert app.post("/v1/wiki/search", json=search, headers=_AUTH).status_code == 503
    build_snapshot(config, qdrant, _articles("october"), make_snapshot("2026-10-01"))
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    after = app.post("/v1/wiki/search", json=search, headers=_AUTH)
    served.close()
    assert (after.status_code, after.json()["snapshot"]) == (200, "2026-10-01")


def test_api_alias_moving_to_a_new_snapshot_is_served_without_a_restart(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    config = make_config(tmp_path)
    qdrant = memory_client()
    build_snapshot(config, qdrant, _articles("october"), make_snapshot("2026-10-01"))
    served = ServedSnapshot(config, qdrant, clock=frozen_clock.monotonic)
    app = TestClient(
        build_service(config, served, qdrant, state_reader=FakeStateStore().read, clock=_now)
    )
    article = "/v1/wiki/article?title=Mercury"
    assert app.get(article, headers=_AUTH).json()["snapshot"] == "2026-10-01"
    build_snapshot(config, qdrant, _articles("november"), make_snapshot("2026-11-01"))
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    after = app.get(article, headers=_AUTH).json()
    served.close()
    assert (after["snapshot"], "november" in after["text"]) == ("2026-11-01", True)


def test_api_new_store_incomplete_keeps_serving_the_old_snapshot_and_health_is_degraded(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    config = make_config(tmp_path)
    qdrant = memory_client()
    build_snapshot(config, qdrant, _articles("october"), make_snapshot("2026-10-01"))
    served = ServedSnapshot(config, qdrant, clock=frozen_clock.monotonic)
    app = TestClient(
        build_service(config, served, qdrant, state_reader=FakeStateStore().read, clock=_now)
    )
    assert app.get("/v1/wiki/health", headers=_AUTH).json()["status"] == "ok"
    november = make_snapshot("2026-11-01")
    write_store(config, _articles("november"), november, finalize=False)
    index_without_store(config, qdrant, _articles("november"), november)
    frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
    health = app.get("/v1/wiki/health", headers=_AUTH).json()
    article = app.get("/v1/wiki/article?title=Mercury", headers=_AUTH).json()
    served.close()
    assert (health["status"], health["snapshot"]) == ("degraded", "2026-10-01")
    assert article["snapshot"] == "2026-10-01"


# -- health -----------------------------------------------------------------------------------


def _health(
    tmp_path: Path,
    frozen_clock: FrozenClock,
    state: FakeState,
    *,
    indexed: bool = True,
    client: QdrantClient | None = None,
) -> dict[str, Any]:
    """Run `ServiceHealth` over an (optionally) indexed real store and a given build state."""
    if indexed:
        served, qdrant, config = served_over(tmp_path, _articles(), clock=frozen_clock.monotonic)
    else:
        config = make_config(tmp_path)
        qdrant = memory_client()
        served = ServedSnapshot(config, client or qdrant, clock=frozen_clock.monotonic)
    reader = FakeStateStore(state).read
    health = ServiceHealth(config, served, client or qdrant, state_reader=reader, clock=_now)
    try:
        return health()
    finally:
        served.close()


def test_health_serving_snapshot_is_ok_with_counts_and_the_build_state(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    report = _health(tmp_path, frozen_clock, FakeState(phase="serving", snapshot_date="2026-10-01"))
    assert report["status"] == "ok"
    assert (report["snapshot"], report["articles"], report["collection"]) == (
        "2026-10-01",
        2,
        "wikipedia_lexical_20261001",
    )
    assert report["passages"] > 0
    assert report["build"]["phase"] == "serving"
    assert set(report) == {
        "status",
        "reasons",
        "semantic",
        "maintenance",
        "snapshot",
        "applied_snapshot",
        "articles",
        "passages",
        "collection",
        "build",
        "update",
        "search_modes",
    }


_UPDATE_RECORD = {
    "at": "2026-10-18T02:21:07-05:00",
    "from": "d127090abcde",
    "to": "e238101fghij",
    "outcome": "updated",
    "detail": "healthy at e238101",
    "unattended": True,
}


def _write_update_record(tmp_path: Path, content: str) -> None:
    """Leave `content` where `update.sh` leaves its record (config data_dir is tmp_path/data)."""
    path = tmp_path / "data" / "state" / "update.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_health_reports_the_last_update_from_the_updaters_file(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    _write_update_record(tmp_path, json.dumps(_UPDATE_RECORD))
    report = _health(tmp_path, frozen_clock, FakeState(phase="serving"))
    assert report["update"] == _UPDATE_RECORD


def test_health_without_an_update_record_reports_null(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    report = _health(tmp_path, frozen_clock, FakeState(phase="serving"))
    assert report["update"] is None


def test_health_with_a_garbage_update_record_reports_null_and_still_answers(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    _write_update_record(tmp_path, "{not json")
    report = _health(tmp_path, frozen_clock, FakeState(phase="serving"))
    assert report["update"] is None
    assert report["status"] == "ok"


@pytest.mark.parametrize("outcome", ["failed", "rolled_back", "held"])
def test_health_a_bad_update_outcome_is_reported_but_does_not_change_the_status(
    tmp_path: Path, frozen_clock: FrozenClock, outcome: str
) -> None:
    _write_update_record(tmp_path, json.dumps({**_UPDATE_RECORD, "outcome": outcome}))
    report = _health(tmp_path, frozen_clock, FakeState(phase="serving"))
    assert report["update"]["outcome"] == outcome
    assert report["status"] == "ok"


def test_health_reports_the_applied_snapshot_the_collection_reflects(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    report = _health(tmp_path, frozen_clock, FakeState(phase="serving"))
    assert report["applied_snapshot"] == "20261001"


def test_health_nothing_served_reports_no_applied_snapshot(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    report = _health(tmp_path, frozen_clock, FakeState(), indexed=False)
    assert report["applied_snapshot"] is None


def test_health_counts_the_new_store_when_the_applied_snapshot_moves_under_the_same_collection(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = served_over(tmp_path, _articles(), clock=frozen_clock.monotonic)
    health = ServiceHealth(config, served, client, state_reader=FakeStateStore().read, clock=_now)
    first = health()
    november = make_snapshot("2026-11-01")
    write_store(config, [*_articles(), make_article(7, "Venus")], november)
    state = config.data_dir / "state" / "index_lexical_20261001.json"
    moved = json.loads(state.read_text(encoding="utf-8"))
    moved["applied_snapshot"] = "20261101"
    state.write_text(json.dumps(moved), encoding="utf-8")
    second = health()  # health looks fresh each time, so it sees the move at once
    served.close()
    assert (first["collection"], second["collection"]) == ("wikipedia_lexical_20261001",) * 2
    assert (first["articles"], second["articles"]) == (2, 3)
    assert (first["applied_snapshot"], second["applied_snapshot"]) == ("20261001", "20261101")


def test_health_failed_build_while_serving_is_degraded(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    report = _health(tmp_path, frozen_clock, FakeState(phase="failed", error="parse: OSError"))
    assert report["status"] == "degraded"
    assert report["build"]["error"] == "parse: OSError"


def test_health_alias_ahead_of_every_store_is_degraded(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    config = make_config(tmp_path)
    qdrant = memory_client()
    build_snapshot(config, qdrant, _articles(), make_snapshot("2026-10-01"))
    served = ServedSnapshot(config, qdrant, clock=frozen_clock.monotonic)
    health = ServiceHealth(
        config,
        served,
        qdrant,
        state_reader=FakeStateStore(FakeState(phase="serving")).read,
        clock=_now,
    )
    assert health()["status"] == "ok"
    index_without_store(config, qdrant, _articles(), make_snapshot("2026-11-01"))
    report = health()
    served.close()
    assert report["status"] == "degraded"


def test_health_download_running_with_nothing_indexed_is_building(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    report = _health(tmp_path, frozen_clock, FakeState(phase="downloading"), indexed=False)
    assert (report["status"], report["collection"], report["passages"]) == ("building", None, 0)


def test_health_nothing_indexed_and_no_build_is_empty(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    assert _health(tmp_path, frozen_clock, FakeState(), indexed=False)["status"] == "empty"


def test_health_qdrant_down_is_degraded_not_an_error(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    class Down:
        def get_aliases(self) -> Any:
            raise ConnectionError

    report = _health(
        tmp_path,
        frozen_clock,
        FakeState(phase="serving"),
        indexed=False,
        client=Down(),  # type: ignore[arg-type]
    )
    assert report["status"] == "degraded"


def test_health_unreadable_build_state_still_answers(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    def broken(data_dir: Path) -> Any:
        raise ValueError

    served, client, config = served_over(tmp_path, _articles(), clock=frozen_clock.monotonic)
    health = ServiceHealth(config, served, client, state_reader=broken, clock=_now)
    report = health()
    served.close()
    assert report["build"]["phase"] == "unknown"


def test_health_counts_the_articles_of_a_store_once(
    tmp_path: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    served, client, config = served_over(tmp_path, _articles(), clock=frozen_clock.monotonic)
    health = ServiceHealth(config, served, client, state_reader=FakeStateStore().read, clock=_now)
    health()
    with served.lease() as serving:
        assert serving is not None
        counted: list[int] = []
        real = serving.store.count
        monkeypatch.setattr(serving.store, "count", lambda: (counted.append(1), real())[1])
        health()
        health()
    served.close()
    assert counted == []


def test_api_health_route_returns_the_health_source_body(tmp_path: Path) -> None:
    client = _empty_app(make_config(tmp_path), {"status": "empty", "n": 1})
    assert client.get("/v1/wiki/health", headers=_AUTH).json() == {"status": "empty", "n": 1}


def test_build_service_without_a_snapshot_serves_not_ready_and_health(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    config = make_config(tmp_path)
    qdrant = memory_client()
    served = ServedSnapshot(config, qdrant, clock=frozen_clock.monotonic)
    app = build_service(
        config,
        served,
        qdrant,
        state_reader=FakeStateStore(replace(FakeState(), phase="idle")).read,
        clock=_now,
    )
    client = TestClient(app)
    assert client.get("/v1/wiki/health", headers=_AUTH).json()["status"] == "empty"
    assert client.get("/v1/wiki/article?page_id=1", headers=_AUTH).status_code == 503


# -- a mirror that stops refreshing shows on health --------------------------------------------


class _Clock:
    """A settable UTC clock for the staleness tests."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _stale_health(
    tmp_path: Path, frozen_clock: FrozenClock, clock: _Clock, **kwargs: Any
) -> tuple[ServiceHealth, ServedSnapshot]:
    """A health source over the 2026-10-01 snapshot, reading `clock`."""
    served, client, config = served_over(tmp_path, _articles(), clock=frozen_clock.monotonic)
    health = ServiceHealth(
        config,
        served,
        client,
        state_reader=FakeStateStore(FakeState(phase="serving")).read,
        clock=clock,
        **kwargs,
    )
    return health, served


def test_health_snapshot_older_than_the_limit_is_degraded(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    clock = _Clock(datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days=STALE_AFTER_DAYS + 1))
    health, served = _stale_health(tmp_path, frozen_clock, clock)
    report = health()
    served.close()
    assert (report["status"], report["snapshot"]) == ("degraded", "2026-10-01")


def test_health_snapshot_exactly_at_the_limit_is_still_ok(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    clock = _Clock(datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days=STALE_AFTER_DAYS))
    health, served = _stale_health(tmp_path, frozen_clock, clock)
    report = health()
    served.close()
    assert report["status"] == "ok"


def test_health_limit_is_the_one_it_was_given(tmp_path: Path, frozen_clock: FrozenClock) -> None:
    clock = _Clock(datetime(2026, 10, 11, tzinfo=UTC))
    health, served = _stale_health(tmp_path, frozen_clock, clock, stale_after_days=5)
    report = health()
    served.close()
    assert report["status"] == "degraded"


def test_health_stale_snapshot_is_logged_once_and_again_when_it_is_fresh(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _Clock(datetime(2026, 12, 1, tzinfo=UTC))
    health, served = _stale_health(tmp_path, frozen_clock, clock)
    with caplog.at_level(logging.INFO, logger="wiki_mirror.api"):
        statuses = [health()["status"] for _ in range(3)]
        clock.now = datetime(2026, 10, 5, tzinfo=UTC)
        statuses.append(health()["status"])
    served.close()
    assert statuses == ["degraded", "degraded", "degraded", "ok"]
    assert caplog.text.count("past the 40-day limit") == 1
    assert "2026-10-01" in caplog.text
    assert caplog.text.count("fresh again") == 1


def test_health_snapshot_with_an_unreadable_date_is_degraded(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    config = make_config(tmp_path)
    qdrant = memory_client()
    # Eight digits, so it names a collection and a store, but it is not a calendar date.
    build_snapshot(config, qdrant, _articles(), make_snapshot("2026-13-45"))
    served = ServedSnapshot(config, qdrant, clock=frozen_clock.monotonic)
    health = ServiceHealth(
        config, served, qdrant, state_reader=FakeStateStore(FakeState(phase="serving")).read
    )
    report = health()
    served.close()
    assert (report["status"], report["snapshot"]) == ("degraded", "2026-13-45")


def test_health_building_with_nothing_served_is_not_called_stale(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    config = make_config(tmp_path)
    qdrant = memory_client()
    served = ServedSnapshot(config, qdrant, clock=frozen_clock.monotonic)
    health = ServiceHealth(
        config,
        served,
        qdrant,
        state_reader=FakeStateStore(FakeState(phase="downloading")).read,
        clock=_Clock(datetime(2030, 1, 1, tzinfo=UTC)),
    )
    report = health()
    served.close()
    assert report["status"] == "building"


def test_health_index_cut_by_another_chunker_is_degraded(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, client, config = served_over(tmp_path, _articles(), clock=frozen_clock.monotonic)
    state = config.data_dir / "state" / "index_lexical_20261001.json"
    recorded = json.loads(state.read_text(encoding="utf-8"))
    recorded["chunker"] = "v0:1/1"
    state.write_text(json.dumps(recorded), encoding="utf-8")
    health = ServiceHealth(
        config,
        served,
        client,
        state_reader=FakeStateStore(FakeState(phase="serving")).read,
        clock=_now,
    )
    report = health()
    served.close()
    assert (report["status"], report["snapshot"]) == ("degraded", "2026-10-01")


# -- why health is degraded --------------------------------------------------------------------


def test_health_ok_has_no_reasons(tmp_path: Path, frozen_clock: FrozenClock) -> None:
    report = _health(tmp_path, frozen_clock, FakeState(phase="serving"))
    assert (report["status"], report["reasons"]) == ("ok", [])


def test_health_failed_build_names_the_build_in_reasons(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    report = _health(tmp_path, frozen_clock, FakeState(phase="failed", error="parse: OSError"))
    assert report["reasons"] == ["the last build failed"]


def test_health_qdrant_down_names_qdrant_in_reasons(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    class Down:
        def get_aliases(self) -> Any:
            raise ConnectionError

    report = _health(
        tmp_path,
        frozen_clock,
        FakeState(phase="serving"),
        indexed=False,
        client=Down(),  # type: ignore[arg-type]
    )
    assert report["reasons"] == [
        "serving has a problem: Qdrant could not be asked where the alias points"
    ]


def test_health_qdrant_failing_to_count_names_qdrant_in_reasons(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, real, config = served_over(tmp_path, _articles(), clock=frozen_clock.monotonic)

    class CannotCount:
        def count(self, **_: Any) -> Any:
            raise ConnectionError

        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

    health = ServiceHealth(
        config,
        served,
        CannotCount(),  # type: ignore[arg-type]
        state_reader=FakeStateStore(FakeState(phase="serving")).read,
        clock=_now,
    )
    report = health()
    served.close()
    assert (report["status"], report["reasons"]) == ("degraded", ["Qdrant could not be asked"])


def test_health_serving_problem_is_in_reasons(tmp_path: Path, frozen_clock: FrozenClock) -> None:
    served, client, config = served_over(tmp_path, _articles(), clock=frozen_clock.monotonic)
    state = config.data_dir / "state" / "index_lexical_20261001.json"
    recorded = json.loads(state.read_text(encoding="utf-8"))
    recorded["chunker"] = "v0:1/1"
    state.write_text(json.dumps(recorded), encoding="utf-8")
    health = ServiceHealth(
        config,
        served,
        client,
        state_reader=FakeStateStore(FakeState(phase="serving")).read,
        clock=_now,
    )
    report = health()
    served.close()
    assert len(report["reasons"]) == 1
    assert report["reasons"][0].startswith("serving has a problem:")


def test_health_alias_ahead_of_every_store_gives_the_store_problem_as_the_reason(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    config = make_config(tmp_path)
    qdrant = memory_client()
    build_snapshot(config, qdrant, _articles(), make_snapshot("2026-10-01"))
    served = ServedSnapshot(config, qdrant, clock=frozen_clock.monotonic)
    health = ServiceHealth(
        config,
        served,
        qdrant,
        state_reader=FakeStateStore(FakeState(phase="serving")).read,
        clock=_now,
    )
    health()
    index_without_store(config, qdrant, _articles(), make_snapshot("2026-11-01"))
    report = health()
    served.close()
    assert len(report["reasons"]) == 1
    assert report["reasons"][0].startswith("serving has a problem: the article store for snapshot")


def test_health_holder_not_serving_the_alias_without_a_problem_says_it_has_not_caught_up(
    tmp_path: Path,
) -> None:
    class Counts:
        def count(self, **_: Any) -> Any:
            return SimpleNamespace(count=3)

    health = ServiceHealth(
        make_config(tmp_path),
        StubServed(None, alias="wikipedia_lexical_20261101"),  # type: ignore[arg-type]
        Counts(),  # type: ignore[arg-type]
        state_reader=FakeStateStore(FakeState(phase="serving")).read,
        clock=_now,
    )
    report = health()
    assert report["reasons"] == ["the alias moved and serving has not caught up with it yet"]


def test_health_stale_snapshot_is_in_reasons_with_its_age(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    clock = _Clock(datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days=STALE_AFTER_DAYS + 1))
    health, served = _stale_health(tmp_path, frozen_clock, clock)
    report = health()
    served.close()
    assert report["reasons"] == [
        f"the snapshot served is {STALE_AFTER_DAYS + 1} days old, "
        f"past the {STALE_AFTER_DAYS}-day limit"
    ]


# -- the semantic collection on health ---------------------------------------------------------

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_SEMANTIC = "wikipedia_semantic_20261001"


def _qdrant_result(name: str) -> dict[str, Any]:
    """The `result` of a Qdrant reply recorded in `tests/fixtures/qdrant/`."""
    raw = json.loads((_FIXTURES / "qdrant" / name).read_text(encoding="utf-8"))["result"]
    assert isinstance(raw, dict)
    return raw


class _SemanticQdrant:
    """The in-memory client, except that aliases and collection info replay real Qdrant replies.

    The replies are the ones recorded in `tests/fixtures/qdrant/`, parsed into the client's own
    types, so the code under test reads what it would read from the server.
    """

    def __init__(
        self, real: QdrantClient, *, collection: str = "collection_semantic_yellow.json"
    ) -> None:
        self._real = real
        self._collection = collection
        #: Raised by both semantic calls when set.
        self.error: Exception | None = None
        #: Replaces the recorded `optimizer_status` when set.
        self.optimizer_status: Any = None

    def get_aliases(self) -> Any:
        if self.error is not None:
            raise self.error
        return models.CollectionsAliasesResponse.model_validate(_qdrant_result("aliases.json"))

    def get_collection(self, collection_name: str) -> Any:
        assert collection_name == _SEMANTIC
        raw = _qdrant_result(self._collection)
        if self.optimizer_status is not None:
            raw = {**raw, "optimizer_status": self.optimizer_status}
        return models.CollectionInfo.model_validate(raw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _semantic_health(
    tmp_path: Path,
    frozen_clock: FrozenClock,
    *,
    stub: _SemanticQdrant | None = None,
    clock: Callable[[], datetime] = _now,
) -> dict[str, Any]:
    """Health over a real store, with Qdrant answering the semantic questions from fixtures."""
    served, real, config = served_over(tmp_path, _articles(), clock=frozen_clock.monotonic)
    health = ServiceHealth(
        config,
        served,
        stub or _SemanticQdrant(real),  # type: ignore[arg-type]
        state_reader=FakeStateStore(FakeState(phase="serving")).read,
        clock=clock,
    )
    try:
        return health()
    finally:
        served.close()


def _embed_state(**changes: object) -> str:
    """An embedding run's state file in the form `embed` writes (all fields it reads)."""
    state: dict[str, object] = {
        "born": "20261001",
        "last_page_id": 99,
        "articles": 7,
        "passages": 41590915,
        "over_stride": 0,
        "empty": 0,
        "complete": True,
        "chunker": "v1:1/1",
        "model": "bge-m3",
        "dimensions": 1024,
        "text_form": "indexed_text/title+heading+text/v1",
        "applied_snapshot": "20261001",
    }
    return json.dumps({**state, **changes})


def _write_semantic_state(tmp_path: Path, content: str) -> None:
    """Leave `content` where the embedding run leaves its state for collection `_SEMANTIC`."""
    path = tmp_path / "data" / "state" / "index_semantic_20261001.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_health_semantic_reports_the_collection_the_alias_points_at(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    _write_semantic_state(tmp_path, _embed_state())
    report = _semantic_health(tmp_path, frozen_clock)
    assert report["semantic"] == {
        "collection": _SEMANTIC,
        "status": "yellow",
        "optimizer_status": "ok",
        "points": 41590915,
        "indexed_vectors": 25855559,
        "segments": 85,
        "complete": True,
        # The real client here has no semantic alias of its own (the stub answers the lookup),
        # so the holder has found no index to pair with.
        "searchable_reason": (
            "there is no semantic index yet (the alias wikipedia_semantic does not exist; "
            "`wiki-mirror embed-now` builds it)"
        ),
    }


def test_health_semantic_grey_collection_is_reported_and_leaves_the_status_alone(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, real, config = served_over(tmp_path, _articles(), clock=frozen_clock.monotonic)
    stub = _SemanticQdrant(real, collection="collection_semantic_grey.json")
    health = ServiceHealth(
        config,
        served,
        stub,  # type: ignore[arg-type]
        state_reader=FakeStateStore(FakeState(phase="serving")).read,
        clock=_now,
    )
    report = health()
    served.close()
    assert report["semantic"]["status"] == "grey"
    assert (report["status"], report["reasons"]) == ("ok", [])


def test_health_semantic_optimizer_error_is_reported_with_its_text(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, real, config = served_over(tmp_path, _articles(), clock=frozen_clock.monotonic)
    stub = _SemanticQdrant(real)
    stub.optimizer_status = {"error": "Service internal error: no space left"}
    health = ServiceHealth(
        config,
        served,
        stub,  # type: ignore[arg-type]
        state_reader=FakeStateStore(FakeState(phase="serving")).read,
        clock=_now,
    )
    report = health()
    served.close()
    assert report["semantic"]["optimizer_status"] == "error: Service internal error: no space left"


def test_health_without_a_semantic_alias_reports_semantic_null(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    report = _health(tmp_path, frozen_clock, FakeState(phase="serving"))
    assert report["semantic"] is None


def test_health_semantic_lookup_failing_reports_null_logs_and_still_answers(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    served, real, config = served_over(tmp_path, _articles(), clock=frozen_clock.monotonic)
    stub = _SemanticQdrant(real)
    health = ServiceHealth(
        config,
        served,
        stub,  # type: ignore[arg-type]
        state_reader=FakeStateStore(FakeState(phase="serving")).read,
        clock=_now,
    )
    stub.error = UnexpectedResponse(500, "boom", b"", None)  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger="wiki_mirror.api"):
        report = health()
    served.close()
    assert report["semantic"] is None
    assert report["status"] == "ok"
    assert "semantic collection could not be asked" in caplog.text


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (_embed_state(complete=True), True),
        (_embed_state(complete=False), False),
        (_embed_state(born="20250101"), False),
        (json.dumps({"complete": True}), False),
        (json.dumps([True]), False),
        ("{not json", False),
        (None, False),
    ],
)
def test_health_semantic_complete_follows_the_embedding_state_file(
    tmp_path: Path, frozen_clock: FrozenClock, content: str | None, expected: bool
) -> None:
    if content is not None:
        _write_semantic_state(tmp_path, content)
    report = _semantic_health(tmp_path, frozen_clock)
    assert report["semantic"]["complete"] is expected


# -- the search modes on health ----------------------------------------------------------------


def _health_body(app: SemanticApp) -> dict[str, Any]:
    """`GET /v1/wiki/health` of an app, as JSON."""
    body = app.client.get("/v1/wiki/health", headers=_AUTH).json()
    assert isinstance(body, dict)
    return body


def test_health_search_modes_without_a_semantic_index_is_lexical_only(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    report = _health(tmp_path, frozen_clock, FakeState(phase="serving"))
    assert report["search_modes"] == ["lexical"]


def test_health_search_modes_with_nothing_served_is_empty(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    report = _health(tmp_path, frozen_clock, FakeState(phase="idle"), indexed=False)
    assert report["search_modes"] == []


def test_health_search_modes_with_a_paired_semantic_index_lists_all_three(
    semantic_app: SemanticApp,
) -> None:
    report = _health_body(semantic_app)
    assert report["search_modes"] == ["lexical", "semantic", "hybrid"]
    assert report["semantic"]["searchable_reason"] is None
    assert report["status"] == "ok"


def test_health_semantic_index_not_paired_is_not_degraded_and_says_why(
    semantic_app: SemanticApp,
) -> None:
    path = semantic_app.config.data_dir / "state" / "index_semantic_20261001.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**state, "complete": False}), encoding="utf-8")
    report = _health_body(semantic_app)
    assert report["search_modes"] == ["lexical"]
    assert "still being built" in report["semantic"]["searchable_reason"]
    assert (report["status"], report["reasons"]) == ("ok", [])


def test_health_after_the_monthly_build_says_the_semantic_index_is_of_the_earlier_snapshot(
    semantic_app: SemanticApp,
) -> None:
    build_snapshot(
        semantic_app.config, semantic_app.qdrant, _articles("november"), make_snapshot("2026-11-01")
    )
    report = _health_body(semantic_app)
    assert report["search_modes"] == ["lexical"]
    assert "of snapshot 20261001" in report["semantic"]["searchable_reason"]


def test_health_never_calls_the_embedding_server(semantic_app: SemanticApp) -> None:
    _health_body(semantic_app)
    _health_body(semantic_app)
    assert semantic_app.server.requests == []


# -- the maintenance record on health ----------------------------------------------------------

_MAINTENANCE_FIXTURE = _FIXTURES / "maintenance" / "maintenance_after_crash.json"

#: The instant the fixture's record was written.
_RAN_AT = datetime(2026, 10, 23, 12, 30, 4, tzinfo=UTC)


def _stamp(moment: datetime) -> str:
    """A time as the maintenance record writes it."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_maintenance(tmp_path: Path, *, ran_at: datetime, **unit: object) -> dict[str, Any]:
    """Write the fixture record with a new `ran_at` and Qdrant-unit fields; return what it holds.

    The crash is moved three days before the run by default, so a test is about one cause unless
    it moves it itself.
    """
    record = json.loads(_MAINTENANCE_FIXTURE.read_text(encoding="utf-8"))
    record["ran_at"] = _stamp(ran_at)
    record["qdrant"]["last_crash_at"] = _stamp(ran_at - timedelta(days=3))
    record["qdrant"].update(unit)
    path = tmp_path / "data" / "state" / "maintenance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return record  # type: ignore[no-any-return]


def _maintenance_health(tmp_path: Path, frozen_clock: FrozenClock, now: datetime) -> dict[str, Any]:
    """Health with the clock at `now`; the lexical snapshot (2026-10-01) is fresh then."""
    health, served = _stale_health(tmp_path, frozen_clock, _Clock(now))
    try:
        return health()
    finally:
        served.close()


def test_health_maintenance_is_the_record_the_job_left(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    written = _write_maintenance(tmp_path, ran_at=_RAN_AT)
    report = _maintenance_health(tmp_path, frozen_clock, _RAN_AT + timedelta(minutes=5))
    assert report["maintenance"] == written
    assert (report["status"], report["reasons"]) == ("ok", [])


def test_health_without_a_maintenance_record_reports_null_and_is_not_degraded(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    report = _maintenance_health(tmp_path, frozen_clock, _RAN_AT)
    assert (report["maintenance"], report["status"], report["reasons"]) == (None, "ok", [])


def test_health_with_a_garbage_maintenance_record_reports_null_and_still_answers(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    path = tmp_path / "data" / "state" / "maintenance.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    report = _maintenance_health(tmp_path, frozen_clock, _RAN_AT)
    assert (report["maintenance"], report["status"]) == (None, "ok")


def test_health_maintenance_record_older_than_the_limit_is_degraded_with_its_reason(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    _write_maintenance(tmp_path, ran_at=_RAN_AT)
    report = _maintenance_health(
        tmp_path, frozen_clock, _RAN_AT + timedelta(minutes=MAINTENANCE_STALE_MINUTES, seconds=1)
    )
    assert report["status"] == "degraded"
    assert len(report["reasons"]) == 1
    assert "maintenance job last ran at 2026-10-23T12:30:04Z" in report["reasons"][0]
    assert "stopped" in report["reasons"][0]


def test_health_maintenance_record_exactly_at_the_limit_is_not_degraded(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    _write_maintenance(tmp_path, ran_at=_RAN_AT)
    report = _maintenance_health(
        tmp_path, frozen_clock, _RAN_AT + timedelta(minutes=MAINTENANCE_STALE_MINUTES)
    )
    assert report["status"] == "ok"


def test_health_maintenance_record_with_an_unreadable_time_is_degraded(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    record = _write_maintenance(tmp_path, ran_at=_RAN_AT)
    record["ran_at"] = "yesterday"
    (tmp_path / "data" / "state" / "maintenance.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    report = _maintenance_health(tmp_path, frozen_clock, _RAN_AT)
    assert report["reasons"] == ["the maintenance job's last run time is unreadable"]


def test_health_qdrant_crash_within_a_day_is_degraded_with_its_time(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    crash = _RAN_AT - timedelta(hours=QDRANT_CRASH_REPORT_HOURS - 1)
    _write_maintenance(tmp_path, ran_at=_RAN_AT, last_crash_at=_stamp(crash))
    report = _maintenance_health(tmp_path, frozen_clock, _RAN_AT)
    assert report["status"] == "degraded"
    assert report["reasons"] == [f"Qdrant was restarted after a failure at {_stamp(crash)}"]


def test_health_qdrant_crash_older_than_a_day_is_not_degraded(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    crash = _RAN_AT - timedelta(hours=QDRANT_CRASH_REPORT_HOURS, seconds=1)
    _write_maintenance(tmp_path, ran_at=_RAN_AT, last_crash_at=_stamp(crash))
    report = _maintenance_health(tmp_path, frozen_clock, _RAN_AT)
    assert report["status"] == "ok"


def test_health_qdrant_that_never_crashed_is_not_degraded(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    _write_maintenance(tmp_path, ran_at=_RAN_AT, last_crash_at=None, restarts=0)
    assert _maintenance_health(tmp_path, frozen_clock, _RAN_AT)["status"] == "ok"


def test_health_qdrant_memory_over_ninety_percent_of_its_cap_is_degraded(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    _write_maintenance(tmp_path, ran_at=_RAN_AT, anon_bytes=91, memory_max_bytes=100)
    report = _maintenance_health(tmp_path, frozen_clock, _RAN_AT)
    assert report["status"] == "degraded"
    assert report["reasons"] == ["Qdrant holds 91% of its memory cap and is close to being killed"]


def test_health_qdrant_memory_at_ninety_percent_of_its_cap_is_not_degraded(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    _write_maintenance(tmp_path, ran_at=_RAN_AT, anon_bytes=90, memory_max_bytes=100)
    assert _maintenance_health(tmp_path, frozen_clock, _RAN_AT)["status"] == "ok"


@pytest.mark.parametrize(
    "unit",
    [
        {"anon_bytes": 10**12, "memory_max_bytes": None},
        {"anon_bytes": None, "memory_max_bytes": 100},
    ],
)
def test_health_qdrant_memory_with_an_unknown_figure_is_not_degraded(
    tmp_path: Path, frozen_clock: FrozenClock, unit: dict[str, int | None]
) -> None:
    _write_maintenance(tmp_path, ran_at=_RAN_AT, **unit)
    assert _maintenance_health(tmp_path, frozen_clock, _RAN_AT)["status"] == "ok"


def test_health_maintenance_causes_add_to_the_existing_reasons(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    _write_maintenance(tmp_path, ran_at=_RAN_AT, anon_bytes=99, memory_max_bytes=100)
    served, client, config = served_over(tmp_path, _articles(), clock=frozen_clock.monotonic)
    health = ServiceHealth(
        config,
        served,
        client,
        state_reader=FakeStateStore(FakeState(phase="failed", error="x")).read,
        clock=_Clock(_RAN_AT),
    )
    report = health()
    served.close()
    assert report["reasons"][0] == "the last build failed"
    assert len(report["reasons"]) == 2


def _hold_semantic(tmp_path: Path, held: str | None) -> None:
    """Set (or clear) `held` on the semantic collection of the record already written."""
    path = tmp_path / "data" / "state" / "maintenance.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    for collection in record["collections"]:
        if collection["alias"] == "wikipedia_semantic":
            collection["held"] = held
    path.write_text(json.dumps(record), encoding="utf-8")


def test_health_a_held_collection_is_degraded_with_its_name_and_reason(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    _write_maintenance(tmp_path, ran_at=_RAN_AT)
    _hold_semantic(tmp_path, "Qdrant holds 95% of its memory cap")
    report = _maintenance_health(tmp_path, frozen_clock, _RAN_AT)
    assert report["status"] == "degraded"
    assert report["reasons"] == [
        "wikipedia_semantic_20261001 is held by the maintenance job: "
        "Qdrant holds 95% of its memory cap"
    ]


def test_health_a_hold_is_logged_once_and_again_when_it_lifts(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    _write_maintenance(tmp_path, ran_at=_RAN_AT)
    _hold_semantic(tmp_path, "waiting")
    health, served = _stale_health(tmp_path, frozen_clock, _Clock(_RAN_AT))
    with caplog.at_level(logging.INFO, logger="wiki_mirror.api"):
        statuses = [health()["status"] for _ in range(2)]
        _hold_semantic(tmp_path, None)
        statuses.append(health()["status"])
    served.close()
    assert statuses == ["degraded", "degraded", "ok"]
    assert caplog.text.count("is held by the maintenance job") == 1
    assert caplog.text.count("cause of degraded has cleared") == 1


def test_health_an_unreadable_qdrant_unit_is_degraded_with_its_reason(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    _write_maintenance(
        tmp_path,
        ran_at=_RAN_AT,
        found=None,
        active=False,
        restarts=None,
        started_at=None,
        last_crash_at=None,
        anon_bytes=None,
        memory_max_bytes=None,
    )
    report = _maintenance_health(tmp_path, frozen_clock, _RAN_AT)
    assert report["status"] == "degraded"
    assert report["reasons"] == [
        "the maintenance job could not read Qdrant's unit, so crashes and memory are not watched"
    ]


def test_health_a_unit_systemd_says_is_not_installed_is_degraded_with_its_own_reason(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    _write_maintenance(tmp_path, ran_at=_RAN_AT, found=False, last_crash_at=None)
    report = _maintenance_health(tmp_path, frozen_clock, _RAN_AT)
    assert report["status"] == "degraded"
    assert report["reasons"] == [
        "Qdrant's unit is not installed, so the maintenance job does not watch crashes and memory"
    ]


def test_health_failed_passage_count_logs_the_type_once_and_the_recovery_once(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    served, real, config = served_over(tmp_path, _articles(), clock=frozen_clock.monotonic)
    failing = [True]

    class Flaky:
        def count(self, **kwargs: Any) -> Any:
            if failing[0]:
                raise ConnectionError("SECRET-HOST refused")
            return real.count(**kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

    health = ServiceHealth(
        config,
        served,
        Flaky(),  # type: ignore[arg-type]
        state_reader=FakeStateStore(FakeState(phase="serving")).read,
        clock=_now,
    )
    with caplog.at_level(logging.INFO, logger="wiki_mirror.api"):
        down = [health() for _ in range(3)]
        failing[0] = False
        up = health()
    served.close()
    assert all(report["reasons"] == ["Qdrant could not be asked"] for report in down)
    assert up["status"] == "ok"
    assert caplog.text.count("could not be asked (ConnectionError)") == 1
    assert caplog.text.count("Qdrant can be asked again") == 1
    assert "Traceback" not in caplog.text
    assert "SECRET-HOST" not in caplog.text


def test_health_a_second_poll_during_the_first_ones_bookkeeping_waits_and_logs_nothing_twice(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    # Deterministic: events hold the first poll inside its "cause raised" log line, with the
    # health lock taken, while the second poll starts. The second must wait for the lock and then
    # find the cause already raised, so the log line appears once and neither poll fails.
    _write_maintenance(tmp_path, ran_at=_RAN_AT)
    _hold_semantic(tmp_path, "waiting")
    health, served = _stale_health(tmp_path, frozen_clock, _Clock(_RAN_AT))
    entered = threading.Event()
    release = threading.Event()
    api_logger = logging.getLogger("wiki_mirror.api")
    real_warning = api_logger.warning

    def slow_warning(*args: Any, **kwargs: Any) -> None:
        entered.set()
        release.wait(timeout=10)
        real_warning(*args, **kwargs)

    results: list[str] = []
    with caplog.at_level(logging.INFO, logger="wiki_mirror.api"):
        api_logger.warning = slow_warning  # type: ignore[method-assign]
        try:
            first = threading.Thread(target=lambda: results.append(health()["status"]))
            first.start()
            assert entered.wait(timeout=10)
            second = threading.Thread(target=lambda: results.append(health()["status"]))
            second.start()
            release.set()
            first.join(timeout=10)
            second.join(timeout=10)
        finally:
            del api_logger.warning
    served.close()
    assert results == ["degraded", "degraded"]
    assert caplog.text.count("is held by the maintenance job") == 1


def test_health_semantic_lookup_is_skipped_when_qdrant_already_failed_the_count(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served, real, config = served_over(tmp_path, _articles(), clock=frozen_clock.monotonic)
    asked: list[str] = []

    class CannotCount:
        def count(self, **_: Any) -> Any:
            raise ConnectionError

        def get_aliases(self) -> Any:
            asked.append("get_aliases")
            return real.get_aliases()

        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

    health = ServiceHealth(
        config,
        served,
        CannotCount(),  # type: ignore[arg-type]
        state_reader=FakeStateStore(FakeState(phase="serving")).read,
        clock=_now,
    )
    report = health()
    served.close()
    assert report["semantic"] is None
    assert asked == []


def test_health_semantic_failure_log_names_only_the_exception_type_and_is_paired(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    served, real, config = served_over(tmp_path, _articles(), clock=frozen_clock.monotonic)
    stub = _SemanticQdrant(real)
    health = ServiceHealth(
        config,
        served,
        stub,  # type: ignore[arg-type]
        state_reader=FakeStateStore(FakeState(phase="serving")).read,
        clock=_now,
    )
    with caplog.at_level(logging.INFO, logger="wiki_mirror.api"):
        stub.error = UnexpectedResponse(500, "SECRET-BODY", b"SECRET-BODY", None)  # type: ignore[arg-type]
        for _ in range(3):
            health()
        stub.error = None
        health()
    served.close()
    assert caplog.text.count("could not be asked (UnexpectedResponse)") == 1
    assert caplog.text.count("can be asked again") == 1
    assert "SECRET-BODY" not in caplog.text
    assert "Traceback" not in caplog.text


def test_health_a_maintenance_cause_is_logged_once_and_again_when_it_clears(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    _write_maintenance(tmp_path, ran_at=_RAN_AT)
    clock = _Clock(_RAN_AT + timedelta(minutes=MAINTENANCE_STALE_MINUTES + 5))
    health, served = _stale_health(tmp_path, frozen_clock, clock)
    with caplog.at_level(logging.INFO, logger="wiki_mirror.api"):
        statuses = [health()["status"] for _ in range(3)]
        _write_maintenance(tmp_path, ran_at=clock.now)  # the job ran again
        statuses.append(health()["status"])
    served.close()
    assert statuses == ["degraded", "degraded", "degraded", "ok"]
    assert caplog.text.count("maintenance job last ran at") == 1
    assert caplog.text.count("maintenance cause of degraded has cleared") == 1


def test_health_a_stale_maintenance_record_does_not_hide_a_build_in_progress(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    _write_maintenance(tmp_path, ran_at=_RAN_AT)
    config = make_config(tmp_path)
    qdrant = memory_client()
    served = ServedSnapshot(config, qdrant, clock=frozen_clock.monotonic)
    health = ServiceHealth(
        config,
        served,
        qdrant,
        state_reader=FakeStateStore(FakeState(phase="downloading")).read,
        clock=_Clock(_RAN_AT + timedelta(days=2)),
    )
    report = health()
    served.close()
    assert (report["status"], report["reasons"]) == ("building", [])


# -- only HTTP is served ---------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/mcp", "/v1/wiki/health", "/anything"])
def test_api_websocket_is_closed_with_1008_even_with_a_valid_key(
    app_factory: AppFactory, path: str
) -> None:
    client, _ = app_factory()
    with (
        pytest.raises(WebSocketDisconnect) as raised,
        client.websocket_connect(path, headers=_AUTH),
    ):
        pytest.fail("a websocket must not be accepted")
    assert raised.value.code == 1008


def test_api_websocket_refusal_is_logged_with_its_scope_type(
    app_factory: AppFactory, caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = app_factory()
    with (
        caplog.at_level(logging.WARNING, logger="wiki_mirror.api"),
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/mcp"),
    ):
        pytest.fail("a websocket must not be accepted")
    assert "refused a websocket connection" in caplog.text


@pytest.mark.asyncio
async def test_api_scope_type_it_does_not_know_is_dropped_without_reaching_a_route(
    app_factory: AppFactory, caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = app_factory()
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        raise AssertionError("an unknown scope has nothing to receive")

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    with caplog.at_level(logging.WARNING, logger="wiki_mirror.api"):
        await client.app({"type": "future-protocol", "headers": []}, receive, send)  # type: ignore[arg-type]
    assert sent == []
    assert "refused a future-protocol connection" in caplog.text


def test_api_lifespan_still_runs_through_the_guard(app_factory: AppFactory) -> None:
    client, _ = app_factory()
    with client as started:
        assert started.get("/v1/wiki/health", headers=_AUTH).status_code == 200


# -- page ids are bounded to what SQLite and Qdrant can hold ---------------------------------


def test_api_article_page_id_beyond_64_bits_is_422_not_a_500(app_factory: AppFactory) -> None:
    client, _ = app_factory()
    response = client.get(f"/v1/wiki/article?page_id={MAX_PAGE_ID + 1}", headers=_AUTH)
    assert (response.status_code, response.json()["error"]) == (422, "invalid_request")


def test_api_article_largest_page_id_is_a_plain_404(app_factory: AppFactory) -> None:
    client, _ = app_factory()
    response = client.get(f"/v1/wiki/article?page_id={MAX_PAGE_ID}", headers=_AUTH)
    assert (response.status_code, response.json()["error"]) == (404, "not_found")
