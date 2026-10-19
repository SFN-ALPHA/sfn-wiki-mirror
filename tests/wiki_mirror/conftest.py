"""Fixtures shared by the mirror tests: the real Enterprise records and a snapshot.

The records come from `tests/fixtures/wme/records.ndjson`, cut from a real Wikimedia Enterprise
snapshot (see that directory's README). Tests look a record up by its title so a failing test
names the article it was reading.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wiki_mirror.records import SnapshotMeta

FIXTURE_FILE = Path(__file__).resolve().parents[1] / "fixtures" / "wme" / "records.ndjson"


@pytest.fixture(scope="session")
def wme_records() -> dict[str, dict[str, object]]:
    """The eight real Enterprise records, keyed by article title."""
    records = [json.loads(line) for line in FIXTURE_FILE.read_text(encoding="utf-8").splitlines()]
    return {record["name"]: record for record in records}


@pytest.fixture
def snapshot() -> SnapshotMeta:
    """A small snapshot description for store and build tests."""
    return SnapshotMeta(
        identifier="enwiki_namespace_0",
        date="2026-10-01",
        record_count=8,
        chunk_ids=("enwiki_namespace_0_chunk_0", "enwiki_namespace_0_chunk_1"),
    )
