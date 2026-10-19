"""Qualifies the mirror's HTTP API: auth, request ids, validation, errors, shapes, health.

Runs the FastAPI app in-process through `TestClient` over a real finalized store and an
in-memory index (search and article routes) or a stub search (error paths), so the contract with
the main client (JSON shapes, status codes, `X-Request-ID`) is checked without a server. Failure
paths
named here: no key, a wrong key and a revoked key (401), a bad request id (replaced), unknown
fields and out-of-range values (422), no snapshot yet (503 not_ready), Qdrant refusing (503
index_unavailable), a crash (JSON 500), a malformed clients file (refused at start, ignored at
runtime), the alias moving or its store being unusable, and every health state.

See docs/DESIGN.md section 6.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from starlette.testclient import WebSocketDisconnect

from tests.fixtures.clock import FrozenClock
from tests.fixtures.wiki_mirror_fakes import (
    API_KEY,
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
from wiki_mirror.api import STALE_AFTER_DAYS, ServiceHealth, build_service, create_app
from wiki_mirror.config import MirrorConfig
from wiki_mirror.keys import (
    RECHECK_SECONDS,
    ClientKeyError,
    ClientKeys,
    add_client,
    revoke_client,
)
from wiki_mirror.records import MAX_PAGE_ID, Article
from wiki_mirror.search import SearchReply
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

    def search(self, query: str, limit: int) -> SearchReply:
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
    health = ServiceHealth(
        settings, served, client, state_reader=FakeStateStore().read, clock=_now
    )
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
    report = _health(
        tmp_path, frozen_clock, FakeState(phase="serving", snapshot_date="2026-10-01")
    )
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
        "snapshot",
        "applied_snapshot",
        "articles",
        "passages",
        "collection",
        "build",
        "update",
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
    health = ServiceHealth(
        config, served, client, state_reader=FakeStateStore().read, clock=_now
    )
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
    report = _health(
        tmp_path, frozen_clock, FakeState(phase="failed", error="parse: OSError")
    )
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
    health = ServiceHealth(
        config, served, client, state_reader=FakeStateStore().read, clock=_now
    )
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
