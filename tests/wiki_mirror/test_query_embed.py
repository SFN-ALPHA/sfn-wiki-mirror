"""Qualifies the query embedding client: one valid vector for one question, or one typed error.

The embedding server is `httpx.MockTransport`, so nothing waits and no socket opens. Failure paths
named here: no `embedding_url`, a refused connection, a timeout, a refused key (401 and 403), any
other HTTP status, a reply that is not JSON, is compressed and cannot be decoded, has no vectors,
has the wrong number of them, has the wrong length or holds a non-finite number or an integer too
big for a float, a key that is not ASCII, and a key or a question that must never reach an error
message. Also qualified: the model sent is the one given (the semantic index's own), never the
settings'; no key means no `Authorization` header; the extra headers are the query ones; one
client serves every call, with the split timeout; at most four embeddings run at once (`busy`);
and the circuit breaker opens after three unanswered calls, ignores calls the server answered,
and closes or re-opens on its one trial call, all on an injected clock.

See docs/DECISIONS.md D10 and docs/DESIGN.md section 12.5.
"""

from __future__ import annotations

import gzip
import json
import re
import threading
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from tests.fixtures.wiki_mirror_fakes import EMBEDDING_URL, make_config
from wiki_mirror import query_embed
from wiki_mirror.query_embed import (
    QUERY_EMBED_BREAKER_FAILURES,
    QUERY_EMBED_BREAKER_PAUSE_SECONDS,
    QUERY_EMBED_CONNECT_SECONDS,
    QUERY_EMBED_MAX_IN_FLIGHT,
    QUERY_EMBED_TIMEOUT_SECONDS,
    QueryEmbedder,
    QueryEmbedError,
    key_problem,
)

pytestmark = pytest.mark.contract

#: Vector length of the tests' fake model.
DIMS = 4

#: Stands in for the API's embedding key: it must never appear in an error message.
KEY = "sk-sentinel-0123456789abcdef"

#: A question that must never appear in an error message either.
QUESTION = "what is the sentinel question about private matters"

#: The request-id rule the cluster uses: at most 64 of alphanumerics and `- _ . :`.
_REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _embedder(handler: Callable[[httpx.Request], httpx.Response], **kw: object) -> QueryEmbedder:
    """A query embedder over a handler; no socket."""
    fields: dict[str, object] = {"model": "bge-m3:latest", "dimensions": DIMS}
    fields.update(kw)
    return QueryEmbedder(
        EMBEDDING_URL,
        transport=httpx.MockTransport(handler),
        **fields,  # type: ignore[arg-type]
    )


def _ok(request: httpx.Request) -> httpx.Response:
    """A well-formed reply for a request."""
    return httpx.Response(200, json={"embeddings": [[0.5, 0.25, 0.125, 0.0625]]})


def _raising(error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    """A handler that raises `error`, as a transport that failed does."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return handler


def _fails(embedder: QueryEmbedder) -> QueryEmbedError:
    """The error `embedder.embed(QUESTION)` raises (the test fails if it does not)."""
    with pytest.raises(QueryEmbedError) as raised:
        embedder.embed(QUESTION)
    return raised.value


def test_embed_returns_the_one_vector_the_server_answers() -> None:
    assert _embedder(_ok).embed(QUESTION) == [0.5, 0.25, 0.125, 0.0625]


def test_embed_posts_the_given_model_and_the_question_as_one_input_to_api_embed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(request)

    _embedder(handler, model="the-index-model").embed(QUESTION)
    (request,) = seen
    assert (request.method, request.url.path) == ("POST", "/api/embed")
    assert json.loads(request.content) == {"model": "the-index-model", "input": [QUESTION]}


def test_embed_with_a_key_sends_a_bearer_header_and_without_one_sends_none() -> None:
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return _ok(request)

    _embedder(handler, api_key=KEY).embed(QUESTION)
    _embedder(handler).embed(QUESTION)
    assert seen[0]["authorization"] == f"Bearer {KEY}"
    assert "authorization" not in seen[1]


def test_embed_request_has_its_own_valid_request_id_and_the_extra_headers() -> None:
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return _ok(request)

    embedder = _embedder(handler, headers={"X-Priority": "1"})
    embedder.embed(QUESTION)
    embedder.embed(QUESTION)
    ids = [headers["x-request-id"] for headers in seen]
    assert len(set(ids)) == 2
    assert all(_REQUEST_ID.fullmatch(request_id) for request_id in ids)
    assert [headers["x-priority"] for headers in seen] == ["1", "1"]


def _request_id_sent(request_id: str | None) -> str:
    """The `X-Request-ID` an embed call with this id puts on the wire."""
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return _ok(request)

    _embedder(handler).embed(QUESTION, request_id)
    return seen[0]["x-request-id"]


def test_embed_given_request_id_is_the_header() -> None:
    assert _request_id_sent("turn-42:a.b_c") == "turn-42:a.b_c"


def test_embed_without_a_request_id_sends_a_generated_one() -> None:
    sent = _request_id_sent(None)
    assert sent.startswith("wiki-query-")
    assert _REQUEST_ID.fullmatch(sent)


@pytest.mark.parametrize("bad", ["", "has space", "x" * 65, "semi;colon", "line\nbreak"])
def test_embed_request_id_that_breaks_the_rule_falls_back_to_a_generated_one(bad: str) -> None:
    sent = _request_id_sent(bad)
    assert sent != bad
    assert sent.startswith("wiki-query-")


def test_from_config_without_an_embedding_url_raises_not_configured(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with pytest.raises(QueryEmbedError) as raised:
        QueryEmbedder.from_config(config, {}, model="m", dimensions=DIMS)
    assert raised.value.kind == "not_configured"
    assert "embedding_url" in str(raised.value)


def test_from_config_uses_the_model_given_not_the_settings_and_the_query_headers(
    tmp_path: Path,
) -> None:
    config = make_config(
        tmp_path,
        embedding_url=EMBEDDING_URL,
        embedding_model="a-newer-model",
        embedding_headers={"X-Priority": "3"},
        query_embedding_headers={"X-Priority": "1"},
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(request)

    embedder = QueryEmbedder.from_config(
        config,
        {},
        model="the-index-model",
        dimensions=DIMS,
        transport=httpx.MockTransport(handler),
    )
    embedder.embed(QUESTION)
    (request,) = seen
    assert json.loads(request.content)["model"] == "the-index-model"
    assert request.headers["x-priority"] == "1"


def test_from_config_reads_the_key_from_the_environment_trimmed_and_empty_means_none(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, embedding_url=EMBEDDING_URL)
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return _ok(request)

    for environ in ({"EMBEDDING_API_KEY": f" {KEY}\n"}, {"EMBEDDING_API_KEY": "  "}, {}):
        QueryEmbedder.from_config(
            config, environ, model="m", dimensions=DIMS, transport=httpx.MockTransport(handler)
        ).embed(QUESTION)
    assert seen[0]["authorization"] == f"Bearer {KEY}"
    assert "authorization" not in seen[1]
    assert "authorization" not in seen[2]


def test_embed_connection_refused_raises_unreachable() -> None:
    error = _fails(_embedder(_raising(httpx.ConnectError("refused at 100.64.0.9:8080"))))
    assert error.kind == "unreachable"
    assert "could not be reached" in str(error)


def test_embed_timeout_raises_timeout_naming_the_limit() -> None:
    error = _fails(_embedder(_raising(httpx.ReadTimeout("slow"))))
    assert error.kind == "timeout"
    assert f"{QUERY_EMBED_TIMEOUT_SECONDS:g} s" in str(error)


@pytest.mark.parametrize("status", [401, 403])
def test_embed_refused_key_raises_rejected_without_the_key(status: int) -> None:
    embedder = _embedder(lambda request: httpx.Response(status, text="bad key"), api_key=KEY)
    error = _fails(embedder)
    assert error.kind == "rejected"
    assert "EMBEDDING_API_KEY" in str(error)
    assert KEY not in str(error)


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
def test_embed_any_other_status_raises_status_with_only_the_number(status: int) -> None:
    error = _fails(_embedder(lambda request: httpx.Response(status, text="secret body")))
    assert error.kind == "status"
    assert str(error) == f"the embedding server answered {status}"


def test_embed_reply_that_is_not_json_raises_bad_reply() -> None:
    error = _fails(_embedder(lambda request: httpx.Response(200, content=b"<html>proxy</html>")))
    assert error.kind == "bad_reply"
    assert "not JSON" in str(error)


def test_embed_reply_without_embeddings_raises_bad_reply() -> None:
    error = _fails(_embedder(lambda request: httpx.Response(200, json={"error": "model busy"})))
    assert error.kind == "bad_reply"


def test_embed_reply_with_two_vectors_for_one_question_raises_bad_reply() -> None:
    vectors = [[1.0] * DIMS, [2.0] * DIMS]
    error = _fails(_embedder(lambda request: httpx.Response(200, json={"embeddings": vectors})))
    assert error.kind == "bad_reply"
    assert "2 vectors for 1 inputs" in str(error)


def test_embed_vector_of_the_wrong_dimension_raises_bad_reply() -> None:
    error = _fails(
        _embedder(lambda request: httpx.Response(200, json={"embeddings": [[1.0, 2.0]]}))
    )
    assert error.kind == "bad_reply"
    assert "not 4 numbers long" in str(error)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", '"x"', "true"])
def test_embed_vector_with_a_non_finite_or_non_numeric_value_raises_bad_reply(bad: str) -> None:
    body = f'{{"embeddings": [[1, 2, 3, {bad}]]}}'.encode()
    error = _fails(_embedder(lambda request: httpx.Response(200, content=body)))
    assert error.kind == "bad_reply"
    assert "non-finite" in str(error)


def test_embed_errors_never_hold_the_question_the_key_or_the_address() -> None:
    handlers = [
        _raising(httpx.ConnectError(f"refused at {EMBEDDING_URL} for {QUESTION}")),
        _raising(httpx.ReadTimeout(QUESTION)),
        lambda request: httpx.Response(401, text=f"{QUESTION} {KEY}"),
        lambda request: httpx.Response(500, text=f"{QUESTION} {KEY}"),
        lambda request: httpx.Response(200, text=f"{QUESTION} {KEY}"),
    ]
    for handler in handlers:
        message = str(_fails(_embedder(handler, api_key=KEY)))
        assert QUESTION not in message
        assert KEY not in message
        assert "100.64.0.9" not in message


def test_embed_compressed_reply_that_does_not_decompress_raises_bad_reply() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=b"not gzip")

    error = _fails(_embedder(handler))
    assert error.kind == "bad_reply"
    assert "cannot be decoded" in str(error)


def test_embed_compressed_reply_that_decompresses_is_read() -> None:
    body = gzip.compress(b'{"embeddings": [[0.5, 0.25, 0.125, 0.0625]]}')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=body)

    assert _embedder(handler).embed(QUESTION) == [0.5, 0.25, 0.125, 0.0625]


def test_embed_vector_with_an_integer_too_big_for_a_float_raises_bad_reply() -> None:
    body = ('{"embeddings": [[1, 1' + "0" * 400 + ", 3, 4]]}").encode()
    error = _fails(_embedder(lambda request: httpx.Response(200, content=body)))
    assert error.kind == "bad_reply"
    assert "position 1" in str(error)
    assert "0" * 20 not in str(error)


def test_embed_other_httpx_failure_raises_unreachable_without_its_text() -> None:
    error = _fails(_embedder(_raising(httpx.TooManyRedirects(f"{QUESTION} {EMBEDDING_URL}"))))
    assert error.kind == "unreachable"
    assert QUESTION not in str(error)


@pytest.mark.parametrize("key", ["kéy-0123456789", "key-☃", "key\nline"])
def test_embed_key_that_is_not_printable_ascii_refuses_every_question_without_sending(
    key: str,
) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return _ok(request)

    embedder = _embedder(handler, api_key=key)
    for _ in range(QUERY_EMBED_BREAKER_FAILURES + 1):
        error = _fails(embedder)
        assert error.kind == "not_configured"
        assert "EMBEDDING_API_KEY" in str(error)
        assert key not in str(error)
        assert not any(ord(char) > 0x7E for char in str(error))
    assert sent == []


def test_from_config_with_a_non_ascii_key_builds_an_embedder_that_refuses(tmp_path: Path) -> None:
    config = make_config(tmp_path, embedding_url=EMBEDDING_URL)
    embedder = QueryEmbedder.from_config(
        config,
        {"EMBEDDING_API_KEY": "kéy-0123456789"},
        model="m",
        dimensions=DIMS,
        transport=httpx.MockTransport(_ok),
    )
    assert _fails(embedder).kind == "not_configured"


def test_key_problem_names_the_variable_for_a_bad_key_and_is_none_otherwise() -> None:
    assert key_problem("kéy") is not None
    assert "EMBEDDING_API_KEY" in (key_problem("kéy") or "")
    assert key_problem(KEY) is None
    assert key_problem(None) is None
    assert key_problem("") is None


def test_embed_builds_one_client_for_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[httpx.Client] = []
    real = httpx.Client

    def counting(*args: object, **kwargs: object) -> httpx.Client:
        client = real(*args, **kwargs)  # type: ignore[arg-type]
        built.append(client)
        return client

    monkeypatch.setattr(query_embed.httpx, "Client", counting)
    embedder = _embedder(_ok)
    for _ in range(3):
        embedder.embed(QUESTION)
    assert len(built) == 1


def test_embed_sends_the_split_timeout_connect_short_and_the_rest_long() -> None:
    seen: list[dict[str, float | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return _ok(request)

    _embedder(handler).embed(QUESTION)
    assert seen == [
        {
            "connect": QUERY_EMBED_CONNECT_SECONDS,
            "read": QUERY_EMBED_TIMEOUT_SECONDS,
            "write": QUERY_EMBED_TIMEOUT_SECONDS,
            "pool": QUERY_EMBED_TIMEOUT_SECONDS,
        }
    ]
    assert QUERY_EMBED_CONNECT_SECONDS < QUERY_EMBED_TIMEOUT_SECONDS


def test_embed_connect_timeout_names_the_connect_limit_not_the_read_limit() -> None:
    error = _fails(_embedder(_raising(httpx.ConnectTimeout("slow"))))
    assert error.kind == "timeout"
    assert f"{QUERY_EMBED_CONNECT_SECONDS:g} s" in str(error)


class _Gate:
    """A server that holds every request until `open()`, and says when each one has arrived."""

    def __init__(self) -> None:
        self.arrived = threading.Semaphore(0)
        self._open = threading.Event()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.arrived.release()
        assert self._open.wait(5), "the test never opened the gate"
        return _ok(request)

    def open(self) -> None:
        self._open.set()


def _in_threads(embedder: QueryEmbedder, count: int) -> list[threading.Thread]:
    """Start `count` threads that each embed once (their outcome is not recorded)."""
    threads = [threading.Thread(target=embedder.embed, args=(QUESTION,)) for _ in range(count)]
    for thread in threads:
        thread.start()
    return threads


def test_embed_with_the_limit_in_flight_raises_busy_at_once_and_recovers_when_they_finish() -> None:
    gate = _Gate()
    embedder = _embedder(gate.handler)
    threads = _in_threads(embedder, QUERY_EMBED_MAX_IN_FLIGHT)
    try:
        for _ in threads:
            assert gate.arrived.acquire(timeout=5)
        error = _fails(embedder)
        assert error.kind == "busy"
        assert str(error) == "the embedding server is busy with other searches; try again"
    finally:
        gate.open()
        for thread in threads:
            thread.join(5)
    assert embedder.embed(QUESTION) == [0.5, 0.25, 0.125, 0.0625]


def test_embed_busy_is_not_counted_by_the_breaker() -> None:
    gate = _Gate()
    embedder = _embedder(gate.handler)
    threads = _in_threads(embedder, QUERY_EMBED_MAX_IN_FLIGHT)
    try:
        for _ in threads:
            assert gate.arrived.acquire(timeout=5)
        kinds = {_fails(embedder).kind for _ in range(QUERY_EMBED_BREAKER_FAILURES + 1)}
    finally:
        gate.open()
        for thread in threads:
            thread.join(5)
    assert kinds == {"busy"}


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Server:
    """A server whose next answer the test chooses, and that counts the requests it saw."""

    def __init__(self) -> None:
        self.requests = 0
        self.answer: Callable[[httpx.Request], httpx.Response] = _ok

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return self.answer(request)


def _down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused at 100.64.0.9:8080")


def _breaker_fixture() -> tuple[QueryEmbedder, _Server, _Clock]:
    """An embedder over a server the test controls, with a clock the test moves."""
    server, clock = _Server(), _Clock()
    return _embedder(server.handler, clock=clock), server, clock


def _open_the_breaker(embedder: QueryEmbedder, server: _Server) -> None:
    """Fail `QUERY_EMBED_BREAKER_FAILURES` calls in a row against a dead server."""
    server.answer = _down
    for _ in range(QUERY_EMBED_BREAKER_FAILURES):
        assert _fails(embedder).kind == "unreachable"


@pytest.mark.parametrize("failure", [httpx.ConnectError("x"), httpx.ReadTimeout("x")])
def test_embed_breaker_opens_after_three_unanswered_calls_and_then_asks_nothing(
    failure: Exception,
) -> None:
    embedder, server, _ = _breaker_fixture()
    server.answer = _raising(failure)
    for _ in range(QUERY_EMBED_BREAKER_FAILURES):
        assert _fails(embedder).kind in {"unreachable", "timeout"}
    error = _fails(embedder)
    assert error.kind == "breaker_open"
    assert str(error) == (
        "the embedding server failed 3 times in a row; semantic search pauses for 30 s, 30 s left"
    )
    assert server.requests == QUERY_EMBED_BREAKER_FAILURES


def test_embed_breaker_says_how_many_seconds_of_the_pause_are_left() -> None:
    embedder, server, clock = _breaker_fixture()
    _open_the_breaker(embedder, server)
    clock.now += 12.5
    assert str(_fails(embedder)).endswith("18 s left")


def test_embed_breaker_does_not_count_answers_the_server_gave() -> None:
    embedder, server, _ = _breaker_fixture()
    for status in (401, 403, 500, 503):
        server.answer = lambda request, status=status: httpx.Response(status)
        for _ in range(QUERY_EMBED_BREAKER_FAILURES):
            assert _fails(embedder).kind in {"rejected", "status"}
    server.answer = lambda request: httpx.Response(200, content=b"<html>")
    for _ in range(QUERY_EMBED_BREAKER_FAILURES):
        assert _fails(embedder).kind == "bad_reply"
    # Every call went to the server: none was refused by an open breaker.
    assert server.requests == 5 * QUERY_EMBED_BREAKER_FAILURES


def test_embed_breaker_run_is_broken_by_a_success() -> None:
    embedder, server, _ = _breaker_fixture()
    server.answer = _down
    for _ in range(QUERY_EMBED_BREAKER_FAILURES - 1):
        _fails(embedder)
    server.answer = _ok
    embedder.embed(QUESTION)
    server.answer = _down
    for _ in range(QUERY_EMBED_BREAKER_FAILURES - 1):
        assert _fails(embedder).kind == "unreachable"
    assert server.requests == 2 * (QUERY_EMBED_BREAKER_FAILURES - 1) + 1


def test_embed_breaker_run_is_broken_by_a_refused_key() -> None:
    embedder, server, _ = _breaker_fixture()
    server.answer = _down
    for _ in range(QUERY_EMBED_BREAKER_FAILURES - 1):
        _fails(embedder)
    server.answer = lambda request: httpx.Response(401)
    assert _fails(embedder).kind == "rejected"
    server.answer = _down
    assert _fails(embedder).kind == "unreachable"
    assert _fails(embedder).kind == "unreachable"


def test_embed_breaker_after_the_pause_lets_one_trial_through_and_a_success_closes_it() -> None:
    embedder, server, clock = _breaker_fixture()
    _open_the_breaker(embedder, server)
    clock.now += QUERY_EMBED_BREAKER_PAUSE_SECONDS
    server.answer = _ok
    assert embedder.embed(QUESTION) == [0.5, 0.25, 0.125, 0.0625]
    assert embedder.embed(QUESTION) == [0.5, 0.25, 0.125, 0.0625]
    # Closed means closed: it takes the full run of failures to open again.
    server.answer = _down
    for _ in range(QUERY_EMBED_BREAKER_FAILURES):
        assert _fails(embedder).kind == "unreachable"
    assert _fails(embedder).kind == "breaker_open"


def test_embed_breaker_failed_trial_opens_it_again_for_a_whole_pause() -> None:
    embedder, server, clock = _breaker_fixture()
    _open_the_breaker(embedder, server)
    asked = server.requests
    clock.now += QUERY_EMBED_BREAKER_PAUSE_SECONDS
    assert _fails(embedder).kind == "unreachable"  # the trial, and it fails
    assert server.requests == asked + 1
    assert _fails(embedder).kind == "breaker_open"
    clock.now += QUERY_EMBED_BREAKER_PAUSE_SECONDS - 1
    assert _fails(embedder).kind == "breaker_open"
    assert server.requests == asked + 1
    clock.now += 1
    server.answer = _ok
    assert embedder.embed(QUESTION)  # a second trial, and it succeeds


def test_embed_breaker_refuses_a_second_call_while_the_trial_is_out() -> None:
    gate = _Gate()
    server, clock = _Server(), _Clock()
    embedder = _embedder(server.handler, clock=clock)
    _open_the_breaker(embedder, server)
    clock.now += QUERY_EMBED_BREAKER_PAUSE_SECONDS
    server.answer = gate.handler
    (trial,) = _in_threads(embedder, 1)
    try:
        assert gate.arrived.acquire(timeout=5)
        assert _fails(embedder).kind == "breaker_open"
    finally:
        gate.open()
        trial.join(5)
    assert embedder.embed(QUESTION)  # the trial succeeded, so the breaker is closed
