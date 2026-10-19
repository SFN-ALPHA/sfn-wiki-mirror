"""Qualifies the passage cutter: v1 equals the vendored chunker, and packing obeys its limit."""

from __future__ import annotations

import subprocess
import sys

import pytest

from wiki_mirror.passages import (
    CHUNK_CONFIG,
    Passage,
    chunker_fingerprint,
    cut_passages,
)
from wiki_mirror.text.sanitize import chunk_text

pytestmark = pytest.mark.unit

_INTRO = "intro text " * 10
_A = "alpha " * 20
_B = "bravo " * 20
_C = "gamma sentence. " * 300  # long enough that the chunker splits the section

#: Intro, two short sections, a too-short one (dropped by the chunker), and a long split one.
_ARTICLE = (
    f"{_INTRO.strip()}\n\n## A\n\n{_A.strip()}\n\n## B\n\n{_B.strip()}\n\n"
    f"## Tiny\n\ntiny\n\n## C\n\n{_C.strip()}"
)


def test_cut_passages_v1_equals_chunk_text_field_for_field() -> None:
    chunks = chunk_text(_ARTICLE, CHUNK_CONFIG)
    passages = cut_passages(_ARTICLE)

    assert len(chunks) > 4  # the fixture really does exercise a split section
    assert [(p.index, p.text, p.heading, p.section_path) for p in passages] == [
        (c.index, c.text, c.heading, c.section_path) for c in chunks
    ]
    assert [p.indexed_text("Title") for p in passages] == [c.indexed_text("Title") for c in chunks]


def test_cut_passages_v1_dropped_short_piece_leaves_numbers_contiguous() -> None:
    passages = cut_passages(_ARTICLE)

    assert "Tiny" not in {p.heading for p in passages}
    assert [p.index for p in passages] == list(range(len(passages)))


def test_cut_passages_packing_merges_short_consecutive_sections() -> None:
    packed = cut_passages(_ARTICLE, pack_chars=500)

    assert packed[0].text.startswith(_INTRO.strip())
    assert _A.strip() in packed[0].text
    assert _B.strip() in packed[0].text


def test_cut_passages_packing_adds_inline_heading_only_when_it_changes() -> None:
    text = f"## A\n\n{_A.strip()}\n\n## B\n\n{_B.strip()}"
    chunks = chunk_text(text, CHUNK_CONFIG)
    assert [c.heading for c in chunks] == ["A", "B"]

    (passage,) = cut_passages(text, pack_chars=1000)

    assert passage.text == f"{chunks[0].text}\n\nB\n{chunks[1].text}"
    assert passage.heading == "A"
    assert passage.section_path == "A"


def test_cut_passages_packing_same_heading_pieces_join_without_heading_line() -> None:
    pieces = chunk_text(f"## C\n\n{_C.strip()}", CHUNK_CONFIG)
    assert len({p.heading for p in pieces}) == 1
    assert len(pieces) > 1
    limit = sum(len(p.text) for p in pieces) + 2 * len(pieces)

    (passage,) = cut_passages(f"## C\n\n{_C.strip()}", pack_chars=limit)

    assert passage.text == "\n\n".join(p.text for p in pieces)


def test_cut_passages_packing_never_exceeds_limit_except_for_a_single_oversized_piece() -> None:
    limit = 300
    pieces = chunk_text(_ARTICLE, CHUNK_CONFIG)
    oversized = {p.text for p in pieces if len(p.text) > limit}

    for passage in cut_passages(_ARTICLE, pack_chars=limit):
        assert len(passage.text) <= limit or passage.text in oversized


def test_cut_passages_packing_oversized_piece_stands_alone() -> None:
    passages = cut_passages(_ARTICLE, pack_chars=300)
    long_pieces = [p for p in chunk_text(_ARTICLE, CHUNK_CONFIG) if len(p.text) > 300]

    assert long_pieces
    assert [p.text for p in passages if len(p.text) > 300] == [p.text for p in long_pieces]


def test_cut_passages_packing_numbers_passages_contiguously_from_zero() -> None:
    passages = cut_passages(_ARTICLE, pack_chars=300)

    assert len(passages) < len(cut_passages(_ARTICLE))
    assert [p.index for p in passages] == list(range(len(passages)))


@pytest.mark.parametrize("pack_chars", [0, 300, 5000])
def test_cut_passages_same_text_gives_the_same_cut(pack_chars: int) -> None:
    assert cut_passages(_ARTICLE, pack_chars=pack_chars) == cut_passages(
        _ARTICLE, pack_chars=pack_chars
    )


@pytest.mark.parametrize("pack_chars", [0, 300])
def test_cut_passages_empty_text_gives_no_passages(pack_chars: int) -> None:
    assert cut_passages("", pack_chars=pack_chars) == []


def test_passage_indexed_text_composes_heading_and_title() -> None:
    passage = Passage(0, "body text goes here", "Heading", "Heading")

    assert "Heading" in passage.indexed_text("Title")
    assert "Title" in passage.indexed_text("Title")
    assert "body text goes here" in passage.indexed_text("Title")


def test_chunker_fingerprint_names_version_and_sizes() -> None:
    assert chunker_fingerprint() == "v1:512/50"


def test_passages_module_imports_without_zstandard() -> None:
    code = (
        "import sys\n"
        "sys.modules['zstandard'] = None\n"
        "sys.modules['qdrant_client'] = None\n"
        "import wiki_mirror.passages\n"
    )

    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
