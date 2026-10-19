"""Qualifies the shared record shapes: title normalisation, citation URLs, point ids."""

from __future__ import annotations

import hashlib

import pytest

from wiki_mirror.records import (
    PASSAGE_STRIDE,
    Article,
    SnapshotMeta,
    normalise_title,
    oldid_url,
    passage_point_id,
    split_point_id,
    text_hash,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("albert_einstein", "Albert einstein"),
        ("  Albert   Einstein ", "Albert Einstein"),
        ("iPhone", "IPhone"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalise_title_typed_spelling_gives_display_form(typed: str, expected: str) -> None:
    assert normalise_title(typed) == expected


@pytest.mark.unit
def test_oldid_url_title_with_spaces_and_parentheses_keeps_wikipedia_form() -> None:
    url = oldid_url("Mercury (planet)", 42)
    assert url == "https://en.wikipedia.org/wiki/Mercury_(planet)?oldid=42"


@pytest.mark.unit
def test_oldid_url_title_with_question_mark_is_escaped() -> None:
    assert "%3F" in oldid_url("Who? What?", 1)


@pytest.mark.unit
def test_article_url_points_at_its_own_revision() -> None:
    article = Article(
        page_id=1, title="A b", revision_id=7, revision_time="", text="x", text_hash=text_hash("x")
    )
    assert article.url.endswith("/A_b?oldid=7")


@pytest.mark.unit
def test_snapshot_tag_drops_the_dashes() -> None:
    meta = SnapshotMeta("enwiki_namespace_0", "2026-10-01", 1, ())
    assert meta.tag == "20261001"


@pytest.mark.unit
def test_passage_point_id_round_trips_through_split() -> None:
    assert split_point_id(passage_point_id(736, 12)) == (736, 12)


@pytest.mark.unit
@pytest.mark.parametrize(("page", "chunk"), [(-1, 0), (1, -1), (1, PASSAGE_STRIDE)])
def test_passage_point_id_out_of_range_raises(page: int, chunk: int) -> None:
    with pytest.raises(ValueError, match="does not fit"):
        passage_point_id(page, chunk)


@pytest.mark.unit
def test_text_hash_is_the_first_eight_sha256_bytes_as_a_signed_big_endian_integer() -> None:
    # SHA-256("abc") = ba7816bf 8f01cfea ...; the top bit of 0xba is set, so the value is negative.
    assert text_hash("abc") == int.from_bytes(bytes.fromhex("ba7816bf8f01cfea"), "big", signed=True)
    assert text_hash("abc") < 0


@pytest.mark.unit
def test_text_hash_same_text_gives_the_same_value_and_different_text_a_different_one() -> None:
    assert text_hash("Mercury is a planet.") == text_hash("Mercury is a planet.")
    assert text_hash("Mercury is a planet.") != text_hash("Mercury is a planet!")


@pytest.mark.unit
@pytest.mark.parametrize(
    "text", ["", "abc", "caf\N{LATIN SMALL LETTER E WITH ACUTE}", "x" * 10_000]
)
def test_text_hash_fits_a_signed_64_bit_integer(text: str) -> None:
    assert -(2**63) <= text_hash(text) <= 2**63 - 1


@pytest.mark.unit
def test_text_hash_hashes_the_utf8_bytes_of_the_text() -> None:
    text = "caf\N{LATIN SMALL LETTER E WITH ACUTE}"
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    assert text_hash(text) == int.from_bytes(digest[:8], "big", signed=True)
