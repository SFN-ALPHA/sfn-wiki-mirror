"""Qualifies the semantic index stage: the embedding client, the windows, resume and the finish.

`embed.run_embed` runs against Qdrant's in-memory local mode, an in-memory article source and a
fake embedder that derives each vector from its text, so a vector landing on the wrong point is
visible. The embedding client is qualified through `httpx.MockTransport` with an injected sleep
and clock, so no test waits or opens a socket. Failure paths named here: a reply with fewer
vectors than inputs, a wrong-sized or non-finite vector, a busy server (503 with `Retry-After`),
a refused key (401, never retried), a server that stays down past the retry budget, a crash
between windows, a resume with different settings, a collection without a state, a checkpoint
that Qdrant does not bear out (a lost tail, a half-written article, nothing left) and a key that
must never reach a log line or an exception.

The shared guarantees are checked against the lexical build itself: the same point ids and the
same payload for the same passage, and a checkpoint that never passes a window that is not yet
in Qdrant even when later windows finish first.

See docs/DESIGN.md sections 5 and 12.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from qdrant_client import QdrantClient, models

from tests.fixtures.wiki_mirror_fakes import (
    FakeSource,
    FakeStateStore,
    build_snapshot,
    make_article,
    make_config,
    memory_client,
)
from wiki_mirror import embed, index
from wiki_mirror.config import MirrorConfig
from wiki_mirror.embed import (
    ALIAS,
    EMBEDDING_API_KEY_ENV,
    TEXT_FORM,
    EmbeddingClient,
    EmbedError,
    open_source_store,
    passage_embedding_text,
    run_embed,
)
from wiki_mirror.index import indexable_chunks, run_index
from wiki_mirror.records import Article, passage_point_id
from wiki_mirror.text.sanitize import ChunkConfig

pytestmark = pytest.mark.contract

#: Vector length of the tests' fake model.
DIMS = 4

#: Stands in for the embedding server's key: it must never appear in a log line or an exception.
KEY = "sk-sentinel-0123456789abcdef"

_COLLECTION = "wikipedia_semantic_20261001"
_FILLER = "The committee reviewed the proposal and recorded its findings in the minutes. "

#: The request-id rule the cluster uses: at most 64 of alphanumerics and `- _ . :`.
_REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _vector_for(text: str) -> list[float]:
    """A vector derived from the text alone: small whole numbers, exact in float16."""
    digest = hashlib.sha256(text.encode()).digest()
    return [float(1 + digest[i] % 60) for i in range(DIMS)]


def _unit(vector: list[float]) -> list[float]:
    """The vector as Qdrant stores it under cosine distance: scaled to length 1."""
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


def _articles(count: int = 8) -> list[Article]:
    """Articles with two passages each (a lead and a section), titled by page id."""
    return [
        make_article(
            page_id,
            f"Title{page_id}",
            {"": f"Title{page_id} is a letter. {_FILLER}", "History": f"It is old. {_FILLER}"},
        )
        for page_id in range(1, count + 1)
    ]


def _config(tmp_path: Path, **overrides: object) -> MirrorConfig:
    """A config with the fake model's size and small requests, three in flight."""
    fields: dict[str, object] = {
        "embedding_dimensions": DIMS,
        "embedding_batch": 2,
        "embedding_concurrency": 3,
    }
    fields.update(overrides)
    return make_config(tmp_path, **fields)


def _run(
    config: MirrorConfig,
    client: QdrantClient,
    source: FakeSource,
    embedder: Any,
    **kw: Any,
) -> int:
    """Run the stage to its end and return the passage count."""
    return asyncio.run(run_embed(config, source, client, embedder, **kw))


def _state_file(tmp_path: Path, born: str = "20261001") -> Path:
    """Where the test run's semantic state is (on-disk name)."""
    return tmp_path / "data" / "state" / f"index_semantic_{born}.json"


def _state(tmp_path: Path) -> dict[str, Any]:
    """The semantic state file, parsed."""
    return json.loads(_state_file(tmp_path).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _points(client: QdrantClient, name: str) -> list[Any]:
    """Every point of a collection with payload and vector, ordered by id."""
    points, _ = client.scroll(name, limit=10_000, with_payload=True, with_vectors=True)
    return sorted(points, key=lambda point: int(point.id))


def _expected_texts(articles: list[Article]) -> list[str]:
    """The text of every passage of the articles, as the stage embeds them."""
    return [
        passage_embedding_text(article, chunk)
        for article in articles
        for chunk in indexable_chunks(article)[0]
    ]


class _Embedder:
    """A fake embedding server: a vector per text, finishing in a scrambled order.

    Each request yields to the event loop a text-dependent number of times before answering, so
    replies arrive out of order without any real waiting.
    """

    def __init__(self) -> None:
        """Start with nothing asked."""
        #: Every request's texts, in the order the requests began.
        self.requests: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Answer one vector per text."""
        self.requests.append(list(texts))
        for _ in range(hashlib.sha256(texts[0].encode()).digest()[0] % 5):
            await asyncio.sleep(0)
        return [_vector_for(text) for text in texts]

    @property
    def texts(self) -> list[str]:
        """Every text asked for, in request order."""
        return [text for request in self.requests for text in request]


class _FailsAt(_Embedder):
    """Fails its `call`-th request, as a server that went away would."""

    def __init__(self, call: int) -> None:
        """Fail the request numbered `call` (from 1)."""
        super().__init__()
        self._call = call

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Answer, except on the doomed request."""
        if len(self.requests) + 1 == self._call:
            self.requests.append(list(texts))
            msg = "the embedding server went away"
            raise EmbedError(msg)
        return await super().embed(texts)


class _Time:
    """A clock that only moves when something sleeps, and a record of the sleeps."""

    def __init__(self) -> None:
        """Start at zero with no sleeps."""
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        """Monotonic seconds."""
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Record the wait and move the clock by it."""
        self.sleeps.append(seconds)
        self.now += seconds


def _client(
    handler: Callable[[httpx.Request], Any],
    time: _Time | None = None,
    **kw: Any,
) -> EmbeddingClient:
    """An embedding client over a handler, with a fake clock; no socket."""
    clock = time or _Time()
    return EmbeddingClient(
        "http://100.64.0.9:8080",
        model="bge-m3",
        dimensions=DIMS,
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        clock=clock.clock,
        **kw,
    )


def _embed_once(client: EmbeddingClient, texts: list[str]) -> list[list[float]]:
    """One `embed` call, with the client opened and closed around it."""

    async def go() -> list[list[float]]:
        async with client:
            return await client.embed(texts)

    return asyncio.run(go())


def _ok(texts: list[str]) -> httpx.Response:
    """A well-formed reply for these inputs."""
    return httpx.Response(200, json={"embeddings": [_vector_for(text) for text in texts]})


def _inputs(request: httpx.Request) -> list[str]:
    """The `input` list of a request."""
    return json.loads(request.content)["input"]  # type: ignore[no-any-return]


# -- the embedding client ---------------------------------------------------------------------


def test_client_returns_one_vector_per_input_in_order() -> None:
    client = _client(lambda request: _ok(_inputs(request)))
    assert _embed_once(client, ["a", "b", "c"]) == [_vector_for(t) for t in ("a", "b", "c")]


def test_client_posts_the_model_and_the_inputs_to_api_embed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(_inputs(request))

    _embed_once(_client(handler), ["a", "b"])
    (request,) = seen
    assert (request.method, request.url.path) == ("POST", "/api/embed")
    assert json.loads(request.content) == {"model": "bge-m3", "input": ["a", "b"]}


def test_client_with_a_key_sends_it_as_a_bearer_header_and_without_one_sends_nothing() -> None:
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return _ok(_inputs(request))

    _embed_once(_client(handler, api_key=KEY), ["a"])
    _embed_once(_client(handler), ["a"])
    assert seen[0]["authorization"] == f"Bearer {KEY}"
    assert "authorization" not in seen[1]


def test_client_every_request_has_its_own_valid_request_id_and_the_extra_headers() -> None:
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return _ok(_inputs(request))

    client = _client(handler, headers={"X-Priority": "3"})

    async def go() -> None:
        async with client:
            await client.embed(["a"])
            await client.embed(["b"])

    asyncio.run(go())
    ids = [headers["x-request-id"] for headers in seen]
    assert len(set(ids)) == 2
    assert all(_REQUEST_ID.fullmatch(request_id) for request_id in ids)
    assert [headers["x-priority"] for headers in seen] == ["3", "3"]


def test_client_reply_with_fewer_vectors_than_inputs_raises() -> None:
    client = _client(lambda request: _ok(_inputs(request)[:-1]))
    with pytest.raises(EmbedError, match=r"returned 1 vectors for 2 inputs"):
        _embed_once(client, ["a", "b"])


def test_client_reply_with_no_embeddings_key_raises() -> None:
    client = _client(lambda request: httpx.Response(200, json={"error": "model busy"}))
    with pytest.raises(EmbedError, match="none vectors for 1 inputs"):
        _embed_once(client, ["a"])


def test_client_reply_that_is_not_json_raises() -> None:
    client = _client(lambda request: httpx.Response(200, content=b"<html>proxy</html>"))
    with pytest.raises(EmbedError, match="not JSON"):
        _embed_once(client, ["a"])


def test_client_vector_of_the_wrong_dimension_raises() -> None:
    client = _client(lambda request: httpx.Response(200, json={"embeddings": [[1.0, 2.0]]}))
    with pytest.raises(EmbedError, match="not 4 numbers long"):
        _embed_once(client, ["a"])


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", '"x"', "true"])
def test_client_vector_with_a_non_finite_or_non_numeric_value_raises(bad: str) -> None:
    body = f'{{"embeddings": [[1, 2, 3, {bad}]]}}'.encode()
    client = _client(lambda request: httpx.Response(200, content=body))
    with pytest.raises(EmbedError, match="non-finite"):
        _embed_once(client, ["a"])


def test_client_503_with_retry_after_waits_that_long_then_succeeds() -> None:
    answers = iter([httpx.Response(503, headers={"Retry-After": "7"}), None])
    time = _Time()

    def handler(request: httpx.Request) -> httpx.Response:
        return next(answers) or _ok(_inputs(request))

    assert _embed_once(_client(handler, time), ["a"]) == [_vector_for("a")]
    assert time.sleeps == [7.0]


def test_client_retry_after_is_capped() -> None:
    answers = iter([httpx.Response(429, headers={"Retry-After": "86400"}), None])
    time = _Time()

    def handler(request: httpx.Request) -> httpx.Response:
        return next(answers) or _ok(_inputs(request))

    _embed_once(_client(handler, time), ["a"])
    assert time.sleeps == [embed._RETRY_AFTER_MAX_SECONDS]


@pytest.mark.parametrize("status", [502, 503, 504, 429])
def test_client_transient_statuses_back_off_exponentially_then_succeed(status: int) -> None:
    answers = iter([httpx.Response(status)] * 3 + [None])
    time = _Time()

    def handler(request: httpx.Request) -> httpx.Response:
        return next(answers) or _ok(_inputs(request))

    _embed_once(_client(handler, time), ["a"])
    assert time.sleeps == [1.0, 2.0, 4.0]


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")],
    ids=["timeout", "connection"],
)
def test_client_timeouts_and_connection_errors_are_retried(failure: Exception) -> None:
    answers: list[Exception | None] = [failure, None]
    time = _Time()

    def handler(request: httpx.Request) -> httpx.Response:
        problem = answers.pop(0)
        if problem is not None:
            raise problem
        return _ok(_inputs(request))

    assert _embed_once(_client(handler, time), ["a"]) == [_vector_for("a")]
    assert time.sleeps == [1.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_refusals_raise_at_once_without_retrying(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[int] = []
    time = _Time()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, text=f"nope, {KEY} is wrong")

    with caplog.at_level(logging.DEBUG), pytest.raises(EmbedError) as raised:
        _embed_once(_client(handler, time, api_key=KEY), ["secret passage text"])
    assert (len(calls), time.sleeps) == (1, [])
    assert str(status) in str(raised.value)
    # Neither the key nor the response body nor the input reaches the message or the log.
    for leaked in (KEY, "nope", "secret passage text"):
        assert leaked not in str(raised.value)
        assert leaked not in caplog.text


def test_client_an_unlisted_status_is_not_retried() -> None:
    time = _Time()
    with pytest.raises(EmbedError, match="501"):
        _embed_once(_client(lambda request: httpx.Response(501), time), ["a"])
    assert time.sleeps == []


def test_client_a_500_is_retried_then_succeeds() -> None:
    answers = [500, 200]
    time = _Time()

    def handler(request: httpx.Request) -> httpx.Response:
        status = answers.pop(0)
        return _ok(_inputs(request)) if status == 200 else httpx.Response(status)

    assert _embed_once(_client(handler, time), ["a"]) == [_vector_for("a")]
    assert time.sleeps == [1.0]


def test_client_failures_beyond_the_retry_budget_raise_and_leak_no_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    time = _Time()
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    with caplog.at_level(logging.DEBUG), pytest.raises(EmbedError) as raised:
        _embed_once(_client(handler, time, api_key=KEY), ["a"])
    assert 5 < len(calls) < 40
    assert sum(time.sleeps) <= embed._RETRY_BUDGET_SECONDS
    assert max(time.sleeps) == embed._BACKOFF_MAX_SECONDS
    assert "gave up" in str(raised.value)
    assert KEY not in str(raised.value)
    assert KEY not in caplog.text


def test_client_retry_log_lines_name_no_text(caplog: pytest.LogCaptureFixture) -> None:
    answers = iter([httpx.Response(503), None])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(answers) or _ok(_inputs(request))

    with caplog.at_level(logging.INFO):
        _embed_once(_client(handler, api_key=KEY), ["private passage"])
    assert "retrying" in caplog.text
    assert "private passage" not in caplog.text
    assert KEY not in caplog.text


def test_client_from_config_reads_the_key_from_the_environment_only(tmp_path: Path) -> None:
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return _ok(_inputs(request))

    config = _config(tmp_path, embedding_url="http://100.64.0.9:8080/")
    for environ in ({EMBEDDING_API_KEY_ENV: f" {KEY}\n"}, {}):
        client = EmbeddingClient.from_config(
            config, environ, transport=httpx.MockTransport(handler)
        )
        _embed_once(client, ["a"])
    assert seen[0]["authorization"] == f"Bearer {KEY}"
    assert "authorization" not in seen[1]


def test_client_from_config_without_a_url_raises(tmp_path: Path) -> None:
    with pytest.raises(EmbedError, match="embedding_url"):
        EmbeddingClient.from_config(_config(tmp_path), {})


# -- the stage: vectors, windows, resume ------------------------------------------------------


def test_run_embed_every_vector_lands_on_the_point_of_its_own_passage(tmp_path: Path) -> None:
    articles = _articles()
    client = memory_client()
    total = _run(_config(tmp_path), client, FakeSource(articles), _Embedder(), window_articles=3)
    points = _points(client, _COLLECTION)
    expected = {
        passage_point_id(article.page_id, chunk.index): _unit(
            _vector_for(passage_embedding_text(article, chunk))
        )
        for article in articles
        for chunk in indexable_chunks(article)[0]
    }
    assert total == len(points) == len(expected) > 8
    for point in points:
        assert point.vector == pytest.approx(expected[int(point.id)], abs=2e-3)


def test_run_embed_embeds_the_title_heading_and_passage_text(tmp_path: Path) -> None:
    article = make_article(5, "Zebra", {"": f"Stripes. {_FILLER}", "Habitat": f"Plains. {_FILLER}"})
    embedder = _Embedder()
    _run(_config(tmp_path), memory_client(), FakeSource([article]), embedder)
    assert sorted(embedder.texts) == sorted(
        [
            f"Zebra\n\nStripes. {_FILLER.strip()}",
            f"Zebra\n\nHabitat\n\nPlains. {_FILLER.strip()}",
        ]
    )


def test_run_embed_a_reply_short_of_vectors_upserts_nothing_for_that_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    articles = [
        make_article(1, "Alpha"),
        make_article(2, "Beta"),
        make_article(3, "Gamma"),
        make_article(4, "Delta"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        texts = _inputs(request)
        vectors = [_vector_for(text) for text in texts]
        if any("Gamma" in text for text in texts):
            vectors = vectors[:-1]
        return httpx.Response(200, json={"embeddings": vectors})

    client = memory_client()
    seen = _record_upserts(client, monkeypatch)

    async def go() -> None:
        async with _client(handler) as embedder:
            await run_embed(
                _config(tmp_path), FakeSource(articles), client, embedder, window_articles=1
            )

    with pytest.raises(EmbedError, match="vectors for"):
        asyncio.run(go())
    assert set(seen) <= {1, 2}  # window 3 failed; window 4 must not be committed past it
    assert _state(tmp_path)["last_page_id"] <= 2


def _record_upserts(client: QdrantClient, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the page id of every point the client is asked to upsert."""
    seen: list[int] = []
    real = client.upsert

    def spy(collection_name: str, points: list[models.PointStruct], **kw: Any) -> Any:
        seen.extend(int(point.id) // index.PASSAGE_STRIDE for point in points)
        return real(collection_name=collection_name, points=points, **kw)

    monkeypatch.setattr(client, "upsert", spy)
    return seen


def test_run_embed_resumes_after_the_checkpoint_and_equals_an_uninterrupted_run(
    tmp_path: Path,
) -> None:
    articles = _articles()
    options = {"window_articles": 1, "windows_queued": 2}
    config = _config(tmp_path / "a", embedding_concurrency=1)
    interrupted = memory_client()
    with pytest.raises(EmbedError, match="went away"):
        _run(config, interrupted, FakeSource(articles), _FailsAt(5), **options)
    checkpoint = _state(tmp_path / "a")["last_page_id"]
    assert 3 <= checkpoint < 8
    resumed = _Embedder()
    _run(config, interrupted, FakeSource(articles), resumed, **options)

    whole = memory_client()
    _run(_config(tmp_path / "b"), whole, FakeSource(articles), _Embedder(), **options)

    # Nothing at or below the checkpoint was embedded again: at most the windows in flight at
    # the crash are repeated, and each window is one article here.
    assert sorted(resumed.texts) == sorted(
        _expected_texts([a for a in articles if a.page_id > checkpoint])
    )
    assert [(p.id, p.payload, p.vector) for p in _points(interrupted, _COLLECTION)] == [
        (p.id, p.payload, p.vector) for p in _points(whole, _COLLECTION)
    ]
    assert _state(tmp_path / "a")["passages"] == _state(tmp_path / "b")["passages"]


def test_run_embed_resume_asks_the_store_to_seek_past_the_checkpoint(tmp_path: Path) -> None:
    asked: list[int | None] = []

    class Spy(FakeSource):
        def iter_articles(self, after: int | None = None) -> Any:
            asked.append(after)
            return super().iter_articles(after)

    articles = _articles()
    client = memory_client()
    config = _config(tmp_path, embedding_concurrency=1)
    options = {"window_articles": 1, "windows_queued": 2}
    with pytest.raises(EmbedError):
        _run(config, client, FakeSource(articles), _FailsAt(5), **options)
    checkpoint = _state(tmp_path)["last_page_id"]
    _run(config, client, Spy(articles), _Embedder(), **options)
    assert asked == [checkpoint]


class _Gated(_Embedder):
    """Holds back the request for article "Beta" until a later article has been answered."""

    def __init__(self) -> None:
        """Start with the gate closed."""
        super().__init__()
        self.release = asyncio.Event()
        #: The article titles in the order their requests were answered.
        self.answered: list[str] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Answer, with Beta's reply waiting for Gamma's or Delta's."""
        title = texts[0].split("\n")[0]
        if title == "Beta":
            await self.release.wait()
        reply = await super().embed(texts)
        self.answered.append(title)
        if title in {"Gamma", "Delta"}:
            self.release.set()
        return reply


def test_run_embed_the_checkpoint_never_passes_a_window_that_is_not_yet_upserted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    articles = [
        make_article(1, "Alpha"),
        make_article(2, "Beta"),
        make_article(3, "Gamma"),
        make_article(4, "Delta"),
    ]
    client = memory_client()
    upserted: list[int] = []
    real_upsert = client.upsert

    def spy_upsert(collection_name: str, points: list[models.PointStruct], **kw: Any) -> Any:
        upserted.extend(int(point.id) // index.PASSAGE_STRIDE for point in points)
        return real_upsert(collection_name=collection_name, points=points, **kw)

    monkeypatch.setattr(client, "upsert", spy_upsert)
    writes: list[tuple[int, set[int]]] = []
    real_write = embed._write_state

    def spy_write(path: Path, state: Any) -> None:
        writes.append((state.last_page_id, set(upserted)))
        real_write(path, state)

    monkeypatch.setattr(embed, "_write_state", spy_write)

    embedder = _Gated()
    _run(
        _config(tmp_path, embedding_concurrency=4),
        client,
        FakeSource(articles),
        embedder,
        window_articles=1,
    )
    # The scenario happened: a later window was answered while window 2 was still waiting.
    assert embedder.answered.index("Gamma") < embedder.answered.index("Beta")
    # Every page at or below a written checkpoint was already in Qdrant when it was written.
    for last_page_id, present in writes:
        assert {page for page in range(1, 5) if page <= last_page_id} <= present
    assert upserted == sorted(upserted)


@pytest.mark.parametrize(
    ("label", "change"),
    [
        ("model", {"embedding_model": "another-model"}),
        ("dimensions", {"embedding_dimensions": 8}),
    ],
)
def test_run_embed_resume_with_another_model_or_dimension_is_refused(
    tmp_path: Path, label: str, change: dict[str, object]
) -> None:
    articles = _articles()
    client = memory_client()
    options = {"window_articles": 1, "windows_queued": 2}
    config = _config(tmp_path, embedding_concurrency=1)
    with pytest.raises(EmbedError):
        _run(config, client, FakeSource(articles), _FailsAt(5), **options)
    resumed = _Embedder()
    changed = _config(tmp_path, embedding_concurrency=1, **change)
    with pytest.raises(EmbedError, match=label) as raised:
        _run(changed, client, FakeSource(articles), resumed)
    assert "delete the collection" in str(raised.value)
    assert resumed.requests == []


def test_run_embed_resume_with_another_text_form_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    articles = _articles()
    client = memory_client()
    options = {"window_articles": 1, "windows_queued": 2}
    config = _config(tmp_path, embedding_concurrency=1)
    with pytest.raises(EmbedError):
        _run(config, client, FakeSource(articles), _FailsAt(5), **options)
    monkeypatch.setattr(embed, "TEXT_FORM", TEXT_FORM + "-bumped")
    with pytest.raises(EmbedError, match="text form"):
        _run(config, client, FakeSource(articles), _Embedder())


def test_run_embed_resume_with_another_chunker_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    articles = _articles()
    client = memory_client()
    options = {"window_articles": 1, "windows_queued": 2}
    config = _config(tmp_path, embedding_concurrency=1)
    with pytest.raises(EmbedError):
        _run(config, client, FakeSource(articles), _FailsAt(5), **options)
    monkeypatch.setattr(index, "CHUNKER_VERSION", index.CHUNKER_VERSION + 1)
    with pytest.raises(EmbedError, match="chunker"):
        _run(config, client, FakeSource(articles), _Embedder())


def test_run_embed_a_collection_without_a_state_is_never_deleted(tmp_path: Path) -> None:
    client = memory_client()
    client.create_collection(
        _COLLECTION, vectors_config=models.VectorParams(size=DIMS, distance=models.Distance.COSINE)
    )
    client.upsert(_COLLECTION, points=[models.PointStruct(id=1, vector=[1.0, 0, 0, 0])])
    embedder = _Embedder()
    with pytest.raises(EmbedError, match="no usable state"):
        _run(_config(tmp_path), client, FakeSource(_articles()), embedder)
    assert embedder.requests == []
    assert len(_points(client, _COLLECTION)) == 1


def test_run_embed_a_state_whose_collection_is_gone_starts_the_collection_over(
    tmp_path: Path,
) -> None:
    articles = _articles()
    client = memory_client()
    options = {"window_articles": 1, "windows_queued": 2}
    config = _config(tmp_path, embedding_concurrency=1)
    with pytest.raises(EmbedError):
        _run(config, client, FakeSource(articles), _FailsAt(5), **options)
    client.delete_collection(_COLLECTION)
    embedder = _Embedder()
    _run(config, client, FakeSource(articles), embedder, **options)
    assert sorted(embedder.texts) == sorted(_expected_texts(articles))
    assert len(_points(client, _COLLECTION)) == len(embedder.texts)


def test_run_embed_an_empty_store_is_refused_before_anything_is_created(tmp_path: Path) -> None:
    client = memory_client()
    with pytest.raises(EmbedError, match="empty"):
        _run(_config(tmp_path), client, FakeSource([]), _Embedder())
    assert not client.collection_exists(_COLLECTION)


def test_run_embed_an_embedder_that_returns_the_wrong_count_stops_the_run(
    tmp_path: Path,
) -> None:
    class Short(_Embedder):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [_vector_for(text) for text in texts[:-1]]

    client = memory_client()
    with pytest.raises(EmbedError, match="vectors for"):
        _run(_config(tmp_path), client, FakeSource(_articles()), Short())
    assert _points(client, _COLLECTION) == []


# -- verifying the checkpoint against Qdrant ---------------------------------------------------

_WINDOWS_OF_ONE = {"window_articles": 1, "windows_queued": 2}


def _crashed_run(tmp_path: Path, articles: list[Article]) -> tuple[QdrantClient, MirrorConfig, int]:
    """Run until the embedder fails, one article to a window; return the checkpoint it left."""
    client = memory_client()
    config = _config(tmp_path, embedding_concurrency=1)
    with pytest.raises(EmbedError, match="went away"):
        _run(config, client, FakeSource(articles), _FailsAt(5), **_WINDOWS_OF_ONE)
    checkpoint = _state(tmp_path)["last_page_id"]
    assert 3 <= checkpoint < len(articles)
    return client, config, checkpoint


def _drop_points(client: QdrantClient, condition: models.FieldCondition) -> None:
    """Delete the points matching `condition`, as a lost write-ahead log tail would."""
    client.delete(
        _COLLECTION,
        points_selector=models.FilterSelector(filter=models.Filter(must=[condition])),
    )


def _after(page_id: int) -> models.FieldCondition:
    """Matches the points of articles with a page id above `page_id`."""
    return models.FieldCondition(key="page_id", range=models.Range(gt=page_id))


def _resume_state(tmp_path: Path) -> Any:
    """The state a resume would read from disk."""
    return embed._read_state(_state_file(tmp_path), "20261001")


def _assert_equals_uninterrupted(
    tmp_path: Path, client: QdrantClient, articles: list[Article]
) -> None:
    """The collection and counts equal those of a run that was never interrupted."""
    whole = memory_client()
    _run(_config(tmp_path / "whole"), whole, FakeSource(articles), _Embedder(), **_WINDOWS_OF_ONE)
    got, want = _points(client, _COLLECTION), _points(whole, _COLLECTION)
    assert [(p.id, p.payload) for p in got] == [(p.id, p.payload) for p in want]
    # Approximately: the in-memory client renormalizes a point that is upserted a second time,
    # which moves a stored component by about 1e-8; the same vector, not the same bits.
    for have, expected_point in zip(got, want, strict=True):
        assert have.vector == pytest.approx(expected_point.vector, abs=1e-6)
    state, expected = _state(tmp_path), _state(tmp_path / "whole")
    for key in ("last_page_id", "articles", "passages", "complete"):
        assert state[key] == expected[key]


def _embed_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The messages the embedding code itself logged at WARNING or above.

    The 1.12 client logs its local-mode payload-index notice at WARNING on the root logger
    (newer clients raise a UserWarning, which pyproject.toml ignores), so a check that nothing
    was warned must look at this module's logger alone.
    """
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "wiki_mirror.embed" and r.levelno >= logging.WARNING
    ]


def test_verified_checkpoint_matching_count_keeps_the_checkpoint(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    articles = _articles()
    client, config, checkpoint = _crashed_run(tmp_path, articles)
    resumed = _Embedder()
    with caplog.at_level(logging.INFO, logger="wiki_mirror.embed"):
        _run(config, client, FakeSource(articles), resumed, **_WINDOWS_OF_ONE)
    assert "checkpoint verified" in caplog.text
    assert not _embed_warnings(caplog)
    assert sorted(resumed.texts) == sorted(
        _expected_texts([a for a in articles if a.page_id > checkpoint])
    )
    _assert_equals_uninterrupted(tmp_path, client, articles)


def test_verified_checkpoint_tail_loss_steps_back_and_the_run_equals_an_uninterrupted_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    articles = _articles()
    client, config, checkpoint = _crashed_run(tmp_path, articles)
    survivor = checkpoint - 2
    _drop_points(client, _after(survivor))
    with caplog.at_level(logging.INFO, logger="wiki_mirror.embed"):
        checked = embed._verified_checkpoint(client, _COLLECTION, _resume_state(tmp_path))
    # One article to a window and two passages to an article: the counts follow the page ids.
    assert (checked.last_page_id, checked.articles, checked.passages) == (
        survivor - 1,
        survivor - 1,
        2 * (survivor - 1),
    )
    (warning,) = _embed_warnings(caplog)
    assert f"page id {checkpoint}" in warning
    assert f"page id {survivor - 1}" in warning
    assert f"({2 * checkpoint} passages)" in warning
    assert f"holds {2 * survivor} passages" in warning

    resumed = _Embedder()
    _run(config, client, FakeSource(articles), resumed, **_WINDOWS_OF_ONE)
    assert sorted(resumed.texts) == sorted(
        _expected_texts([a for a in articles if a.page_id > survivor - 1])
    )
    _assert_equals_uninterrupted(tmp_path, client, articles)


def test_verified_checkpoint_step_back_is_written_before_embedding_resumes(
    tmp_path: Path,
) -> None:
    articles = _articles()
    client, config, checkpoint = _crashed_run(tmp_path, articles)
    _drop_points(client, _after(checkpoint - 2))
    seen: list[int] = []

    class Crashing(_Embedder):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            seen.append(_state(tmp_path)["last_page_id"])
            msg = "the embedding server went away"
            raise EmbedError(msg)

    with pytest.raises(EmbedError, match="went away"):
        _run(config, client, FakeSource(articles), Crashing(), **_WINDOWS_OF_ONE)
    assert seen[0] == checkpoint - 3


def test_verified_checkpoint_a_partly_written_last_article_is_embedded_again_whole(
    tmp_path: Path,
) -> None:
    articles = _articles()
    client, config, checkpoint = _crashed_run(tmp_path, articles)
    # The last article's second passage never reached the disk; its first did.
    client.delete(_COLLECTION, points_selector=[passage_point_id(checkpoint, 1)])
    resumed = _Embedder()
    _run(config, client, FakeSource(articles), resumed, **_WINDOWS_OF_ONE)
    assert sorted(resumed.texts) == sorted(
        _expected_texts([a for a in articles if a.page_id >= checkpoint])
    )
    _assert_equals_uninterrupted(tmp_path, client, articles)


def test_verified_checkpoint_no_surviving_points_restarts_from_the_beginning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    articles = _articles()
    client, config, _ = _crashed_run(tmp_path, articles)
    _drop_points(client, _after(-1))
    resumed = _Embedder()
    with caplog.at_level(logging.WARNING, logger="wiki_mirror.embed"):
        _run(config, client, FakeSource(articles), resumed, **_WINDOWS_OF_ONE)
    assert "does not match Qdrant" in caplog.text
    assert sorted(resumed.texts) == sorted(_expected_texts(articles))
    _assert_equals_uninterrupted(tmp_path, client, articles)


def test_verified_checkpoint_points_beyond_the_checkpoint_do_not_step_it_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    articles = _articles()
    client, config, checkpoint = _crashed_run(tmp_path, articles)
    # A crash between an upsert and its state write: the next window is in Qdrant, unrecorded.
    ahead = articles[checkpoint]
    assert ahead.page_id == checkpoint + 1
    client.upsert(
        _COLLECTION,
        points=[
            models.PointStruct(
                id=passage_point_id(ahead.page_id, chunk.index),
                vector=_vector_for(passage_embedding_text(ahead, chunk)),
                payload=index.passage_payload(ahead, chunk),
            )
            for chunk in indexable_chunks(ahead)[0]
        ],
    )
    state = _resume_state(tmp_path)
    with caplog.at_level(logging.INFO, logger="wiki_mirror.embed"):
        assert embed._verified_checkpoint(client, _COLLECTION, state) is state
    assert not _embed_warnings(caplog)
    _run(config, client, FakeSource(articles), _Embedder(), **_WINDOWS_OF_ONE)
    _assert_equals_uninterrupted(tmp_path, client, articles)


def test_verified_checkpoint_more_points_than_the_state_counts_also_steps_back(
    tmp_path: Path,
) -> None:
    articles = _articles()
    client, _, checkpoint = _crashed_run(tmp_path, articles)
    client.upsert(
        _COLLECTION,
        points=[
            models.PointStruct(
                id=passage_point_id(checkpoint - 1, 9),
                vector=[1.0, 0, 0, 0],
                payload={"page_id": checkpoint - 1, "chunk": 9},
            )
        ],
    )
    checked = embed._verified_checkpoint(client, _COLLECTION, _resume_state(tmp_path))
    # The highest article present is the checkpoint's own, which is re-embedded to be safe.
    assert checked.last_page_id == checkpoint - 1


def test_verified_checkpoint_a_complete_state_does_no_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    config = _config(tmp_path)
    first = _run(config, client, FakeSource(_articles(3)), _Embedder())

    def refuse(*args: Any, **kwargs: Any) -> Any:
        msg = "a finished run must not look at the points"
        raise AssertionError(msg)

    monkeypatch.setattr(client, "count", refuse)
    monkeypatch.setattr(client, "scroll", refuse)
    assert _run(config, client, FakeSource(_articles(3)), _Embedder()) == first


# -- the finish -------------------------------------------------------------------------------


def test_run_embed_creates_the_collection_for_dense_search_with_hnsw_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    created: list[dict[str, Any]] = []
    real = client.create_collection

    def spy(**kwargs: Any) -> Any:
        created.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(client, "create_collection", spy)
    _run(_config(tmp_path), client, FakeSource(_articles(2)), _Embedder())
    (made,) = created
    vectors = made["vectors_config"]
    assert (vectors.size, vectors.distance) == (DIMS, models.Distance.COSINE)
    assert vectors.datatype == models.Datatype.FLOAT16
    assert vectors.on_disk is True
    assert made["hnsw_config"].m == 0
    assert made["quantization_config"] == embed.SEMANTIC_QUANTIZATION
    assert made["optimizers_config"].max_segment_size == embed.SEMANTIC_MAX_SEGMENT_KB
    assert made["optimizers_config"].max_optimization_threads == embed.SEMANTIC_OPTIMIZATION_THREADS
    assert made["on_disk_payload"] is True


def test_semantic_settings_keep_everything_on_disk_and_use_two_optimizer_threads() -> None:
    # The operator's decision of 2026-10-23 (docs/DECISIONS.md D8), spelled out so that a change
    # to the layout has to change this test on purpose: the maintenance job reads these values.
    assert (embed.SEMANTIC_HNSW.m, embed.SEMANTIC_HNSW.ef_construct) == (16, 100)
    assert embed.SEMANTIC_HNSW.on_disk is True
    assert embed.SEMANTIC_QUANTIZATION.binary.always_ram is False
    assert embed.SEMANTIC_OPTIMIZATION_THREADS == 2


def test_semantic_segment_cap_is_ten_gigabytes_and_its_disk_factor_is_the_measured_one() -> None:
    # Decision D9 (docs/DECISIONS.md), spelled out like the layout above: the maintenance job
    # rewrites every segment when this changes, and sizes the disk room it waits for from both.
    assert embed.SEMANTIC_MAX_SEGMENT_KB == 10_000_000
    assert embed.SEGMENT_DISK_FACTOR == 1.27


def test_run_embed_completion_switches_hnsw_on_after_the_last_upsert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    events: list[str] = []
    real_upsert, real_update = client.upsert, client.update_collection

    def upsert(**kwargs: Any) -> Any:
        events.append("upsert")
        return real_upsert(**kwargs)

    def update(**kwargs: Any) -> Any:
        events.append("hnsw")
        assert kwargs["collection_name"] == _COLLECTION
        hnsw = kwargs["hnsw_config"]
        assert (hnsw.m, hnsw.ef_construct, hnsw.on_disk) == (16, 100, True)
        return real_update(**kwargs)

    monkeypatch.setattr(client, "upsert", upsert)
    monkeypatch.setattr(client, "update_collection", update)
    _run(_config(tmp_path), client, FakeSource(_articles(3)), _Embedder())
    assert events.count("hnsw") == 1
    assert events[-1] == "hnsw"


def test_run_embed_completion_points_the_alias_and_records_the_applied_snapshot(
    tmp_path: Path,
) -> None:
    client = memory_client()
    total = _run(_config(tmp_path), client, FakeSource(_articles(3)), _Embedder())
    aliases = {a.alias_name: a.collection_name for a in client.get_aliases().aliases}
    assert aliases == {ALIAS: _COLLECTION}
    state = _state(tmp_path)
    assert state["complete"] is True
    assert state["applied_snapshot"] == state["born"] == "20261001"
    assert state["passages"] == total


def test_run_embed_completion_moves_an_existing_alias_and_keeps_the_old_collection(
    tmp_path: Path,
) -> None:
    client = memory_client()
    params = models.VectorParams(size=DIMS, distance=models.Distance.COSINE)
    client.create_collection("wikipedia_semantic_20260901", vectors_config=params)
    client.update_collection_aliases(
        change_aliases_operations=[
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(
                    collection_name="wikipedia_semantic_20260901", alias_name=ALIAS
                )
            )
        ]
    )
    _run(_config(tmp_path), client, FakeSource(_articles(3)), _Embedder())
    aliases = {a.alias_name: a.collection_name for a in client.get_aliases().aliases}
    assert aliases == {ALIAS: _COLLECTION}
    assert client.collection_exists("wikipedia_semantic_20260901")


def test_run_embed_state_records_what_the_vectors_are_made_of(tmp_path: Path) -> None:
    _run(_config(tmp_path), memory_client(), FakeSource(_articles(3)), _Embedder())
    state = _state(tmp_path)
    assert state["chunker"] == index.chunker_fingerprint()
    assert (state["model"], state["dimensions"]) == ("bge-m3", DIMS)
    assert state["text_form"] == TEXT_FORM
    assert KEY not in json.dumps(state)


# -- whether a collection's load has finished ---------------------------------------------------


def test_semantic_index_complete_after_a_finished_run_is_true(tmp_path: Path) -> None:
    _run(_config(tmp_path), memory_client(), FakeSource(_articles(3)), _Embedder())
    assert embed.semantic_index_complete(tmp_path / "data", _COLLECTION) is True


def test_semantic_index_complete_while_a_run_is_still_loading_is_false(tmp_path: Path) -> None:
    config = _config(tmp_path, embedding_concurrency=1)
    with pytest.raises(EmbedError, match="went away"):
        _run(
            config,
            memory_client(),
            FakeSource(_articles()),
            _FailsAt(5),
            window_articles=1,
            windows_queued=2,
        )
    assert _state(tmp_path)["complete"] is False
    assert embed.semantic_index_complete(tmp_path / "data", _COLLECTION) is False


def test_semantic_index_complete_without_a_state_file_is_false(tmp_path: Path) -> None:
    assert embed.semantic_index_complete(tmp_path / "data", _COLLECTION) is False


def test_semantic_index_complete_with_an_unreadable_state_is_false_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _state_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert embed.semantic_index_complete(tmp_path / "data", _COLLECTION) is False
    assert "unreadable" in caplog.text


def test_semantic_index_complete_quiet_with_an_unreadable_state_is_false_and_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _state_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        assert embed.semantic_index_complete(tmp_path / "data", _COLLECTION, quiet=True) is False
    assert caplog.text == ""


def test_semantic_index_complete_with_another_snapshots_state_is_false(tmp_path: Path) -> None:
    _run(_config(tmp_path), memory_client(), FakeSource(_articles(3)), _Embedder())
    # The file of the 20261001 collection says it is complete, but it describes another born.
    state = _state(tmp_path)
    state["born"] = "20260901"
    _state_file(tmp_path).write_text(json.dumps(state), encoding="utf-8")
    assert embed.semantic_index_complete(tmp_path / "data", _COLLECTION) is False


@pytest.mark.parametrize(
    "name", ["wikipedia_lexical_20261001", "wikipedia_semantic_", "wikipedia_semantic_../x"]
)
def test_semantic_index_complete_for_a_name_this_stage_does_not_create_is_false(
    tmp_path: Path, name: str
) -> None:
    _run(_config(tmp_path), memory_client(), FakeSource(_articles(3)), _Embedder())
    assert embed.semantic_index_complete(tmp_path / "data", name) is False


# -- what a collection's state says about its vectors ------------------------------------------


def test_semantic_index_info_after_a_finished_run_is_what_the_state_records(
    tmp_path: Path,
) -> None:
    _run(_config(tmp_path), memory_client(), FakeSource(_articles(3)), _Embedder())
    info = embed.semantic_index_info(tmp_path / "data", _COLLECTION)
    assert info == embed.SemanticIndexInfo(
        complete=True,
        model="bge-m3",
        dimensions=DIMS,
        applied_snapshot="20261001",
        chunker=index.chunker_fingerprint(),
    )


def test_semantic_index_info_while_a_run_is_still_loading_is_incomplete_with_no_snapshot(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, embedding_concurrency=1)
    with pytest.raises(EmbedError, match="went away"):
        _run(
            config,
            memory_client(),
            FakeSource(_articles()),
            _FailsAt(5),
            window_articles=1,
            windows_queued=2,
        )
    info = embed.semantic_index_info(tmp_path / "data", _COLLECTION)
    assert info is not None
    assert (info.complete, info.applied_snapshot) == (False, "")


def test_semantic_index_info_without_a_state_file_is_none(tmp_path: Path) -> None:
    assert embed.semantic_index_info(tmp_path / "data", _COLLECTION) is None


def test_semantic_index_info_quiet_with_an_unreadable_state_is_none_and_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _state_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        assert embed.semantic_index_info(tmp_path / "data", _COLLECTION, quiet=True) is None
    assert caplog.text == ""


def test_semantic_index_info_with_another_snapshots_state_is_none(tmp_path: Path) -> None:
    _run(_config(tmp_path), memory_client(), FakeSource(_articles(3)), _Embedder())
    state = _state(tmp_path)
    state["born"] = "20260901"
    _state_file(tmp_path).write_text(json.dumps(state), encoding="utf-8")
    assert embed.semantic_index_info(tmp_path / "data", _COLLECTION) is None


@pytest.mark.parametrize("name", ["wikipedia_lexical_20261001", "wikipedia_semantic_../x"])
def test_semantic_index_info_for_a_name_this_stage_does_not_create_is_none(
    tmp_path: Path, name: str
) -> None:
    _run(_config(tmp_path), memory_client(), FakeSource(_articles(3)), _Embedder())
    assert embed.semantic_index_info(tmp_path / "data", name) is None


def test_run_embed_a_complete_state_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = memory_client()
    first = _run(_config(tmp_path), client, FakeSource(_articles(3)), _Embedder())
    seen = _record_upserts(client, monkeypatch)
    embedder = _Embedder()
    with caplog.at_level(logging.INFO):
        again = _run(_config(tmp_path), client, FakeSource(_articles(3)), embedder)
    assert again == first
    assert embedder.requests == []
    assert seen == []
    assert "nothing to do" in caplog.text


def test_run_embed_below_the_disk_floor_refuses_before_creating_the_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embed, "ensure_free_space", _refuse_space)
    client = memory_client()
    with pytest.raises(embed.BuildError, match="not enough free disk"):
        _run(_config(tmp_path), client, FakeSource(_articles(2)), _Embedder())
    assert not client.collection_exists(_COLLECTION)


def _refuse_space(directory: Path, floor_gb: int, what: str) -> None:
    """Stands in for `build.ensure_free_space` on a full disk."""
    msg = f"not enough free disk to create {what}"
    raise embed.BuildError(msg)


def test_run_embed_chunks_past_the_stride_are_skipped_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index, "PASSAGE_STRIDE", 2)
    sections = {"": f"Lead. {_FILLER}", "One": _FILLER, "Two": _FILLER, "Three": _FILLER}
    total = _run(
        _config(tmp_path),
        memory_client(),
        FakeSource([make_article(10, "Many", sections)]),
        _Embedder(),
    )
    assert (total, _state(tmp_path)["over_stride"]) == (2, 2)


def test_run_embed_progress_lines_give_counts_and_rates_but_no_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ticks = iter(range(0, 10_000, 70))
    with caplog.at_level(logging.INFO, logger="wiki_mirror.embed"):
        _run(
            _config(tmp_path),
            memory_client(),
            FakeSource(_articles()),
            _Embedder(),
            clock=lambda: float(next(ticks)),
            window_articles=2,
        )
    lines = [r.getMessage() for r in caplog.records if "passages/s" in r.getMessage()]
    assert lines
    assert re.search(r"\d+ of 8 articles \(\d+\.\d%\), \d+ passages; \d+ passages/s", lines[-1])
    assert "left" in lines[-1]
    assert "Title" not in caplog.text
    assert "committee" not in caplog.text


# -- the source and the shared guarantees -----------------------------------------------------


def test_open_source_store_without_a_lexical_collection_says_to_build_first(
    tmp_path: Path,
) -> None:
    with pytest.raises(EmbedError, match="no lexical collection"):
        open_source_store(_config(tmp_path), memory_client())


def test_open_source_store_returns_the_store_of_the_lexical_collections_applied_snapshot(
    tmp_path: Path,
) -> None:
    config, client = _config(tmp_path), memory_client()
    build_snapshot(config, client, _articles(3))
    store = open_source_store(config, client)
    try:
        assert store.snapshot.tag == "20261001"
        assert store.count() == 3
    finally:
        store.close()


def test_open_source_store_a_lexical_collection_with_no_applied_snapshot_is_refused(
    tmp_path: Path,
) -> None:
    config, client = _config(tmp_path), memory_client()
    build_snapshot(config, client, _articles(3))
    (tmp_path / "data" / "state" / "index_lexical_20261001.json").unlink()
    with pytest.raises(EmbedError, match="no completed index state"):
        open_source_store(config, client)


def test_open_source_store_a_lexical_collection_cut_by_another_chunker_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, client = _config(tmp_path), memory_client()
    build_snapshot(config, client, _articles(3))
    monkeypatch.setattr(index, "CHUNK_CONFIG", ChunkConfig(max_tokens=64, overlap_tokens=8))
    with pytest.raises(EmbedError, match="chunker"):
        open_source_store(config, client)


def test_open_source_store_a_missing_store_is_an_embed_error(tmp_path: Path) -> None:
    config, client = _config(tmp_path), memory_client()
    state = FakeStateStore()
    run_index(
        config,
        FakeSource(_articles(3)),
        client,
        workers=1,
        state_reader=state.read,
        state_writer=state.write,
    )
    with pytest.raises(EmbedError, match="cannot open the article store"):
        open_source_store(config, client)


def test_run_embed_points_have_the_ids_and_payloads_the_lexical_build_gives_the_passages(
    tmp_path: Path,
) -> None:
    articles = [*_articles(5), make_article(9, "Disambig", is_disambiguation=True)]
    config, client = _config(tmp_path), memory_client()
    state = FakeStateStore()
    run_index(
        config,
        FakeSource(articles),
        client,
        workers=1,
        state_reader=state.read,
        state_writer=state.write,
    )
    _run(config, client, FakeSource(articles), _Embedder(), window_articles=2)
    lexical_points = _points_without_vectors(client, "wikipedia_lexical_20261001")
    lexical = [(p.id, p.payload) for p in lexical_points]
    semantic = [(p.id, p.payload) for p in _points(client, _COLLECTION)]
    assert semantic == lexical
    assert {payload["disambiguation"] for _, payload in semantic} == {True, False}


def _points_without_vectors(client: QdrantClient, name: str) -> list[Any]:
    """Every point of a collection with payload, ordered by id."""
    points, _ = client.scroll(name, limit=10_000, with_payload=True)
    return sorted(points, key=lambda point: int(point.id))
