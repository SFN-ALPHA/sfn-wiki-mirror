"""What a passage is indexed under: the section separator and `compose_indexed_text`.

`sanitize` builds each chunk's section path with
`SECTION_SEPARATOR`, and `passages` encodes `compose_indexed_text(...)` for every passage.

The built indexes depend on this module's exact behaviour: the string below is what the keyword
vectors were encoded from, so changing it needs a keyword re-index, and changing the separator
changes the section paths the chunker emits, which needs `CHUNKER_VERSION` bumped. See
docs/DESIGN.md section 12.7.
"""

from __future__ import annotations

__all__ = ["SECTION_SEPARATOR", "compose_indexed_text"]

#: Joins the headings of a section path. A guillemet rather than a slash or a colon because
#: a heading is free text and routinely contains both ("Chapter 15: Lubrication", "TiN/MoS2
#: coatings"), and the path is split on this to find the leaf.
SECTION_SEPARATOR = " › "  # noqa: RUF001 - U+203A, deliberately not ">"


def compose_indexed_text(text: str, heading: str = "", title: str = "") -> str:
    """The one definition of what a chunk is indexed under.

    Both vectors (dense embedding and sparse encoding), the sparse corpus statistics, and
    the term-coverage check on lexical hits are all built from this string, because a
    lexical channel that matches on one text and filters on another discards its own
    hits: a note whose title words appear only in its H1 matched on them and was then
    filtered out for not containing them.

    Args:
        text: The plain chunk text.
        heading: The section heading the chunk sits under, if any.
        title: The document title, as context. Put in front of every chunk so a passage
            deep in a note ("## What follows") is still reachable by the note's title -
            how a person asks about their own notes. Dropped when it equals the heading,
            where repeating it would only double that chunk's weight on those words.

    Returns:
        ``title``, ``heading`` and ``text`` joined by blank lines, empty parts skipped.
    """
    title = title.strip()
    heading = heading.strip()
    if title.casefold() == heading.casefold():
        title = ""
    return "\n\n".join(part for part in (title, heading, text.strip()) if part)
