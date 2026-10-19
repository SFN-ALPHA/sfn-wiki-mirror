"""Keyword search over the Wikipedia passage index, and windowed reads of whole articles.

The logic behind `POST /v1/wiki/search` and `GET /v1/wiki/article` (`wiki_mirror.api`),
and so behind the search and read tools every client of the mirror uses. Search asks Qdrant for
the best passages by the lexical vector, folds them to one result per article, puts an article
whose title is the whole query first, and shows each hit with a snippet taken from the article
store. Read serves one article's text a window at a time, so a long article reaches a model that
cannot take 100K characters in a reply.

Non-obvious constraints:

* **The passage payload carries no text.** A snippet is made by re-chunking the stored article
  (the chunker is deterministic) and taking chunk `n`; the index and the store therefore must
  describe the same text, and be cut by the same chunker. A `WikiSearch` is built over one store
  and queries the one collection its caller (`serving`) paired it with, a concrete
  `wikipedia_lexical_<born>` name and never the alias: while a rebuild's alias move is still
  waiting for `serving` to swap the store (up to `ALIAS_RECHECK_SECONDS`), the old store answers
  wholly from its own index. The API reports a differently-cut index on `/v1/wiki/health` as
  `degraded`.
* **A hit must be cut from the text the store holds.** Every passage carries the `text_hash` of
  the article text it was cut from; a hit whose hash differs from the stored article's is
  dropped with a warning (page id only), exactly like a hit the store lacks, because its snippet
  would be cut from text the citation does not point at. W1.5 has no way to produce one in
  normal running; during a W3 monthly update the index is a mix of two months' text, and W3
  will resolve such a hit against the previous month's store instead of dropping it (docs/DESIGN.md
  section 12.6).
* **An exact title beats any score.** A person who asks for "Photosynthesis" wants the article,
  and a lexical ranker can bury it under pages that merely repeat the word. `by_title` also
  resolves redirects, so "Einstein" finds "Albert Einstein".
* **Disambiguation pages rank after articles**, whatever their score: they answer "which X?"
  and are worth showing, but never ahead of the thing itself.
* **A query with no searchable term returns no results**, not an error: "the" or "???" is a
  question with an empty answer, and the caller can say so.
* **Reads are windows of `max_read_chars`.** A window ends on a paragraph or line break when
  one falls in its last 15%, so a reader is not handed half a sentence. An offset at or beyond
  the end returns an empty window with no next offset, never an error.

See docs/DESIGN.md sections 5, 6 and 12.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Final

from qdrant_client import QdrantClient, models

from wiki_mirror.config import MirrorConfig
from wiki_mirror.index import SPARSE_VECTOR
from wiki_mirror.passages import CHUNK_CONFIG
from wiki_mirror.records import Article, ArticleSource
from wiki_mirror.text.sanitize import chunk_text
from wiki_mirror.text.sparse import SparseEncoder

__all__ = ["ReadReply", "SearchReply", "SearchResult", "WikiSearch", "reply_as_dict"]

logger = logging.getLogger("wiki_mirror.search")

#: The retrieval mode named in every reply. W2 adds "hybrid"; a client reads this to say which
#: kind of search answered.
_MODE: Final = "lexical"

#: Passages fetched per result wanted. Several passages of one long article often fill the top
#: of a lexical ranking, so asking for exactly `limit` would return fewer than `limit` articles.
_OVERFETCH: Final = 4

#: Longest snippet, characters, before the ellipsis.
_SNIPPET_CHARS: Final = 320

#: The last share of a read window in which a paragraph or line break ends it early.
_BREAK_ZONE: Final = 0.15


@dataclass(frozen=True)
class SearchResult:
    """One article in a search reply, with its best-matching passage."""

    page_id: int
    revision_id: int
    title: str
    #: The section path of the matching passage; empty before the first heading.
    section: str
    snippet: str
    #: The lexical (BM25) score of the passage; 0.0 for an exact title match no passage hit.
    score: float
    #: The permanent link to the exact revision (`?oldid=`), what an answer cites.
    url: str
    disambiguation: bool


@dataclass(frozen=True)
class SearchReply:
    """The reply to `POST /v1/wiki/search`."""

    #: The snapshot's date, `YYYY-MM-DD`: how old the answer can be.
    snapshot: str
    mode: str
    results: tuple[SearchResult, ...]


@dataclass(frozen=True)
class ReadReply:
    """The reply to `GET /v1/wiki/article`: one window of one article's text."""

    page_id: int
    revision_id: int
    title: str
    snapshot: str
    revision_time: str
    url: str
    license: str
    offset: int
    #: Where the next window starts; None when this window reaches the end.
    next_offset: int | None
    total_chars: int
    text: str


@dataclass(frozen=True)
class _Hit:
    """The best passage of one article, as the index reported it."""

    page_id: int
    chunk: int
    section: str
    score: float
    disambiguation: bool
    #: `records.text_hash` of the article text the passage was cut from, as the index holds it.
    text_hash: int


class WikiSearch:
    """Search and windowed reads over one snapshot's article store and passage index."""

    def __init__(
        self,
        config: MirrorConfig,
        store: ArticleSource,
        client: QdrantClient,
        *,
        collection: str,
    ) -> None:
        """Wire the store and the index.

        Args:
            config: Service settings (`lexical_dimensions`, `max_read_chars`).
            store: The article store of one snapshot.
            client: A Qdrant client.
            collection: The concrete Qdrant collection to query (`wikipedia_lexical_<born>`),
                chosen by `serving` as the one whose index state names this store's snapshot.
                Not the alias, and not derived from `store`: a collection's name records its
                birth, while the snapshot it reflects is in its index state.
        """
        self._config = config
        self._store = store
        self._client = client
        self._encoder = SparseEncoder(dimensions=config.lexical_dimensions)
        # A concrete collection, not the alias: when a rebuild moves the alias, the holder in
        # `serving` swaps in a new store and a new WikiSearch within its recheck interval, and
        # until then this one keeps ranking with the index its store matches, so a snippet is
        # never cut from a different snapshot's text.
        self._collection = collection

    def search(self, query: str, limit: int) -> SearchReply:
        """Find the articles that best answer a keyword query.

        Args:
            query: What to look for. Blank, or with no searchable term, gives no results.
            limit: Most results to return (the API bounds it to 1..20).

        Returns:
            One result per article, best first: an exact title (or redirect) match, then
            articles by passage score, then disambiguation pages by passage score.

        Raises:
            qdrant_client.http.exceptions.UnexpectedResponse: Qdrant is up but refused the
                query (for instance, the store's collection was deleted under it).
            qdrant_client.http.exceptions.ResponseHandlingException: Qdrant is unreachable.
        """
        snapshot = self._store.snapshot.date
        text = query.strip()
        weights = self._encoder.encode_query(text) if text else {}
        if not weights:
            return SearchReply(snapshot=snapshot, mode=_MODE, results=())

        hits = self._best_passages(weights, limit * _OVERFETCH)
        exact = self._store.by_title(text)
        ordered = sorted(hits.values(), key=lambda hit: (hit.disambiguation, -hit.score))
        if exact is not None:
            ordered = [h for h in ordered if h.page_id != exact.page_id]
            ordered.insert(0, hits.get(exact.page_id) or _lead_hit(exact))

        results: list[SearchResult] = []
        for hit in ordered[:limit]:
            if exact is not None and hit.page_id == exact.page_id:
                article: Article | None = exact
            else:
                article = self._store.get(hit.page_id)
            if article is None:
                # The index names an article the store lacks: they are from different
                # snapshots, or the store is damaged. Dropping the hit is loud in the log and
                # honest in the reply; a result without text could not be cited anyway.
                logger.warning("search: page %d is in the index but not in the store", hit.page_id)
                continue
            if hit.text_hash != article.text_hash:
                # The passage was cut from other text than the store holds, so its number and
                # section would point into the wrong article text. Dropped like the case above;
                # W3 will resolve such a hit against the previous month's store instead.
                logger.warning(
                    "search: page %d has a passage cut from different text than the store holds",
                    hit.page_id,
                )
                continue
            results.append(_result(article, hit))
        return SearchReply(snapshot=snapshot, mode=_MODE, results=tuple(results))

    def read(self, *, page_id: int | None, title: str | None, offset: int) -> ReadReply | None:
        """Return one window of an article's text.

        Args:
            page_id: The article's page id, or None.
            title: The article's title (any spelling `by_title` resolves, redirects included),
                or None. Give exactly one of `page_id` and `title`; `page_id` wins if both.
            offset: Characters into the text where the window starts. Negative counts as 0; at
                or beyond the end gives an empty window with `next_offset` None.

        Returns:
            The window, or None when no article matches.
        """
        article = (
            self._store.get(page_id)
            if page_id is not None
            else self._store.by_title(title or "")
        )
        if article is None:
            return None
        text = article.text
        start = max(0, offset)
        if start >= len(text):
            return _window(article, self._store.snapshot.date, start, start, len(text), "")
        end = self._window_end(text, start)
        return _window(article, self._store.snapshot.date, start, end, len(text), text[start:end])

    def _best_passages(self, weights: dict[int, float], points: int) -> dict[int, _Hit]:
        """Query this store's collection and keep the best-scoring passage of each article.

        Qdrant returns points best first, so the first passage seen for an article is its best.
        A payload without `text_hash` (an index from before W1.5) raises `KeyError`, as one
        without `page_id` would: its snippets cannot be verified, and failing loudly beats
        serving them.
        """
        response = self._client.query_points(
            collection_name=self._collection,
            query=models.SparseVector(indices=list(weights.keys()), values=list(weights.values())),
            using=SPARSE_VECTOR,
            limit=points,
            with_payload=True,
        )
        hits: dict[int, _Hit] = {}
        for point in response.points:
            payload = point.payload or {}
            page_id = int(payload["page_id"])
            hits.setdefault(
                page_id,
                _Hit(
                    page_id=page_id,
                    chunk=int(payload["chunk"]),
                    section=str(payload.get("section", "")),
                    score=float(point.score),
                    disambiguation=bool(payload.get("disambiguation", False)),
                    text_hash=int(payload["text_hash"]),
                ),
            )
        return hits

    def _window_end(self, text: str, start: int) -> int:
        """Where the window starting at `start` ends: a break in its last 15%, else the limit."""
        limit = start + self._config.max_read_chars
        if limit >= len(text):
            return len(text)
        floor = limit - int(self._config.max_read_chars * _BREAK_ZONE)
        for separator in ("\n\n", "\n"):
            at = text.rfind(separator, floor, limit)
            if at != -1:
                return at + len(separator)
        return limit


def _lead_hit(article: Article) -> _Hit:
    """A hit for an article that matched by title only, pointing at its first passage."""
    return _Hit(
        page_id=article.page_id,
        chunk=0,
        section="",
        score=0.0,
        disambiguation=article.is_disambiguation,
        text_hash=article.text_hash,
    )


def _result(article: Article, hit: _Hit) -> SearchResult:
    """Build a result: the article's identity, the hit's section and score, a snippet."""
    return SearchResult(
        page_id=article.page_id,
        revision_id=article.revision_id,
        title=article.title,
        section=hit.section,
        snippet=_snippet(article, hit.chunk),
        score=hit.score,
        url=article.url,
        disambiguation=article.is_disambiguation,
    )


def _snippet(article: Article, chunk_index: int) -> str:
    """The text of one passage, whitespace collapsed and cut to a word boundary with an ellipsis.

    Re-chunks the article (deterministic, so chunk `n` here is passage `n` in the index). If
    the chunk is not there, or the article has no passages, the article's abstract stands in.
    """
    # The indexer's own chunk config (`index.CHUNK_CONFIG`), so passage `n` here is passage `n`
    # there; `serving` warns when an index was built with a different cut.
    chunks = chunk_text(article.text, CHUNK_CONFIG)
    source = next((c.text for c in chunks if c.index == chunk_index), None)
    if source is None:
        source = article.abstract or (chunks[0].text if chunks else article.text)
    return _cut(" ".join(source.split()), _SNIPPET_CHARS)


def _cut(text: str, limit: int) -> str:
    """Cut `text` to at most `limit` characters at a word boundary, adding an ellipsis if cut."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    if not text[limit].isspace():
        # The cut is inside a word: back up to the space before it, if there is one.
        head = head.rsplit(" ", 1)[0]
    return head.rstrip() + "…"


def _window(
    article: Article, snapshot: str, start: int, end: int, total: int, text: str
) -> ReadReply:
    """Wrap one window of an article's text in the read reply."""
    return ReadReply(
        page_id=article.page_id,
        revision_id=article.revision_id,
        title=article.title,
        snapshot=snapshot,
        revision_time=article.revision_time,
        url=article.url,
        license=article.license,
        offset=start,
        next_offset=end if end < total else None,
        total_chars=total,
        text=text,
    )


def reply_as_dict(reply: SearchReply | ReadReply) -> dict[str, Any]:
    """A reply as the plain JSON object the API sends (nested results included)."""
    return asdict(reply)
