"""Qualifies the shared helpers of the Wikipedia evaluation tools: mirror reads and the router.

``tools/wiki_common.py`` is what ``wiki_golden`` and ``wiki_passage_eval`` read articles and call
the router with. Tested here: window assembly and its refusal to join windows that do not add up,
the 404-is-None rule, the mirror's bounded retry, and the router client's contract - request
shape and headers, a 503 honouring ``Retry-After`` (capped), a timeout retried, attempts
exhausted raising, a short embed reply refused, model output that is not JSON told apart from a
router fault, and the key never appearing in an error.

The mirror is ``tests/fixtures/wiki_eval_fakes.FakeMirror`` (the real reply shapes) and the router
is an ``httpx.MockTransport`` answering in Ollama's shapes with the router's error envelope. A
recording sleep stands in for the clock, so nothing waits.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable

import httpx
import pytest

from tests.fixtures.wiki_eval_fakes import MIRROR_KEY, FakeMirror
from tools.wiki_common import (
    ArticleReadError,
    MirrorClient,
    MirrorRequestError,
    MirrorUnavailableError,
    ReadGuard,
    ReadResult,
    RouterClient,
    RouterContentError,
    RouterError,
    describe_failure,
    fold,
    new_request_id,
    page_id_stream,
    read_article,
    search_ids,
)

ROUTER_KEY = "test-router-key"


class Sleeps:
    """A sleep that records the waits it was asked for and returns at once."""

    def __init__(self) -> None:
        """Start with no waits."""
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        """Record the wait."""
        self.waits.append(seconds)


@pytest.fixture(autouse=True)
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Export both keys the way a client's env file does."""
    monkeypatch.setenv("WIKI_MIRROR_API_KEY", MIRROR_KEY)
    monkeypatch.setenv("EMBEDDING_API_KEY", ROUTER_KEY)


def router_over(
    handler: Callable[[httpx.Request], httpx.Response], sleeps: Sleeps, attempts: int = 3
) -> RouterClient:
    """A router client whose transport is ``handler`` and whose sleep is recorded."""
    return RouterClient(
        "http://router.test:8080",
        attempts=attempts,
        transport=httpx.MockTransport(handler),
        sleep=sleeps,
    )


def error_body(code: str, retry_after: float | None = None) -> dict[str, object]:
    """The router's flat error envelope (see tests/fixtures/mock_sfn_router.py)."""
    body: dict[str, object] = {"error": "injected", "code": code, "retryable": True}
    if retry_after is not None:
        body["retry_after"] = retry_after
    return body


# -- text and ids ------------------------------------------------------------------------------


@pytest.mark.unit
def test_fold_mixed_case_and_whitespace_returns_single_spaced_lowercase() -> None:
    assert fold("  The  Quick\n\tBROWN fox ") == "the quick brown fox"


@pytest.mark.unit
def test_new_request_id_long_unsafe_prefix_stays_within_64_safe_chars() -> None:
    request_id = new_request_id("a prefix/with unsafe*chars" + "x" * 100)

    assert len(request_id) <= 64
    assert re.fullmatch(r"[A-Za-z0-9_.:-]+", request_id)


@pytest.mark.unit
def test_new_request_id_called_twice_returns_distinct_ids() -> None:
    assert new_request_id("wiki") != new_request_id("wiki")


@pytest.mark.unit
def test_page_id_stream_same_seed_repeats_and_stays_in_range() -> None:
    one = page_id_stream("seed", 1000)
    two = page_id_stream("seed", 1000)
    first = [next(one) for _ in range(50)]
    second = [next(two) for _ in range(50)]

    assert first == second
    assert all(1 <= pid <= 1000 for pid in second)


@pytest.mark.unit
def test_page_id_stream_different_seeds_diverge() -> None:
    a = page_id_stream("a", 10**6)
    b = page_id_stream("b", 10**6)

    assert [next(a) for _ in range(5)] != [next(b) for _ in range(5)]


# -- mirror ------------------------------------------------------------------------------------


@pytest.mark.contract
def test_read_article_several_windows_joins_text_exactly() -> None:
    text = "".join(f"Sentence number {i}. " for i in range(400))
    mirror = FakeMirror({7: ("Seven", 70, text)}, window=1000)

    article = asyncio.run(read_article(mirror.client(), 7, request_id="r1"))

    assert article is not None
    assert article.text == text
    assert (article.page_id, article.revision_id, article.title) == (7, 70, "Seven")
    assert len(mirror.reads) > 3


@pytest.mark.contract
def test_read_article_unknown_page_returns_none() -> None:
    mirror = FakeMirror({})

    assert asyncio.run(read_article(mirror.client(), 99, request_id="r1")) is None


@pytest.mark.contract
def test_read_article_transient_failure_retries_with_backoff_then_succeeds() -> None:
    mirror = FakeMirror({1: ("One", 10, "abc")}, fail_first=2)
    sleeps = Sleeps()

    article = asyncio.run(read_article(mirror.client(), 1, request_id="r1", sleep=sleeps))

    assert article is not None
    assert sleeps.waits == [1.0, 2.0]


@pytest.mark.contract
def test_read_article_mirror_stays_down_raises_after_bounded_attempts() -> None:
    mirror = FakeMirror({1: ("One", 10, "abc")}, fail_first=10)
    sleeps = Sleeps()

    with pytest.raises(MirrorUnavailableError):
        asyncio.run(read_article(mirror.client(), 1, request_id="r1", sleep=sleeps))

    assert len(mirror.requests) == 3


@pytest.mark.contract
def test_read_article_windows_not_adding_up_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        """A window claiming more text than the windows hold."""
        return httpx.Response(
            200,
            json={
                "page_id": 1, "revision_id": 1, "title": "T", "offset": 0,
                "next_offset": None, "total_chars": 99, "text": "short",
            },
        )

    client = MirrorClient(base_url="http://m.test", transport=httpx.MockTransport(handler))

    with pytest.raises(ArticleReadError):
        asyncio.run(read_article(client, 1, request_id="r1"))


@pytest.mark.contract
def test_read_article_offset_not_advancing_raises_instead_of_looping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        """A window whose next offset is where it started."""
        return httpx.Response(
            200,
            json={
                "page_id": 1, "revision_id": 1, "title": "T", "offset": 0,
                "next_offset": 0, "total_chars": 5, "text": "short",
            },
        )

    client = MirrorClient(base_url="http://m.test", transport=httpx.MockTransport(handler))

    with pytest.raises(ArticleReadError):
        asyncio.run(read_article(client, 1, request_id="r1"))


@pytest.mark.contract
def test_read_article_429_is_retried_with_backoff_then_succeeds() -> None:
    replies = [
        httpx.Response(429, json={"error": "rate_limited", "detail": "slow down"}),
        httpx.Response(
            200,
            json={
                "page_id": 1, "revision_id": 2, "title": "T", "offset": 0,
                "next_offset": None, "total_chars": 3, "text": "abc",
            },
        ),
    ]
    client = MirrorClient(
        base_url="http://m.test", transport=httpx.MockTransport(lambda r: replies.pop(0))
    )
    sleeps = Sleeps()

    article = asyncio.run(read_article(client, 1, request_id="r1", sleep=sleeps))

    assert article is not None
    assert sleeps.waits == [1.0]


@pytest.mark.contract
def test_read_article_429_on_every_attempt_raises_the_request_error() -> None:
    client = MirrorClient(
        base_url="http://m.test",
        transport=httpx.MockTransport(lambda r: httpx.Response(429, json={"error": "x"})),
    )

    with pytest.raises(MirrorRequestError) as caught:
        asyncio.run(read_article(client, 1, request_id="r1", sleep=Sleeps()))

    assert caught.value.status == 429


@pytest.mark.contract
@pytest.mark.parametrize("status", [502, 504])
def test_read_article_gateway_errors_are_retried(status: int) -> None:
    replies = [
        httpx.Response(status, json={"error": "gateway"}),
        httpx.Response(
            200,
            json={
                "page_id": 1, "revision_id": 2, "title": "T", "offset": 0,
                "next_offset": None, "total_chars": 1, "text": "a",
            },
        ),
    ]
    client = MirrorClient(
        base_url="http://m.test", transport=httpx.MockTransport(lambda r: replies.pop(0))
    )

    article = asyncio.run(read_article(client, 1, request_id="r1", sleep=Sleeps()))

    assert article is not None


@pytest.mark.contract
def test_read_guard_skips_a_failing_page_counts_it_and_a_success_resets_the_streak() -> None:
    mirror = FakeMirror({1: ("One", 10, "abc")}, fail_first=3)
    guard = ReadGuard(sleep=Sleeps())

    first = asyncio.run(guard.read(mirror.client(), 1, request_id="r1"))
    second = asyncio.run(guard.read(mirror.client(), 1, request_id="r2"))

    assert first.failed
    assert first.article is None
    assert second.article is not None
    assert (guard.failed, guard.consecutive) == (1, 0)


@pytest.mark.contract
def test_read_guard_a_404_is_not_a_failure() -> None:
    guard = ReadGuard(sleep=Sleeps())

    result = asyncio.run(guard.read(FakeMirror({}).client(), 5, request_id="r1"))

    assert result == ReadResult(None, failed=False)
    assert guard.failed == 0


@pytest.mark.contract
def test_read_guard_unassemblable_article_is_skipped_too() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        """Windows that do not add up."""
        return httpx.Response(
            200,
            json={
                "page_id": 1, "revision_id": 1, "title": "T", "offset": 0,
                "next_offset": None, "total_chars": 99, "text": "short",
            },
        )

    client = MirrorClient(base_url="http://m.test", transport=httpx.MockTransport(handler))
    guard = ReadGuard(sleep=Sleeps())

    result = asyncio.run(guard.read(client, 1, request_id="r1"))

    assert result.failed
    assert guard.failed == 1


@pytest.mark.contract
def test_read_guard_failures_in_a_row_stop_the_run_with_the_original_error() -> None:
    mirror = FakeMirror({1: ("One", 10, "abc")}, fail_first=1000)
    guard = ReadGuard(max_consecutive=3, sleep=Sleeps())

    async def go() -> None:
        for _ in range(5):
            await guard.read(mirror.client(), 1, request_id="r1")

    with pytest.raises(MirrorUnavailableError):
        asyncio.run(go())

    assert guard.failed == 3


@pytest.mark.unit
def test_describe_failure_mirror_request_error_gives_status_never_the_detail() -> None:
    error = MirrorRequestError("the mirror refused: 422 query echoed here", status=422)

    text = describe_failure(error)

    assert text == "MirrorRequestError: status 422"
    assert "echoed" not in text


@pytest.mark.unit
def test_describe_failure_mirror_unavailable_error_gives_the_class_only() -> None:
    error = MirrorUnavailableError("the mirror failed: 500 secret detail")

    assert describe_failure(error) == "MirrorUnavailableError"


@pytest.mark.unit
def test_describe_failure_router_error_keeps_its_status_and_code_message() -> None:
    assert describe_failure(RouterError("router refused /api/embed: 404 unknown_model")) == (
        "RouterError: router refused /api/embed: 404 unknown_model"
    )


@pytest.mark.contract
def test_search_ids_returns_page_ids_in_the_mirrors_order_within_the_limit() -> None:
    articles = {pid: (f"T{pid}", pid, "x") for pid in (5, 3, 9, 4)}
    mirror = FakeMirror(articles, search_results={"q": [5, 3, 9, 4]})

    ids = asyncio.run(search_ids(mirror.client(), "q", 3, request_id="r1"))

    assert ids == [5, 3, 9]


# -- router ------------------------------------------------------------------------------------


@pytest.mark.contract
def test_embed_sends_model_inputs_key_and_request_id_and_returns_vectors() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Two inputs in, two vectors out."""
        seen.append(request)
        return httpx.Response(200, json={"embeddings": [[1, 0], [0, 1]]})

    async def go() -> list[list[float]]:
        async with router_over(handler, Sleeps()) as router:
            return await router.embed(["a", "b"])

    vectors = asyncio.run(go())

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    request = seen[0]
    assert request.url.path == "/api/embed"
    assert json.loads(request.content) == {"model": "bge-m3:latest", "input": ["a", "b"]}
    assert request.headers["authorization"] == f"Bearer {ROUTER_KEY}"
    assert len(request.headers["x-request-id"]) <= 64


@pytest.mark.contract
def test_embed_short_reply_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        """One vector for two inputs."""
        return httpx.Response(200, json={"embeddings": [[1, 0]]})

    async def go() -> None:
        async with router_over(handler, Sleeps()) as router:
            await router.embed(["a", "b"])

    with pytest.raises(RouterError, match="1 vectors for 2 inputs"):
        asyncio.run(go())


@pytest.mark.contract
def test_embed_503_with_retry_after_waits_that_long_then_succeeds() -> None:
    replies = [
        httpx.Response(503, json=error_body("capacity_timeout", 3), headers={"Retry-After": "3"}),
        httpx.Response(200, json={"embeddings": [[1.0]]}),
    ]
    sleeps = Sleeps()

    async def go() -> list[list[float]]:
        async with router_over(lambda r: replies.pop(0), sleeps) as router:
            return await router.embed(["a"])

    assert asyncio.run(go()) == [[1.0]]
    assert sleeps.waits == [3.0]


@pytest.mark.contract
def test_embed_503_with_huge_retry_after_is_capped_at_ten_seconds() -> None:
    replies = [
        httpx.Response(503, json=error_body("overloaded"), headers={"Retry-After": "300"}),
        httpx.Response(200, json={"embeddings": [[1.0]]}),
    ]
    sleeps = Sleeps()

    async def go() -> None:
        async with router_over(lambda r: replies.pop(0), sleeps) as router:
            await router.embed(["a"])

    asyncio.run(go())

    assert sleeps.waits == [10.0]


@pytest.mark.contract
def test_embed_503_without_retry_after_backs_off_doubling() -> None:
    replies = [
        httpx.Response(503, json=error_body("overloaded")),
        httpx.Response(503, json=error_body("overloaded")),
        httpx.Response(200, json={"embeddings": [[1.0]]}),
    ]
    sleeps = Sleeps()

    async def go() -> None:
        async with router_over(lambda r: replies.pop(0), sleeps) as router:
            await router.embed(["a"])

    asyncio.run(go())

    assert sleeps.waits == [1.0, 2.0]


@pytest.mark.contract
@pytest.mark.parametrize("status", [429, 502, 504])
def test_embed_other_retryable_statuses_wait_and_try_again(status: int) -> None:
    replies = [
        httpx.Response(status, json=error_body("busy", 2), headers={"Retry-After": "2"}),
        httpx.Response(200, json={"embeddings": [[1.0]]}),
    ]
    sleeps = Sleeps()

    async def go() -> list[list[float]]:
        async with router_over(lambda r: replies.pop(0), sleeps) as router:
            return await router.embed(["a"])

    assert asyncio.run(go()) == [[1.0]]
    assert sleeps.waits == [2.0]


@pytest.mark.contract
def test_embed_429_without_retry_after_backs_off_and_exhaustion_names_the_status() -> None:
    sleeps = Sleeps()

    async def go() -> None:
        async with router_over(
            lambda r: httpx.Response(429, json=error_body("rate_limited")), sleeps, attempts=3
        ) as router:
            await router.embed(["a"])

    with pytest.raises(RouterError, match="429"):
        asyncio.run(go())

    assert sleeps.waits == [1.0, 2.0]


@pytest.mark.contract
def test_embed_503_on_every_attempt_raises_after_the_attempt_budget() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Always busy."""
        calls.append(1)
        return httpx.Response(503, json=error_body("overloaded", 1), headers={"Retry-After": "1"})

    async def go() -> None:
        async with router_over(handler, Sleeps(), attempts=4) as router:
            await router.embed(["a"])

    with pytest.raises(RouterError, match="503"):
        asyncio.run(go())

    assert len(calls) == 4


@pytest.mark.contract
def test_embed_timeout_is_retried_then_succeeds() -> None:
    outcomes: list[httpx.Response | Exception] = [
        httpx.ReadTimeout("slow"),
        httpx.Response(200, json={"embeddings": [[1.0]]}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        """A timeout, then an answer."""
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    sleeps = Sleeps()

    async def go() -> list[list[float]]:
        async with router_over(handler, sleeps) as router:
            return await router.embed(["a"])

    assert asyncio.run(go()) == [[1.0]]
    assert sleeps.waits == [1.0]


@pytest.mark.contract
def test_embed_timeout_on_every_attempt_raises_router_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        """Never answers."""
        raise httpx.ReadTimeout("slow")

    async def go() -> None:
        async with router_over(handler, Sleeps(), attempts=2) as router:
            await router.embed(["a"])

    with pytest.raises(RouterError, match="ReadTimeout"):
        asyncio.run(go())


@pytest.mark.contract
def test_embed_refusal_is_not_retried_and_names_the_code_not_the_message() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """A 404 with a message that must not be echoed."""
        calls.append(1)
        body = error_body("unknown_model")
        body["error"] = "SECRET PROMPT TEXT"
        return httpx.Response(404, json=body)

    async def go() -> None:
        async with router_over(handler, Sleeps()) as router:
            await router.embed(["a"])

    with pytest.raises(RouterError) as caught:
        asyncio.run(go())

    assert "unknown_model" in str(caught.value)
    assert "SECRET" not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.contract
def test_router_missing_key_raises_without_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMBEDDING_API_KEY")
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Would answer, if it were called."""
        calls.append(1)
        return httpx.Response(200, json={"embeddings": [[1.0]]})

    async def go() -> None:
        async with router_over(handler, Sleeps()) as router:
            await router.embed(["a"])

    with pytest.raises(RouterError, match="EMBEDDING_API_KEY"):
        asyncio.run(go())

    assert calls == []


@pytest.mark.contract
def test_chat_json_sends_ollama_chat_shape_and_decodes_the_content() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """A model that answers in JSON."""
        seen.append(request)
        return httpx.Response(200, json={"message": {"content": '{"question": "Why?"}'}})

    async def go() -> dict[str, object]:
        async with router_over(handler, Sleeps()) as router:
            return await router.chat_json(
                "gemma3:12b", [{"role": "user", "content": "hi"}], {"temperature": 0.3}
            )

    assert asyncio.run(go()) == {"question": "Why?"}
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/api/chat"
    assert body["stream"] is False
    assert body["format"] == "json"
    assert body["options"] == {"temperature": 0.3}
    assert body["model"] == "gemma3:12b"


@pytest.mark.contract
def test_chat_json_content_not_json_raises_content_error_without_the_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        """A model that rambles."""
        return httpx.Response(200, json={"message": {"content": "private rambling"}})

    async def go() -> None:
        async with router_over(handler, Sleeps()) as router:
            await router.chat_json("m", [], {})

    with pytest.raises(RouterContentError) as caught:
        asyncio.run(go())

    assert "private" not in str(caught.value)


@pytest.mark.contract
def test_chat_json_content_that_is_a_list_raises_content_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        """Valid JSON, wrong shape."""
        return httpx.Response(200, json={"message": {"content": "[1, 2]"}})

    async def go() -> None:
        async with router_over(handler, Sleeps()) as router:
            await router.chat_json("m", [], {})

    with pytest.raises(RouterContentError):
        asyncio.run(go())
