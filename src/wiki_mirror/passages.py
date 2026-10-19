"""How an article is cut into passages: the chunker's identity, and the cutter itself.

Holds `CHUNK_CONFIG`, `CHUNKER_VERSION` and `chunker_fingerprint()` (moved here from `index`, which
re-exports them) and `cut_passages()`, the one function that turns article text into numbered
passages. `index` (the build) and `search` (the snippet re-cut) use the v1 cut, which is exactly
`text.sanitize.chunk_text`; the W2 passage-length evaluation uses the packed cut, which joins
consecutive pieces up to a target length.

Non-obvious constraints:

* **Must stay importable without `zstandard` or `qdrant_client`.** The evaluation tool runs on a
  box that has neither, and `index` imports `build`, which imports `zstandard`. So this module
  imports only the stdlib and the vendored `text` package, never `build`, `index`, `store` or the
  Qdrant client.
* **The v1 cut is the live point id.** A passage's number is part of its Qdrant point id, so
  `cut_passages(text)` must reproduce `chunk_text(text, CHUNK_CONFIG)` exactly, index for index.

See docs/DESIGN.md section 12.5.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from wiki_mirror.text.indexed_text import compose_indexed_text
from wiki_mirror.text.sanitize import Chunk, ChunkConfig, chunk_text

__all__ = [
    "CHUNKER_VERSION",
    "CHUNK_CONFIG",
    "Passage",
    "chunker_fingerprint",
    "cut_passages",
]

#: Bumped by hand whenever `chunk_text` (vendored in `text.sanitize`, owned here) starts
#: cutting text differently in a way `CHUNK_CONFIG` cannot show: a new split rule, a new minimum
#: piece size. Every passage number in a built index depends on it (`index` module docstring).
CHUNKER_VERSION: Final = 1

#: How an article is cut into passages, for the indexer here and for the snippet cut in
#: `search`. Passed to `chunk_text` explicitly on both sides, so there is one object to change
#: and one fingerprint (`chunker_fingerprint`) that follows it.
CHUNK_CONFIG: Final = ChunkConfig()

#: Separator between the pieces of a packed passage (a blank line, as in the source markdown).
_JOIN: Final = "\n\n"


@dataclass(frozen=True)
class Passage:
    """One passage of an article: what is indexed, and where it sits."""

    #: The passage number: the chunk part of the point id (`records.passage_point_id`).
    index: int
    #: The passage body; for a packed passage, inner headings are inline.
    text: str
    #: The heading of the passage's first piece.
    heading: str
    #: The section path of the passage's first piece.
    section_path: str

    def indexed_text(self, title: str) -> str:
        """What gets encoded for this passage: its text under its heading and the article title.

        Same composition as `text.sanitize.Chunk.indexed_text`, so a v1 passage encodes
        exactly as before.

        Args:
            title: The article title.
        """
        return compose_indexed_text(self.text, self.heading, title)


def chunker_fingerprint(version: int | None = None, config: ChunkConfig | None = None) -> str:
    """A short name for how the running code cuts articles into passages, e.g. `v1:512/50`.

    The chunk sizes in `CHUNK_CONFIG` and `CHUNKER_VERSION`; recorded in each index state and
    compared by `serving` against the index it serves.

    Args:
        version: Chunker version to name; this module's `CHUNKER_VERSION` when omitted.
        config: Chunk sizes to name; this module's `CHUNK_CONFIG` when omitted. `index` passes
            its own re-exported values, so a test that patches them there changes the
            fingerprint the build and `serving` see.
    """
    used = CHUNK_CONFIG if config is None else config
    named = CHUNKER_VERSION if version is None else version
    return f"v{named}:{used.max_tokens}/{used.overlap_tokens}"


def cut_passages(text: str, *, pack_chars: int = 0) -> list[Passage]:
    """Cut article text into numbered passages.

    With `pack_chars <= 0` (v1, what the live index uses) this is exactly one passage per
    `chunk_text(text, CHUNK_CONFIG)` chunk, with the chunk's own number, text, heading and
    section path. With `pack_chars > 0` consecutive pieces are packed greedily, in order, into
    passages of at most `pack_chars` characters; a piece longer than that stands alone and is
    never split. A piece that joins a passage brings its heading inline when it differs from the
    previous piece's. The cut is deterministic, because search re-cuts an article to make a
    snippet.

    Packing is not used by the live index yet: adopting it means bumping `CHUNKER_VERSION` and
    a full rebuild of both indexes (docs/DESIGN.md section 12.7).

    Args:
        text: The article text, as stored.
        pack_chars: Target maximum passage length in characters; 0 or less for the v1 cut.

    Returns:
        Passages in document order, numbered 0..n-1 (contiguous); empty when the text yields no
        piece.
    """
    pieces = chunk_text(text, CHUNK_CONFIG)
    if pack_chars <= 0:
        return [Passage(c.index, c.text, c.heading, c.section_path) for c in pieces]

    passages: list[Passage] = []
    current: list[str] = []
    length = 0
    first: Chunk | None = None
    previous: Chunk | None = None
    for piece in pieces:
        if first is not None and previous is not None:
            joined = _JOIN + _heading_prefix(piece.heading, previous.heading) + piece.text
            if length + len(joined) <= pack_chars:
                current.append(joined)
                length += len(joined)
                previous = piece
                continue
            passages.append(_close(len(passages), current, first))
        first = previous = piece
        current = [piece.text]
        length = len(piece.text)
    if first is not None:
        passages.append(_close(len(passages), current, first))
    return passages


def _heading_prefix(heading: str, previous_heading: str) -> str:
    """The inline heading line for a piece joining a passage: empty unless the heading changed."""
    if heading and heading != previous_heading:
        return heading + "\n"
    return ""


def _close(index: int, parts: list[str], first: Chunk) -> Passage:
    """Build the packed passage from its joined parts; heading and path are the first piece's."""
    return Passage(index, "".join(parts), first.heading, first.section_path)
