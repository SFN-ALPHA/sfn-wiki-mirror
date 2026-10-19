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
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from qdrant_client import QdrantClient

from wiki_mirror.config import MirrorConfig
from wiki_mirror.index import (
    aliased_collection,
    applied_snapshot,
    chunker_fingerprint,
    indexed_chunker,
)
from wiki_mirror.search import WikiSearch
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
    ) -> None:
        """Hold nothing yet; the first `lease()` or `refresh()` opens what the alias names.

        Args:
            config: Service settings.
            client: The Qdrant client, asked where the alias points and given to each search.
            clock: Monotonic seconds, for the recheck interval; tests inject theirs.
            refresh_seconds: Longest between two looks at the alias.
        """
        self._config = config
        self._client = client
        self._clock = clock
        self._interval = refresh_seconds
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._serving: Serving | None = None
        self._alias: str | None = None
        self._problem: str | None = None
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
            self._follow_alias()

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
            self._follow_alias()
        finally:
            self._refresh_lock.release()

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
