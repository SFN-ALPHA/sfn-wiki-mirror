"""Sanitization and chunking: turning article text into indexable passages.

The page-marker and figure-marker handling is never exercised by the mirror's Wikipedia text
but is kept, because dropping it would change the chunking of any text that did contain such a
comment. `parse` calls `sanitize` on every article, and `passages`,
`index` and `search` call `chunk_text` to cut it into the passages the index stores.

The built indexes depend on this module's exact behaviour: the passage numbers the index stores
are positions in `chunk_text`'s output, so any change to where it cuts misaligns every built
passage. A behavioural change needs `CHUNKER_VERSION` bumped (and a rebuild), per docs/DESIGN.md
section 12.7.

Two invariants govern this module:

**Raw sources stay immutable.** Originals are kept untouched (for the mirror, the downloaded
snapshot); only derived text is indexed. When a chunk looks wrong six months
from now, the original is still there to check against - which is the difference between
a knowledge base you can audit and one you have to trust.

**Sanitization is defense in depth, not the defense.** Stripping instruction-shaped
markers from untrusted content raises the cost of a naive injection, and that is all it
does. A determined attacker rephrases. The actual defense is the trust model: untrusted
content is quarantined, weighted down, and never assembled into a context that also
holds tools. Treating sanitization as sufficient is how systems get
compromised by a payload nobody thought to pattern-match.

Chunking is heading-aware for Markdown so a chunk boundary does not fall mid-argument,
with a token overlap so a fact spanning a boundary is retrievable from either side.

Each chunk also records where it came from: the pages it spans and the path of headings above
it. Both are read off the markdown itself - ``<!-- page N -->`` comments and ``#`` headings -
so a rebuild that re-chunks the source markdown recovers them with no side channel, and a
chunk cites "15.3 Solid Lubricants, p. 231" instead of only its document. The comments are
removed before anything is embedded.

See docs/DESIGN.md section 12.7.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

from wiki_mirror.text.indexed_text import SECTION_SEPARATOR, compose_indexed_text

__all__ = ["Chunk", "ChunkConfig", "chunk_text", "sanitize"]

#: Chunks shorter than this carry too little context to be worth an embedding.
#: A shorter floor suits terse text such as emails; 32 suits documents.
MIN_CHUNK_CHARS = 32

#: Rough characters per token. Used to translate the spec's token budgets into the
#: character arithmetic chunking actually does - approximate on purpose, since the real
#: tokenizer differs per model and the boundary does not need to be exact.
_CHARS_PER_TOKEN = 4

#: Phrases that appear in injection attempts. Neutralized rather than deleted, so a
#: reviewer reading the quarantined chunk can still see what the source tried.
#:
#: **Idempotent**: a marker already wrapped as ``[neutralized: <marker>]`` is left alone (the
#: lookbehind). Sanitizing twice must give the first result, because a store
#: that keeps the *sanitized* text gets it sanitized again by a rebuild - without this a
#: document id that hashes the text would change on every rebuild.
_INSTRUCTION_MARKERS = re.compile(
    r"(?<!\[neutralized: )\b(ignore (?:all )?(?:previous|prior|above) instructions"
    r"|disregard (?:all )?(?:previous|prior) (?:instructions|context)"
    r"|you are now"
    r"|system prompt"
    r"|new instructions?"
    r"|forget everything)\b",
    re.IGNORECASE,
)

#: Collapse runs of whitespace, but keep paragraph breaks - they carry structure that
#: heading-aware chunking depends on.
_HORIZONTAL_WS = re.compile(r"[ \t]+")

#: The same collapse, except for a line's leading indentation. A note's nested
#: bullets and code blocks are written with it, and flattening them on save would hand the
#: operator back a different note than the one they typed.
_INNER_HORIZONTAL_WS = re.compile(r"(?<=\S)[ \t]+")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")

#: Markdown ATX headings, which make natural chunk boundaries.
_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

#: The page marker a PDF parser writes at the top of each PDF page, on a line of
#: its own. Consumed with its newline so removing it leaves no blank line behind.
_PAGE_MARKER = re.compile(r"^<!-- page (\d+) -->[ \t]*\n?", re.MULTILINE)

#: The page numbers a marker may carry and be believed. Pages are 1-based (a page number
#: below 1 is invalid), and no document has 100,000 pages; a
#: marker outside the range is text somebody wrote, not one the parser did. Without the
#: bound a page containing ``<!-- page 0 -->`` made every chunk after it an invalid
#: payload, and the same document failed again on every rebuild.
_MIN_PAGE = 1
_MAX_PAGE = 100_000

#: The placeholder for a figure that was found but never described (``figure_marker``).
_FIGURE_MARKER = re.compile(r"<!-- figure \d+\.\d+ -->")


@dataclass(frozen=True)
class ChunkConfig:
    """Chunking parameters, in tokens as the spec states them."""

    max_tokens: int = 512
    overlap_tokens: int = 50

    @property
    def max_chars(self) -> int:
        """Approximate character budget for one chunk."""
        return self.max_tokens * _CHARS_PER_TOKEN

    @property
    def overlap_chars(self) -> int:
        """Approximate character overlap between adjacent chunks."""
        return self.overlap_tokens * _CHARS_PER_TOKEN


@dataclass(frozen=True)
class Chunk:
    """One indexable piece of a document."""

    index: int
    text: str
    #: The heading this chunk sits under, when the source had one. Prepended to the
    #: indexed text so a chunk retains the context its heading gave it.
    heading: str = ""
    #: First and last page the chunk's text came from, 1-based as the PDF numbers them (the
    #: position in the file, not a number printed on the page). ``None`` when the document
    #: carried no page markers. Not part of :meth:`indexed_text`: a page number is a place to
    #: look, not something a query should match.
    page_start: int | None = None
    page_end: int | None = None
    #: Every heading above the chunk, outermost first, joined by ``SECTION_SEPARATOR``.
    #: ``heading`` is the last of them. Empty when the chunk sits under no heading.
    section_path: str = ""

    def indexed_text(self, title: str = "") -> str:
        """What actually gets embedded, sparse-encoded and coverage-checked.

        The heading is included because a chunk reading "It listens on 8080" is
        near-useless on its own and quite useful under "## Load Balancer". The document
        title is too, for the same reason one level up.

        Args:
            title: The document title, when the ingest request carried one.
        """
        return compose_indexed_text(self.text, self.heading, title)


def sanitize(text: str, *, untrusted: bool = False, keep_indent: bool = False) -> str:
    """Normalize text for indexing, neutralizing injection markers in untrusted input.

    Args:
        text: Raw extracted text.
        untrusted: Whether the source is one we do not control. Only then are
            instruction markers neutralized - doing it to operator notes would mangle
            legitimate writing about prompt injection, which is a topic this operator
            has every reason to write about.
        keep_indent: Leave each line's leading whitespace alone and collapse only the runs
            inside a line. For text the operator typed and will read back as
            markdown, where indentation is structure. Off for everything else, so no
            existing document's fingerprint changes.

    Returns:
        Normalized text, ready to chunk.
    """
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = (_INNER_HORIZONTAL_WS if keep_indent else _HORIZONTAL_WS).sub(" ", cleaned)
    cleaned = _EXCESS_BLANK_LINES.sub("\n\n", cleaned)
    cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n"))

    if untrusted:
        # Bracketed rather than removed: a reviewer looking at the quarantined chunk
        # needs to see what the source attempted, and a silently scrubbed payload
        # teaches nobody anything.
        cleaned = _INSTRUCTION_MARKERS.sub(
            lambda match: f"[neutralized: {match.group(0)}]", cleaned
        )

    # Only blank lines are trimmed when indentation is kept: `.strip()` would also eat the
    # indent of a first line that begins with one, and a note that opens with a nested
    # bullet would come back flattened. Each line is already right-stripped above.
    return cleaned.strip("\n") if keep_indent else cleaned.strip()


def chunk_text(
    text: str, config: ChunkConfig | None = None, *, min_chars: int = MIN_CHUNK_CHARS
) -> list[Chunk]:
    """Split sanitized text into overlapping, heading-aware chunks.

    Markdown headings become section boundaries, so a chunk does not straddle two
    unrelated topics. Long sections are split further with an overlap, so a fact that
    lands near a boundary is retrievable from either side.

    Args:
        text: Sanitized text.
        config: Chunk sizing. Defaults to the spec's 512/50.
        min_chars: Shortest piece worth keeping. Defaults to :data:`MIN_CHUNK_CHARS`; an
            operator's own note lowers it to 1, because for a note the whole text
            is the thing and dropping it as "too short" would lose it outright.

    Returns:
        Chunks in document order. Anything shorter than ``min_chars`` is dropped - too
        little context to embed usefully, and it would dilute retrieval. Each carries the
        pages it spans and its section path (see :class:`Chunk`); the page and figure
        comments the parser left in ``text`` are not in any chunk's text.
    """
    settings = config or ChunkConfig()
    chunks: list[Chunk] = []
    clean, page_marks = _strip_markers(text)

    # A piece never crosses a heading: sections are split one at a time, so a chunk's
    # section path and page span describe all of it and not just where it began.
    for section in _sections(clean):
        for start, end in _split_section(section.body, settings):
            piece = section.body[start:end]
            body = piece.strip()
            if len(body) < min_chars:
                continue
            first = section.offset + start + (len(piece) - len(piece.lstrip()))
            page_start, page_end = _page_span(page_marks, first, first + len(body))
            chunks.append(
                Chunk(
                    index=len(chunks),
                    text=body,
                    heading=section.heading,
                    page_start=page_start,
                    page_end=page_end,
                    section_path=section.path,
                )
            )

    return chunks


@dataclass(frozen=True)
class _Section:
    """One run of text under a heading, located in the marker-free document text."""

    #: The nearest heading's text; empty before the first heading.
    heading: str
    #: Every enclosing heading, outermost first, joined by ``SECTION_SEPARATOR``.
    path: str
    body: str
    #: Where ``body`` starts in the text the sections were cut from, so a piece of it can be
    #: placed on a page without searching for it.
    offset: int


def _strip_markers(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Remove the inert HTML-comment markers a parser leaves, noting where the pages begin.

    The parser keeps ``<!-- page N -->`` in the markdown, so a rebuild can recover
    page numbers by re-chunking, and ``<!-- figure P.I -->`` where a diagram was found but
    never described. Neither is content: embedded, they put "page 231" into every chunk's
    vector and print it in every answer's quoted passage. So they are lifted out here, once,
    and the page markers survive only as ``(offset, page)`` positions in the stripped text.

    A page marker inside ``[_MIN_PAGE, _MAX_PAGE]`` is trusted as given. An untrusted capture
    could forge one, and the cost is a wrong page in a citation for that capture, which is
    already marked untrusted. One outside the range cannot be a page: it is removed like any
    marker but starts no page, so the chunks around it carry the page they would have without
    it (or none).

    Args:
        text: Sanitized document text, possibly carrying markers.

    Returns:
        The text without the markers, and ``(offset, page)`` for each page marker in order,
        where ``offset`` is the position in the returned text that page begins at.
    """
    marks: list[tuple[int, int]] = []
    out: list[str] = []
    length = 0
    cursor = 0
    for match in _PAGE_MARKER.finditer(text):
        kept = _FIGURE_MARKER.sub("", text[cursor : match.start()])
        out.append(kept)
        length += len(kept)
        digits = match.group(1)
        # The length test comes first: `int` refuses a digit string past 4,300 characters,
        # and this text can be an untrusted capture.
        if len(digits) <= len(str(_MAX_PAGE)) and _MIN_PAGE <= int(digits) <= _MAX_PAGE:
            marks.append((length, int(digits)))
        cursor = match.end()
    out.append(_FIGURE_MARKER.sub("", text[cursor:]))
    return "".join(out), marks


def _page_span(
    marks: list[tuple[int, int]], start: int, end: int
) -> tuple[int | None, int | None]:
    """The pages a stretch of marker-free text covers.

    Args:
        marks: ``(offset, page)`` from :func:`_strip_markers`, in document order.
        start: Offset of the first character.
        end: Offset one past the last character.

    Returns:
        ``(first_page, last_page)``, or ``(None, None)`` for text before any page marker -
        a document that has none (a note, a web page) never gets a page.
    """
    if not marks:
        return None, None
    offsets = [offset for offset, _ in marks]
    first = bisect.bisect_right(offsets, start) - 1
    last = bisect.bisect_right(offsets, max(start, end - 1)) - 1
    if first < 0:
        return None, None
    return marks[first][1], marks[last][1]


def _sections(text: str) -> list[_Section]:
    """Split into sections at Markdown headings, each carrying its path of enclosing headings.

    The path is a stack by heading level: a heading closes every open heading at its level
    or deeper and then opens itself, so ``# Part`` / ``## Chapter`` / ``### 15.3 X`` reads
    "Part / Chapter / 15.3 X" (joined by ``SECTION_SEPARATOR``) and a following
    ``## Next chapter`` drops the section back out. A heading with no body still opens its
    level - a chapter title page carries nothing but the heading, and every section under it
    needs it in their path.
    """
    matches = list(_HEADING.finditer(text))
    if not matches:
        return [_Section("", "", text, 0)]

    sections: list[_Section] = []

    preamble = text[: matches[0].start()]
    if preamble.strip():
        sections.append(_Section("", "", preamble.strip(), len(preamble) - len(preamble.lstrip())))

    open_headings: list[tuple[int, str]] = []
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        level = len(match.group(1))
        heading = match.group(2).strip()
        while open_headings and open_headings[-1][0] >= level:
            open_headings.pop()
        # The separator is swapped out of a heading that happens to contain it, or the path
        # could not be split back into its levels.
        open_headings.append((level, heading.replace(SECTION_SEPARATOR.strip(), ">")))

        raw = text[match.end() : end]
        body = raw.strip()
        if body:
            path = SECTION_SEPARATOR.join(name for _, name in open_headings)
            sections.append(
                _Section(heading, path, body, match.end() + len(raw) - len(raw.lstrip()))
            )

    return sections


def _split_section(section: str, config: ChunkConfig) -> list[tuple[int, int]]:
    """Split one section into overlapping spans, preferring paragraph boundaries.

    Returns:
        ``(start, end)`` offsets into ``section`` rather than the pieces, so the caller can
        say which page each piece begins and ends on.
    """
    if len(section) <= config.max_chars:
        return [(0, len(section))]

    spans: list[tuple[int, int]] = []
    start = 0

    while start < len(section):
        end = min(start + config.max_chars, len(section))

        if end < len(section):
            # Prefer a paragraph break, then a sentence end, then wherever we landed.
            # Cutting mid-sentence produces chunks that read as gibberish in a citation.
            window = section[start:end]
            boundary = max(window.rfind("\n\n"), window.rfind(". "))
            if boundary > config.max_chars // 2:
                end = start + boundary + 1

        spans.append((start, end))

        if end >= len(section):
            break
        start = max(start + 1, end - config.overlap_chars)

    return spans
