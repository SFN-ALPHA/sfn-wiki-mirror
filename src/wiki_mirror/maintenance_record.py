"""The record the maintenance job leaves for health: what it saw of Qdrant and what it did.

`python -m wiki_mirror maintain` (run by `wiki-mirror-maintain.timer` every 15 minutes, as
`wikimirror-sync`) writes `<data_dir>/state/maintenance.json` atomically at the end of every run,
through `write_maintenance_record`. The API's health route and `wiki-mirror status` read it through
`read_maintenance_record`. The dataclasses below are the contract between the writer and the
readers; the file is their JSON form, one key per field.

Non-obvious constraints:

* **Reading never fails and never guesses**, as for the update record (`update_record`). A
  missing, unreadable or malformed file, or a field of the wrong type or value, reads as None:
  health must answer on a box where the job has never run, and a half-trusted record is worse
  than none.
* **Times are UTC, `YYYY-MM-DDTHH:MM:SSZ`**, so a reader compares them without time-zone rules.
* **Bounded.** Text fields are cut to `MAX_TEXT_CHARS` and lists to `MAX_ITEMS` on both write and
  read, so a damaged or runaway file cannot bloat a health reply.
* **Nothing secret goes in.** Every field comes from Qdrant's collection info or from systemd's view
  of Qdrant's unit; neither carries a key. Qdrant's own error text (a `red` collection's optimizer
  error) is kept, cut to length, because it is the one clue to what broke.
* **The schema number moves with any change a reader must know about.** A reader that finds
  another number treats the file as absent rather than misreading it.

See docs/OPERATIONS.md ("The maintenance job") and docs/DESIGN.md section 12.5.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

__all__ = [
    "ACTIONS",
    "COLLECTION_STATUSES",
    "MAINTENANCE_RECORD_NAME",
    "MAX_ITEMS",
    "MAX_TEXT_CHARS",
    "SCHEMA",
    "CollectionReport",
    "MaintenanceAction",
    "MaintenanceRecord",
    "QdrantUnitReport",
    "maintenance_record_file",
    "read_maintenance_record",
    "record_as_dict",
    "write_maintenance_record",
]

#: The file's name under `<data_dir>/state/`.
MAINTENANCE_RECORD_NAME: Final = "maintenance.json"

#: The format's version; see the module docstring. 2: `QdrantUnitReport.found` may be None (could
#: not be read), and `CollectionReport` carries `held` (the crash-loop guard).
SCHEMA: Final = 2

#: What the job can do to a collection. `resumed`: the collection was `grey` (optimizations pending
#: but not started, as after a Qdrant restart) and was sent an empty optimizers update, which
#: starts them; `applied_settings`: its storage or optimizer settings differed from the code's and
#: were updated (a storage change makes Qdrant rebuild the collection's graphs in the background).
ACTIONS: Final = frozenset({"resumed", "applied_settings"})

#: A collection's status as Qdrant 1.12 reports it, plus `unknown` for a value it did not know.
COLLECTION_STATUSES: Final = frozenset({"green", "yellow", "grey", "red", "unknown"})

#: Longest text field kept (a detail, a problem, Qdrant's optimizer error). Room for a hold's
#: sentence with the hand-resume command at its end (about 300 characters), which must not be cut.
MAX_TEXT_CHARS: Final = 500

#: Most entries kept in any list (collections, actions, problems).
MAX_ITEMS: Final = 20


@dataclass(frozen=True, slots=True)
class QdrantUnitReport:
    """What systemd says about `wiki-mirror-qdrant.service`, and its memory.

    Every field but `found` and `active` is None when it could not be read (no unit, systemd
    unreachable, a cgroup file missing).
    """

    #: The unit is installed (systemd's `LoadState` is not `not-found`); False when systemd says it
    #: is not; None when systemd could not be asked at all, so nobody knows (crashes and memory are
    #: then unwatched, which health reports).
    found: bool | None
    #: The unit is running (`ActiveState=active`).
    active: bool
    #: systemd's `NRestarts`: automatic restarts after a failure since the unit was last started
    #: by hand. A start by hand resets it to 0.
    restarts: int | None = None
    #: systemd's `InvocationID`; it changes on every start, automatic or by hand.
    invocation_id: str | None = None
    #: When the running Qdrant process started (`ExecMainStartTimestamp`), UTC.
    started_at: str | None = None
    #: When systemd last restarted Qdrant after a failure (an OOM kill, a crash), UTC: the start
    #: time of the run in which `restarts` went up. None when no such restart has been seen.
    last_crash_at: str | None = None
    #: Anonymous memory of the unit's cgroup (`memory.stat` `anon`), bytes: what Qdrant holds and
    #: cannot give back. Page cache is not in it.
    anon_bytes: int | None = None
    #: The unit's memory cap (`memory.max`), bytes; None when unlimited or unreadable.
    memory_max_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class CollectionReport:
    """One mirror collection as Qdrant described it during the run."""

    name: str
    #: The alias that points at it (`wikipedia_lexical`, `wikipedia_semantic`), or None.
    alias: str | None
    #: One of `COLLECTION_STATUSES`.
    status: str
    #: `ok`, or `error: <Qdrant's text>` when the optimizer has failed (the `red` case).
    optimizer_status: str
    points: int | None
    indexed_vectors: int | None
    segments: int | None
    #: Why the job will not resume this `grey` collection by itself, or None: the crash-loop guard
    #: (Qdrant has restarted itself after failures too often since it was last started by hand, or
    #: that cannot be read), Qdrant near its memory cap, the disk at its floor. Recomputed on every
    #: run, never carried. Health calls a held collection `degraded`.
    held: str | None = None


@dataclass(frozen=True, slots=True)
class MaintenanceAction:
    """One thing the job did to a collection."""

    #: When, UTC.
    at: str
    collection: str
    #: One of `ACTIONS`.
    action: str
    #: Short and plain: what was wrong and what was sent ("status was grey; resumed its
    #: optimizations").
    detail: str


@dataclass(frozen=True, slots=True)
class MaintenanceRecord:
    """One maintenance run: when it ran, what it saw, what it did, what it could not fix."""

    #: When the run finished, UTC.
    ran_at: str
    qdrant: QdrantUnitReport
    collections: tuple[CollectionReport, ...] = ()
    #: What this run did; empty when all was well.
    actions: tuple[MaintenanceAction, ...] = ()
    #: The most recent action of any run, carried forward from the previous record, so health can
    #: show the last thing done even on a quiet run.
    last_action: MaintenanceAction | None = None
    #: What the job saw and could not fix, one short sentence each.
    problems: tuple[str, ...] = ()
    schema: int = field(default=SCHEMA)


def maintenance_record_file(data_dir: Path) -> Path:
    """Where the record lives: `<data_dir>/state/maintenance.json`."""
    return data_dir / "state" / MAINTENANCE_RECORD_NAME


def record_as_dict(record: MaintenanceRecord) -> dict[str, Any]:
    """The record as plain JSON-ready data (tuples become lists), as health returns it."""
    plain: dict[str, Any] = json.loads(json.dumps(asdict(record)))
    return plain


def write_maintenance_record(data_dir: Path, record: MaintenanceRecord) -> None:
    """Write the record atomically (temp file in the same directory, flushed to disk, then rename).

    Text and lists are cut to the module's bounds first, so what is written is what a reader will
    accept. The temp file is fsynced before the rename, so a power cut leaves the old record or
    the new one, never an empty file. The file's mode follows the process umask (the unit's 0027:
    readable by the API's group, nobody else).

    Args:
        data_dir: The service's local data directory.
        record: The run to record.

    Raises:
        OSError: The state directory cannot be created or the file cannot be written. The caller
            decides; the job exits 1 so systemd shows the failed run.
    """
    path = maintenance_record_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(record_as_dict(_bounded(record)), indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def read_maintenance_record(data_dir: Path) -> MaintenanceRecord | None:
    """The last maintenance run's record, or None when there is none that can be trusted.

    Args:
        data_dir: The service's local data directory.

    Returns:
        The record, with text and lists cut to the module's bounds; or None when the file is
        absent, unreadable, not JSON, of another schema, or any field has the wrong type or value.
        Never raises.
    """
    try:
        raw = json.loads(maintenance_record_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Absent is the normal state of a box whose timer is not enabled yet, and a damaged file
        # is not worth failing health for; both read as "no record".
        return None
    try:
        return _parse(raw)
    except (TypeError, ValueError, KeyError):
        return None


def _parse(raw: object) -> MaintenanceRecord:
    """Validate and build a record from decoded JSON.

    Raises:
        TypeError, ValueError, KeyError: Any field is missing, of the wrong type or out of range.
    """
    data = _mapping(raw)
    if _int(data["schema"]) != SCHEMA:
        raise ValueError("another schema")
    qdrant = _mapping(data["qdrant"])
    unit = QdrantUnitReport(
        found=None if qdrant["found"] is None else _bool(qdrant["found"]),
        active=_bool(qdrant["active"]),
        restarts=_optional_int(qdrant["restarts"]),
        invocation_id=_optional_text(qdrant["invocation_id"]),
        started_at=_optional_text(qdrant["started_at"]),
        last_crash_at=_optional_text(qdrant["last_crash_at"]),
        anon_bytes=_optional_int(qdrant["anon_bytes"]),
        memory_max_bytes=_optional_int(qdrant["memory_max_bytes"]),
    )
    collections = tuple(_collection(item) for item in _list(data["collections"]))
    actions = tuple(_action(item) for item in _list(data["actions"]))
    last = data["last_action"]
    return MaintenanceRecord(
        ran_at=_text(data["ran_at"]),
        qdrant=unit,
        collections=collections,
        actions=actions,
        last_action=None if last is None else _action(last),
        problems=tuple(_text(item) for item in _list(data["problems"])),
    )


def _collection(raw: object) -> CollectionReport:
    """One collection entry, validated."""
    data = _mapping(raw)
    status = _text(data["status"])
    if status not in COLLECTION_STATUSES:
        raise ValueError("unknown status")
    return CollectionReport(
        name=_text(data["name"]),
        alias=_optional_text(data["alias"]),
        status=status,
        optimizer_status=_text(data["optimizer_status"]),
        points=_optional_int(data["points"]),
        indexed_vectors=_optional_int(data["indexed_vectors"]),
        segments=_optional_int(data["segments"]),
        held=_optional_text(data["held"]),
    )


def _action(raw: object) -> MaintenanceAction:
    """One action entry, validated."""
    data = _mapping(raw)
    action = _text(data["action"])
    if action not in ACTIONS:
        raise ValueError("unknown action")
    return MaintenanceAction(
        at=_text(data["at"]),
        collection=_text(data["collection"]),
        action=action,
        detail=_text(data["detail"]),
    )


def _bounded(record: MaintenanceRecord) -> MaintenanceRecord:
    """The record with every text field and list cut to the module's bounds."""

    def cut_action(action: MaintenanceAction) -> MaintenanceAction:
        return MaintenanceAction(
            at=action.at,
            collection=action.collection[:MAX_TEXT_CHARS],
            action=action.action,
            detail=action.detail[:MAX_TEXT_CHARS],
        )

    collections = tuple(
        CollectionReport(
            name=item.name[:MAX_TEXT_CHARS],
            alias=item.alias,
            status=item.status,
            optimizer_status=item.optimizer_status[:MAX_TEXT_CHARS],
            points=item.points,
            indexed_vectors=item.indexed_vectors,
            segments=item.segments,
            held=None if item.held is None else item.held[:MAX_TEXT_CHARS],
        )
        for item in record.collections[:MAX_ITEMS]
    )
    return MaintenanceRecord(
        ran_at=record.ran_at,
        qdrant=record.qdrant,
        collections=collections,
        actions=tuple(cut_action(item) for item in record.actions[:MAX_ITEMS]),
        last_action=None if record.last_action is None else cut_action(record.last_action),
        problems=tuple(item[:MAX_TEXT_CHARS] for item in record.problems[:MAX_ITEMS]),
    )


def _mapping(value: object) -> dict[str, Any]:
    """`value` if it is a JSON object."""
    if not isinstance(value, dict):
        raise TypeError("not an object")
    return value


def _list(value: object) -> list[Any]:
    """`value` if it is a JSON array, cut to `MAX_ITEMS`."""
    if not isinstance(value, list):
        raise TypeError("not a list")
    return value[:MAX_ITEMS]


def _text(value: object) -> str:
    """`value` if it is a string, cut to `MAX_TEXT_CHARS`."""
    if not isinstance(value, str):
        raise TypeError("not a string")
    return value[:MAX_TEXT_CHARS]


def _optional_text(value: object) -> str | None:
    """`value` if it is a string or null."""
    return None if value is None else _text(value)


def _bool(value: object) -> bool:
    """`value` if it is a JSON boolean."""
    if not isinstance(value, bool):
        raise TypeError("not a boolean")
    return value


def _int(value: object) -> int:
    """`value` if it is a non-negative JSON integer (a boolean is not one)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError("not a non-negative integer")
    return value


def _optional_int(value: object) -> int | None:
    """`value` if it is a non-negative integer or null."""
    return None if value is None else _int(value)
