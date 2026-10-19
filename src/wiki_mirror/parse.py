"""Enterprise's rendered (Parsoid) article HTML to clean, sectioned plain text.

The text this module produces is what the assistant reads and cites, so it is the one place in
the mirror where quality is the whole job. `parse_record` turns one Enterprise NDJSON
record into an `Article`; `iter_chunk_articles` streams every record out of a downloaded chunk
(`<chunk>.tar.gz` holding one `chunk_<n>.ndjson`). `build` runs it in worker processes and
`store` keeps the result.

What the text looks like (docs/DESIGN.md section 5):

* **Headings** `h2`..`h6` become `##`..`######` lines, so the chunker (`text.sanitize`, which
  splits on markdown headings and records each chunk's section path) gets the article's own outline.
  Paragraphs are separated by a blank line; list items are `- ` lines (nesting is flattened, the
  chunker has no use for indentation and `sanitize` would collapse it anyway).
* **The infobox** (`table.infobox`) is one `label: value` line per row (values trimmed to 200
  characters, 40 rows at most) under a `## Infobox` heading. It is placed AFTER the lead
  paragraphs and before the first real heading, never at the top: the chunker prepends
  the nearest heading to every chunk, so an infobox first would file the lead paragraph under
  "Infobox" and drop the sentence a search most wants ("X is a ...") out of the lead section.
  An infobox met after the first heading stays where it is.
* **Data tables** (`table.wikitable`) are one line per row, cells joined by ` | `, header row
  first, 40 rows at most then `(table truncated: N more rows)`. Other tables are layout
  (multi-column lists) and are read through as ordinary blocks.
* **Maths** keeps its LaTeX alt text inline as `$...$`; it is never dropped.
* **Figures** keep their caption as a `Figure: <caption>` line; bare images are dropped.
* **Dropped:** `head`, styles, scripts, the `[1]` reference markers, reference lists, navboxes and
  sidebars, hatnotes ("For other uses, see ..."), maintenance and message boxes, the hidden short
  description, edit links, `noprint` content (including "[citation needed]"), anything styled
  `display:none`, and whole sections headed References, Notes, Citations, Footnotes, Sources,
  Bibliography, Further reading, External links or See also.

Non-obvious constraints:

* **Wikipedia is untrusted.** Both `text` and `abstract` pass through
  `sanitize(..., untrusted=True)`, which neutralises instruction-shaped phrases. That also
  rewrites an article that merely discusses them ("system prompt"); the rule lives in
  `text.sanitize`, and is an accepted cost.
* **No DOM mutation after the first pass.** A single CSS query collects the `mem_id` of every
  node to skip, and the walk consults that set. `decompose()` on a node nested inside another
  already-decomposed one frees it twice, which is unsafe, and the set is cheaper anyway.
* **Ids are ints in the sample but `_as_int` also takes digit strings**, because the schema
  Wikimedia publishes types its identifiers loosely and a silent drop of every record would be
  the cost of guessing wrong.
* **One bad record never costs its neighbours.** `iter_chunk_articles` logs and skips a malformed
  line or a record that makes the parser raise; a corrupt archive does raise.
* **The output text is versioned by hand.** `PARSER_VERSION` is recorded in every article store,
  and the monthly update (W3) copies a previous month's text forward only when the versions
  match, so a parser change must bump it. The parser's output also depends on
  `text.sanitize.sanitize`, which this module calls: a change there that alters text is a bump too.
* **Every article carries `text_hash(text)`**, computed here from the final sanitized text and
  nothing else, so the store, the index payload and the W3 diff all agree on it.

See docs/DESIGN.md sections 5 and 12.
"""

from __future__ import annotations

import json
import logging
import re
import tarfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Final

from selectolax.lexbor import LexborHTMLParser, LexborNode

from wiki_mirror.records import Article, text_hash
from wiki_mirror.text.sanitize import sanitize

__all__ = ["PARSER_VERSION", "iter_chunk_articles", "parse_record"]

logger = logging.getLogger("wiki_mirror.parse")

#: Bumped by hand whenever the parser's output text changes for the same input; a change forces a
#: full reparse instead of copying stale text forward (docs/DESIGN.md section 12.6). Recorded in the
#: article store's `meta` when a build creates it. The first value, 1, is the W1 parser.
PARSER_VERSION: Final = 1

#: Longest infobox or data table, in rows. A sports-results table can run to thousands of rows,
#: which would drown the prose around it in a passage index; the cap keeps the head, where the
#: header row and the first (usually most important) rows are.
_MAX_ROWS: Final = 40

#: Longest infobox value, in characters. Infobox cells hold lists (every member of a band,
#: every episode); the first 200 characters carry the answer a search is after.
_MAX_VALUE_CHARS: Final = 200

#: Longest cell in a data table. Longer than an infobox value because table cells can hold a
#: sentence of prose (a timeline's event), which is content rather than a label.
_MAX_CELL_CHARS: Final = 500

#: Beyond this nesting the walk falls back to flat text. Real pages stay under about 60 levels;
#: the cap is a guard against a pathological document hitting Python's recursion limit.
_MAX_DEPTH: Final = 300

#: Everything the walk must not read, as one CSS query so lexbor matches it in a single pass.
#: Each entry is a Wikipedia convention, checked against the 40-record Enterprise sample:
#: `.noprint` is the "[citation needed]" and "(edit)" chrome; `.metadata` and `.ambox` are the
#: maintenance and message boxes; `span[typeof^="mw:File"]` is an inline image (flag icons); the
#: two `display:none` spellings catch hidden microformat duplicates such as the `bday` span.
_SKIP_SELECTOR: Final = ", ".join(
    (
        "sup.reference",
        ".mw-ref",
        ".cite-bracket",
        ".mw-cite-backlink",
        ".reflist",
        ".references",
        ".mw-references-wrap",
        ".navbox",
        ".navbox-styles",
        ".navigation-not-searchable",
        "[role=navigation]",
        ".navbar",
        ".sidebar",
        ".noprint",
        ".Inline-Template",
        ".metadata",
        ".ambox",
        ".hatnote",
        ".dablink",
        ".rellink",
        ".shortdescription",
        ".mw-editsection",
        ".mw-empty-elt",
        ".sistersitebox",
        ".side-box",
        ".toc",
        "#toc",
        ".catlinks",
        ".authority-control",
        ".Z3988",
        "[hidden]",
        'span[typeof^="mw:File"]',
        '[style*="display:none"]',
        '[style*="display: none"]',
    )
)

#: Elements that are never text: document furniture, scripts and embedded media.
_SKIP_TAGS: Final = frozenset(
    {
        "head", "style", "script", "link", "meta", "base", "title", "noscript", "template",
        "img", "svg", "audio", "video", "iframe", "button", "input", "select", "textarea",
        "map", "canvas", "object", "embed",
    }
)  # fmt: skip

#: Elements that end the text before them and start new text after: a paragraph break, not an
#: inline span. Table rows and cells are here so a layout table reads as a run of blocks.
_BLOCK_TAGS: Final = frozenset(
    {
        "p", "div", "section", "article", "aside", "header", "footer", "main", "nav", "ul", "ol",
        "dl", "dt", "dd", "table", "caption", "thead", "tbody", "tfoot", "tr", "td", "th",
        "blockquote", "pre", "hr", "center", "details", "summary", "address", "body", "figure",
        "figcaption", "form", "fieldset",
    }
)  # fmt: skip

_HEADING_TAGS: Final = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

#: Sections whose body is a link farm or a bibliography, not content. Compared case-insensitively
#: after trimming a trailing colon. The first nine are the brief's list; the rest are the common
#: variants of the same sections on the real site.
_DROPPED_HEADINGS: Final = frozenset(
    {
        "references", "notes", "citations", "footnotes", "sources", "bibliography",
        "further reading", "external links", "see also",
        "notes and references", "references and notes", "explanatory notes", "works cited",
        "general references", "cited sources", "external links and references",
    }
)  # fmt: skip

#: Marks a boundary inside text collected for a table cell or heading (see `_Walker.collect`).
_SEPARATOR: Final = "\x00"

#: Invisible characters Wikipedia sprinkles around names (directional marks, soft hyphens,
#: zero-width spaces). They break lexical matching and say nothing.
_INVISIBLE: Final = str.maketrans(
    dict.fromkeys(
        "\N{ZERO WIDTH SPACE}\N{ZERO WIDTH NON-JOINER}\N{ZERO WIDTH JOINER}"
        "\N{LEFT-TO-RIGHT MARK}\N{RIGHT-TO-LEFT MARK}\N{WORD JOINER}"
        "\N{ZERO WIDTH NO-BREAK SPACE}\N{SOFT HYPHEN}"
    )
)

#: The `{\displaystyle ...}` wrapper MediaWiki's Math extension puts around every alt text.
_MATH_WRAPPER: Final = re.compile(
    r"^\{\\(?:displaystyle|textstyle|scriptstyle)\s*(.*)\}$", re.DOTALL
)

#: A superscript that is an exponent (`10<sup>3</sup>`), not an ordinal suffix (`1<sup>st</sup>`).
#: Digits, signs (hyphen, minus, en dash), and the separators a number may carry.
_EXPONENT: Final = re.compile(r"^[\d+\-\N{MINUS SIGN}\N{EN DASH}.,]+$")

#: Namespace prefixes whose redirects are not article titles ("Wikipedia:Foo", "WP:FOO",
#: "Talk:Foo"). Titles that merely contain a colon ("Star Trek: Voyager") are real article names
#: and kept.
_NON_ARTICLE_PREFIXES: Final = frozenset(
    {
        "wikipedia", "wp", "wt", "project", "project talk", "talk", "user", "user talk", "template",
        "template talk", "t", "category", "cat", "file", "image", "media", "help", "h", "portal",
        "p", "draft", "module", "mediawiki", "mos", "mp", "book", "timedtext", "special", "topic",
        "wikipedia talk", "file talk", "category talk", "help talk", "portal talk", "draft talk",
        "module talk", "mediawiki talk", "book talk",
    }
)  # fmt: skip

#: `CC-BY-SA-4.0` -> `CC BY-SA 4.0`.
_LICENSE_ID: Final = re.compile(r"^CC-(BY(?:-[A-Za-z]+)*)-(\d(?:\.\d)?)$")

#: What Enterprise calls a disambiguation page, in its categories ("Disambiguation pages", "Human
#: name disambiguation pages", "All article disambiguation pages") and templates.
_DISAMBIGUATION_CATEGORY: Final = "disambiguation pages"
_DISAMBIGUATION_TEMPLATES: Final = frozenset(
    {"template:disambiguation", "template:disambig", "template:dmbox", "template:geodis"}
)

_DEFAULT_LICENSE: Final = "CC BY-SA 4.0"


def parse_record(record: Mapping[str, object]) -> Article | None:
    """Turn one Enterprise record into an `Article`.

    Args:
        record: One decoded NDJSON line from a snapshot chunk.

    Returns:
        The article, or None for a record the mirror does not keep: outside namespace 0, missing
        its page id, title, revision or HTML, or with no readable text once the page furniture is
        removed.
    """
    namespace = record.get("namespace")
    if isinstance(namespace, Mapping):
        ns_id = _as_int(namespace.get("identifier"))
        if ns_id is not None and ns_id != 0:
            return None
    page_id = _as_int(record.get("identifier"))
    title = record.get("name")
    version = record.get("version")
    revision_id = _as_int(version.get("identifier")) if isinstance(version, Mapping) else None
    body = record.get("article_body")
    html = body.get("html") if isinstance(body, Mapping) else None
    if (
        page_id is None
        or revision_id is None
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(html, str)
        or not html.strip()
    ):
        return None

    text = sanitize(html_to_text(html), untrusted=True)
    if not text:
        return None
    abstract = record.get("abstract")
    modified = record.get("date_modified")
    categories = _names(record.get("categories"), strip_prefix="Category:")
    return Article(
        page_id=page_id,
        title=title.strip(),
        revision_id=revision_id,
        revision_time=modified if isinstance(modified, str) else "",
        text=text,
        text_hash=text_hash(text),
        abstract=sanitize(abstract, untrusted=True) if isinstance(abstract, str) else "",
        categories=categories,
        redirects=_redirect_titles(record.get("redirects")),
        is_disambiguation=_is_disambiguation(categories, record.get("templates")),
        license=_license(record.get("license")),
    )


def iter_chunk_articles(path: Path) -> Iterator[Article]:
    """Stream the articles of one downloaded chunk, one NDJSON line at a time.

    Never reads a whole member into memory: a chunk expands to about 2 GB, and the parse workers
    run several at once on an 8 GB box.

    Args:
        path: A chunk archive, `<chunk>.tar.gz` holding one or more `.ndjson` members.

    Yields:
        Every article that `parse_record` keeps, in file order. Duplicates across chunks are the
        store's business (highest revision wins).

    Raises:
        tarfile.ReadError: The file is not a tar archive.
        EOFError: The gzip stream is truncated.
        OSError: The file cannot be read, or the gzip stream is corrupt (`gzip.BadGzipFile`,
            `zlib.error` are raised as themselves). A corrupt archive is never skipped: a chunk
            parsed half-way would be marked done and its articles lost for the month.
    """
    kept = skipped = malformed = failed = 0
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith(".ndjson"):
                continue
            stream = archive.extractfile(member)
            if stream is None:
                continue
            with stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        malformed += 1
                        logger.warning(
                            "skipped a malformed NDJSON line (%d so far)",
                            malformed,
                            extra={
                                "component": "parse",
                                "event": "malformed_line",
                                "line": line_number,
                            },
                        )
                        continue
                    if not isinstance(record, dict):
                        malformed += 1
                        continue
                    try:
                        article = parse_record(record)
                    except Exception as exc:
                        # Deliberately broad: a parser bug on one odd page would otherwise abort
                        # ~20,000 neighbours. Each skip is logged and the total is logged at the
                        # end, so a systematic failure is loud.
                        failed += 1
                        logger.warning(
                            "skipped a record the parser raised on (%s)",
                            type(exc).__name__,
                            extra={
                                "component": "parse",
                                "event": "record_failed",
                                "line": line_number,
                            },
                        )
                        continue
                    if article is None:
                        skipped += 1
                        continue
                    kept += 1
                    yield article
    logger.info(
        "parsed a chunk: %d kept, %d skipped, %d malformed, %d failed",
        kept,
        skipped,
        malformed,
        failed,
        extra={"component": "parse", "event": "chunk_parsed", "chunk": path.name},
    )


def html_to_text(html: str) -> str:
    """Render an Enterprise article's HTML as the mirror's clean text (see the module docstring).

    Args:
        html: The full Parsoid document from `article_body.html`.

    Returns:
        The text, before `sanitize`; empty when the page has no readable content.
    """
    tree = LexborHTMLParser(html)
    body = tree.body
    if body is None:
        return ""
    # Maths first: its wrapper is `display:none` (the visible part is an image), so the skip
    # pass below would otherwise delete the one thing about it that is text.
    # Two passes, because the wrapper contains the bare `math` element: replacing the wrapper
    # first and then touching its (now freed) child from the same result list would be unsafe.
    for selector in (".mwe-math-element", "math"):
        for math in tree.css(selector):
            latex = _math_text(math)
            math.replace_with(f"${latex}$" if latex else "")
    skip = {node.mem_id for node in tree.css(_SKIP_SELECTOR)}
    walker = _Walker(skip)
    walker.walk(body, 0)
    return walker.finish()


class _Walker:
    """Reads a Parsoid DOM into ordered text blocks.

    One instance per document. `walk` appends to `buf` (the paragraph being built) and ends it at
    block boundaries; `collect` runs the same walk in a mode that joins a subtree's text into one
    line instead (table cells, headings, captions).
    """

    def __init__(self, skip: set[int]) -> None:
        self._skip = skip
        #: Finished blocks as (kind, text); kind is "p", "li", "h" or "pre" (a multi-line block).
        self._blocks: list[tuple[str, str]] = []
        self._buf: list[str] = []
        #: List-item prefix waiting for the next `_flush` ("- " when inside an `li`).
        self._prefix = ""
        self._collecting = False
        #: Infobox blocks held back until the lead paragraphs are done (module docstring).
        self._pending_infobox: list[str] = []
        self._lead_done = False
        #: Level of the dropped section being skipped (0 when not skipping).
        self._drop_level = 0

    def walk(self, node: LexborNode, depth: int) -> None:
        """Read `node`'s children in order, recursing into elements.

        Args:
            node: The element whose children are read.
            depth: Nesting so far; past `_MAX_DEPTH` the subtree is read as flat text.
        """
        child = node.child
        while child is not None:
            tag = child.tag
            if tag == "-text":
                if not self._drop_level:
                    self._buf.append(child.text_content or "")
            elif tag is None or tag[0] == "-" or tag in _SKIP_TAGS or child.mem_id in self._skip:
                pass
            elif depth > _MAX_DEPTH:
                if not self._drop_level:
                    self._buf.append(child.text() or "")
            else:
                self._element(child, tag, depth)
            child = child.next

    def _element(self, node: LexborNode, tag: str, depth: int) -> None:
        """Dispatch one element on its tag."""
        if tag in _HEADING_TAGS:
            self._heading(node, tag)
        elif tag == "table":
            self._table(node, depth)
        elif self._drop_level and tag not in ("section", "div", "article", "main", "body"):
            # Inside a dropped section only headings matter (they end it); everything else, a
            # long "See also" list for instance, is not worth walking.
            return
        elif tag == "br":
            self._buf.append(self._line_break())
        elif tag == "figure":
            self._figure(node)
        elif tag == "li":
            self._boundary()
            self._prefix = "- " if not self._collecting else ""
            self.walk(node, depth + 1)
            self._boundary()
        elif tag == "sup":
            text = self.collect(node, " ")
            if text:
                self._buf.append(f"^{text}" if _EXPONENT.match(text) else text)
        elif tag in _BLOCK_TAGS:
            self._boundary()
            self.walk(node, depth + 1)
            self._boundary()
        else:
            self.walk(node, depth + 1)

    def _line_break(self) -> str:
        """What a `<br>` becomes: a separator when collecting a cell, else a space."""
        return _SEPARATOR if self._collecting else " "

    def _boundary(self) -> None:
        """End the current text run: a flush, or a separator while collecting."""
        if self._collecting:
            self._buf.append(_SEPARATOR)
        else:
            self._flush()

    def _flush(self) -> None:
        """Turn the paragraph being built into a block, if it holds any text."""
        if not self._buf:
            self._prefix = ""
            return
        text = _squash("".join(self._buf))
        self._buf.clear()
        prefix, self._prefix = self._prefix, ""
        if text:
            self._blocks.append(("li", prefix + text) if prefix else ("p", text))

    def collect(self, node: LexborNode, sep: str) -> str:
        """Read a subtree as one line, with `sep` between its blocks, items and line breaks.

        Args:
            node: The element to read.
            sep: What goes between separated runs: a space for headings and captions, ", " for
                table cells so a list of three links reads as "A, B, C".
        """
        saved = (self._buf, self._collecting, self._prefix, self._drop_level)
        self._buf, self._collecting, self._prefix, self._drop_level = [], True, "", 0
        try:
            self.walk(node, 1)
            joined = "".join(self._buf)
        finally:
            self._buf, self._collecting, self._prefix, self._drop_level = saved
        runs = (_squash(run) for run in joined.split(_SEPARATOR))
        return sep.join(run for run in runs if run)

    def _heading(self, node: LexborNode, tag: str) -> None:
        """Emit a `##` heading, or start (or continue) skipping a dropped section."""
        text = self.collect(node, " ")
        if not text:
            return
        if self._collecting:
            self._buf.append(_SEPARATOR + text + _SEPARATOR)
            return
        level = int(tag[1])
        if self._drop_level and level > self._drop_level:
            return
        self._drop_level = 0
        self._flush()
        if text.casefold().rstrip(": ") in _DROPPED_HEADINGS:
            self._drop_level = level
            return
        self._release_infobox()
        self._blocks.append(("h", f"{'#' * level} {text}"))

    def _release_infobox(self) -> None:
        """End the lead: write out any held infobox under its own heading."""
        if self._pending_infobox:
            self._blocks.append(("h", "## Infobox"))
            self._blocks.extend(("pre", block) for block in self._pending_infobox)
            self._pending_infobox.clear()
        self._lead_done = True

    def _figure(self, node: LexborNode) -> None:
        """Keep a figure's caption as a `Figure:` line; the image itself is dropped."""
        if self._collecting or self._drop_level:
            return
        for child in _children(node):
            if child.tag == "figcaption" and child.mem_id not in self._skip:
                caption = self.collect(child, " ")
                if caption:
                    self._flush()
                    self._blocks.append(("p", f"Figure: {caption}"))
                return

    def _table(self, node: LexborNode, depth: int) -> None:
        """Route a table: infobox and data tables become line blocks, others are read through."""
        classes = (node.attributes.get("class") or "").split()
        if self._drop_level:
            return
        if self._collecting or not ("infobox" in classes or "wikitable" in classes):
            self._boundary()
            self.walk(node, depth + 1)
            self._boundary()
            return
        if "infobox" in classes:
            lines = self._infobox_lines(node)
            if lines:
                block = "\n".join(lines)
                if self._lead_done:
                    self._flush()
                    self._blocks.append(("pre", block))
                else:
                    self._pending_infobox.append(block)
        else:
            lines = self._table_lines(node)
            if lines:
                self._flush()
                self._blocks.append(("pre", "\n".join(lines)))

    def _rows(self, table: LexborNode) -> Iterator[list[LexborNode]]:
        """Yield the cells (th/td) of each of a table's own rows, skipping nested tables' rows."""
        for section in _children(table):
            tag = section.tag
            if tag == "tr":
                rows = [section]
            elif tag in ("thead", "tbody", "tfoot"):
                rows = [row for row in _children(section) if row.tag == "tr"]
            else:
                continue
            for row in rows:
                if row.mem_id in self._skip:
                    continue
                yield [
                    cell
                    for cell in _children(row)
                    if cell.tag in ("th", "td") and cell.mem_id not in self._skip
                ]

    def _infobox_lines(self, table: LexborNode) -> list[str]:
        """One `label: value` line per two-cell row; a one-cell row (title, header) is bare."""
        lines: list[str] = []
        for cells in self._rows(table):
            texts = [self.collect(cell, ", ") for cell in cells]
            texts = [text for text in texts if text]
            if not texts:
                continue
            if len(lines) >= _MAX_ROWS:
                break
            if len(texts) == 1:
                lines.append(_clip(texts[0], _MAX_VALUE_CHARS))
            else:
                label = texts[0].rstrip(": ")
                value = _clip("; ".join(texts[1:]), _MAX_VALUE_CHARS)
                lines.append(f"{label}: {value}" if label else value)
        return lines

    def _table_lines(self, table: LexborNode) -> list[str]:
        """Rows of a data table, cells joined by ` | `, capped at `_MAX_ROWS` plus a tally."""
        lines: list[str] = []
        extra = 0
        for child in _children(table):
            if child.tag == "caption" and child.mem_id not in self._skip:
                caption = self.collect(child, " ")
                if caption:
                    lines.append(f"Table: {caption}")
        for cells in self._rows(table):
            texts = [_clip(self.collect(cell, ", "), _MAX_CELL_CHARS) for cell in cells]
            if not any(texts):
                continue
            if len(lines) >= _MAX_ROWS:
                extra += 1
            else:
                lines.append(" | ".join(texts))
        if extra:
            lines.append(f"(table truncated: {extra} more rows)")
        return lines

    def finish(self) -> str:
        """End the document and join the blocks into the final text."""
        self._flush()
        self._release_infobox()
        pieces: list[str] = []
        previous = ""
        for kind, text in self._blocks:
            if pieces:
                # Consecutive list items stay on adjacent lines; everything else is a paragraph.
                pieces.append("\n" if kind == "li" and previous == "li" else "\n\n")
            pieces.append(text)
            previous = kind
        return "".join(pieces)


def _children(node: LexborNode) -> Iterator[LexborNode]:
    """Iterate a node's direct child elements (text and comments excluded)."""
    child = node.child
    while child is not None:
        tag = child.tag
        if tag is not None and tag[0] != "-":
            yield child
        child = child.next


def _squash(text: str) -> str:
    """Collapse all whitespace (non-breaking spaces included) and drop invisible characters."""
    return " ".join(text.translate(_INVISIBLE).split())


def _clip(text: str, limit: int) -> str:
    """Trim text to `limit` characters, marking the cut with an ellipsis."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\N{HORIZONTAL ELLIPSIS}"


def _math_text(node: LexborNode) -> str:
    r"""The LaTeX source of a rendered formula.

    Takes the MathML `alttext`, else the TeX annotation, else the fallback image's alt text; the
    `{\displaystyle ...}` wrapper is removed.
    """
    candidates: list[str | None] = []
    math = node if node.tag == "math" else node.css_first("math")
    if math is not None:
        candidates.append(math.attributes.get("alttext"))
        annotation = math.css_first("annotation")
        candidates.append(annotation.text() if annotation is not None else None)
    image = node.css_first("img")
    if image is not None:
        candidates.append(image.attributes.get("alt"))
    for candidate in candidates:
        if candidate and candidate.strip():
            latex = " ".join(candidate.split())
            wrapped = _MATH_WRAPPER.match(latex)
            return wrapped.group(1).strip() if wrapped else latex
    return ""


def _as_int(value: object) -> int | None:
    """An int from an int or a digit string (Enterprise sends ids as strings); else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _names(entries: object, *, strip_prefix: str = "") -> tuple[str, ...]:
    """The `name` of each `{name, url}` entry, de-duplicated in order, with a prefix removed."""
    if not isinstance(entries, list):
        return ()
    seen: dict[str, None] = {}
    for entry in entries:
        name = entry.get("name") if isinstance(entry, Mapping) else None
        if isinstance(name, str):
            name = name.removeprefix(strip_prefix).strip()
            if name:
                seen[name] = None
    return tuple(seen)


def _redirect_titles(entries: object) -> tuple[str, ...]:
    """Titles that redirect to the article, minus project, talk and shortcut namespaces."""
    kept = []
    for name in _names(entries):
        prefix, colon, _ = name.partition(":")
        if colon and prefix.replace("_", " ").strip().casefold() in _NON_ARTICLE_PREFIXES:
            continue
        kept.append(name)
    return tuple(kept)


def _is_disambiguation(categories: tuple[str, ...], templates: object) -> bool:
    """Whether the page is a disambiguation page, by its categories or its page template."""
    if any(_DISAMBIGUATION_CATEGORY in category.casefold() for category in categories):
        return True
    return any(
        name.casefold() in _DISAMBIGUATION_TEMPLATES for name in _names(templates)
    )


def _license(entries: object) -> str:
    """The first licence as a display line ("CC BY-SA 4.0"); the dump's own licence if absent."""
    if isinstance(entries, list) and entries and isinstance(entries[0], Mapping):
        identifier = entries[0].get("identifier")
        if isinstance(identifier, str):
            match = _LICENSE_ID.match(identifier.strip())
            if match:
                return f"CC {match.group(1)} {match.group(2)}"
            if identifier.strip():
                return identifier.strip()
    return _DEFAULT_LICENSE
