"""The maintenance job: heals Qdrant's mirror collections and records what it saw of Qdrant.

`python -m wiki_mirror maintain`, run every 15 minutes by `wiki-mirror-maintain.timer` as the build
user `wikimirror-sync`. One run reads Qdrant's systemd unit and its memory, asks Qdrant about every
mirror collection (`wikipedia_lexical_*`, `wikipedia_semantic_*`), repairs two things, and writes
`<data_dir>/state/maintenance.json` (`maintenance_record`) for `/v1/wiki/health` and
`wiki-mirror status`. The two repairs:

* **Resume.** A collection whose status is `grey` has optimizations pending that Qdrant does not
  start. Qdrant 1.12 does not restart them after it restarts until some update operation arrives:
  after the 2026-10-23 02:12 out-of-memory kill systemd restarted Qdrant 6 s later, and the
  semantic collection then sat `grey` until the operator sent an empty optimizers update by hand.
  The job sends it (`{"optimizers_config": {}}`, Qdrant's documented way to trigger them), unless
  the collection is held (below).
* **Settings.** The semantic collection's live HNSW, quantization and optimizer-thread settings are
  kept equal to the constants in `embed` (`SEMANTIC_HNSW`, `SEMANTIC_QUANTIZATION`,
  `SEMANTIC_OPTIMIZATION_THREADS`: docs/DECISIONS.md D8), changing only what differs.

Non-obvious constraints:

* **The crash-loop guard: a restart's `grey` is deliberate, so the job resumes carefully.** Qdrant
  leaves a collection `grey` after a restart so that an optimization that just ran it out of memory
  does not start again by itself and kill it again. Resuming every `grey` collection on every run
  would turn one such optimization into a kill every 15 minutes, and every kill takes keyword
  search down. The guard is stateless on purpose: it reads systemd's `NRestarts`, the count of
  Qdrant's automatic restarts after a failure since it was last started by hand. That number
  survives everything that can happen to the job (a lost or foreign record, a run killed by its
  timeout after it sent its updates, a PATCH that timed out but was applied), which a record the
  job keeps of its own work does not. **A `grey` collection is resumed by itself only while Qdrant
  has restarted itself at most `_RESUMES_AFTER_RESTARTS` times since it was last started by
  hand;** above that, or when the count cannot be read, it is held: no resume, no settings
  update, a reason in its record (ending with the exact command to resume it by hand) and a
  problem. The bound: at most two crashes follow a start by hand before the job stops. A first
  crash the job did not cause (2026-10-23) is resumed once; if that resume kills Qdrant the count
  is 2 and the collection is held. After a clean start (a reboot, a restart by hand: count 0) a
  resume that kills Qdrant is retried once more, then held. Anything that resets the count (a
  start by hand, `systemctl reset-failed`, a reboot) gives the job its allowance again, which is
  deliberate: the operator has looked. The accepted cost is that a second crash holds the
  collection even if the job did not cause it; two crashes since the last start by hand are
  worth a person's look. Qdrant above 90% of its memory cap, or the disk under its floor (or
  unmeasurable), holds a `grey` collection as well, and each hold is recomputed on every run.
* **A storage change is a rebuild, and any change interrupts.** In Qdrant 1.12, changing the HNSW
  graph's `m`, `ef_construct` or `on_disk`, or any quantization field, makes Qdrant rebuild every
  segment of the collection in the background (a long job on 41.6M passages, with disk writes to
  match). The optimizer thread count starts no rebuild, but *any* collection update makes Qdrant
  stop the optimizer workers (`on_optimizer_config_update`), throwing away the segment being
  rebuilt; so a threads-only change waits until the collection is `green` (or `grey`, where
  nothing is in flight). A settings change is applied only when it is safe to start one: the
  embedding run that loads the collection has finished (it keeps HNSW off during the load, on
  purpose), no build or embedding run holds the build lock, the collection is not `red`, and for
  a rebuild also Qdrant is under 90% of its memory cap, has not restarted itself more than
  `_RESUMES_AFTER_RESTARTS` times since it was last started by hand (its count readable), and the
  disk has its free floor. A threads-only change cannot cause an out-of-memory kill, so it
  ignores the restart count. Otherwise a change is skipped, and the record's problems say what it
  waits for.
* **The code is the truth for the semantic settings.** A value changed by hand on the box is put
  back on the next run. To change the layout, change the constants in `embed`.
* **The build lock is probed, never held.** An embed run or a monthly build that started while this
  job held the lock would exit 1 and wait an hour for systemd's retry; the probe's window is
  microseconds. What the probe guards is a settings change landing while a run is loading or
  rebuilding; the collection it changes is a complete one, which neither stage rewrites.
* **Nothing Qdrant or systemd does stops the record.** Qdrant not answering, `systemctl` missing
  or slow, an unreadable cgroup file: each becomes `None` fields and a problem sentence, and the
  record is still written. The command exits 1 only when the record cannot be written or the
  run raises; the state directory is probed for writability before anything is sent to Qdrant,
  because a run that cannot report must not act. Whether memory is near the cap for the
  operator's eyes is health's call (it computes that from the recorded numbers); the job uses the
  same numbers only to decide whether it is safe to start work.
* **Out of scope:** starting or retrying embedding runs (operator-started, they use the
  operator's GPU cluster), the lexical collection's settings, and anything that changes segment
  sizes.
* **No secrets.** Qdrant has no key on the box and the job logs none; log lines carry names,
  numbers and exception type names, never a response body.

See docs/OPERATIONS.md ("The maintenance job") and docs/DESIGN.md section 12.5.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from wiki_mirror import embed, index
from wiki_mirror.build import (
    BuildError,
    BuildLockedError,
    build_lock,
    ensure_free_space,
)
from wiki_mirror.config import MirrorConfig
from wiki_mirror.maintenance_record import (
    COLLECTION_STATUSES,
    CollectionReport,
    MaintenanceAction,
    MaintenanceRecord,
    QdrantUnitReport,
    maintenance_record_file,
    read_maintenance_record,
    write_maintenance_record,
)

__all__ = [
    "CGROUP_ROOT",
    "QDRANT_TIMEOUT_SECONDS",
    "QDRANT_UNIT",
    "UnitReader",
    "UnitReading",
    "parse_unit_properties",
    "read_qdrant_unit",
    "run_maintenance",
    "unit_reading_from",
]

logger = logging.getLogger("wiki_mirror.maintain")

#: Qdrant's systemd unit (`deploy/systemd/wiki-mirror-qdrant.service`).
QDRANT_UNIT: Final = "wiki-mirror-qdrant.service"

#: Where the unified cgroup hierarchy (cgroup v2, Debian 12's default) is mounted.
CGROUP_ROOT: Final = Path("/sys/fs/cgroup")

#: The properties asked of `systemctl show`; it prints them in its own order, not this one.
_UNIT_PROPERTIES: Final = (
    "LoadState",
    "ActiveState",
    "NRestarts",
    "ExecMainStartTimestamp",
    "InvocationID",
    "ControlGroup",
)

#: Longest `systemctl show` may take, seconds. It answers from the manager's memory in
#: milliseconds; a longer wait means systemd is wedged, which is itself the finding.
_SYSTEMCTL_TIMEOUT_SECONDS: Final = 10

#: Longest the job waits for one Qdrant call, seconds. The job runs every 15 minutes and a
#: Qdrant that does not answer within a minute is the finding, not something to wait out.
QDRANT_TIMEOUT_SECONDS: Final = 60

#: The collections the job looks after: the names `index` and `embed` create.
_MIRROR_PREFIXES: Final = ("wikipedia_lexical_", "wikipedia_semantic_")

#: Time stamps in the record: UTC, second resolution (`maintenance_record`).
_STAMP: Final = "%Y-%m-%dT%H:%M:%SZ"

#: The longest Qdrant error text quoted inside a longer sentence; the record cuts every problem
#: to `maintenance_record.MAX_TEXT_CHARS`, and the reason must survive the cut.
_QUOTED_ERROR_CHARS: Final = 100

#: Most digits accepted in a number read from systemd or the cgroup (a count or a byte total;
#: 18 digits fit a signed 64-bit integer), so a damaged value cannot become a huge integer.
_MAX_DIGITS: Final = 18

#: Qdrant's anonymous memory, as a share of its cap, above which the job starts no work that
#: could use more of it (a resume, a rebuild). A judgment, not a measurement: it leaves room for
#: the graph builds the optimizer runs (about 120 MB each for a 470,000-point segment) and for
#: the growth that makes Qdrant's memory a poor predictor, since the kill of 2026-10-23 came
#: when the in-RAM graphs outgrew the cap. Health warns separately, from the same numbers.
_MEMORY_HOLD_PERCENT: Final = 90

#: The most automatic restarts after a failure (systemd's `NRestarts`, since Qdrant was last
#: started by hand) at which the job still resumes a `grey` collection by itself; above it, the
#: collection is held until a person acts. The bound it gives: at most two crashes follow a start
#: by hand before the job stops. A first crash the job did not cause (2026-10-23) is resumed once;
#: if that resume kills Qdrant the count is 2 and the collection is held. After a clean start
#: (count 0) a resume that kills Qdrant is retried once more, then held. Anything that resets the
#: count (a start by hand, `systemctl reset-failed`, a reboot) gives the job its allowance again,
#: deliberately: the operator has looked. The accepted cost is that a second crash holds the
#: collection even when the job did not cause it; two crashes since the last start by hand are
#: worth a person's look. The count is systemd's, not the job's own record, because it survives a
#: lost record, a run killed by its timeout and an update that timed out but was applied.
_RESUMES_AFTER_RESTARTS: Final = 1

#: The user the operator runs a hand resume as: the build user (`SYNC_USER` in
#: `deploy/install.sh`), which the job itself runs as.
_SYNC_USER: Final = "wikimirror-sync"

#: The current time as a timezone-aware datetime; tests inject a frozen one.
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class UnitReading:
    """What one look at Qdrant's unit gave: the report and what could not be read or is wrong."""

    #: The unit as far as it could be read. `last_crash_at` is left None: only `run_maintenance`
    #: knows the previous run, so only it can tell a crash from a start by hand.
    report: QdrantUnitReport
    #: One short sentence each: a source that could not be read, a unit that is missing or not
    #: running. Empty when the unit is running and everything was read.
    problems: tuple[str, ...] = ()


#: Reads Qdrant's unit. Production uses `read_qdrant_unit`; tests feed the recorded fixtures.
#: It must not raise: a source it cannot read is a None field and a problem sentence.
UnitReader = Callable[[], UnitReading]


# -- Qdrant's unit ----------------------------------------------------------------------------


def parse_unit_properties(text: str) -> dict[str, str]:
    """The `KEY=VALUE` lines of `systemctl show` as a dict.

    systemd prints the properties in its own order and omits some for a unit that is not
    installed, so nothing here depends on order or presence. A line without `=` is ignored; a
    repeated key keeps its last value.

    Args:
        text: The command's standard output.
    """
    properties: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key:
            properties[key] = value.strip()
    return properties


def read_qdrant_unit(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    cgroup_root: Path = CGROUP_ROOT,
) -> UnitReading:
    """Ask systemd about Qdrant's unit and read its memory from the unit's cgroup.

    The argument vector is fixed (no shell, nothing from outside), and `systemctl show` needs no
    privilege, so this runs as the build user.

    Args:
        run: `subprocess.run`; tests inject a recorder that answers with the fixtures.
        cgroup_root: Where the cgroup hierarchy is mounted; tests point it at a temporary tree.

    Returns:
        The reading. Never raises: `systemctl` missing, slow or failing, or a cgroup file that
        cannot be read, gives None for what could not be read and one sentence saying so.
    """
    argv = ["systemctl", "show", QDRANT_UNIT, "--timestamp=unix", "-p", ",".join(_UNIT_PROPERTIES)]
    try:
        done = run(
            argv,
            capture_output=True,
            text=True,
            # Lenient decoding: a stray byte must not turn into an exception out of the run.
            errors="replace",
            timeout=_SYSTEMCTL_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return _unreadable("systemctl was not found")
    except subprocess.TimeoutExpired:
        return _unreadable(f"systemctl did not answer within {_SYSTEMCTL_TIMEOUT_SECONDS} s")
    except OSError as exc:
        return _unreadable(f"systemctl could not be run ({type(exc).__name__})")
    if done.returncode != 0:
        # Its stderr is not quoted: it is free text and says nothing the exit status does not.
        return _unreadable(f"systemctl exited with status {done.returncode}")
    return unit_reading_from(parse_unit_properties(done.stdout), cgroup_root)


def unit_reading_from(
    properties: Mapping[str, str], cgroup_root: Path = CGROUP_ROOT
) -> UnitReading:
    """Build the reading from the properties `systemctl show` printed (and the cgroup files).

    Args:
        properties: `parse_unit_properties` of the command's output.
        cgroup_root: Where the cgroup hierarchy is mounted.

    Returns:
        A unit that is not installed (`LoadState=not-found`) is `found=False` with every other
        field None. One that is installed has its restart count, invocation, start time and
        memory, and a problem when it is not `active`. A reply without `LoadState` is unreadable:
        `found=None`.
    """
    load_state = properties.get("LoadState")
    if load_state is None:
        return _unreadable("systemctl did not say whether Qdrant's unit is installed")
    if load_state == "not-found":
        return UnitReading(
            QdrantUnitReport(found=False, active=False),
            (f"Qdrant's unit {QDRANT_UNIT} is not installed",),
        )
    active_state = properties.get("ActiveState", "")
    active = active_state == "active"
    problems: list[str] = []
    if not active:
        problems.append(f"Qdrant's unit is not running (ActiveState={active_state[:20] or '?'})")
    anon, limit, memory_problems = _read_memory(properties.get("ControlGroup", ""), cgroup_root)
    problems.extend(memory_problems)
    report = QdrantUnitReport(
        found=True,
        active=active,
        restarts=_count(properties.get("NRestarts")),
        invocation_id=properties.get("InvocationID") or None,
        started_at=_unix_stamp(properties.get("ExecMainStartTimestamp")),
        anon_bytes=anon,
        memory_max_bytes=limit,
    )
    return UnitReading(report, tuple(problems))


def _unreadable(why: str) -> UnitReading:
    """A reading of nothing: systemd could not be asked, for the reason given.

    `found` is None, not False: "not installed" is an answer, this is the lack of one, and the
    crash-loop guard treats them differently (crashes and memory are then unwatched).
    """
    return UnitReading(
        QdrantUnitReport(found=None, active=False), (f"could not read Qdrant's unit: {why}",)
    )


def _count(value: str | None) -> int | None:
    """A non-negative integer from systemd or the cgroup, or None if it is anything else."""
    if value is None or not value.isascii() or not value.isdigit() or len(value) > _MAX_DIGITS:
        return None
    return int(value)


def _unix_stamp(value: str | None) -> str | None:
    """`@<epoch seconds>` (systemctl's `--timestamp=unix`) as a record time, else None.

    An empty value is how systemd says "never started"; both that and a value that is not
    `@<digits>` read as unknown.
    """
    if value is None or not value.startswith("@"):
        return None
    seconds = _count(value[1:])
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC).strftime(_STAMP)
    except (ValueError, OverflowError, OSError):
        # A year beyond datetime's range: damaged, not a time; unknown is the honest reading.
        return None


def _read_memory(
    control_group: str, cgroup_root: Path
) -> tuple[int | None, int | None, tuple[str, ...]]:
    """Qdrant's anonymous memory and its cap, from the unit's cgroup.

    `memory.stat`'s `anon` line is what Qdrant holds and cannot give back (page cache is not in
    it); `memory.max` is the cap, or the word `max` for none, which reads as None without a
    problem. An unset `ControlGroup` (a unit that is not running has none) reads as no memory and
    no problem: the unit's own state already says why.

    Args:
        control_group: systemd's `ControlGroup` property, e.g. `/system.slice/<unit>`.
        cgroup_root: Where the cgroup hierarchy is mounted.

    Returns:
        `(anon_bytes, memory_max_bytes, problems)`; a value that could not be read is None, and
        everything that went wrong is one sentence.
    """
    if not control_group:
        return None, None, ()
    parts = PurePosixPath(control_group).parts
    if not control_group.startswith("/") or ".." in parts:
        # systemd prints an absolute path inside the hierarchy; anything else is not trusted
        # enough to build a file name from.
        return None, None, ("could not read Qdrant's memory: its cgroup path looks wrong",)
    directory = cgroup_root.joinpath(*parts[1:])
    failures: list[str] = []
    anon: int | None = None
    limit: int | None = None
    try:
        for line in (directory / "memory.stat").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(" ")
            if key == "anon":
                anon = _count(value.strip())
    except (OSError, ValueError) as exc:
        # ValueError: a path with a NUL byte, or a file that is not UTF-8. Neither is expected
        # from a cgroup, and neither may stop the run.
        failures.append(f"memory.stat {type(exc).__name__}")
    else:
        if anon is None:
            failures.append("memory.stat has no anon line")
    try:
        text = (directory / "memory.max").read_text(encoding="utf-8").strip()
    except (OSError, ValueError) as exc:
        failures.append(f"memory.max {type(exc).__name__}")
    else:
        if text != "max":
            limit = _count(text)
            if limit is None:
                failures.append("memory.max is not a number")
    problems = (f"could not read Qdrant's memory: {'; '.join(failures)}",) if failures else ()
    return anon, limit, problems


def _last_crash_at(unit: QdrantUnitReport, previous: MaintenanceRecord | None) -> str | None:
    """When systemd last restarted Qdrant after a failure, carrying the previous value forward.

    `NRestarts` counts automatic restarts and a start by hand resets it to 0, so a run of Qdrant
    that began with `NRestarts > 0` began as a recovery from a crash, and its start time is when.
    The invocation id, which changes on every start, tells whether this is a run the previous
    record has not seen:

    * a new invocation with `NRestarts > 0` is a crash at the new start time, whatever the
      previous count (two crashes between runs still give one value, the latest start; a start by
      hand and then a crash, or a reboot and then a crash, between two runs give the crash);
    * a new invocation with the count at 0 is a start by hand: not a crash, the old value stays;
    * the same invocation changed nothing.

    With no previous record, or one that could not read the unit (no invocation to compare with),
    a count above 0 is taken as the crash it is: the current run's start time.

    This value is for the record and for health ("crashed in the last 24 hours"). The crash-loop
    guard does not depend on it (`_RESUMES_AFTER_RESTARTS`), because it has blind spots that were
    checked on a real systemd: `systemctl reset-failed` on the running unit sets `NRestarts` back
    to 0 with the same invocation, and a restart by hand during the `RestartSec` wait after a
    crash sets it to 0 with a new one. In both a crash leaves no trace here, and a reboot and a
    record of another schema (read as no record) lose the carried value.

    Args:
        unit: This run's reading.
        previous: The previous run's record, or None on the first run.

    Returns:
        The time, or the previous record's value (None if it never had one).
    """
    carried = None if previous is None else previous.qdrant.last_crash_at
    if unit.restarts is None or unit.started_at is None or unit.invocation_id is None:
        return carried
    before = None if previous is None else previous.qdrant.invocation_id
    if (before is None or unit.invocation_id != before) and unit.restarts > 0:
        return unit.started_at
    return carried


# -- one run ----------------------------------------------------------------------------------


def run_maintenance(
    config: MirrorConfig,
    client: QdrantClient,
    *,
    unit_reader: UnitReader = read_qdrant_unit,
    clock: Clock = lambda: datetime.now(UTC),
) -> MaintenanceRecord:
    """Do one maintenance run and write its record.

    In order: read the previous record; read Qdrant's unit; ask Qdrant about every mirror
    collection (if it does not answer, record that and stop); resume the `grey` ones unless the
    crash-loop guard holds them (module docstring); reconcile the semantic collection's settings;
    write the record. Nothing in the previous record decides what is sent: the guard reads
    systemd's restart count afresh, so a missing, damaged or foreign record changes nothing but
    the carried `last_action` and crash time.

    Args:
        config: Service settings (`data_dir`, `min_free_disk_gb`).
        client: A Qdrant client for the loopback server.
        unit_reader: Reads Qdrant's systemd unit and memory; tests inject the fixtures.
        clock: The current time, timezone-aware; tests inject a frozen one.

    Returns:
        The record that was written.

    Raises:
        OSError: The record cannot be written (checked before anything is sent to Qdrant).
            Everything else that can go wrong with Qdrant or systemd is a problem in the record,
            not an exception.
    """
    # First, before any update can be sent: a run that cannot report must not act, or what it
    # did would be invisible to the operator.
    _require_writable_state(config.data_dir)
    previous = read_maintenance_record(config.data_dir)
    reading = unit_reader()
    unit = replace(reading.report, last_crash_at=_last_crash_at(reading.report, previous))
    job = _Job(config, client, clock, unit, previous)
    job.problems.extend(reading.problems)
    job.heal()
    last_action = job.actions[-1] if job.actions else (previous.last_action if previous else None)
    record = MaintenanceRecord(
        ran_at=_stamp(clock),
        qdrant=unit,
        collections=tuple(job.reports),
        actions=tuple(job.actions),
        last_action=last_action,
        problems=tuple(job.problems),
    )
    write_maintenance_record(config.data_dir, record)
    for problem in record.problems:
        logger.warning(
            "maintain: %s", problem, extra={"component": "maintain", "event": "problem"}
        )
    logger.info(
        "maintain: %d collections, %d actions, %d problems",
        len(record.collections),
        len(record.actions),
        len(record.problems),
        extra={"component": "maintain", "event": "run"},
    )
    return record


def _stamp(clock: Clock) -> str:
    """The clock's current time as a record time (UTC, `YYYY-MM-DDTHH:MM:SSZ`)."""
    return clock().astimezone(UTC).strftime(_STAMP)


def _require_writable_state(data_dir: Path) -> None:
    """Raise `OSError` unless the record's directory can be created and written to.

    The probe is a small file in the same directory, made and removed at once; the real write
    (`write_maintenance_record`) is a temp file and a rename there, so what the probe needs is
    what the write needs. It cannot promise the disk will have room at the end of the run, but
    it catches the usual cause of a lost record: a state directory the build user cannot write.

    Raises:
        OSError: The directory cannot be created, or a file cannot be written in it.
    """
    probe = maintenance_record_file(data_dir).with_name(f"maintenance.{os.getpid()}.probe")
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("", encoding="utf-8")
    probe.unlink()


@dataclass(frozen=True, slots=True)
class _SettingsChange:
    """The difference between the semantic collection's live settings and the code's."""

    #: Keyword arguments for `update_collection`: only the parts that differ.
    diffs: dict[str, Any]
    #: One plain statement per difference ("graph on disk: false -> true").
    changes: tuple[str, ...]
    #: Whether Qdrant rebuilds the collection's segments for it (a graph or quantization change).
    rebuilds: bool


@dataclass(frozen=True, slots=True)
class _Hold:
    """Why a `grey` collection is not resumed: the record's `held`, and the problem sentence."""

    #: For `CollectionReport.held`: short, and for a crash it ends with the hand-resume command.
    held: str
    #: For the record's problems: the full reasoning, without the command.
    problem: str


class _Job:
    """The state of one run: what it saw, what it did and what it could not fix."""

    def __init__(
        self,
        config: MirrorConfig,
        client: QdrantClient,
        clock: Clock,
        unit: QdrantUnitReport,
        previous: MaintenanceRecord | None,
    ) -> None:
        """Start a run with nothing seen or done.

        Args:
            config: Service settings.
            client: A Qdrant client for the loopback server.
            clock: The current time, timezone-aware.
            unit: This run's reading of Qdrant's unit, `last_crash_at` filled in.
            previous: The previous run's record, or None on the first run. Only its `resumed`
                actions are read, for the "still paused" problem; no decision depends on it.
        """
        self._config = config
        self._client = client
        self._clock = clock
        self._unit = unit
        #: The previous run's `resumed` actions by collection. One that is `grey` again now was
        #: not helped by the resume. Only that run's own actions count, not the carried
        #: `last_action`: a resume long ago says nothing about a pause today.
        self._resumed_before = {
            action.collection: action
            for action in (previous.actions if previous else ())
            if action.action == "resumed"
        }
        self.reports: list[CollectionReport] = []
        self.actions: list[MaintenanceAction] = []
        self.problems: list[str] = []

    def heal(self) -> None:
        """Survey the mirror's collections, then resume and reconcile them.

        If Qdrant does not answer, that is the one problem recorded and nothing else is done.
        """
        try:
            aliases = {
                entry.alias_name: str(entry.collection_name)
                for entry in self._client.get_aliases().aliases
            }
            names = sorted(
                description.name
                for description in self._client.get_collections().collections
                if description.name.startswith(_MIRROR_PREFIXES)
            )
        except Exception as exc:
            # Deliberately broad: the client raises its own family (`ApiException`, wrapping
            # connection refused, timeouts and unparseable replies) and, on odd replies, pydantic
            # and assertion errors. Whichever it is, Qdrant did not answer, and the record says so.
            self.problems.append(f"Qdrant did not answer: {type(exc).__name__}")
            return
        alias_of = {
            collection: alias
            for alias, collection in aliases.items()
            if alias in (index.ALIAS, embed.ALIAS)
        }
        target = aliases.get(embed.ALIAS)
        for name in names:
            info = self._describe(name)
            if info is not None:
                self._look_after(name, alias_of.get(name), info, is_target=name == target)

    def _describe(self, name: str) -> models.CollectionInfo | None:
        """Qdrant's description of one collection, or None (with a problem unless it is gone)."""
        try:
            return self._client.get_collection(name)
        except Exception as exc:
            # Broad for the reason in `heal`. The listing just worked, so a failure here is about
            # this collection: it was deleted since (a monthly build's cleanup; nothing to say),
            # or its description could not be read (a new field value the client rejects).
            if isinstance(exc, UnexpectedResponse) and exc.status_code == 404:
                logger.info(
                    "maintain: %s vanished before it could be described",
                    name,
                    extra={"component": "maintain", "event": "vanished"},
                )
            else:
                self.problems.append(f"{name}: Qdrant would not describe it ({type(exc).__name__})")
            return None

    def _look_after(
        self, name: str, alias: str | None, info: models.CollectionInfo, *, is_target: bool
    ) -> None:
        """Report one collection, and resume it or reconcile its settings if it needs it.

        A `grey` collection whose settings also differ gets one update carrying the settings,
        which also starts its optimizations, and is recorded as `applied_settings` only; if that
        update fails, the plain resume follows in the same run. A `grey` collection the
        crash-loop guard holds gets neither (`_hold`).
        """
        status = _status(info.status)
        error = _optimizer_error(info)
        if status == "red":
            self.problems.append(
                f"{name}: optimizer failed: {error}"
                if error
                else f"{name}: status is red, and Qdrant gave no optimizer error"
            )
        held: str | None = None
        hold = self._hold(name) if status == "grey" else None
        if hold is not None:
            held = hold.held
            self.problems.append(hold.problem)
        else:
            if status == "grey" and name in self._resumed_before:
                self.problems.append(
                    f"{name} is still paused after a resume at {self._resumed_before[name].at}"
                )
            self._resume_or_reconcile(name, info, status, error, is_target=is_target)
        self.reports.append(_collection_report(name, alias, info, held))

    def _resume_or_reconcile(
        self,
        name: str,
        info: models.CollectionInfo,
        status: str,
        error: str | None,
        *,
        is_target: bool,
    ) -> None:
        """Send the one update this collection needs, if any: its settings, else a resume.

        A `grey` collection whose settings update was refused still needs its resume, and gets it
        in the same run: each failure is its own problem in the record.
        """
        change = _settings_change(info) if is_target else None
        sent = False
        if change is not None:
            waiting = self._what_settings_wait_for(name, status, error, change)
            if waiting is None:
                sent = self._apply_settings(name, change, grey=status == "grey")
            else:
                self.problems.append(
                    f"{name}: settings not changed yet: {waiting}; they differ from the code's: "
                    f"{'; '.join(change.changes)}"
                )
        if status == "grey" and not sent:
            self._resume(name)

    def _hold(self, name: str) -> _Hold | None:
        """Why this `grey` collection must not be resumed now, or None when it may be.

        The crash-loop guard (module docstring). Checked in order: the restart count cannot be
        read; Qdrant has restarted itself more than `_RESUMES_AFTER_RESTARTS` times since it was
        last started by hand; Qdrant too near its memory cap; the disk at its floor. Nothing here
        reads the previous record: the answer is the same whether it is missing, damaged or from
        another schema.

        The first two end `held` with the hand-resume command, the way out for a person who has
        looked; the last two lift by themselves when memory or disk recovers.

        Args:
            name: The collection.
        """
        unit = self._unit
        unreadable = _restart_count_unreadable(unit)
        if unreadable is not None:
            return _Hold(
                held=(
                    f"{unreadable}, so a crash loop cannot be ruled out; if Qdrant is healthy, "
                    f"resume by hand: {_resume_command(self._config.qdrant_url, name)}"
                ),
                problem=(
                    f"{name} is held: {unreadable}, so a crash loop cannot be ruled out; it is "
                    "not resumed by itself until the count can be read"
                ),
            )
        if unit.restarts is not None and unit.restarts > _RESUMES_AFTER_RESTARTS:
            last = f" (the last at {unit.started_at})" if unit.started_at else ""
            return _Hold(
                held=(
                    f"{_restarted_phrase(unit.restarts)}; the work may be what kills it. Fix the "
                    "cause (memory cap, disk), then resume by hand: "
                    f"{_resume_command(self._config.qdrant_url, name)}"
                ),
                problem=(
                    f"{name} is held: {_restarted_phrase(unit.restarts)}{last}; resuming it may "
                    "be what kills Qdrant again, so it is not resumed by itself"
                ),
            )
        share = _memory_share(unit)
        if share is not None and share > _MEMORY_HOLD_PERCENT:
            why = (
                f"Qdrant holds {share:.1f}% of its memory cap; resuming would risk another "
                "out-of-memory kill"
            )
            return _Hold(held=why, problem=f"{name} is held: {why}")
        no_room = self._no_room_for(f"the optimizations of {name}")
        if no_room is not None:
            return _Hold(held=no_room, problem=f"{name} is held: {no_room}")
        return None

    def _no_room_for(self, what: str) -> str | None:
        """A sentence saying the disk is under its floor (or cannot be checked), or None if fine.

        `OSError` is a reason to wait too: a disk that cannot be measured is not known to have
        room, and it must not cost the run its record.
        """
        try:
            ensure_free_space(self._config.data_dir, self._config.min_free_disk_gb, what)
        except BuildError as exc:
            return str(exc)
        except OSError as exc:
            return f"the free disk space could not be checked ({type(exc).__name__})"
        return None

    def _what_settings_wait_for(
        self, name: str, status: str, error: str | None, change: _SettingsChange
    ) -> str | None:
        """Why a settings change must not start now, or None when it may.

        The checks run from the cheapest to the one that touches the disk, and the first that
        fails is the answer, as a phrase for the problem sentence. A change that rebuilds waits
        for memory headroom and disk room; one that does not still interrupts the optimizations
        in flight (module docstring), so it waits for `green` (a `grey` collection has nothing
        in flight).
        """
        if not embed.semantic_index_complete(self._config.data_dir, name):
            return "its embedding run has not finished (the index state is not complete)"
        if status == "red":
            quoted = f" ({error[:_QUOTED_ERROR_CHARS]})" if error else ""
            return f"it is red{quoted}, and a settings change would hide why"
        if not change.rebuilds and status not in ("green", "grey"):
            return (
                f"it waits for green (it is {status}): any collection update stops the "
                "optimizations in flight, which would throw away the segment being rebuilt"
            )
        if change.rebuilds:
            # The crash-loop guard covers a rebuild too (`_hold`): it is the heaviest work the
            # job can start, and a threads-only change cannot cause an out-of-memory kill.
            unreadable = _restart_count_unreadable(self._unit)
            if unreadable is not None:
                return f"{unreadable}, so a crash loop cannot be ruled out"
            restarts = self._unit.restarts
            if restarts is not None and restarts > _RESUMES_AFTER_RESTARTS:
                return (
                    f"{_restarted_phrase(restarts)}, and a rebuild may be what kills it; it is "
                    "tried again after a person has looked and started Qdrant by hand"
                )
        share = _memory_share(self._unit)
        if change.rebuilds and share is not None and share > _MEMORY_HOLD_PERCENT:
            return (
                f"Qdrant holds {share:.1f}% of its memory cap and a rebuild would risk an "
                "out-of-memory kill"
            )
        try:
            # Probe, never hold (the module docstring): enter and leave at once.
            with build_lock(self._config.data_dir):
                pass
        except BuildLockedError:
            return "a build or embedding run holds the build lock (it will be tried again)"
        except OSError as exc:
            return f"the build lock could not be checked ({type(exc).__name__})"
        if change.rebuilds:
            # A rebuild rewrites segments, and the optimizer needs room the size of the largest.
            return self._no_room_for(f"the rewritten segments of {name}")
        return None

    def _apply_settings(self, name: str, change: _SettingsChange, *, grey: bool) -> bool:
        """Send the differing settings in one update and record it as `applied_settings`.

        Returns:
            Whether Qdrant took the update. A `grey` collection that is still paused after a
            refusal needs its resume (`_resume_or_reconcile`).
        """
        if change.rebuilds:
            logger.warning(
                "maintain: %s: a storage change starts a background rebuild of every segment "
                "(%s)",
                name,
                "; ".join(change.changes),
                extra={"component": "maintain", "event": "rebuild_starting"},
            )
        detail = "; ".join(change.changes)
        if grey:
            detail += " (it was grey; this also resumed its optimizations)"
        if not self._send(name, "apply settings", **change.diffs):
            return False
        self._record(name, "applied_settings", detail)
        return True

    def _resume(self, name: str) -> None:
        """Send the empty optimizers update that starts a paused collection's optimizations."""
        if self._send(name, "resume", optimizers_config=models.OptimizersConfigDiff()):
            self._record(
                name,
                "resumed",
                "status was grey (optimizations pending, not started); resumed them",
            )

    def _send(self, name: str, what: str, **diffs: Any) -> bool:
        """Send one `update_collection`; False (with a problem) if Qdrant did not take it.

        The update is idempotent, so a request that timed out but was applied is found equal
        by the next run, and one that was not applied is sent again.
        """
        try:
            self._client.update_collection(collection_name=name, **diffs)
        except Exception as exc:
            # Broad for the reason in `heal`: one refused update must not stop the record, which
            # is how the operator learns of it.
            self.problems.append(f"{name}: could not {what}: {type(exc).__name__}")
            return False
        return True

    def _record(self, name: str, action: str, detail: str) -> None:
        """Remember an action taken and log it."""
        self.actions.append(
            MaintenanceAction(at=_stamp(self._clock), collection=name, action=action, detail=detail)
        )
        logger.info(
            "maintain: %s %s: %s",
            action,
            name,
            detail,
            extra={"component": "maintain", "event": action},
        )


def _collection_report(
    name: str,
    alias: str | None,
    info: models.CollectionInfo,
    held: str | None,
) -> CollectionReport:
    """What the record says about one collection, from Qdrant's description of it.

    Args:
        name: The collection.
        alias: The mirror alias that points at it, or None.
        info: Qdrant's description of it.
        held: Why the job will not resume it by itself, or None. Worked out afresh each run.
    """
    error = _optimizer_error(info)
    return CollectionReport(
        name=name,
        alias=alias,
        status=_status(info.status),
        optimizer_status="ok" if error is None else f"error: {error}",
        points=info.points_count,
        indexed_vectors=info.indexed_vectors_count,
        segments=info.segments_count,
        held=held,
    )


def _memory_share(unit: QdrantUnitReport) -> float | None:
    """Qdrant's anonymous memory as a percentage of its cap, or None if either is unknown."""
    if unit.anon_bytes is None or not unit.memory_max_bytes:
        return None
    return 100 * unit.anon_bytes / unit.memory_max_bytes


def _restart_count_unreadable(unit: QdrantUnitReport) -> str | None:
    """Why systemd's restart count is not known, as a phrase, or None when it is.

    The crash-loop guard fails closed on all three: a unit that could not be read, one that is
    not installed (so there is no count, and no way to tell a crash loop from a quiet Qdrant),
    and a reply without the count.
    """
    if unit.found is None:
        return "Qdrant's unit could not be read"
    if unit.found is False:
        return f"Qdrant's unit {QDRANT_UNIT} is not installed"
    if unit.restarts is None:
        return "systemd did not report Qdrant's restart count"
    return None


def _restarted_phrase(restarts: int) -> str:
    """The sentence part for a restart count above the bound, shared by every hold that says it."""
    return (
        f"Qdrant has restarted itself after a failure {restarts} times since it was last "
        "started by hand"
    )


def _resume_command(qdrant_url: str, name: str) -> str:
    """The shell command that resumes a collection by hand: the empty optimizers update.

    Args:
        qdrant_url: `MirrorConfig.qdrant_url` (validated as loopback http).
        name: The collection; quoted for the shell, which matters only for a name nobody expects.
    """
    url = shlex.quote(f"{qdrant_url.rstrip('/')}/collections/{name}")
    return (
        f"runuser -u {_SYNC_USER} -- curl -X PATCH {url} "
        "-H 'Content-Type: application/json' -d '{\"optimizers_config\":{}}'"
    )


def _status(status: object) -> str:
    """Qdrant's collection status as the record's word; a value not known here is `unknown`."""
    value = getattr(status, "value", status)
    return value if isinstance(value, str) and value in COLLECTION_STATUSES else "unknown"


def _optimizer_error(info: models.CollectionInfo) -> str | None:
    """Qdrant's optimizer error text, or None when the optimizer is fine (`ok`)."""
    optimizer = info.optimizer_status
    if isinstance(optimizer, models.OptimizersStatusOneOf1):
        return optimizer.error
    return None


def _flag(value: bool | None) -> str:
    """A setting's on/off as the plain words used in the record's detail."""
    return "true" if value else "false"


def _settings_change(info: models.CollectionInfo) -> _SettingsChange | None:
    """The semantic collection's live settings compared with `embed`'s; None when they are equal.

    Compared: the HNSW graph's `m`, `ef_construct` and `on_disk`; whether the quantization is
    binary with its copy in RAM or on disk; and the optimizer thread count. A flag Qdrant leaves
    unset is its default, off. Only the parts that differ go into the update, so a threads-only
    difference does not touch the graph or the quantization and starts no rebuild.
    """
    config = info.config
    changes: list[str] = []
    graph: dict[str, Any] = {}
    live_graph, wanted_graph = config.hnsw_config, embed.SEMANTIC_HNSW
    if live_graph.m != wanted_graph.m:
        graph["m"] = wanted_graph.m
        changes.append(f"graph m: {live_graph.m} -> {wanted_graph.m}")
    if live_graph.ef_construct != wanted_graph.ef_construct:
        graph["ef_construct"] = wanted_graph.ef_construct
        changes.append(
            f"graph ef_construct: {live_graph.ef_construct} -> {wanted_graph.ef_construct}"
        )
    if bool(live_graph.on_disk) != bool(wanted_graph.on_disk):
        graph["on_disk"] = bool(wanted_graph.on_disk)
        changes.append(
            f"graph on disk: {_flag(live_graph.on_disk)} -> {_flag(wanted_graph.on_disk)}"
        )
    diffs: dict[str, Any] = {}
    if graph:
        diffs["hnsw_config"] = models.HnswConfigDiff(**graph)
    live_quantization = config.quantization_config
    wanted_ram = embed.SEMANTIC_QUANTIZATION.binary.always_ram
    if isinstance(live_quantization, models.BinaryQuantization):
        if bool(live_quantization.binary.always_ram) != bool(wanted_ram):
            diffs["quantization_config"] = embed.SEMANTIC_QUANTIZATION
            changes.append(
                f"binary copy in RAM: {_flag(live_quantization.binary.always_ram)} -> "
                f"{_flag(wanted_ram)}"
            )
    else:
        kind = (
            "none"
            if live_quantization is None
            else type(live_quantization).__name__.removesuffix("Quantization").lower()
        )
        diffs["quantization_config"] = embed.SEMANTIC_QUANTIZATION
        changes.append(f"quantization: {kind} -> binary, copy in RAM: {_flag(wanted_ram)}")
    rebuilds = bool(diffs)
    live_threads = config.optimizer_config.max_optimization_threads
    if live_threads != embed.SEMANTIC_OPTIMIZATION_THREADS:
        diffs["optimizers_config"] = models.OptimizersConfigDiff(
            max_optimization_threads=embed.SEMANTIC_OPTIMIZATION_THREADS
        )
        changes.append(
            f"optimizer threads: {live_threads} -> {embed.SEMANTIC_OPTIMIZATION_THREADS}"
        )
    if not changes:
        return None
    return _SettingsChange(diffs=diffs, changes=tuple(changes), rebuilds=rebuilds)
