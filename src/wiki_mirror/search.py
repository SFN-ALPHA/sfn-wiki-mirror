"""Search over the Wikipedia passage indexes in three modes, and windowed reads of whole articles.

The logic behind `POST /v1/wiki/search` and `GET /v1/wiki/article` (`wiki_mirror.api`),
and so behind the search and read tools every client of the mirror uses. Search asks Qdrant for
the best passages by the lexical vector (`lexical`), by the semantic vector of the question
(`semantic`), or by both fused with reciprocal rank fusion (`hybrid`); folds them to one result per
article, puts an article whose title is the whole query first, and shows each hit with a snippet
taken from the article store. Read serves one article's text a window at a time, so a long
article reaches a model that cannot take 100K characters in a reply.

Non-obvious constraints:

* **One result shape, whatever the mode.** The two collections share point ids and carry the same
  payload (`embed` module docstring), so a hit from either is a `_Hit` and goes through the same
  fold, ordering, store and `text_hash` checks, and `_result`. A client reads the same fields
  from every reply; only `score` means something different in each (see `SearchResult.score`),
  and the reply's `mode` names the one that answered.
* **No silent fallback.** A `semantic` or `hybrid` request that cannot be served (no semantic index
  paired with the store being served, or the question could not be embedded) raises
  `SemanticUnavailableError`. It never answers with keyword results under another mode's name
  (docs/DECISIONS.md D10).
* **Hybrid fuses passages, keyed on (point id, `text_hash`)**, then folds to the best fused
  passage per article. A passage updated in one index and not yet in the other has the same
  point id and a different `text_hash` in the two lists; keying on both means the two are never
  merged into one score (docs/ROADMAP.md W2 item 5, docs/DESIGN.md section 12.6).

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
  question with an empty answer, and the caller can say so. That is the lexical mode's rule; any
  non-blank text can be embedded, so `semantic` has no such case, and `hybrid` with no
  searchable term answers from its semantic list alone and still says `hybrid`.
* **Reads are windows of `max_read_chars`.** A window ends on a paragraph or line break when
  one falls in its last 15%, so a reader is not handed half a sentence. An offset at or beyond
  the end returns an empty window with no next offset, never an error.

See docs/DESIGN.md sections 5, 6 and 12.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from typing import Any, Final, Literal, get_args

from qdrant_client import QdrantClient, models

from wiki_mirror.config import MirrorConfig
from wiki_mirror.index import SPARSE_VECTOR
from wiki_mirror.passages import CHUNK_CONFIG
from wiki_mirror.query_embed import QueryEmbedError
from wiki_mirror.records import Article, ArticleSource
from wiki_mirror.text.sanitize import chunk_text
from wiki_mirror.text.sparse import SparseEncoder

__all__ = [
    "SEARCH_MODES",
    "ReadReply",
    "SearchMode",
    "SearchReply",
    "SearchResult",
    "SemanticBinding",
    "SemanticUnavailableError",
    "WikiSearch",
    "reply_as_dict",
]

logger = logging.getLogger("wiki_mirror.search")

#: How a search ranks: `lexical` (BM25 over the keyword vector; the default, docs/ROADMAP.md W2
#: item 5), `semantic` (cosine similarity of the question's embedding) or `hybrid` (both, fused).
SearchMode = Literal["lexical", "semantic", "hybrid"]

#: The modes, in the order the API names them.
SEARCH_MODES: Final[tuple[str, ...]] = get_args(SearchMode)

#: The modes that need the semantic index.
_SEMANTIC_MODES: Final = frozenset({"semantic", "hybrid"})

#: Passages fetched per result wanted. Several passages of one long article often fill the top
#: of a lexical ranking, so asking for exactly `limit` would return fewer than `limit` articles.
_OVERFETCH: Final = 4

#: Reciprocal rank fusion's constant: a passage at rank `r` adds `weight / (_RRF_K + r)`. 60 is
#: the value of the original RRF paper (Cormack, Clarke and Buettcher, 2009) and the one the
#: design names (docs/DESIGN.md section 12.5); it is not tuned.
_RRF_K: Final = 60

#: Fusion weights of the two lists, equal for now. They are untuned on purpose: on the evaluation
#: sample equal-weight fusion's hit@1 (0.713) trailed dense alone (0.823) with hard negatives, and
#: docs/ROADMAP.md W2 item 6 requires a weight check on the golden set at full scale before hybrid
#: becomes the default. Until then `lexical` stays the default and these are named, not guessed.
_LEXICAL_WEIGHT: Final = 1.0
_SEMANTIC_WEIGHT: Final = 1.0

#: The reason given for a fresh `WikiSearch` that `serving` has not yet paired with a semantic
#: index. It lasts as long as it takes the holder to look (milliseconds after a snapshot swap).
_SEMANTIC_NOT_CHECKED: Final = (
    "the semantic index has not been checked for this snapshot yet; try again shortly"
)

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
    #: The passage's score in the mode that answered: the lexical BM25 score, the semantic cosine
    #: similarity, or the hybrid reciprocal-rank-fusion sum. The three are not comparable with
    #: each other (the reply's `mode` says which applies); within one reply, higher is better.
    #: 0.0 for an exact title match no passage hit.
    score: float
    #: The permanent link to the exact revision (`?oldid=`), what an answer cites.
    url: str
    disambiguation: bool


@dataclass(frozen=True)
class SearchReply:
    """The reply to `POST /v1/wiki/search`."""

    #: The snapshot's date, `YYYY-MM-DD`: how old the answer can be.
    snapshot: str
    #: The mode that answered, always the one asked for: `lexical`, `semantic` or `hybrid`.
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


class SemanticUnavailableError(Exception):
    """A `semantic` or `hybrid` search cannot be served; the message is the plain reason.

    Attributes:
        reason: A plain sentence for the caller. It holds no key, no response body and no query.
        kind: A short word for logs: `unpaired` (no usable semantic index for the snapshot being
            served), or a `query_embed.QueryEmbedError` kind (`timeout`, `unreachable`, ...).
    """

    def __init__(self, reason: str, kind: str) -> None:
        """Record the reason and its kind."""
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


@dataclass(frozen=True)
class SemanticBinding:
    """What a `WikiSearch` needs to answer `semantic` and `hybrid`: a collection and an embedder."""

    #: The concrete semantic collection (`wikipedia_semantic_<born>`), never the alias, for the
    #: reason `WikiSearch` queries a concrete lexical collection.
    collection: str
    #: Embeds one question (and the search request's id, for the embedding server's log) with the
    #: model the collection was built with; raises `QueryEmbedError`.
    embed_query: Callable[[str, str | None], list[float]]


@dataclass(frozen=True)
class _Hit:
    """A passage as an index reported it (and, once folded, the best passage of its article)."""

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
        # The semantic side: a binding when `serving` found a semantic index that reflects this
        # store's snapshot, else the reason it did not. One tuple, replaced whole, so a request
        # on another thread never sees a binding with a stale reason (assignment is atomic).
        self._semantic: tuple[SemanticBinding | None, str | None] = (None, _SEMANTIC_NOT_CHECKED)

    def pair_semantic(self, binding: SemanticBinding) -> None:
        """Allow `semantic` and `hybrid` searches, answered from `binding`'s collection.

        Called by `serving` when the semantic index reflects the snapshot of this search's store.
        """
        self._semantic = (binding, None)

    def unpair_semantic(self, reason: str) -> None:
        """Refuse `semantic` and `hybrid` searches with `reason` (a plain sentence).

        Called by `serving` when there is no semantic index that can be trusted with this
        store's snapshot.
        """
        self._semantic = (None, reason)

    @property
    def semantic_reason(self) -> str | None:
        """Why `semantic` and `hybrid` cannot be served now, or None when they can."""
        binding, reason = self._semantic
        return None if binding is not None else reason

    @property
    def modes(self) -> tuple[str, ...]:
        """The modes a search can use right now: `lexical`, and the other two when paired."""
        return SEARCH_MODES if self._semantic[0] is not None else SEARCH_MODES[:1]

    def search(
        self,
        query: str,
        limit: int,
        mode: SearchMode = "lexical",
        *,
        request_id: str | None = None,
    ) -> SearchReply:
        """Find the articles that best answer a query, ranked by `mode`.

        Args:
            query: What to look for. Blank gives no results. In `lexical` mode, a query with no
                searchable term gives none either.
            limit: Most results to return (the API bounds it to 1..20).
            mode: `lexical` (keywords), `semantic` (the question's meaning) or `hybrid` (both,
                fused). The reply names the mode that answered, which is always this one.
            request_id: The id of the request this search serves, forwarded to the embedding
                server as `X-Request-ID` in `semantic` and `hybrid` modes. Not used otherwise.

        Returns:
            One result per article, best first: an exact title (or redirect) match, then
            articles by passage score, then disambiguation pages by passage score. The shape is
            the same in every mode; `score` is the mode's own (`SearchResult.score`).

        Raises:
            SemanticUnavailableError: `mode` is `semantic` or `hybrid` and there is no semantic
                index paired with this store, or the question could not be embedded. Never
                answered with keyword results instead.
            qdrant_client.http.exceptions.UnexpectedResponse: Qdrant is up but refused the
                query (for instance, the store's collection was deleted under it).
            qdrant_client.http.exceptions.ResponseHandlingException: Qdrant is unreachable.
        """
        snapshot = self._store.snapshot.date
        binding, reason = self._semantic
        if mode in _SEMANTIC_MODES and binding is None:
            raise SemanticUnavailableError(reason or _SEMANTIC_NOT_CHECKED, "unpaired")
        text = query.strip()
        if not text:
            return SearchReply(snapshot=snapshot, mode=mode, results=())

        points = limit * _OVERFETCH
        weights = self._encoder.encode_query(text) if mode != "semantic" else {}
        if mode == "lexical" and not weights:
            return SearchReply(snapshot=snapshot, mode=mode, results=())
        # One after the other, not in a thread pool: the embedding is 25-70 ms and the two Qdrant
        # queries are each far shorter than a thread hand-off is worth to reason about, until a
        # measurement on the full-size index says otherwise. The embedding comes first so that a
        # server that is down fails the search before any Qdrant work is done.
        semantic = (
            self._dense_passages(binding, text, points, request_id)
            if binding is not None and mode != "lexical"
            else {}
        )
        lexical = self._lexical_passages(weights, points) if weights else {}
        if mode == "lexical":
            passages = list(lexical.values())
        elif mode == "semantic":
            passages = list(semantic.values())
        else:
            passages = _fuse(lexical, semantic)

        hits = _best_per_article(passages)
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
        return SearchReply(snapshot=snapshot, mode=mode, results=tuple(results))

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

    def _lexical_passages(self, weights: dict[int, float], points: int) -> dict[int, _Hit]:
        """The best `points` passages of the keyword collection for an encoded query."""
        return self._ranked_passages(
            self._collection,
            models.SparseVector(indices=list(weights.keys()), values=list(weights.values())),
            SPARSE_VECTOR,
            points,
        )

    def _dense_passages(
        self, binding: SemanticBinding, text: str, points: int, request_id: str | None
    ) -> dict[int, _Hit]:
        """The best `points` passages of the semantic collection for a question.

        Args:
            binding: The paired semantic collection and its embedder.
            text: The question.
            points: Most passages to fetch.
            request_id: Forwarded to the embedding server, or None.

        Raises:
            SemanticUnavailableError: The question could not be embedded. Only its kind and the
                embedder's plain sentence are kept; the question is not.
        """
        try:
            vector = binding.embed_query(text, request_id)
        except QueryEmbedError as exc:
            raise SemanticUnavailableError(str(exc), exc.kind) from exc
        # The collection's one vector is unnamed (`embed._create_collection`), so no `using`.
        return self._ranked_passages(binding.collection, vector, None, points)

    def _ranked_passages(
        self,
        collection: str,
        query: models.SparseVector | list[float],
        using: str | None,
        points: int,
    ) -> dict[int, _Hit]:
        """Ask one collection for its best passages, best first, keyed by point id.

        The two collections share point ids and payload fields, so one reader serves both. A
        payload without `text_hash` (an index from before W1.5) raises `KeyError`, as one without
        `page_id` would: its snippets cannot be verified, and failing loudly beats serving them.

        Args:
            collection: The concrete collection to query.
            query: A sparse vector for the keyword collection, a dense one for the semantic.
            using: The named vector to search (`SPARSE_VECTOR`), or None for the unnamed one.
            points: Most passages to fetch.

        Returns:
            Passage hits in rank order (a dict keeps insertion order), the first being the best.
        """
        response = self._client.query_points(
            collection_name=collection,
            query=query,
            using=using,
            limit=points,
            with_payload=True,
        )
        passages: dict[int, _Hit] = {}
        for point in response.points:
            payload = point.payload or {}
            passages[int(point.id)] = _Hit(
                page_id=int(payload["page_id"]),
                chunk=int(payload["chunk"]),
                section=str(payload.get("section", "")),
                score=float(point.score),
                disambiguation=bool(payload.get("disambiguation", False)),
                text_hash=int(payload["text_hash"]),
            )
        return passages

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


def _best_per_article(passages: Iterable[_Hit]) -> dict[int, _Hit]:
    """Keep the first passage seen for each article.

    `passages` is in rank order (best first), so the first passage seen for an article is its
    best.
    """
    best: dict[int, _Hit] = {}
    for passage in passages:
        best.setdefault(passage.page_id, passage)
    return best


def _fuse(lexical: dict[int, _Hit], semantic: dict[int, _Hit]) -> list[_Hit]:
    """Fuse two ranked passage lists with weighted reciprocal rank fusion.

    Each passage scores `weight / (_RRF_K + rank)` in every list it appears in, with ranks from 1,
    and the scores add. A passage is identified by (point id, `text_hash`), not by point id
    alone: if a passage was updated in one index and not yet in the other, its text differs and
    so does its hash, and the two versions must not share a score (module docstring). The hash
    is part of the key, so such a passage is two entries, each ranked on the evidence of its own
    index.

    Args:
        lexical: The keyword list, best first, by point id (empty when the query had no
            searchable term; the fusion is then the semantic list alone).
        semantic: The dense list, best first, by point id.

    Returns:
        One hit per (point id, `text_hash`), best fused score first, carrying the fused score as
        its `score`. Equal scores keep the keyword list's order first, then the dense list's.
    """
    fused: dict[tuple[int, int], _Hit] = {}
    for hits, weight in ((lexical, _LEXICAL_WEIGHT), (semantic, _SEMANTIC_WEIGHT)):
        for rank, (point_id, hit) in enumerate(hits.items(), start=1):
            key = (point_id, hit.text_hash)
            share = weight / (_RRF_K + rank)
            seen = fused.get(key)
            # A new entry takes this list's hit: the payload fields besides the score are the
            # same in both lists for the same point id and `text_hash`.
            fused[key] = replace(seen or hit, score=(seen.score if seen else 0.0) + share)
    # `sorted` is stable, so ties keep insertion order (keyword list first).
    return sorted(fused.values(), key=lambda hit: -hit.score)


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
