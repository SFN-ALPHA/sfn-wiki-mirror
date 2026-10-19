"""The shapes every mirror module shares: an article, a snapshot, and the citation URL.

`parse` produces `Article`; `store` keeps it; `index` and `search` read it back through the
`ArticleSource` protocol, so the search side is tested against an in-memory source without a
SQLite file. `oldid_url` is the one place the citation link is built, because a citation that
points at the exact revision read is the whole reason the mirror keeps revision ids (docs/DESIGN.md
section 5).

Non-obvious constraints:

* **Titles are stored as Wikipedia displays them** ("Albert Einstein", spaces, first letter
  upper case). `normalise_title` turns what a person or a planner types ("albert_einstein") into
  that form; case-insensitive matching beyond the first letter is the store's job, because two
  distinct pages can differ only in case ("AIDS" and "Aids").
* **`page_id * PASSAGE_STRIDE + chunk_index` is a passage's Qdrant point id.** Deterministic, so
  a rebuild of an unchanged article lands on the same ids (the W3 diff copies vectors by id), and
  decodable, so a search hit names its article without a payload lookup.
* **`Article.text_hash` names the exact text a passage was cut from.** The first 8 bytes of
  SHA-256 of the text (`text_hash`), as a signed 64-bit integer so the one value fits a SQLite
  INTEGER column and a Qdrant integer payload. The parser computes it from the final text, the
  store keeps it in a column and hands it back as stored (it is never recomputed on read), and
  every indexed passage carries it, so search, and the W3 monthly diff, can tell which text a
  passage belongs to without decompressing anything (docs/DESIGN.md section 12.3).

See docs/DESIGN.md sections 5 and 12.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import quote

__all__ = [
    "MAX_PAGE_ID",
    "PASSAGE_STRIDE",
    "Article",
    "ArticleSource",
    "SnapshotMeta",
    "normalise_title",
    "oldid_url",
    "passage_point_id",
    "split_point_id",
    "text_hash",
]

#: Room for chunk indexes within one article's block of point ids. The longest English article
#: is about 1M characters of HTML and well under 500 passages at the 2,000-character
#: chunks, so 10,000 leaves a wide margin; page ids (under 10^8) times this stay far below 2^63.
PASSAGE_STRIDE: Final = 10_000

#: The largest page id any route accepts. Page ids are SQLite integers and Qdrant payload
#: integers, both signed 64-bit: beyond this SQLite raises `OverflowError`, which a route would
#: turn into a 500 instead of a plain "no such article".
MAX_PAGE_ID: Final = 2**63 - 1

#: Characters that stay literal in a citation URL's title, as Wikipedia itself writes them.
_TITLE_SAFE: Final = "/:(),'!*-._~"


@dataclass(frozen=True, slots=True)
class Article:
    """One English Wikipedia article, as the mirror serves it."""

    #: Wikipedia's page id (Enterprise `identifier`). Stable across renames and revisions.
    page_id: int
    #: Display title (Enterprise `name`), spaces not underscores.
    title: str
    #: The revision the text came from (Enterprise `version.identifier`). Increases with edits.
    revision_id: int
    #: When that revision was saved, ISO-8601 UTC (Enterprise `date_modified`).
    revision_time: str
    #: Clean text: markdown headings (`## Section`), list items as `- `, an infobox as a short
    #: `label: value` block under `## Infobox`. Already passed through
    #: `sanitize(untrusted=True)`. References, navigation boxes and link-farm sections removed.
    text: str
    #: `text_hash(text)`, filled by the parser and kept as stored by the store (module docstring).
    #: Required, with no default: a hash that is silently wrong would make search drop good
    #: passages and the W3 diff skip changed text, so every constructor has to supply one.
    text_hash: int
    #: Enterprise's own first-paragraph summary (`abstract`), sanitized; may be empty.
    abstract: str = ""
    #: Category names without the `Category:` prefix.
    categories: tuple[str, ...] = ()
    #: Titles that redirect here, without namespace prefixes, display form.
    redirects: tuple[str, ...] = ()
    #: A disambiguation page: kept (it answers "which X?") but ranked below articles.
    is_disambiguation: bool = False
    #: The licence line kept with any excerpt, e.g. "CC BY-SA 4.0".
    license: str = "CC BY-SA 4.0"

    @property
    def url(self) -> str:
        """The permanent link to the exact revision this text came from."""
        return oldid_url(self.title, self.revision_id)


@dataclass(frozen=True, slots=True)
class SnapshotMeta:
    """What Enterprise says about one snapshot, recorded when a build starts."""

    #: Enterprise's snapshot identifier, e.g. `enwiki_namespace_0`.
    identifier: str
    #: The snapshot's date, `YYYY-MM-DD`, from Enterprise's `date_modified`. Names the article
    #: store and the collection a full build of this snapshot creates, and dates every citation.
    date: str
    #: Number of NDJSON lines Enterprise reports. Duplicates are possible (the highest
    #: revision wins), so the stored article count can be a little lower.
    record_count: int
    #: Chunk identifiers in Enterprise's order, e.g. `enwiki_namespace_0_chunk_0`.
    chunk_ids: tuple[str, ...]

    @property
    def tag(self) -> str:
        """The date without dashes, used in collection and file names (`20261001`)."""
        return self.date.replace("-", "")


class ArticleSource(Protocol):
    """Read access to one snapshot's articles. Implemented by `store.ArticleStore`."""

    @property
    def snapshot(self) -> SnapshotMeta:
        """The snapshot these articles came from."""
        ...

    def get(self, page_id: int) -> Article | None:
        """The article with this page id, or None."""
        ...

    def by_title(self, title: str) -> Article | None:
        """Resolve a title: exact display title, then case-insensitive, then a redirect.

        Args:
            title: Any spelling a person or a planner might type; it is normalised first.
        """
        ...

    def iter_articles(self, after: int | None = None) -> Iterator[Article]:
        """Every article, in page-id order. Streams; never loads the snapshot into memory.

        Args:
            after: Resume point: only articles with a page id greater than this (the indexer
                passes the last page id it finished). None, or any negative number, starts from
                the beginning.
        """
        ...

    def count(self) -> int:
        """Number of articles stored."""
        ...


def normalise_title(title: str) -> str:
    """Turn a typed title into Wikipedia's display form.

    Underscores become spaces, runs of whitespace collapse, and the first letter is upper-cased
    (Wikipedia treats the first letter as case-insensitive and nothing else). A leading
    `en.wikipedia.org/wiki/` link prefix is NOT handled here; callers strip URLs themselves.

    Args:
        title: The title as typed.

    Returns:
        The normalised title; empty when nothing but whitespace was given.
    """
    words = title.replace("_", " ").split()
    cleaned = " ".join(words)
    return cleaned[:1].upper() + cleaned[1:]


def oldid_url(title: str, revision_id: int) -> str:
    """The permanent link to one revision: `https://en.wikipedia.org/wiki/<Title>?oldid=<rev>`.

    Args:
        title: Display title.
        revision_id: Revision id.
    """
    path = quote(title.replace(" ", "_"), safe=_TITLE_SAFE)
    return f"https://en.wikipedia.org/wiki/{path}?oldid={revision_id}"


def passage_point_id(page_id: int, chunk_index: int) -> int:
    """The Qdrant point id of an article's passage (see the module docstring).

    Raises:
        ValueError: The chunk index does not fit the stride, or either number is negative.
    """
    if page_id < 0 or not 0 <= chunk_index < PASSAGE_STRIDE:
        msg = f"passage ({page_id}, {chunk_index}) does not fit the point-id scheme"
        raise ValueError(msg)
    return page_id * PASSAGE_STRIDE + chunk_index


def split_point_id(point_id: int) -> tuple[int, int]:
    """Invert `passage_point_id`: the page id and chunk index of a point."""
    return divmod(point_id, PASSAGE_STRIDE)


def text_hash(text: str) -> int:
    """A short, stable fingerprint of an article's text: SHA-256's first 8 bytes, signed.

    Signed big-endian int64, so the value is a legal SQLite INTEGER and a legal Qdrant integer
    payload without conversion. Eight bytes is ample for telling two versions of one article's
    text apart (the question is "same text or not" for one page, never a lookup across the
    corpus); it is not a security control.

    Args:
        text: The article's final plain text (`Article.text`).

    Returns:
        An integer in `[-2**63, 2**63 - 1]`.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)
