"""Qualifies the MCP front door with the SDK's own client, in process, against the real app.

The app (`api.create_app`, so the real authentication in front) runs with its lifespan, which
starts the MCP session manager, and the v2 `mcp` client talks to it through an ASGI transport,
in both protocol modes it can use (`legacy`: the initialize handshake; `auto`: per-request
negotiation). The index is Qdrant's in-memory mode over a real finalized article store. Failure
paths named here: no key, a wrong key and a revoked key (401 at HTTP level), both or neither of
title and page id, an unknown article, arguments out of range, no snapshot yet, the index
refusing, an unexpected crash (its text must not reach the client), a GET (405, never a standing
stream), and a snapshot swap seen through MCP.

Each test opens the app itself (`async with _door(...)`) instead of through a fixture: the
session manager lives in an anyio task group, which must be entered and left by the same task,
and pytest-asyncio runs a fixture's setup and teardown in different ones.

See docs/DESIGN.md section 5 (docs/DECISIONS.md D2).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from qdrant_client.http.exceptions import UnexpectedResponse

from tests.fixtures.clock import FrozenClock
from tests.fixtures.wiki_mirror_fakes import (
    API_KEY,
    FakeStateStore,
    StubServed,
    build_snapshot,
    make_article,
    make_config,
    make_snapshot,
    memory_client,
    served_over,
)
from wiki_mirror.api import ServiceHealth, create_app
from wiki_mirror.config import MirrorConfig
from wiki_mirror.keys import RECHECK_SECONDS, ClientKeys, add_client, revoke_client
from wiki_mirror.records import MAX_PAGE_ID, Article
from wiki_mirror.search import SearchReply
from wiki_mirror.serving import ALIAS_RECHECK_SECONDS, ServedSnapshot

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

_FILLER = "The committee reviewed the proposal and recorded its findings in the minutes. "
_MODES = ["legacy", "auto"]
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
_JSON_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _articles(label: str = "") -> list[Article]:
    """Two articles: a long one (for paged reads) and a short one."""
    long_body = "\n\n".join(f"Paragraph {n} about zeppelins. {_FILLER * 4}" for n in range(30))
    return [
        make_article(3, "Zeppelin", {"": long_body}),
        make_article(5, "Mercury", {"": f"Mercury is a planet. {label} {_FILLER}"}),
    ]


@dataclass
class Door:
    """A running app, its snapshot holder and the clock the holder and the keys share."""

    app: FastAPI
    config: MirrorConfig
    clock: FrozenClock

    def http(self, key: str | None = API_KEY) -> httpx2.AsyncClient:
        """An HTTP client onto the app, sending `key` as the bearer token unless None."""
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app),
            headers=headers,
            base_url="http://testserver",
        )

    def client(
        self, key: str | None = API_KEY, mode: str = "legacy"
    ) -> AbstractAsyncContextManager[Client]:
        """An MCP client connected to the door with `key`, in protocol `mode`."""
        return self._connect(key, mode)

    @asynccontextmanager
    async def _connect(self, key: str | None, mode: str) -> AsyncIterator[Client]:
        """Connect the v2 SDK client to the app's `/mcp` over the ASGI transport."""
        async with self.http(key) as http:
            transport = streamable_http_client("http://testserver/mcp", http_client=http)
            async with Client(transport, mode=mode) as client:
                yield client


@asynccontextmanager
async def _serve(
    served: Any, config: MirrorConfig, clock: FrozenClock, health: Any = dict
) -> AsyncIterator[Door]:
    """Create the app over `served` and keep its lifespan running while the body runs."""
    keys = ClientKeys(config.clients_file, API_KEY, clock=clock.monotonic)
    app = create_app(config, served, health, keys=keys)
    async with app.router.lifespan_context(app):
        yield Door(app, config, clock)


@asynccontextmanager
async def _door(tmp_path: Path, clock: FrozenClock, **settings: object) -> AsyncIterator[Door]:
    """The door over a real index of `_articles`, with a clients file in `tmp_path`.

    `settings` are `MirrorConfig` overrides.
    """
    served, qdrant, config = served_over(
        tmp_path,
        _articles(),
        clock=clock.monotonic,
        clients_file=tmp_path / "clients.json",
        **settings,
    )
    try:
        health = ServiceHealth(
            config,
            served,
            qdrant,
            state_reader=FakeStateStore().read,
            clock=lambda: datetime(2026, 10, 5, tzinfo=UTC),
        )
        async with _serve(served, config, clock, health) as door:
            yield door
    finally:
        served.close()


class _StubSearch:
    """A search that raises what it is given, to drive the tool error paths."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def search(self, query: str, limit: int) -> SearchReply:
        raise self._error


def _text(result: Any) -> str:
    """The text of a tool result's first content block."""
    return str(result.content[0].text)


# -- the tools --------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", _MODES)
async def test_mcp_lists_exactly_the_two_tools(
    tmp_path: Path, frozen_clock: FrozenClock, mode: str
) -> None:
    async with _door(tmp_path, frozen_clock) as door, door.client(mode=mode) as client:
        tools = await client.list_tools()
    assert [tool.name for tool in tools.tools] == ["wiki_search", "wiki_read"]


async def test_mcp_tools_are_marked_read_only(tmp_path: Path, frozen_clock: FrozenClock) -> None:
    async with _door(tmp_path, frozen_clock) as door, door.client() as client:
        tools = (await client.list_tools()).tools
    assert all(tool.annotations is not None and tool.annotations.read_only_hint for tool in tools)


async def test_mcp_search_description_states_the_terms_of_use(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    async with _door(tmp_path, frozen_clock) as door, door.client() as client:
        tool = (await client.list_tools()).tools[0]
    text = tool.description or ""
    for needed in ("English Wikipedia", "snapshot", "untrusted", "CC BY-SA 4.0", "`url`"):
        assert needed in text


async def test_mcp_read_description_explains_windows_and_the_configured_size(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    async with _door(tmp_path, frozen_clock) as door, door.client() as client:
        tool = (await client.list_tools()).tools[1]
    text = tool.description or ""
    assert "next_offset" in text
    assert "12,000" in text
    assert "untrusted" in text


@pytest.mark.parametrize("mode", _MODES)
async def test_mcp_wiki_search_returns_the_same_json_as_the_rest_route(
    tmp_path: Path, frozen_clock: FrozenClock, mode: str
) -> None:
    search = {"query": "mercury planet", "limit": 3}
    async with _door(tmp_path, frozen_clock) as door:
        async with door.client(mode=mode) as client:
            result = await client.call_tool("wiki_search", search)
        async with door.http() as http:
            rest = await http.post("/v1/wiki/search", json=search)
    assert result.is_error is False
    assert _text(result) == rest.text
    assert json.loads(_text(result))["results"][0]["title"] == "Mercury"


async def test_mcp_wiki_search_limit_defaults_to_eight_like_rest(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    async with _door(tmp_path, frozen_clock) as door:
        async with door.client() as client:
            default = await client.call_tool("wiki_search", {"query": "committee proposal"})
        async with door.http() as http:
            rest = await http.post("/v1/wiki/search", json={"query": "committee proposal"})
    assert _text(default) == rest.text


@pytest.mark.parametrize("mode", _MODES)
async def test_mcp_wiki_read_by_title_returns_the_same_json_as_the_rest_route(
    tmp_path: Path, frozen_clock: FrozenClock, mode: str
) -> None:
    async with _door(tmp_path, frozen_clock) as door:
        async with door.client(mode=mode) as client:
            result = await client.call_tool("wiki_read", {"title": "mercury"})
        async with door.http() as http:
            rest = await http.get("/v1/wiki/article", params={"title": "mercury"})
    assert result.is_error is False
    assert _text(result) == rest.text
    assert json.loads(_text(result))["page_id"] == 5


async def test_mcp_wiki_read_by_page_id_windows_with_next_offset(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    async with _door(tmp_path, frozen_clock, max_read_chars=1000) as door, door.client() as client:
        first = json.loads(_text(await client.call_tool("wiki_read", {"page_id": 3})))
        later = {"page_id": 3, "offset": first["next_offset"]}
        second = json.loads(_text(await client.call_tool("wiki_read", later)))
    assert first["title"] == "Zeppelin"
    assert first["next_offset"] is not None
    assert second["offset"] == first["next_offset"]
    assert first["url"].endswith("?oldid=31")


async def test_mcp_wiki_read_page_id_zero_is_a_selector_not_an_absent_one(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    async with _door(tmp_path, frozen_clock) as door, door.client() as client:
        result = await client.call_tool("wiki_read", {"page_id": 0})
    assert result.is_error is True
    assert "no article matches" in _text(result)


@pytest.mark.parametrize(
    "arguments",
    [{}, {"title": "Mercury", "page_id": 5}, {"title": "   "}, {"title": ""}],
    ids=["neither", "both", "blank-title", "empty-title"],
)
async def test_mcp_wiki_read_needs_exactly_one_of_title_and_page_id(
    tmp_path: Path, frozen_clock: FrozenClock, arguments: dict[str, Any]
) -> None:
    async with _door(tmp_path, frozen_clock) as door, door.client() as client:
        result = await client.call_tool("wiki_read", arguments)
    assert result.is_error is True
    assert "exactly one of title or page_id" in _text(result)


async def test_mcp_wiki_read_unknown_article_is_a_tool_error_that_does_not_echo_the_title(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    async with _door(tmp_path, frozen_clock) as door, door.client() as client:
        result = await client.call_tool("wiki_read", {"title": "Nonexistent Zzyzx"})
    assert result.is_error is True
    assert "no article matches" in _text(result)
    assert "Zzyzx" not in _text(result)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("wiki_search", {"query": ""}),
        ("wiki_search", {"query": "x" * 501}),
        ("wiki_search", {"query": "x", "limit": 0}),
        ("wiki_search", {"query": "x", "limit": 21}),
        ("wiki_read", {"page_id": -1}),
        ("wiki_read", {"page_id": MAX_PAGE_ID + 1}),
        ("wiki_read", {"page_id": 3, "offset": -1}),
    ],
)
async def test_mcp_arguments_out_of_range_are_tool_errors(
    tmp_path: Path, frozen_clock: FrozenClock, tool: str, arguments: dict[str, Any]
) -> None:
    async with _door(tmp_path, frozen_clock) as door, door.client() as client:
        result = await client.call_tool(tool, arguments)
    assert result.is_error is True


async def test_mcp_wiki_read_largest_page_id_is_a_plain_no_such_article(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    async with _door(tmp_path, frozen_clock) as door, door.client() as client:
        result = await client.call_tool("wiki_read", {"page_id": MAX_PAGE_ID})
    assert result.is_error is True
    assert "no article matches" in _text(result)


async def test_mcp_without_a_snapshot_both_tools_say_it_is_not_ready(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    config = make_config(tmp_path)
    served = ServedSnapshot(config, memory_client(), clock=frozen_clock.monotonic)
    try:
        async with _serve(served, config, frozen_clock) as door, door.client() as client:
            search = await client.call_tool("wiki_search", {"query": "mercury"})
            read = await client.call_tool("wiki_read", {"page_id": 3})
    finally:
        served.close()
    assert (search.is_error, read.is_error) == (True, True)
    assert "no snapshot to serve yet" in _text(search)
    assert "no snapshot to serve yet" in _text(read)


async def test_mcp_search_when_the_index_refuses_is_a_tool_error(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    error = UnexpectedResponse(404, "Not Found", b"alias missing", {})  # type: ignore[arg-type]
    served = StubServed(_StubSearch(error))
    async with (
        _serve(served, make_config(tmp_path), frozen_clock) as door,
        door.client() as client,
    ):
        result = await client.call_tool("wiki_search", {"query": "mercury"})
    assert result.is_error is True
    assert "passage index could not answer" in _text(result)


async def test_mcp_search_unexpected_crash_never_shows_its_text_to_the_client(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    served = StubServed(_StubSearch(RuntimeError("secret internal detail")))
    async with (
        _serve(served, make_config(tmp_path), frozen_clock) as door,
        door.client() as client,
    ):
        result = await client.call_tool("wiki_search", {"query": "mercury"})
    assert result.is_error is True
    assert "secret internal detail" not in _text(result)


async def test_mcp_search_follows_the_alias_to_a_new_snapshot(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    config = make_config(tmp_path)
    qdrant = memory_client()
    build_snapshot(config, qdrant, _articles("october"), make_snapshot("2026-10-01"))
    served = ServedSnapshot(config, qdrant, clock=frozen_clock.monotonic)
    search = {"query": "mercury"}
    try:
        async with _serve(served, config, frozen_clock) as door, door.client() as client:
            before = json.loads(_text(await client.call_tool("wiki_search", search)))
            build_snapshot(config, qdrant, _articles("november"), make_snapshot("2026-11-01"))
            frozen_clock.advance(ALIAS_RECHECK_SECONDS + 1)
            after = json.loads(_text(await client.call_tool("wiki_search", search)))
    finally:
        served.close()
    assert (before["snapshot"], after["snapshot"]) == ("2026-10-01", "2026-11-01")


# -- authentication and logging ---------------------------------------------------------------


async def test_mcp_request_without_a_key_is_401_at_http_level(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    async with _door(tmp_path, frozen_clock) as door, door.http(key=None) as http:
        response = await http.post("/mcp", json=_INITIALIZE, headers=_JSON_HEADERS)
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"
    assert len(response.headers["X-Request-ID"]) == 32
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_mcp_request_with_a_wrong_key_is_401_and_does_not_echo_it(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    async with _door(tmp_path, frozen_clock) as door, door.http(key="w" * 40) as http:
        response = await http.post("/mcp", json=_INITIALIZE, headers=_JSON_HEADERS)
    assert response.status_code == 401
    assert "w" * 40 not in response.text


async def test_mcp_client_without_a_key_cannot_connect(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    async with _door(tmp_path, frozen_clock) as door:
        with pytest.raises(ExceptionGroup):
            async with door.client(key=None) as client:
                await client.list_tools()


async def test_mcp_authorised_request_echoes_a_valid_request_id(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    headers = {**_JSON_HEADERS, "X-Request-ID": "agent-7:turn.3"}
    async with _door(tmp_path, frozen_clock) as door, door.http() as http:
        response = await http.post("/mcp", json=_INITIALIZE, headers=headers)
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "agent-7:turn.3"


async def test_mcp_agent_key_works_and_its_name_is_in_the_tool_log_without_the_query(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    async with _door(tmp_path, frozen_clock) as door:
        key = add_client(door.config.clients_file, "claude-code")
        frozen_clock.advance(RECHECK_SECONDS + 1)
        with caplog.at_level(logging.INFO, logger="wiki_mirror.mcp"):
            async with door.client(key=key) as client:
                await client.call_tool("wiki_search", {"query": "mercury secretword"})
                await client.call_tool("wiki_read", {"page_id": 5})
    assert "mcp tool=wiki_search client=claude-code" in caplog.text
    assert "query_chars=18 results=" in caplog.text
    assert "mcp tool=wiki_read client=claude-code" in caplog.text
    assert "secretword" not in caplog.text


async def test_mcp_primary_key_is_logged_as_primary(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="wiki_mirror.mcp"):
        async with _door(tmp_path, frozen_clock) as door, door.client() as client:
            await client.call_tool("wiki_read", {"page_id": 5})
    assert "mcp tool=wiki_read client=primary" in caplog.text


async def test_mcp_revoked_agent_key_is_401_without_a_restart(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    async with _door(tmp_path, frozen_clock) as door:
        key = add_client(door.config.clients_file, "worker")
        frozen_clock.advance(RECHECK_SECONDS + 1)
        async with door.http(key=key) as http:
            before = await http.post("/mcp", json=_INITIALIZE, headers=_JSON_HEADERS)
        revoke_client(door.config.clients_file, "worker")
        frozen_clock.advance(RECHECK_SECONDS + 1)
        async with door.http(key=key) as http:
            after = await http.post("/mcp", json=_INITIALIZE, headers=_JSON_HEADERS)
    assert (before.status_code, after.status_code) == (200, 401)


# -- transport behaviour ----------------------------------------------------------------------


async def test_mcp_get_is_refused_so_no_stream_is_held_open(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    async with _door(tmp_path, frozen_clock) as door, door.http() as http:
        response = await http.get("/mcp", headers={"Accept": "text/event-stream"})
    assert response.status_code == 405
    assert response.headers["Allow"] == "POST"


async def test_mcp_post_with_the_wrong_content_type_is_refused(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    headers = {"Content-Type": "text/plain", "Accept": "application/json"}
    async with _door(tmp_path, frozen_clock) as door, door.http() as http:
        response = await http.post("/mcp", content="x", headers=headers)
    assert response.status_code == 400


async def test_mcp_clients_in_both_modes_share_nothing_and_see_the_same_answer(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    async with (
        _door(tmp_path, frozen_clock) as door,
        door.client() as first,
        door.client(mode="auto") as second,
    ):
        a = await first.call_tool("wiki_read", {"page_id": 5})
        b = await second.call_tool("wiki_read", {"page_id": 5})
    assert _text(a) == _text(b)
