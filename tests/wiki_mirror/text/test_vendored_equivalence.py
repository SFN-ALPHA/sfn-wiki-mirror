"""Qualifies that `wiki_mirror.text` behaves exactly as when the live index was built.

The built index depends on this code's exact output (passage numbers from the chunker, keyword
vectors from the encoder; docs/DESIGN.md section 12.7), so it must reproduce the recorded
outputs byte for byte, now and after any later edit.

`tests/fixtures/vendored_golden.json` records the outputs of the implementation (`text.sanitize`,
`text.sparse`, and `compose_indexed_text` through `Chunk.indexed_text`) as of 2026-10-19, when the
live index was built, by a one-off script (not kept here: the fixture is the reference, and
regenerating it from changed code is a deliberate act, below). It holds 33 texts: the eight
real Enterprise records of `tests/fixtures/wme/records.ndjson` as the mirror parses them
(`html_to_text`, then `sanitize(untrusted=True)`), and hand-made edge cases (headings at every
level, long sections that split on paragraph breaks, sentence ends and not at all, overlap
arithmetic, the minimum piece length, unicode, a heading containing the section separator, CRLF,
injection markers, page and figure markers). For each: the sanitized text, and every chunk's
index, text, heading, section path, page span and title-composed indexed text. For the encoder:
tokens and the document and query vectors of 21 strings at the live 2**24 dimensions.

A failure here means the text code changed behaviour. If that was deliberate, bump
`CHUNKER_VERSION` (chunker) or plan a keyword re-index (encoder), then regenerate the fixture from
the changed code, which is then the reference. Never edit the fixture to make a test pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wiki_mirror.text.indexed_text import SECTION_SEPARATOR, compose_indexed_text
from wiki_mirror.text.sanitize import ChunkConfig, chunk_text, sanitize
from wiki_mirror.text.sparse import SparseEncoder, tokenize

pytestmark = pytest.mark.unit

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "vendored_golden.json"
_GOLDEN: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
_CASES: list[dict[str, Any]] = _GOLDEN["cases"]
_VARIANT_CASES = [case for case in _CASES if "sanitized_trusted" in case]
_IDS = [case["name"] for case in _CASES]


def _config(case: dict[str, Any]) -> ChunkConfig:
    """The chunk sizes the case was generated with."""
    max_tokens, overlap_tokens = case["config"]
    return ChunkConfig(max_tokens=max_tokens, overlap_tokens=overlap_tokens)


def _rows(case: dict[str, Any], text: str) -> list[dict[str, Any]]:
    """`chunk_text(text)` in the fixture's row shape, with the case title composed in."""
    return [
        {
            "index": chunk.index,
            "text": chunk.text,
            "heading": chunk.heading,
            "section_path": chunk.section_path,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "indexed_text": chunk.indexed_text(case["title"]),
        }
        for chunk in chunk_text(text, _config(case))
    ]


def test_golden_fixture_covers_thirty_texts_and_both_kinds_of_source() -> None:
    sources = {case["source"] for case in _CASES}

    assert len(_CASES) >= 30
    assert sources == {"record", "handmade"}
    assert _GOLDEN["generated_from"]["commit"] == "121c7f4"


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_sanitize_untrusted_matches_the_original(case: dict[str, Any]) -> None:
    assert sanitize(case["raw"], untrusted=True) == case["sanitized"]


@pytest.mark.parametrize("case", [c for c in _CASES if "abstract" in c], ids=lambda c: c["name"])
def test_sanitize_abstract_matches_the_original(case: dict[str, Any]) -> None:
    assert sanitize(case["abstract"], untrusted=True) == case["abstract_sanitized"]


@pytest.mark.parametrize("case", _VARIANT_CASES, ids=lambda c: c["name"])
def test_sanitize_trusted_and_keep_indent_match_the_original(case: dict[str, Any]) -> None:
    assert sanitize(case["raw"]) == case["sanitized_trusted"]
    assert sanitize(case["raw"], keep_indent=True) == case["sanitized_keep_indent"]


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_chunk_text_matches_the_original_chunk_for_chunk(case: dict[str, Any]) -> None:
    assert _rows(case, case["sanitized"]) == case["chunks"]


@pytest.mark.parametrize("case", _VARIANT_CASES, ids=lambda c: c["name"])
def test_chunk_text_min_chars_one_matches_the_original(case: dict[str, Any]) -> None:
    rows = [
        {
            "index": chunk.index,
            "text": chunk.text,
            "heading": chunk.heading,
            "section_path": chunk.section_path,
        }
        for chunk in chunk_text(case["sanitized"], _config(case), min_chars=1)
    ]

    assert rows == case["chunks_min_chars_1"]


def test_chunk_text_default_config_is_512_over_50() -> None:
    assert ChunkConfig() == ChunkConfig(max_tokens=512, overlap_tokens=50)
    assert ChunkConfig().max_chars == 2048
    assert ChunkConfig().overlap_chars == 200


def test_section_separator_is_the_guillemet_with_spaces() -> None:
    separator = f" {chr(0x203A)} "

    assert separator == SECTION_SEPARATOR


def test_compose_indexed_text_drops_a_title_equal_to_the_heading() -> None:
    assert compose_indexed_text("body", "Heading", "heading") == "Heading\n\nbody"
    assert compose_indexed_text("body", "Heading", "Title") == "Title\n\nHeading\n\nbody"
    assert compose_indexed_text("  body  ") == "body"


def test_sparse_encoder_dimensions_default_is_the_originals() -> None:
    assert SparseEncoder().dimensions == 2**18
    assert SparseEncoder().k1 == 1.5


@pytest.mark.parametrize("row", _GOLDEN["sparse"], ids=lambda r: r["text"][:30] or "<empty>")
def test_tokenize_matches_the_original(row: dict[str, Any]) -> None:
    assert tokenize(row["text"]) == row["tokens"]


@pytest.mark.parametrize("row", _GOLDEN["sparse"], ids=lambda r: r["text"][:30] or "<empty>")
def test_sparse_vectors_match_the_original_at_the_live_dimensions(row: dict[str, Any]) -> None:
    encoder = SparseEncoder(dimensions=_GOLDEN["dimensions"])

    assert {str(k): v for k, v in encoder.encode_document(row["text"]).items()} == row["document"]
    assert {str(k): v for k, v in encoder.encode_query(row["text"]).items()} == row["query"]


def test_sparse_document_vector_matches_the_original_at_the_default_dimensions() -> None:
    golden = _GOLDEN["sparse_default_dimensions"]

    encoded = SparseEncoder().encode_document(golden["text"])

    assert {str(k): v for k, v in encoded.items()} == golden["document"]
