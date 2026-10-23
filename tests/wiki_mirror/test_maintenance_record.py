"""Qualifies the maintenance record: a good file round-trips, anything else reads as None.

`python -m wiki_mirror maintain` writes `state/maintenance.json`; health and `wiki-mirror status`
read it. The reader must never raise and never half-trust a file, so every way the file can be
wrong (absent, unreadable, not JSON, another schema, a field of the wrong type, a status or action
nobody defined) has a named test that ends in None. The good case is the hand-written record of the
2026-10-23 crash in `tests/fixtures/maintenance/`.

See docs/OPERATIONS.md ("The maintenance job").
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from wiki_mirror.maintenance_record import (
    MAX_ITEMS,
    MAX_TEXT_CHARS,
    MaintenanceAction,
    MaintenanceRecord,
    QdrantUnitReport,
    maintenance_record_file,
    read_maintenance_record,
    record_as_dict,
    write_maintenance_record,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "maintenance"
_FIXTURE = _FIXTURES / "maintenance_after_crash.json"
_FIELDS = ["ran_at", "qdrant", "collections", "actions", "last_action", "problems", "schema"]


def _good() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return data


def _write(data_dir: Path, content: str) -> None:
    path = maintenance_record_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_maintenance_record_file_lives_in_the_state_directory(tmp_path: Path) -> None:
    assert maintenance_record_file(tmp_path) == tmp_path / "state" / "maintenance.json"


def test_read_maintenance_record_fixture_reads_as_written(tmp_path: Path) -> None:
    _write(tmp_path, json.dumps(_good()))
    record = read_maintenance_record(tmp_path)
    assert record is not None
    assert record_as_dict(record) == _good()


def test_write_maintenance_record_round_trips_through_read(tmp_path: Path) -> None:
    _write(tmp_path, json.dumps(_good()))
    record = read_maintenance_record(tmp_path)
    assert record is not None
    other = tmp_path / "other"
    write_maintenance_record(other, record)
    assert read_maintenance_record(other) == record


def test_write_maintenance_record_leaves_no_temp_file(tmp_path: Path) -> None:
    record = MaintenanceRecord(ran_at="2026-10-23T12:00:00Z", qdrant=QdrantUnitReport(False, False))
    write_maintenance_record(tmp_path, record)
    assert sorted(p.name for p in (tmp_path / "state").iterdir()) == ["maintenance.json"]


def test_write_maintenance_record_cuts_overlong_text_and_lists(tmp_path: Path) -> None:
    action = MaintenanceAction("2026-10-23T12:00:00Z", "c", "resumed", "x" * 5000)
    record = MaintenanceRecord(
        ran_at="2026-10-23T12:00:00Z",
        qdrant=QdrantUnitReport(True, True),
        actions=(action,) * (MAX_ITEMS + 5),
        problems=("y" * 5000,),
    )
    write_maintenance_record(tmp_path, record)
    written = json.loads(maintenance_record_file(tmp_path).read_text(encoding="utf-8"))
    assert len(written["actions"]) == MAX_ITEMS
    assert len(written["actions"][0]["detail"]) == MAX_TEXT_CHARS
    assert len(written["problems"][0]) == MAX_TEXT_CHARS


def test_read_maintenance_record_absent_file_is_none(tmp_path: Path) -> None:
    assert read_maintenance_record(tmp_path) is None


def test_read_maintenance_record_unreadable_file_is_none(tmp_path: Path) -> None:
    maintenance_record_file(tmp_path).mkdir(parents=True)  # a directory where the file should be
    assert read_maintenance_record(tmp_path) is None


@pytest.mark.parametrize("content", ["", "not json", "{", "[]", "null", '"text"', "42"])
def test_read_maintenance_record_garbage_is_none(tmp_path: Path, content: str) -> None:
    _write(tmp_path, content)
    assert read_maintenance_record(tmp_path) is None


def test_read_maintenance_record_invalid_utf8_is_none(tmp_path: Path) -> None:
    path = maintenance_record_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe{")
    assert read_maintenance_record(tmp_path) is None


@pytest.mark.parametrize("schema", [1, 3])
def test_read_maintenance_record_another_schema_is_none(tmp_path: Path, schema: int) -> None:
    _write(tmp_path, json.dumps({**_good(), "schema": schema}))
    assert read_maintenance_record(tmp_path) is None


def test_read_maintenance_record_unit_that_could_not_be_read_is_found_none(tmp_path: Path) -> None:
    good = _good()
    good["qdrant"]["found"] = None
    _write(tmp_path, json.dumps(good))
    record = read_maintenance_record(tmp_path)
    assert record is not None
    assert record.qdrant.found is None


def test_read_maintenance_record_a_held_collection_keeps_its_reason(tmp_path: Path) -> None:
    good = _good()
    good["collections"][1]["held"] = "Qdrant restarted itself after a failure 2 times"
    _write(tmp_path, json.dumps(good))
    record = read_maintenance_record(tmp_path)
    assert record is not None
    assert record.collections[1].held == "Qdrant restarted itself after a failure 2 times"


def test_read_maintenance_record_a_missing_held_field_is_none(tmp_path: Path) -> None:
    good = _good()
    del good["collections"][0]["held"]
    _write(tmp_path, json.dumps(good))
    assert read_maintenance_record(tmp_path) is None


def test_write_maintenance_record_cuts_an_overlong_held_reason(tmp_path: Path) -> None:
    _write(tmp_path, json.dumps(_good()))
    record = read_maintenance_record(tmp_path)
    assert record is not None
    held = replace(record.collections[1], held="z" * 5000)
    write_maintenance_record(tmp_path, replace(record, collections=(record.collections[0], held)))
    written = json.loads(maintenance_record_file(tmp_path).read_text(encoding="utf-8"))
    assert len(written["collections"][1]["held"]) == MAX_TEXT_CHARS


@pytest.mark.parametrize("missing", _FIELDS)
def test_read_maintenance_record_a_missing_field_is_none(tmp_path: Path, missing: str) -> None:
    broken = {key: value for key, value in _good().items() if key != missing}
    _write(tmp_path, json.dumps(broken))
    assert read_maintenance_record(tmp_path) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("found", "yes"),
        ("active", 1),
        ("restarts", -1),
        ("restarts", True),
        ("restarts", "1"),
        ("anon_bytes", 1.5),
        ("started_at", 1792739543),
    ],
)
def test_read_maintenance_record_a_bad_qdrant_field_is_none(
    tmp_path: Path, field: str, value: object
) -> None:
    good = _good()
    good["qdrant"][field] = value
    _write(tmp_path, json.dumps(good))
    assert read_maintenance_record(tmp_path) is None


def test_read_maintenance_record_an_unknown_collection_status_is_none(tmp_path: Path) -> None:
    good = _good()
    good["collections"][0]["status"] = "purple"
    _write(tmp_path, json.dumps(good))
    assert read_maintenance_record(tmp_path) is None


def test_read_maintenance_record_an_unknown_action_is_none(tmp_path: Path) -> None:
    good = _good()
    good["actions"][0]["action"] = "deleted_everything"
    _write(tmp_path, json.dumps(good))
    assert read_maintenance_record(tmp_path) is None


def test_read_maintenance_record_null_last_action_is_allowed(tmp_path: Path) -> None:
    _write(tmp_path, json.dumps({**_good(), "last_action": None, "actions": []}))
    record = read_maintenance_record(tmp_path)
    assert record is not None
    assert record.last_action is None


def test_read_maintenance_record_unknown_extra_fields_are_dropped(tmp_path: Path) -> None:
    _write(tmp_path, json.dumps({**_good(), "secret": "nope"}))
    record = read_maintenance_record(tmp_path)
    assert record is not None
    assert "secret" not in record_as_dict(record)
