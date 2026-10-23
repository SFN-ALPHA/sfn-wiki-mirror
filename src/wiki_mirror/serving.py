"""The snapshot being served: which article store is open, and following the alias to the next.

A full build ends by pointing the Qdrant alias `wikipedia_lexical` at a new collection
(`index.run_index`); the collection's index state names the snapshot it reflects
(`index.applied_snapshot`), and the article store of that snapshot is a file the API opens
read-only. `ServedSnapshot` keeps the three paired while the service runs: on a request, at most
every `ALIAS_RECHECK_SECONDS`, it asks Qdrant where the alias points and the index state which
snapshot that collection reflects, and when that pair (collection, applied snapshot) is not the
one it holds it opens the store of the applied snapshot and swaps it in. So a full rebuild
reaches the API with no restart (and the first build reaches a service that started with
nothing), and so will W3's monthly update, which moves `applied_snapshot` in place under an
unchanged alias (docs/DESIGN.md section 12.6). `api.py` and `mcp_server.py` take what they serve
from `lease()`.

Non-obvious constraints:

* **A request holds a lease on the snapshot it reads.** `ArticleStore.close()` takes the
  store's lock, but the lock only serialises calls: a call made after `close()` fails on the
  closed connection. So a swapped-out snapshot is *retired*, not closed, and its store is closed
  by whoever ends the last lease on it (the swapper itself if none is open). A request that
  began on the old snapshot finishes on it, and a request that begins after the swap reads the
  new one; nothing closes a store under a reader and nothing leaks one.
* **All bookkeeping is under one short lock; nothing slow is.** Asking Qdrant and opening a
  store happen outside it. Only one thread refreshes at a time and the others carry on with the
  current snapshot instead of queueing behind it.
* **A refresh that fails changes nothing but the report.** If Qdrant cannot be asked, the
  aliased collection has no usable index state (missing, unreadable or unfinished: there is no
  telling which store it matches, and guessing "the newest" could serve text the passages were
  not cut from), or the store of the applied snapshot is missing, incomplete or unreadable, the
  old snapshot keeps serving and `problem` says why (health then reads `degraded`, and the log
  has the detail once). The next try is one interval later, not on every request. A service
  with nothing yet serves `None`, which the routes answer as `503 not_ready`, and retries the
  same way.
* **The pair is keyed on (collection, applied snapshot), not on the collection alone.** An
  in-place update changes which snapshot a collection reflects while its name and the alias stay
  put; keyed on the name, the API would keep reading last month's store against this month's
  passages until restarted.
* **The store is chosen by what is recorded in it, not by its file name.** The applied snapshot
  and the store file are tied by the snapshot recorded in the store's own `meta` row
  (`ArticleStore.snapshot.tag`), the thing that must match for a snippet to be cut from the
  text that was indexed. Opening a candidate costs a connection and three small queries; the
  newest file is tried first, which is the applied snapshot's after any normal build.
* **Between the alias moving and the next refresh (at most one interval) the old snapshot
  answers wholly from its own index.** A `WikiSearch` queries the collection it was paired with
  (a concrete `wikipedia_lexical_<born>` name, not the alias), so the ranking, the article text
  and the snippet all come from one snapshot and the answer is internally consistent. The old
  collection is still there to be asked: a build keeps the one it replaced
  (`index._swap_alias`).
* **The semantic index is paired with the served store the same way, on the same cadence, and
  never raises.** After each look at the lexical alias, `_follow_semantic` follows the
  `wikipedia_semantic` alias and gives the served `WikiSearch` either a `SemanticBinding` or a
  plain reason why not. The semantic index is usable only when all of these hold: the alias
  exists; its state file is readable and `complete`; its `applied_snapshot` is the served
  store's; it was cut by this code's chunker; and `embedding_url` is set. Otherwise `semantic`
  and `hybrid` searches answer 503 `semantic_unavailable` with that reason, never keyword
  results. This matters at the monthly build: the lexical index moves to the new snapshot at
  once and the semantic one stays on the old until the catch-up (docs/ROADMAP.md), and a
  semantic hit's snippet would be cut from the wrong month's text. The pairing reads only local
  state and Qdrant and never calls the embedding server, so a down embedding server cannot
  slow a look or turn a pairing off: it fails the one search that needs it. A semantic problem
  is not `problem`: nothing depends on the semantic index by default, so health stays `ok`.
  If Qdrant cannot be asked, the previous verdict stands (as for the lexical alias).
* **The index must have been cut by this code's chunker.** The index state a build leaves
  records `index.chunker_fingerprint()`; when the served collection's differs from the running
  code's, passage numbers no longer line up with the stored text and snippets would be cut from
  the wrong place. That is logged as a warning when the snapshot is opened and kept in `problem`,
  so health reads `degraded` until the index is rebuilt. An index state that predates
  fingerprints records none, which is no verdict (it is not called a mismatch).

See docs/DESIGN.md sections 5, 6 and 12.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field

import httpx
from qdrant_client import QdrantClient

from wiki_mirror.config import MirrorConfig
from wiki_mirror.embed import ALIAS as SEMANTIC_ALIAS
from wiki_mirror.embed import semantic_index_info
from wiki_mirror.index import (
    aliased_collection,
    applied_snapshot,
    chunker_fingerprint,
    indexed_chunker,
)
from wiki_mirror.query_embed import QueryEmbedder
from wiki_mirror.search import SemanticBinding, WikiSearch
from wiki_mirror.store import ArticleStore, StoreError

__all__ = [
    "ALIAS_RECHECK_SECONDS",
    "ServedSnapshot",
    "Serving",
    "open_store_for_snapshot",
]

logger = logging.getLogger("wiki_mirror.serving")

#: Longest between two looks at the alias, seconds (docs/DECISIONS.md D2): a new snapshot is
#: served within this long of the build's swap, and Qdrant is asked about the alias about twice a
#: minute at most.
ALIAS_RECHECK_SECONDS = 30.0

#: What `ServedSnapshot.problem` says when the served index was cut by another chunker.
_CHUNKER_PROBLEM = "the served index was built with a different chunker than this code's"

#: Why semantic search is off when Qdrant could not be asked about the semantic alias and there is
#: no earlier verdict to keep.
_SEMANTIC_QDRANT_DOWN = "Qdrant could not be asked where the semantic alias points"


@dataclass(frozen=True)
class _SemanticPairing:
    """A semantic collection usable with the snapshot being served, and what to embed with."""

    #: The concrete collection the alias pointed at (`wikipedia_semantic_<born>`).
    collection: str
    #: The model and vector length the collection's state records: what a question is embedded
    #: with, not the settings' `embedding_model` (`query_embed`).
    model: str
    dimensions: int


@dataclass(eq=False)
class Serving:
    """One snapshot, open for reading: its article store and the search over it."""

    store: ArticleStore
    search: WikiSearch
    #: The Qdrant collection this snapshot is indexed in, e.g. `wikipedia_lexical_20261001`.
    collection: str
    #: The snapshot tag the collection's index state named when this pair was opened; the
    #: `store` is that snapshot's. With `collection`, it is what the pair is keyed on.
    applied_snapshot: str
    #: The index was cut by a different chunker than the running code's (module docstring).
    chunker_mismatch: bool = False
    # Owned by `ServedSnapshot` and touched only under its lock.
    _leases: int = field(default=0, init=False, repr=False)
    _retired: bool = field(default=False, init=False, repr=False)


def open_store_for_snapshot(config: MirrorConfig, tag: str) -> ArticleStore:
    """Open, read-only, the article store of one snapshot.

    Args:
        config: Service settings (`data_dir`, `snapshot_identifier`).
        tag: The snapshot tag (`SnapshotMeta.tag`), usually a collection's `applied_snapshot`.

    Returns:
        The open store: complete (a read-only open refuses anything else) and recording that
        snapshot. The caller closes it.

    Raises:
        StoreError: No complete store under `data_dir/articles` records that snapshot.
    """
    directory = config.data_dir / "articles"
    candidates = sorted(directory.glob(f"{config.snapshot_identifier}_*.db"), reverse=True)
    unusable = 0
    for path in candidates:
        try:
            store = ArticleStore.open(path, readonly=True)
        except (StoreError, ValueError, sqlite3.Error):
            # An incomplete store (a build in progress) or a damaged file must not hide a good
            # one; it is counted so the error below can say how many were passed over.
            unusable += 1
            continue
        if store.snapshot.identifier == config.snapshot_identifier and store.snapshot.tag == tag:
            return store
        store.close()
    msg = (
        f"no complete article store for snapshot {tag} in {directory} "
        f"({len(candidates)} files, {unusable} unusable)"
    )
    raise StoreError(msg)


class ServedSnapshot:
    """The article store and search being served now, following the Qdrant alias."""

    def __init__(
        self,
        config: MirrorConfig,
        client: QdrantClient,
        *,
        clock: Callable[[], float] = time.monotonic,
        refresh_seconds: float = ALIAS_RECHECK_SECONDS,
        environ: Mapping[str, str] | None = None,
        embed_transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Hold nothing yet; the first `lease()` or `refresh()` opens what the alias names.

        Args:
            config: Service settings.
            client: The Qdrant client, asked where the alias points and given to each search.
            clock: Monotonic seconds, for the recheck interval; tests inject theirs.
            refresh_seconds: Longest between two looks at the alias.
            environ: The process environment, where the API's own `EMBEDDING_API_KEY` for query
                embedding is read; `os.environ` when None. Tests inject theirs.
            embed_transport: Test seam for the query embedding's HTTP. Never set in production.
        """
        self._config = config
        self._client = client
        self._clock = clock
        self._interval = refresh_seconds
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._embed_transport = embed_transport
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._serving: Serving | None = None
        self._alias: str | None = None
        self._problem: str | None = None
        # The last semantic verdict and the snapshot it was given for: a pairing, or the reason
        # there is none. Touched only under the refresh lock, so it needs no other.
        self._semantic_verdict: tuple[Serving, _SemanticPairing | str] | None = None
        self._closed = False
        # Negative infinity: the first request always looks.
        self._next_check = float("-inf")

    @property
    def alias(self) -> str | None:
        """What the alias pointed at when last asked; None if it did not exist or was not asked."""
        return self._alias

    @property
    def problem(self) -> str | None:
        """Why serving is not fully healthy, or None when all is well.

        Either what the alias names is not what is being served (Qdrant cannot be asked, the
        collection has no usable index state, or the store of its applied snapshot is missing or
        unreadable), or the snapshot being served was indexed by a different chunker.
        """
        if self._problem is not None:
            return self._problem
        serving = self._serving
        return _CHUNKER_PROBLEM if serving is not None and serving.chunker_mismatch else None

    @contextlib.contextmanager
    def lease(self, *, fresh: bool = False) -> Iterator[Serving | None]:
        """Borrow the current snapshot for the length of one request.

        Looks at the alias first if the interval has passed. The snapshot stays open until the
        lease ends even if a swap happens meanwhile.

        Args:
            fresh: Look at the alias now whatever the interval (health uses this, so a report
                never says `degraded` merely because the next scheduled look is still ahead).

        Yields:
            The snapshot, or None when there is nothing to serve yet.
        """
        self._look(force=fresh)
        with self._lock:
            serving = None if self._closed else self._serving
            if serving is not None:
                serving._leases += 1
        try:
            yield serving
        finally:
            if serving is not None:
                self._release(serving)

    def refresh(self) -> None:
        """Look at the alias now, waiting for a look already in progress; never raises.

        What `serve` calls once at start so the first request is answered from a warm store.
        """
        with self._refresh_lock:
            self._next_check = self._clock() + self._interval
            self._follow_aliases()

    def close(self) -> None:
        """Stop serving; the open store is closed as soon as no request is using it."""
        with self._lock:
            self._closed = True
            old, self._serving = self._serving, None
            if old is not None:
                old._retired = True
            close_now = old is not None and old._leases == 0
        if old is not None and close_now:
            old.store.close()

    def _look(self, *, force: bool) -> None:
        """Follow the alias if the interval has passed (or `force`), unless another thread is."""
        if self._closed or (not force and self._clock() < self._next_check):
            return
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            # Set before the work, so a failing look is repeated after the interval and not on
            # every request while Qdrant or the disk is struggling.
            self._next_check = self._clock() + self._interval
            self._follow_aliases()
        finally:
            self._refresh_lock.release()

    def _follow_aliases(self) -> None:
        """Follow the lexical alias, then pair the semantic index with what is served; no raise.

        In this order because the semantic index is judged against the snapshot being served,
        which the first step may just have changed.
        """
        self._follow_alias()
        self._follow_semantic()

    def _follow_alias(self) -> None:
        """Ask where the alias points and make the served snapshot match; never raises."""
        try:
            alias = aliased_collection(self._client)
        except Exception as exc:
            # Deliberately broad: the Qdrant client raises its own family of errors for a server
            # that is down, restarting or refusing, and a look must not break the request that
            # triggered it. The old snapshot keeps serving.
            self._note_problem("Qdrant could not be asked where the alias points", exc)
            return
        self._alias = alias
        current = self._serving
        if alias is None:
            if current is None:
                self._clear_problem()  # nothing built yet: not a fault, health says so
            else:
                self._note_problem("the alias no longer exists", None)
            return
        applied = applied_snapshot(self._config.data_dir, alias)
        if applied is None:
            # Not "use the newest store": the passages were cut from one specific snapshot's
            # text, and a store of another would give wrong snippets and citations.
            self._note_problem(
                f"{alias} has no usable index state (state/index_lexical_<born>.json is missing,"
                " unreadable or unfinished), so the article store it matches is unknown; restore"
                " the file, or delete the collection and run `wiki-mirror build-now`",
                None,
            )
            return
        served_pair = None if current is None else (current.collection, current.applied_snapshot)
        if served_pair == (alias, applied):
            self._clear_problem()
            return
        try:
            store = open_store_for_snapshot(self._config, applied)
        except Exception as exc:
            # Deliberately broad, for the same reason: whatever is wrong with the new store,
            # the snapshot that is open keeps serving.
            self._note_problem(
                f"the article store for snapshot {applied} (collection {alias}) cannot be opened",
                exc,
            )
            return
        self._swap_in(
            Serving(
                store,
                WikiSearch(self._config, store, self._client, collection=alias),
                alias,
                applied,
                chunker_mismatch=self._chunker_differs(alias),
            )
        )

    def _chunker_differs(self, collection: str) -> bool:
        """Whether the index behind `collection` was cut by another chunker than the running one.

        Logs the finding (once per snapshot opened, since it is called when one is). No recorded
        fingerprint gives no verdict, so it is `False`: an index state that predates fingerprints
        (or lacks the field) must not turn a working service `degraded`.
        """
        built = indexed_chunker(self._config.data_dir, collection)
        running = chunker_fingerprint()
        if built is None:
            logger.info(
                "no chunker fingerprint is recorded for %s; cannot check its passages line up",
                collection,
                extra={"component": "serving", "event": "chunker_unknown"},
            )
            return False
        if built == running:
            return False
        logger.warning(
            "%s was indexed with chunker %s but this code cuts with %s: snippets and passage "
            "numbers may not line up; rebuild the index",
            collection,
            built,
            running,
            extra={"component": "serving", "event": "chunker_mismatch"},
        )
        return True

    def _follow_semantic(self) -> None:
        """Give the served search its semantic verdict: a pairing, or the reason there is none.

        Never raises. A verdict that has not changed for the snapshot being served is left alone
        (and not logged again), so a look every half minute costs one alias lookup and one small
        file read. With nothing served there is nothing to pair; a snapshot opened later starts
        unpaired and is judged on the look that opened it.
        """
        current = self._serving
        if current is None:
            return
        try:
            verdict = self._semantic_for(current)
        except Exception:
            # Deliberately broad, as for the lexical alias: the Qdrant client raises its own
            # family of errors for a server that is down, restarting or refusing. Not logged
            # here: the lexical look, which asks Qdrant the same kind of question just before,
            # logs a failure once (`_note_problem`). An earlier verdict for this snapshot stands,
            # so a hiccup does not switch semantic search off for half a minute; with none, the
            # reason says why.
            if self._semantic_verdict is not None and self._semantic_verdict[0] is current:
                return
            verdict = _SEMANTIC_QDRANT_DOWN
        if self._semantic_verdict == (current, verdict):
            return
        self._semantic_verdict = (current, verdict)
        if isinstance(verdict, str):
            current.search.unpair_semantic(verdict)
            logger.info(
                "semantic and hybrid search are unavailable: %s",
                verdict,
                extra={"component": "serving", "event": "semantic_unpaired"},
            )
            return
        # The model and length are the index's own, from its state (`query_embed` says why).
        embedder = QueryEmbedder.from_config(
            self._config,
            self._environ,
            model=verdict.model,
            dimensions=verdict.dimensions,
            transport=self._embed_transport,
        )
        current.search.pair_semantic(SemanticBinding(verdict.collection, embedder.embed))
        logger.info(
            "semantic and hybrid search are available over %s",
            verdict.collection,
            extra={"component": "serving", "event": "semantic_paired"},
        )

    def _semantic_for(self, serving: Serving) -> _SemanticPairing | str:
        """Judge the semantic index against the snapshot being served.

        Args:
            serving: The snapshot being served.

        Returns:
            The pairing when the semantic index is usable with it; else one plain sentence saying
            why not (the conditions are in the module docstring). Holds no key, path or
            exception text.

        Raises:
            Exception: Whatever the Qdrant client raises when it cannot be asked.
        """
        aliases = self._client.get_aliases().aliases
        name = next((a.collection_name for a in aliases if a.alias_name == SEMANTIC_ALIAS), None)
        if name is None:
            return (
                f"there is no semantic index yet (the alias {SEMANTIC_ALIAS} does not exist; "
                "`wiki-mirror embed-now` builds it)"
            )
        info = semantic_index_info(self._config.data_dir, name, quiet=True)
        if info is None:
            return (
                f"the semantic index {name} has no readable state "
                "(state/index_semantic_<born>.json is missing or unreadable)"
            )
        if not info.complete:
            return f"the semantic index {name} is still being built (its embedding run is not done)"
        if info.applied_snapshot != serving.applied_snapshot:
            return (
                f"the semantic index is of snapshot {info.applied_snapshot}, the articles served "
                f"are of {serving.applied_snapshot}; the semantic catch-up brings it up to date "
                "(docs/ROADMAP.md)"
            )
        if info.chunker != chunker_fingerprint():
            return (
                f"the semantic index {name} was built with a different chunker than this code's, "
                "so its passage numbers would not line up with the articles"
            )
        if self._config.embedding_url is None:
            return "the embedding server is not configured (embedding_url in mirror.yaml)"
        return _SemanticPairing(name, info.model, info.dimensions)

    def _swap_in(self, new: Serving) -> None:
        """Make `new` the served snapshot and retire the old one (closed once unused)."""
        with self._lock:
            if self._closed:
                # `close()` ran while the new store was being opened: nobody will use it.
                old, new_unused = None, True
            else:
                old, self._serving = self._serving, new
                new_unused = False
                if old is not None:
                    old._retired = True
            close_now = old is not None and old._leases == 0
        if new_unused:
            new.store.close()
            return
        self._clear_problem()
        logger.info(
            "serving snapshot %s (collection %s)",
            new.store.snapshot.date,
            new.collection,
            extra={"component": "serving", "event": "swapped"},
        )
        if old is not None and close_now:
            old.store.close()

    def _release(self, serving: Serving) -> None:
        """End one lease; the last lease on a retired snapshot closes its store."""
        with self._lock:
            serving._leases -= 1
            close_now = serving._retired and serving._leases == 0
        if close_now:
            serving.store.close()

    def _note_problem(self, summary: str, cause: BaseException | None) -> None:
        """Record why the alias's snapshot is not served; log the detail when it first appears."""
        if summary != self._problem:
            logger.warning(
                "%s%s; %s",
                summary,
                f" ({type(cause).__name__}: {cause})" if cause is not None else "",
                "serving the previous snapshot" if self._serving else "nothing is being served",
                extra={"component": "serving", "event": "degraded"},
            )
        self._problem = summary

    def _clear_problem(self) -> None:
        """Record that the alias's snapshot is the one served (or that there is none yet)."""
        if self._problem is not None:
            logger.info(
                "serving is healthy again",
                extra={"component": "serving", "event": "recovered"},
            )
        self._problem = None
