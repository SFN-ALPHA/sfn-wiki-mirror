"""The per-snapshot article store: one SQLite file, zstd-compressed text, title lookup.

`build.run_parse` fills it (one writer, a transaction per batch of articles); `index` and
`search` read it back through the `records.ArticleSource` protocol; the API serves article
windows out of it. One file per snapshot, `articles/<snapshot>_<date>.db` on local SSD, so a new
snapshot builds beside the old one and the old one keeps serving until the swap.

Layout (schema version 2):

* `meta` - key/value: the snapshot (`snapshot`, JSON), the schema version, and the
  `parser_version` of the `parse` module that produced the text (written once, when the build
  creates the store);
* `chunks` - the chunk ids already ingested, so an interrupted parse resumes (`chunks_done`);
* `articles` - one row per page id: title, folded title, revision, categories (JSON), the
  disambiguation flag, licence, abstract (plain text), the text (zstd) and its `text_hash`;
* `redirects` - alias titles, folded for lookup, pointing at a page id.

Non-obvious constraints:

* **`text_hash` is a column, so nobody has to decompress to compare texts.** Search compares a
  hit's payload hash with the stored article's, and the W3 monthly diff compares two months'
  rows, both without touching the zstd blobs. It is stored as given and handed back as stored;
  `add_many` refuses an article whose hash is not `records.text_hash` of its own text, because a
  wrong hash would silently drop good search hits and hide changed text from the diff.
* **`parser_version` is passed in by the build, not imported here.** The store is a container,
  not a parser, and the API process that opens it read-only should not import the HTML parser.
  `build.run_parse` passes `parse.PARSER_VERSION`. Schema 1 files (no hash column, no parser
  version) are refused like any other version; none exists on a live box, so there is no
  migration.

* **Highest revision wins.** Enterprise can list a page twice (an edit landed between chunks).
  `add_many` upserts with `WHERE excluded.revision_id > articles.revision_id`, so replaying a
  chunk after a crash, or ingesting chunks out of order, converges on the same rows. A page's
  redirects are replaced only when its row was written.
* **WAL while building, a plain immutable file when serving.** `create`/writable `open` use WAL
  with `synchronous=NORMAL` (a crash may lose the last transaction, never corrupt; the chunk was
  not marked done, so it is re-ingested). `finalize` checkpoints, switches the file back to a
  rollback journal and records `complete=1` in `meta`. A WAL database cannot be opened by a
  reader that cannot create `-wal`/`-shm` files, and the API user has read-only access to the
  directory (docs/DESIGN.md section 6); so a read-only `open` requires `complete=1`, refuses
  anything else, and opens with `immutable=1` (no locking at all; sound because a finished store
  never changes again).
* **A finished store is never reopened for writing by accident.** A writable `open` of a store
  flagged `complete=1` raises `StoreError` unless the caller passes `allow_complete=True`: the
  switch back to WAL it would make, and the flag it would withdraw, happen under the feet of an
  API that may be serving the file. The build never needs it (a finished store is done), so only
  tests and a deliberate repair use the escape hatch, and the first write through it withdraws
  the flag.
* **`create` is atomic.** The file is built under a temporary name and renamed when its schema
  and snapshot row are committed, so an existing store file always has both.
* **Compressors are not thread-shared.** One `ZstdCompressor` and one `ZstdDecompressor` per
  store instance, used under the instance lock. A read-only store may be shared by the API's
  worker threads; every read takes the lock for the few hundred microseconds it needs.
* **Titles resolve in three steps** (see `by_title`): exact, case-insensitive, redirect.

See docs/DESIGN.md sections 5 and 12.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Final

import zstandard

from wiki_mirror.records import Article, SnapshotMeta, normalise_title, text_hash

__all__ = [
    "ArticleStore",
    "StoreError",
    "snapshot_from_json",
    "snapshot_to_json",
    "store_path",
]

#: Bumped when the table layout changes; `open` refuses a file from another version rather than
#: guess (a store is rebuildable from the snapshot, so refusing is cheap). 2 added the
#: `articles.text_hash` column and the `parser_version` meta row (docs/DESIGN.md section 12.3).
_SCHEMA_VERSION: Final = "2"

#: zstd level for article text. About 3x on prose; level 9 compresses roughly a third faster
#: than 19 for a few percent less ratio, which matters when the writer is the build's bottleneck.
_ZSTD_LEVEL: Final = 9

#: Rows fetched per round trip when streaming the whole store.
_FETCH_BATCH: Final = 500

_SCHEMA: Final = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE articles (
    page_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    title_fold TEXT NOT NULL,
    revision_id INTEGER NOT NULL,
    revision_time TEXT NOT NULL,
    categories TEXT NOT NULL,
    is_disambiguation INTEGER NOT NULL,
    license TEXT NOT NULL,
    abstract TEXT NOT NULL,
    text BLOB NOT NULL,
    chars INTEGER NOT NULL,
    text_hash INTEGER NOT NULL
);
CREATE INDEX articles_title ON articles (title);
CREATE INDEX articles_title_fold ON articles (title_fold);
CREATE TABLE redirects (
    alias_fold TEXT NOT NULL,
    page_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    PRIMARY KEY (alias_fold, page_id)
) WITHOUT ROWID;
CREATE INDEX redirects_page ON redirects (page_id);
"""

_UPSERT: Final = """
INSERT INTO articles (page_id, title, title_fold, revision_id, revision_time, categories,
                      is_disambiguation, license, abstract, text, chars, text_hash)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(page_id) DO UPDATE SET
    title = excluded.title, title_fold = excluded.title_fold,
    revision_id = excluded.revision_id, revision_time = excluded.revision_time,
    categories = excluded.categories, is_disambiguation = excluded.is_disambiguation,
    license = excluded.license, abstract = excluded.abstract, text = excluded.text,
    chars = excluded.chars, text_hash = excluded.text_hash
WHERE excluded.revision_id > articles.revision_id
"""

_ARTICLE_COLUMNS: Final = (
    "page_id, title, revision_id, revision_time, categories, is_disambiguation, license,"
    " abstract, text, text_hash"
)


class StoreError(RuntimeError):
    """The file is not an article store this code can use."""


def store_path(data_dir: Path, snapshot: SnapshotMeta) -> Path:
    """Where a snapshot's article store lives: `<data_dir>/articles/<id>_<date>.db`."""
    return data_dir / "articles" / f"{snapshot.identifier}_{snapshot.tag}.db"


class ArticleStore:
    """One snapshot's articles. Implements `records.ArticleSource`.

    Build with `create` then `add_many` and `mark_chunk_done`; serve with
    `open(path, readonly=True)`. Do not construct directly.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        snapshot: SnapshotMeta,
        *,
        parser_version: int,
        readonly: bool,
        complete: bool = False,
    ) -> None:
        """Wrap an open connection (use `create` or `open`)."""
        self._conn = conn
        self._snapshot = snapshot
        self._parser_version = parser_version
        self._readonly = readonly
        self._complete = complete
        self._lock = threading.RLock()
        self._compressor = zstandard.ZstdCompressor(level=_ZSTD_LEVEL)
        self._decompressor = zstandard.ZstdDecompressor()

    # -- construction -----------------------------------------------------------------------

    @classmethod
    def create(cls, path: Path, snapshot: SnapshotMeta, *, parser_version: int) -> ArticleStore:
        """Create a new, empty store for a snapshot and open it for writing.

        Args:
            path: Where the file goes (`store_path`). Its directory is created.
            snapshot: The snapshot being built; kept in the file.
            parser_version: `parse.PARSER_VERSION` of the code that will fill the store; kept in
                the file so a later run can tell whether its text may be copied forward.

        Raises:
            FileExistsError: A store is already there. Resume with `open(readonly=False)`.
        """
        if path.exists():
            raise FileExistsError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        building = path.with_name(path.name + ".creating")
        building.unlink(missing_ok=True)
        conn = sqlite3.connect(building)
        try:
            with conn:
                conn.executescript(_SCHEMA)
                conn.execute(
                    "INSERT INTO meta (key, value)"
                    " VALUES ('schema', ?), ('snapshot', ?), ('parser_version', ?)",
                    (_SCHEMA_VERSION, snapshot_to_json(snapshot), str(parser_version)),
                )
        finally:
            conn.close()
        building.replace(path)
        return cls.open(path, readonly=False)

    @classmethod
    def open(
        cls, path: Path, *, readonly: bool = True, allow_complete: bool = False
    ) -> ArticleStore:
        """Open an existing store.

        Read-only access is for serving and indexing, and only a COMPLETE store (one `finalize`
        has been called on) may be opened that way: the API must never serve a half-built store.
        The connection uses `mode=ro&immutable=1`, so SQLite takes no locks and creates no
        `-wal`/`-shm`/journal files, and the file opens from a directory the caller cannot write
        to (the API user has read-only access to `articles/`). That is only safe because a
        finished store never changes again; a writer must not touch it while it is served.

        The connection is created with `check_same_thread=False` so FastAPI's threadpool can
        share one instance; every method takes the instance lock, which serialises use of the
        connection and of the (non-thread-safe) zstd decompressor. One shared connection rather
        than one per thread: reads take microseconds and the lock is never the bottleneck.

        Args:
            path: The store file.
            readonly: True (default) for the serving mode above. False is for the build's single
                writer (WAL, `synchronous=NORMAL`); it reopens an incomplete store to resume.
            allow_complete: Let a writable open take a store that is already complete. Off by
                default (module docstring); meaningless with `readonly=True`.

        Raises:
            StoreError: The file is missing, is not a store, is from another schema version
                (a schema-1 store included), records no parser version, (read-only) is not
                complete, or (writable) is complete and `allow_complete` is not set. The refusal
                happens before anything is written to the file.
        """
        if not path.is_file():
            msg = f"no article store at {path}"
            raise StoreError(msg)
        if readonly:
            conn = cls._connect_readonly(path, immutable=False)
        else:
            conn = sqlite3.connect(path, check_same_thread=False)
        try:
            version = _meta(conn, "schema")
            raw = _meta(conn, "snapshot")
            parser = _meta(conn, "parser_version")
            complete = _meta(conn, "complete") == "1"
        except sqlite3.DatabaseError:
            conn.close()
            msg = f"{path} is not an article store"
            raise StoreError(msg) from None
        if version != _SCHEMA_VERSION or raw is None:
            conn.close()
            msg = f"{path} is a store from another schema version ({version!r}); rebuild it"
            raise StoreError(msg)
        if parser is None or not parser.isdigit():
            # `create` writes the row in the same transaction as the schema, so a schema-2 file
            # without it was damaged or made by hand; nothing can be said about its text.
            conn.close()
            msg = f"{path} records no parser version; it is damaged, rebuild it"
            raise StoreError(msg)
        if readonly:
            conn.close()
            if not complete:
                msg = f"{path} is an incomplete store (the build has not finished); not serving it"
                raise StoreError(msg)
            conn = cls._connect_readonly(path, immutable=True)
        elif complete and not allow_complete:
            # Refused before the WAL switch below, which is the step that would change a file
            # the API may have open.
            conn.close()
            msg = f"{path} is a finished store, never reopened for writing (delete it to rebuild)"
            raise StoreError(msg)
        else:
            conn.execute("PRAGMA journal_mode = WAL")
            # NORMAL, not FULL: a power cut may lose the last transaction but cannot corrupt the
            # file, and the chunk that held it was not yet marked done, so it is re-ingested.
            conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -65536")
        return cls(
            conn,
            snapshot_from_json(raw),
            parser_version=int(parser),
            readonly=readonly,
            complete=complete,
        )

    @staticmethod
    def _connect_readonly(path: Path, *, immutable: bool) -> sqlite3.Connection:
        """Open a read-only connection; `immutable` also promises the file never changes.

        The first, non-immutable probe reads the `complete` flag. A store still in WAL mode
        (incomplete) cannot be opened from an unwritable directory, which surfaces as a
        `StoreError` rather than a raw SQLite error.
        """
        flags = "mode=ro&immutable=1" if immutable else "mode=ro"
        uri = f"{path.resolve().as_uri()}?{flags}"
        try:
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        except sqlite3.OperationalError:
            msg = f"{path} cannot be opened read-only (is it an incomplete, WAL-mode store?)"
            raise StoreError(msg) from None
        return conn

    # -- writing ----------------------------------------------------------------------------

    def add_many(self, articles: Iterable[Article]) -> int:
        """Store articles in one transaction; for a repeated page the highest revision wins.

        Args:
            articles: Articles of any number of pages. A page already stored with an equal or
                higher revision is left untouched (and so are its redirects).

        Returns:
            How many articles were written (inserted or replaced by a newer revision).

        Raises:
            StoreError: The store was opened read-only, or an article's `text_hash` is not the
                hash of its own text (the whole batch is rolled back).
        """
        self._require_writable()
        written = 0
        with self._lock:
            try:
                self._clear_complete()
                for article in articles:
                    written += self._add(article)
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return written

    def _add(self, article: Article) -> int:
        """Upsert one article inside the open transaction; 1 if its row was written."""
        if article.text_hash != text_hash(article.text):
            # A bug upstream (parser, spool, a hand-built Article), not bad data: refuse loudly
            # rather than store a hash that would mislead search and the W3 diff.
            msg = f"article {article.page_id} carries a text hash that is not its text's"
            raise StoreError(msg)
        text = article.text.encode("utf-8")
        cursor = self._conn.execute(
            _UPSERT,
            (
                article.page_id,
                article.title,
                article.title.casefold(),
                article.revision_id,
                article.revision_time,
                json.dumps(article.categories, ensure_ascii=False),
                int(article.is_disambiguation),
                article.license,
                article.abstract,
                self._compressor.compress(text),
                len(article.text),
                article.text_hash,
            ),
        )
        if cursor.rowcount != 1:
            return 0
        self._conn.execute("DELETE FROM redirects WHERE page_id = ?", (article.page_id,))
        self._conn.executemany(
            "INSERT OR IGNORE INTO redirects (alias_fold, page_id, alias) VALUES (?, ?, ?)",
            [
                (normalise_title(alias).casefold(), article.page_id, alias)
                for alias in article.redirects
                if normalise_title(alias)
            ],
        )
        return 1

    def mark_chunk_done(self, chunk_id: str) -> None:
        """Record that every article of a chunk has been added (commits immediately)."""
        self._require_writable()
        with self._lock, self._conn:
            self._clear_complete()
            self._conn.execute("INSERT OR IGNORE INTO chunks (chunk_id) VALUES (?)", (chunk_id,))

    @property
    def is_complete(self) -> bool:
        """Whether `finalize` has run and nothing has been written since.

        Only a complete store can be opened read-only; callers (the API) check this instead of
        catching `StoreError` when they want to report "building" rather than fail.
        """
        return self._complete

    def _clear_complete(self) -> None:
        """Withdraw the complete flag before the first write to a finalized store."""
        if self._complete:
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('complete', '0')")
            self._complete = False

    def finalize(self) -> None:
        """Make the store servable: checkpoint the WAL, leave WAL mode and flag it complete.

        Called by the build once every chunk is in. The order matters: the checkpoint folds the
        WAL into the main file, `journal_mode = DELETE` removes the `-wal`/`-shm` files and
        makes the file self-contained, and only then is `complete=1` recorded (and checkpointed
        in turn by the journal switch's own commit), so a crash between steps leaves a store
        that still reads as incomplete. Safe to call more than once.
        """
        self._require_writable()
        with self._lock:
            self._conn.commit()
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("PRAGMA journal_mode = DELETE")
            self._conn.execute("PRAGMA optimize")
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('complete', '1')"
                )
            self._complete = True

    def _require_writable(self) -> None:
        """Refuse a write on a read-only store with a clear error."""
        if self._readonly:
            msg = "the article store was opened read-only"
            raise StoreError(msg)

    def close(self) -> None:
        """Close the database."""
        with self._lock:
            self._conn.close()

    # -- reading (records.ArticleSource) ----------------------------------------------------

    @property
    def snapshot(self) -> SnapshotMeta:
        """The snapshot these articles came from."""
        return self._snapshot

    @property
    def parser_version(self) -> int:
        """The `parse.PARSER_VERSION` of the code that built this store, as recorded at creation.

        Read-only: the build records it once, and a store whose text came from two parsers would
        make the number a lie.
        """
        return self._parser_version

    def chunks_done(self) -> frozenset[str]:
        """The chunk ids already ingested."""
        with self._lock:
            rows = self._conn.execute("SELECT chunk_id FROM chunks").fetchall()
        return frozenset(row[0] for row in rows)

    def revision_map(self) -> dict[int, int]:
        """Page id to revision id for every article (the W3 sync diffs on this).

        About 100 bytes per article in memory: 700 MB for the full English Wikipedia.
        """
        with self._lock:
            cursor = self._conn.execute("SELECT page_id, revision_id FROM articles")
            return dict(cursor.fetchall())

    def get(self, page_id: int) -> Article | None:
        """The article with this page id, or None."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_ARTICLE_COLUMNS} FROM articles WHERE page_id = ?",  # noqa: S608
                (page_id,),
            ).fetchone()
            return self._article(row) if row is not None else None

    def by_title(self, title: str) -> Article | None:
        """Resolve a title: exact display title, then case-insensitive, then a redirect.

        The title is normalised first (`records.normalise_title`: underscores to spaces, runs of
        whitespace collapsed, first letter upper-cased). Then:

        1. an article whose title is exactly that;
        2. an article whose title matches ignoring case. Two pages can differ only in case
           ("AIDS" and "Aids"); when the typed spelling matches neither exactly, the lowest page
           id wins, which is the older and almost always the primary page;
        3. an alias a redirect page points from, matched ignoring case, lowest page id first.
        """
        wanted = normalise_title(title)
        if not wanted:
            return None
        folded = wanted.casefold()
        queries = (
            ("WHERE title = ? ORDER BY page_id LIMIT 1", wanted),
            ("WHERE title_fold = ? ORDER BY page_id LIMIT 1", folded),
            (
                "WHERE page_id = (SELECT page_id FROM redirects WHERE alias_fold = ?"
                " ORDER BY page_id LIMIT 1)",
                folded,
            ),
        )
        with self._lock:
            for clause, value in queries:
                row = self._conn.execute(
                    # Columns and clauses are module constants; only `value` is data.
                    f"SELECT {_ARTICLE_COLUMNS} FROM articles {clause}",  # noqa: S608
                    (value,),
                ).fetchone()
                if row is not None:
                    return self._article(row)
        return None

    def iter_articles(self, after: int | None = None) -> Iterator[Article]:
        """Every article in page-id order, fetched in bounded batches.

        Args:
            after: Resume point: only articles with a page id greater than this (the indexer
                passes the last page id it finished). None starts from the beginning.
        """
        last = -1 if after is None else after
        while True:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT {_ARTICLE_COLUMNS} FROM articles WHERE page_id > ?"  # noqa: S608
                    " ORDER BY page_id LIMIT ?",
                    (last, _FETCH_BATCH),
                ).fetchall()
                batch = [self._article(row) for row in rows]
            if not batch:
                return
            yield from batch
            last = batch[-1].page_id

    def count(self) -> int:
        """Number of articles stored."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM articles").fetchone()
        return int(row[0])

    def _article(self, row: tuple[Any, ...]) -> Article:
        """Build an `Article` from an `_ARTICLE_COLUMNS` row (caller holds the lock)."""
        aliases = self._conn.execute(
            "SELECT alias FROM redirects WHERE page_id = ? ORDER BY alias", (row[0],)
        ).fetchall()
        return Article(
            page_id=row[0],
            title=row[1],
            revision_id=row[2],
            revision_time=row[3],
            text=self._decompressor.decompress(row[8]).decode("utf-8"),
            # As stored, not recomputed: the point of the column is that a read can tell which
            # text it holds without trusting, or paying for, a second hash of the blob.
            text_hash=row[9],
            abstract=row[7],
            categories=tuple(json.loads(row[4])),
            redirects=tuple(alias[0] for alias in aliases),
            is_disambiguation=bool(row[5]),
            license=row[6],
        )


def snapshot_to_json(snapshot: SnapshotMeta) -> str:
    """Serialise a snapshot description (the store's meta row and the download's snapshot.json)."""
    return json.dumps(
        {
            "identifier": snapshot.identifier,
            "date": snapshot.date,
            "record_count": snapshot.record_count,
            "chunk_ids": list(snapshot.chunk_ids),
        }
    )


def snapshot_from_json(raw: str) -> SnapshotMeta:
    """Inverse of `snapshot_to_json`.

    Raises:
        ValueError: The text is not JSON or lacks a field (KeyError and TypeError are folded in).
    """
    try:
        data = json.loads(raw)
        return SnapshotMeta(
            identifier=str(data["identifier"]),
            date=str(data["date"]),
            record_count=int(data["record_count"]),
            chunk_ids=tuple(str(chunk) for chunk in data["chunk_ids"]),
        )
    except (KeyError, TypeError) as exc:
        msg = "not a snapshot description"
        raise ValueError(msg) from exc


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    """One value from the meta table, or None."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])
