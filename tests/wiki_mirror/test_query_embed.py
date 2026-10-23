"""Qualifies the query embedding client: one valid vector for one question, or one typed error.

The embedding server is `httpx.MockTransport`, so nothing waits and no socket opens. Failure paths
named here: no `embedding_url`, a refused connection, a timeout, a refused key (401 and 403), any
other HTTP status, a reply that is not JSON, has no vectors, has the wrong number of them, has
the wrong length or holds a non-finite number, and a key or a question that must never reach an
error message. Also qualified: the model sent is the one given (the semantic index's own), never
the settings'; no key means no `Authorization` header; the extra headers are the query ones.

See docs/DECISIONS.md D10 and docs/DESIGN.md section 12.5.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from tests.fixtures.wiki_mirror_fakes import EMBEDDING_URL, make_config
from wiki_mirror.query_embed import QUERY_EMBED_TIMEOUT_SECONDS, QueryEmbedder, QueryEmbedError

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
