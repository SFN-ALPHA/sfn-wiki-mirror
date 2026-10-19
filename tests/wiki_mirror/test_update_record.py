"""Qualifies reading the updater's record: a good file is passed on, anything else is None.

`deploy/update.sh` writes `state/update.json`; health and `status` read it here. The
reader must never raise and never half-trust a file, so every way the file can be wrong (absent,
unreadable, not JSON, not an object, a field of the wrong type, an outcome nobody defined) has a
named test that ends in None.

See docs/DESIGN.md section 12.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wiki_mirror.update_record import (
    OUTCOMES,
    read_update_record,
    update_record_file,
)

pytestmark = pytest.mark.unit

_GOOD: dict[str, Any] = {
    "at": "2026-10-18T02:21:07-05:00",
    "from": "d127090abcde",
    "to": "e238101fghij",
    "outcome": "updated",
    "detail": "healthy at e238101 feat: something",
    "unattended": True,
}


def _write(data_dir: Path, content: str) -> None:
    path = update_record_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_update_record_file_lives_in_the_state_directory(tmp_path: Path) -> None:
    assert update_record_file(tmp_path) == tmp_path / "state" / "update.json"


def test_read_update_record_good_file_is_returned_as_written(tmp_path: Path) -> None:
    _write(tmp_path, json.dumps(_GOOD))
    assert read_update_record(tmp_path) == _GOOD


@pytest.mark.parametrize("outcome", sorted(OUTCOMES))
def test_read_update_record_every_defined_outcome_is_accepted(tmp_path: Path, outcome: str) -> None:
    _write(tmp_path, json.dumps({**_GOOD, "outcome": outcome}))
    record = read_update_record(tmp_path)
    assert record is not None
    assert record["outcome"] == outcome


def test_read_update_record_target_not_yet_known_is_none_inside_the_record(tmp_path: Path) -> None:
    _write(tmp_path, json.dumps({**_GOOD, "to": None, "outcome": "failed"}))
    record = read_update_record(tmp_path)
    assert record is not None
    assert record["to"] is None


def test_read_update_record_absent_file_is_none(tmp_path: Path) -> None:
    assert read_update_record(tmp_path) is None


def test_read_update_record_unreadable_file_is_none(tmp_path: Path) -> None:
    update_record_file(tmp_path).mkdir(parents=True)  # a directory where the file should be
    assert read_update_record(tmp_path) is None


@pytest.mark.parametrize("content", ["", "not json", "{", "[]", "null", '"text"', "42"])
def test_read_update_record_garbage_is_none(tmp_path: Path, content: str) -> None:
    _write(tmp_path, content)
    assert read_update_record(tmp_path) is None


def test_read_update_record_invalid_utf8_is_none(tmp_path: Path) -> None:
    path = update_record_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe{")
    assert read_update_record(tmp_path) is None


@pytest.mark.parametrize("missing", sorted(_GOOD))
def test_read_update_record_a_missing_field_is_none(tmp_path: Path, missing: str) -> None:
    broken = {key: value for key, value in _GOOD.items() if key != missing}
    _write(tmp_path, json.dumps(broken))
    assert read_update_record(tmp_path) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("at", 12345),
        ("from", None),
        ("to", 7),
        ("detail", ["list"]),
        ("unattended", "yes"),
        ("outcome", "exploded"),
        ("outcome", ["updated"]),
        ("outcome", None),
    ],
)
def test_read_update_record_a_field_of_the_wrong_type_or_value_is_none(
    tmp_path: Path, field: str, value: object
) -> None:
    _write(tmp_path, json.dumps({**_GOOD, field: value}))
    assert read_update_record(tmp_path) is None


def test_read_update_record_unknown_extra_fields_are_dropped(tmp_path: Path) -> None:
    _write(tmp_path, json.dumps({**_GOOD, "secret": "nope"}))
    assert read_update_record(tmp_path) == _GOOD


def test_read_update_record_an_overlong_detail_is_cut(tmp_path: Path) -> None:
    _write(tmp_path, json.dumps({**_GOOD, "detail": "x" * 5000}))
    record = read_update_record(tmp_path)
    assert record is not None
    assert len(record["detail"]) == 400
