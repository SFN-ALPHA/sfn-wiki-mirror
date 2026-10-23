"""Shared helpers for the mirror's tests: synthetic articles and in-memory fakes.

Imported by `tests/wiki_mirror/test_index.py`, `test_search.py`, `test_api.py`,
`test_serving.py`, `test_mcp_server.py` and `test_main.py` (a plain module, not a pytest plugin).
Provides an in-memory `ArticleSource` (`FakeSource`), articles shaped like the ones `parse`
produces (markdown `## Heading` sections, an `## Infobox` block of `label: value` lines, no
references), a Qdrant client in local mode, real finalized article stores written under a test's
`data_dir` (`write_store`, `build_snapshot`, `served_over`) for the tests that exercise the
holder that follows the alias, and a stand-in for the build-state functions of `build`. Also a
semantic index small enough to reason about (`add_semantic_index`, the real `run_embed` with a fake
model whose vectors count topic words) and `FakeEmbeddingServer`, an Ollama-shaped `/api/embed`
over `httpx.MockTransport` for the API's query embedding.

Non-obvious constraints:

* The article text is invented but follows the parsed shape documented in `records.Article` and
  seen in the real sample records; the real parse output is qualified by `test_parse.py`. Every
  article carries the real `records.text_hash` of its text, which the store checks on write, so
  an article built with different text (`make_article`) has a different hash, as it would live.
* A test snapshot is built by the same calls the build makes: a store created with
  `parse.PARSER_VERSION`, then `run_index`, which writes the collection's index state
  (`applied_snapshot`) that `serving` pairs the store with.
* `qdrant_client`'s local mode honours sparse vectors, `Modifier.IDF`, aliases and payload
  filters, so index and search tests run real Qdrant semantics without a server. It ignores
  payload indexes (the project's pytest filter already allows that warning).

See docs/DESIGN.md sections 5 and 12.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
from qdrant_client import QdrantClient

from wiki_mirror.config import MirrorConfig
from wiki_mirror.embed import collection_name as embed_collection_name
from wiki_mirror.embed import run_embed
from wiki_mirror.index import collection_name, run_index
from wiki_mirror.parse import PARSER_VERSION
from wiki_mirror.records import Article, SnapshotMeta, normalise_title, text_hash
from wiki_mirror.search import WikiSearch
from wiki_mirror.serving import ServedSnapshot
from wiki_mirror.store import ArticleStore, store_path

__all__ = [
    "API_KEY",
    "EMBEDDING_URL",
    "TOPIC_DIMS",
    "TOPIC_WORDS",
    "FakeEmbeddingServer",
    "FakeSource",
    "FakeState",
    "FakeStateStore",
    "StubServed",
    "TopicEmbedder",
    "add_semantic_index",
    "build_snapshot",
    "index_articles",
    "index_without_store",
    "make_article",
    "make_config",
    "make_snapshot",
    "memory_client",
    "served_over",
    "topic_vector",
    "write_store",
]

#: A bearer key long enough for `MirrorConfig` to accept.
API_KEY = "k" * 40

_FILLER = (
    "The committee reviewed the proposal and recorded its findings in the minutes of the "
    "meeting, noting the dates, the places and the people who had taken part in the work. "
)


def make_snapshot(date: str = "2026-10-01") -> SnapshotMeta:
    """A snapshot description with one chunk id."""
    return SnapshotMeta(
        identifier="enwiki_namespace_0",
        date=date,
        record_count=10,
        chunk_ids=("enwiki_namespace_0_chunk_0",),
    )


def make_config(tmp_path: Path, **overrides: object) -> MirrorConfig:
    """A config pointing at `tmp_path`, with an API key and a small hash space.

    Args:
        tmp_path: Becomes `data_dir`.
        **overrides: Any `MirrorConfig` field, replacing the test default.
    """
    fields: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "snapshot_dir": tmp_path / "snapshots",
        "lexical_dimensions": 2**18,
        "api_key": SecretStr(API_KEY),
        # The free-disk floor reads the machine the tests run on, which must not decide whether
        # they pass; the tests of the floor itself set it and fake the disk.
        "min_free_disk_gb": 0,
    }
    fields.update(overrides)
    return MirrorConfig.model_validate(fields)


def make_article(
    page_id: int,
    title: str,
    sections: dict[str, str] | None = None,
    *,
    revision_id: int | None = None,
    is_disambiguation: bool = False,
    redirects: tuple[str, ...] = (),
    abstract: str = "",
) -> Article:
    """An article shaped like `parse` output.

    Args:
        page_id: Wikipedia page id.
        title: Display title.
        sections: Heading to body text, in order; the empty heading is the lead. Default: a
            lead mentioning the title and an infobox.
        revision_id: Defaults to `page_id * 10 + 1`.
        is_disambiguation: Mark as a disambiguation page.
        redirects: Titles that redirect here.
        abstract: Enterprise-style summary.
    """
    body = sections or {
        "": f"{title} is a subject described in this article. {_FILLER}",
        "Infobox": f"Name: {title}\nType: Example",
    }
    parts = [f"## {heading}\n{text}" if heading else text for heading, text in body.items()]
    full_text = "\n\n".join(parts)
    return Article(
        page_id=page_id,
        title=title,
        revision_id=revision_id if revision_id is not None else page_id * 10 + 1,
        revision_time="2026-09-30T12:00:00Z",
        text=full_text,
        text_hash=text_hash(full_text),
        abstract=abstract,
        redirects=redirects,
        is_disambiguation=is_disambiguation,
    )


class FakeSource:
    """An in-memory `ArticleSource` over a list of articles, iterated in page-id order."""

    def __init__(self, articles: Iterable[Article], snapshot: SnapshotMeta | None = None) -> None:
        """Hold the articles (sorted by page id) and the snapshot they belong to."""
        self._articles = sorted(articles, key=lambda article: article.page_id)
        self._snapshot = snapshot or make_snapshot()

    @property
    def snapshot(self) -> SnapshotMeta:
        """The snapshot these articles belong to."""
        return self._snapshot

    def get(self, page_id: int) -> Article | None:
        """The article with this page id, or None."""
        return next((a for a in self._articles if a.page_id == page_id), None)

    def by_title(self, title: str) -> Article | None:
        """Exact display title, then case-insensitive, then a redirect."""
        wanted = normalise_title(title)
        if not wanted:
            return None
        for matches in (
            lambda a: a.title == wanted,
            lambda a: a.title.casefold() == wanted.casefold(),
            lambda a: wanted in a.redirects,
        ):
            found = next((a for a in self._articles if matches(a)), None)
            if found is not None:
                return found
        return None

    def iter_articles(self, after: int | None = None) -> Iterator[Article]:
        """Every article in page-id order, past `after` when given."""
        floor = -1 if after is None else after
        return iter([a for a in self._articles if a.page_id > floor])

    def count(self) -> int:
        """Number of articles."""
        return len(self._articles)


@dataclass(frozen=True)
class FakeState:
    """A stand-in for R1's `BuildState`: the fields the mirror tests read and write."""

    snapshot_date: str = ""
    phase: str = "idle"
    chunks_total: int = 0
    chunks_downloaded: int = 0
    chunks_parsed: int = 0
    passages_indexed: int = 0
    articles: int = 0
    error: str = ""
    updated_at: str = ""


class FakeStateStore:
    """Holds one `FakeState`; `read` and `write` match the build module's state functions."""

    def __init__(self, state: FakeState | None = None) -> None:
        """Start from `state`, or an idle one."""
        self.state = state or FakeState()
        #: Every phase written, in order, for tests that assert the build's progress.
        self.phases: list[str] = [self.state.phase]

    def read(self, data_dir: Path) -> FakeState:
        """Return the current state; `data_dir` is accepted to match the real signature."""
        del data_dir
        return self.state

    def write(self, data_dir: Path, state: FakeState) -> None:
        """Replace the current state."""
        del data_dir
        self.state = replace(state)
        if not self.phases or self.phases[-1] != state.phase:
            self.phases.append(state.phase)


def memory_client() -> QdrantClient:
    """A Qdrant client in local in-memory mode."""
    return QdrantClient(":memory:")


def index_articles(
    tmp_path: Path, articles: list[Article], **config: object
) -> tuple[WikiSearch, QdrantClient, FakeSource]:
    """Index `articles` into an in-memory Qdrant and return a search over them.

    Args:
        tmp_path: Becomes the config's `data_dir` parent (index state and build state files land
            there).
        articles: The articles to index.
        **config: `MirrorConfig` overrides.

    Returns:
        The search, the client holding the index, and the article source.
    """
    source = FakeSource(articles)
    client = memory_client()
    state = FakeStateStore()
    settings = make_config(tmp_path, **config)
    run_index(
        settings, source, client, workers=1, state_reader=state.read, state_writer=state.write
    )
    search = WikiSearch(settings, source, client, collection=collection_name(source.snapshot))
    return search, client, source


def write_store(
    config: MirrorConfig,
    articles: Iterable[Article],
    snapshot: SnapshotMeta | None = None,
    *,
    finalize: bool = True,
) -> Path:
    """Write a real article store under `config.data_dir` and close it.

    Args:
        config: Decides where the store lives (`store.store_path`).
        articles: What it holds.
        snapshot: The snapshot it records; the default test snapshot (2026-10-01) if None.
        finalize: False leaves it incomplete, as a build in progress would.

    Returns:
        The store file's path.
    """
    snapshot = snapshot or make_snapshot()
    path = store_path(config.data_dir, snapshot)
    store = ArticleStore.create(path, snapshot, parser_version=PARSER_VERSION)
    try:
        store.add_many(articles)
        if finalize:
            store.finalize()
    finally:
        store.close()
    return path


def build_snapshot(
    config: MirrorConfig,
    client: QdrantClient,
    articles: Iterable[Article],
    snapshot: SnapshotMeta | None = None,
) -> Path:
    """What a build leaves behind: a finalized store, indexed, the alias moved onto it.

    Args:
        config: Service settings of the test.
        client: The Qdrant client the index goes into.
        articles: The snapshot's articles.
        snapshot: Its description; the default test snapshot if None.

    Returns:
        The store file's path.
    """
    path = write_store(config, articles, snapshot)
    store = ArticleStore.open(path, readonly=True)
    state = FakeStateStore()
    try:
        run_index(
            config, store, client, workers=1, state_reader=state.read, state_writer=state.write
        )
    finally:
        store.close()
    return path


def index_without_store(
    config: MirrorConfig,
    client: QdrantClient,
    articles: Iterable[Article],
    snapshot: SnapshotMeta,
) -> None:
    """Index a snapshot and move the alias onto it while its article store is not (yet) there.

    The state a service can find itself in when the store of the snapshot the alias names is
    missing, still being built, or unreadable.

    Args:
        config: Service settings of the test.
        client: The Qdrant client the index goes into.
        articles: The snapshot's articles (indexed from memory, no store file involved).
        snapshot: Its description.
    """
    state = FakeStateStore()
    run_index(
        config,
        FakeSource(articles, snapshot),
        client,
        workers=1,
        state_reader=state.read,
        state_writer=state.write,
    )


def served_over(
    tmp_path: Path,
    articles: list[Article],
    *,
    clock: Callable[[], float],
    semantic: FakeEmbeddingServer | None = None,
    environ: Mapping[str, str] | None = None,
    **config: object,
) -> tuple[ServedSnapshot, QdrantClient, MirrorConfig]:
    """A snapshot holder over a real store and an in-memory index of `articles`.

    Args:
        tmp_path: Becomes the config's `data_dir` parent.
        articles: The default snapshot's articles.
        clock: The holder's clock (tests pass `frozen_clock.monotonic`).
        semantic: When given, the snapshot also gets a semantic index of the same articles
            (`add_semantic_index`), `embedding_url` is set, and the holder embeds its questions
            through this fake server.
        environ: The holder's process environment (where `EMBEDDING_API_KEY` is read); empty
            when None, whatever the machine running the tests has.
        **config: `MirrorConfig` overrides.

    Returns:
        The holder (nothing opened yet: its first look happens on first use), the client, and
        the config.
    """
    if semantic is not None:
        config = {"embedding_url": EMBEDDING_URL, "embedding_dimensions": TOPIC_DIMS, **config}
    settings = make_config(tmp_path, **config)
    client = memory_client()
    build_snapshot(settings, client, articles)
    if semantic is not None:
        add_semantic_index(settings, client, articles)
    return (
        ServedSnapshot(
            settings,
            client,
            clock=clock,
            environ=environ or {},
            embed_transport=semantic.transport if semantic is not None else None,
        ),
        client,
        settings,
    )


# -- a semantic index and an embedding server, small enough to reason about ------------------

#: A made-up embedding server address (the shared address space, nobody's own network).
EMBEDDING_URL = "http://100.64.0.9:8080"

#: The "topics" of the tests' fake model, one per vector component. A text's vector counts how
#: often each topic word occurs in it, so which passage a question lands nearest is plain to see.
TOPIC_WORDS = ("airship", "planet", "chlorophyll", "relativity")

#: Vector length of the fake model.
TOPIC_DIMS = len(TOPIC_WORDS)


def topic_vector(text: str) -> list[float]:
    """The fake model's vector for a text: topic-word counts, plus a little so none is zero."""
    lowered = text.lower()
    return [lowered.count(word) + 0.01 for word in TOPIC_WORDS]


class TopicEmbedder:
    """The bulk embedder of `run_embed`, backed by `topic_vector`."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per text, in order."""
        return [topic_vector(text) for text in texts]


class FakeEmbeddingServer:
    """An Ollama-shaped `/api/embed` over `httpx.MockTransport`, for the query embedding.

    Answers `{"embeddings": [[...]]}` (the keys the clients read; the existing embed tests use the
    same shape) with `topic_vector` of the input, or the vector a test chose for that exact text,
    so a question can be made to land near a passage that shares none of its words. A test makes
    it fail by setting `reply`.
    """

    def __init__(self, answers: Mapping[str, list[float]] | None = None) -> None:
        """Start answering, with `answers` (question to vector) overriding the topic vector."""
        self.answers: dict[str, list[float]] = dict(answers or {})
        #: Every request received, in order.
        self.requests: list[httpx.Request] = []
        #: When set, answered instead of a vector: a response, or an exception to raise.
        self.reply: httpx.Response | Exception | None = None
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        """Record the request and answer it."""
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        if self.reply is not None:
            return self.reply
        inputs = json.loads(request.content)["input"]
        vectors = [self.answers.get(text, topic_vector(text)) for text in inputs]
        return httpx.Response(200, json={"embeddings": vectors})


def add_semantic_index(
    config: MirrorConfig,
    client: QdrantClient,
    articles: Iterable[Article],
    snapshot: SnapshotMeta | None = None,
) -> str:
    """What `wiki-mirror embed-now` leaves behind: a complete semantic collection, alias and state.

    Runs the real `run_embed` over an in-memory source with the topic model, so the collection,
    its payloads, its point ids and its state file are the stage's own.

    Args:
        config: Service settings; `embedding_dimensions` must be `TOPIC_DIMS`.
        client: The Qdrant client the collection goes into (the one holding the lexical index).
        articles: The articles to embed.
        snapshot: Their snapshot; the default test snapshot if None.

    Returns:
        The collection's name (`wikipedia_semantic_<born>`).
    """
    source = FakeSource(articles, snapshot)
    # In a thread of its own: an async test (the MCP door's) already has a running event loop,
    # and `asyncio.run` refuses to start inside one.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(
            asyncio.run, run_embed(config, source, client, TopicEmbedder())
        ).result()
    return embed_collection_name(source.snapshot.tag)


class StubServed:
    """A snapshot holder double: serves a given search (or nothing) without any store.

    For the tests that drive the error paths of the routes with a search that raises.
    """

    def __init__(self, search: object | None, *, alias: str | None = None) -> None:
        """Serve `search`, or nothing when it is None."""
        self._search = search
        self.alias = alias
        self.problem: str | None = None

    @contextmanager
    def lease(self, *, fresh: bool = False) -> Iterator[SimpleNamespace | None]:
        """Yield an object with the stub search, or None when there is no search."""
        del fresh
        yield None if self._search is None else SimpleNamespace(search=self._search)
