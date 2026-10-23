"""Qualifies the maintenance job: reading Qdrant's unit, the resume, the settings, the record.

The pure parsing of systemd's and the cgroup's output is checked against the recorded
fixtures in `tests/fixtures/systemd/` (unit tier). `maintain.run_maintenance` runs against
`FakeQdrant`, which answers with the collection replies recorded in `tests/fixtures/qdrant/` parsed
by the client's own models, with the unit reader fed those systemd fixtures, a temporary data
directory and the frozen clock (contract tier); some tests put the real `QdrantClient` in front of
the recorded files, served byte for byte, to see what it parses and the requests it really sends.

Failure paths named here: a `grey` collection (resumed with an empty optimizers update, resumed
again with a problem when a resume did not take), a `red` one (a problem, no settings call), a
settings difference that must wait (the embedding run unfinished, the build lock held or not
checkable, the disk floor and the room a rebuild needs above it, memory near the cap, a
threads-only change until green; a segment size cap that differs is a rebuild, D9), Qdrant not
answering, a collection that vanished or cannot be described, an update Qdrant refuses,
`systemctl` missing, slow or failing, unreadable cgroup files, a unit that is not installed or not
running, and what is and is not a crash.

The crash-loop guard is qualified end to end, and without the previous record: every hold test
runs with that record absent, damaged, of another schema and intact, and gets the same answer.
A `grey` collection is resumed while systemd says Qdrant restarted itself at most once since it
was last started by hand; at two or more, or when the count cannot be read (unit unreadable, not
installed, or no count in the reply), it is held: nothing is sent (over the real client, not even
a settings update), and the record carries the reason and the hand-resume command, whole, even
for a long Qdrant URL. A rebuilding settings change waits on the same two conditions; a threads-only
change does not. Memory over 90% of the cap or a disk under its floor (or one that cannot be
measured) holds a `grey` collection too. A hold lifts by itself when the count is back at zero,
and a change the fake Qdrant applies settles instead of repeating.

See docs/OPERATIONS.md ("The maintenance job") and docs/DESIGN.md section 12.5.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from tests.fixtures.clock import FrozenClock
from tests.fixtures.qdrant_fakes import (
    LEXICAL,
    SEMANTIC,
    FakeQdrant,
    lexical_info,
    recorded_bytes,
    recorded_info,
    recorded_transport,
    semantic_info,
)
from tests.fixtures.wiki_mirror_fakes import make_config
from wiki_mirror import build, embed, maintain
from wiki_mirror.build import build_lock
from wiki_mirror.config import MirrorConfig
from wiki_mirror.maintain import (
    UnitReader,
    UnitReading,
    parse_unit_properties,
    read_qdrant_unit,
    run_maintenance,
    unit_reading_from,
)
from wiki_mirror.maintenance_record import (
    MAX_TEXT_CHARS,
    CollectionReport,
    MaintenanceAction,
    MaintenanceRecord,
    QdrantUnitReport,
    maintenance_record_file,
    read_maintenance_record,
    write_maintenance_record,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_SYSTEMD = _FIXTURES / "systemd"

#: Qdrant's start time in the recorded `systemctl show`: `@1792739543`.
_RECORDED_START = "2026-10-23T07:12:23Z"

#: The clock's stamp: `tests.fixtures.clock.EPOCH`.
_NOW = "2026-01-01T12:00:00Z"

_OLD_SEMANTIC = "wikipedia_semantic_20260901"


# -- fixtures and helpers -------------------------------------------------------------------------


def _show(**overrides: str | None) -> str:
    """The recorded `systemctl show` output, with properties replaced (None drops one)."""
    text = (_SYSTEMD / "qdrant_show_after_crash.txt").read_text(encoding="utf-8")
    properties = parse_unit_properties(text)
    for key, value in overrides.items():
        if value is None:
            properties.pop(key, None)
        else:
            properties[key] = value
    return "".join(f"{key}={value}\n" for key, value in properties.items())


def _cgroup(tmp_path: Path, *, with_stat: bool = True) -> Path:
    """A cgroup tree under `tmp_path` holding the recorded `memory.stat` and `memory.max`."""
    root = tmp_path / "cgroup"
    unit_directory = root / "system.slice" / "wiki-mirror-qdrant.service"
    unit_directory.mkdir(parents=True)
    if with_stat:
        shutil.copyfile(_SYSTEMD / "qdrant_memory.stat", unit_directory / "memory.stat")
    shutil.copyfile(_SYSTEMD / "qdrant_memory.max", unit_directory / "memory.max")
    return root


def _unit_files(root: Path) -> Path:
    """The Qdrant unit's directory inside a tree made by `_cgroup`."""
    return root / "system.slice" / "wiki-mirror-qdrant.service"


def _reader(show: str, cgroup_root: Path) -> UnitReader:
    """A unit reader that answers from the given `systemctl show` text and cgroup tree."""
    return lambda: unit_reading_from(parse_unit_properties(show), cgroup_root)


@pytest.fixture
def config(tmp_path: Path) -> MirrorConfig:
    """Settings pointing at a temporary data directory, with the disk floor off."""
    return make_config(tmp_path)


@pytest.fixture
def cgroup_root(tmp_path: Path) -> Path:
    """The recorded cgroup files in a temporary tree."""
    return _cgroup(tmp_path)


def _mirror(*, semantic: models.CollectionInfo | None = None, **kwargs: Any) -> FakeQdrant:
    """A fake Qdrant with the semantic collection (default: the code's layout) and the lexical."""
    return FakeQdrant({SEMANTIC: semantic or semantic_info(), LEXICAL: lexical_info()}, **kwargs)


def _mark_load(config: MirrorConfig, *, complete: bool, born: str = "20261001") -> None:
    """Write the semantic collection's index state, as the embed stage leaves it."""
    state = embed._EmbedState(
        born=born,
        last_page_id=10,
        articles=10,
        passages=20,
        over_stride=0,
        empty=0,
        complete=complete,
        chunker="v1:512/50",
        model="bge-m3",
        dimensions=1024,
        text_form=embed.TEXT_FORM,
    )
    embed._write_state(embed.semantic_state_file(config.data_dir, born), state)


def _run(
    config: MirrorConfig,
    qdrant: Any,
    cgroup_root: Path,
    clock: FrozenClock,
    *,
    show: str | None = None,
    reader: UnitReader | None = None,
) -> MaintenanceRecord:
    """One maintenance run against the fakes (the recorded unit unless told otherwise)."""
    return run_maintenance(
        config,
        qdrant,
        unit_reader=reader or _reader(show or _show(), cgroup_root),
        clock=clock.now,
    )


def _previous_record(config: MirrorConfig, name: str = "maintenance_after_crash.json") -> None:
    """Put a recorded maintenance record where the job reads the previous run's."""
    path = maintenance_record_file(config.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_FIXTURES / "maintenance" / name, path)


def _unit(
    restarts: int | None = 1,
    invocation: str | None = "inv-a",
    started: str | None = _RECORDED_START,
    crash: str | None = None,
) -> QdrantUnitReport:
    """A unit report with the fields the crash rule looks at."""
    return QdrantUnitReport(
        found=True,
        active=True,
        restarts=restarts,
        invocation_id=invocation,
        started_at=started,
        last_crash_at=crash,
    )


def _record_of(unit: QdrantUnitReport) -> MaintenanceRecord:
    """A previous run's record holding that unit report."""
    return MaintenanceRecord(ran_at="2026-10-23T12:30:04Z", qdrant=unit)


class _Systemctl:
    """Stands in for `subprocess.run`: records the call and answers (or fails) as told."""

    def __init__(
        self, stdout: str = "", returncode: int = 0, error: BaseException | None = None
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


def _refused() -> ResponseHandlingException:
    """What the client raises when Qdrant refuses the connection."""
    return ResponseHandlingException(httpx.ConnectError("connection refused"))


def _memory(tmp_path: Path, *, anon: int, cap: int) -> Path:
    """A cgroup tree whose Qdrant holds `anon` bytes under a cap of `cap` bytes."""
    root = _cgroup(tmp_path)
    (_unit_files(root) / "memory.stat").write_text(f"anon {anon}\nfile 1000\n", encoding="utf-8")
    (_unit_files(root) / "memory.max").write_text(f"{cap}\n", encoding="utf-8")
    return root


# -- parsing systemd's answer ---------------------------------------------------------------------


@pytest.mark.unit
def test_parse_unit_properties_reads_key_value_lines_in_any_order() -> None:
    properties = parse_unit_properties("B=2\nA=1\n\nnot a property\nC=x=y\n=orphan\n")
    assert properties == {"B": "2", "A": "1", "C": "x=y"}


@pytest.mark.unit
def test_unit_reading_of_the_recorded_unit_has_every_field(tmp_path: Path) -> None:
    reading = unit_reading_from(parse_unit_properties(_show()), _cgroup(tmp_path))
    assert reading == UnitReading(
        QdrantUnitReport(
            found=True,
            active=True,
            restarts=1,
            invocation_id="eca45b0334914a06bc0c80efc9d8cb79",
            started_at=_RECORDED_START,
            last_crash_at=None,
            anon_bytes=16908554240,
            memory_max_bytes=27917287424,
        ),
        (),
    )


@pytest.mark.unit
def test_unit_reading_of_a_unit_that_is_not_installed_is_found_false_with_a_problem(
    tmp_path: Path,
) -> None:
    text = (_SYSTEMD / "missing_unit_show.txt").read_text(encoding="utf-8")
    reading = unit_reading_from(parse_unit_properties(text), _cgroup(tmp_path))
    assert reading.report == QdrantUnitReport(found=False, active=False)
    assert reading.problems == ("Qdrant's unit wiki-mirror-qdrant.service is not installed",)


@pytest.mark.unit
def test_unit_reading_of_an_installed_unit_that_is_not_active_says_which_state(
    tmp_path: Path,
) -> None:
    show = _show(ActiveState="failed")
    reading = unit_reading_from(parse_unit_properties(show), _cgroup(tmp_path))
    assert (reading.report.found, reading.report.active) == (True, False)
    assert reading.problems == ("Qdrant's unit is not running (ActiveState=failed)",)


@pytest.mark.unit
def test_unit_reading_without_a_load_state_is_unreadable_not_a_missing_unit(
    tmp_path: Path,
) -> None:
    show = _show(LoadState=None)
    reading = unit_reading_from(parse_unit_properties(show), _cgroup(tmp_path))
    assert reading.report == QdrantUnitReport(found=None, active=False)
    assert "did not say whether" in reading.problems[0]


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "yesterday", "@", "@-5", "@12x", "@99999999999999999"])
def test_unit_reading_start_time_that_is_not_an_epoch_is_unknown(
    tmp_path: Path, value: str
) -> None:
    show = _show(ExecMainStartTimestamp=value)
    reading = unit_reading_from(parse_unit_properties(show), _cgroup(tmp_path))
    assert reading.report.started_at is None


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "many", "-1", "1.5", "9" * 40])
def test_unit_reading_restart_count_that_is_not_a_number_is_unknown(
    tmp_path: Path, value: str
) -> None:
    show = _show(NRestarts=value)
    reading = unit_reading_from(parse_unit_properties(show), _cgroup(tmp_path))
    assert reading.report.restarts is None


@pytest.mark.unit
def test_unit_reading_empty_invocation_id_is_unknown(tmp_path: Path) -> None:
    reading = unit_reading_from(parse_unit_properties(_show(InvocationID="")), _cgroup(tmp_path))
    assert reading.report.invocation_id is None


@pytest.mark.unit
def test_unit_reading_memory_max_of_max_is_none_without_a_problem(tmp_path: Path) -> None:
    root = _cgroup(tmp_path)
    (_unit_files(root) / "memory.max").write_text("max\n", encoding="utf-8")
    reading = unit_reading_from(parse_unit_properties(_show()), root)
    assert reading.report.memory_max_bytes is None
    assert reading.report.anon_bytes == 16908554240
    assert reading.problems == ()


@pytest.mark.unit
def test_unit_reading_without_memory_stat_keeps_the_cap_and_names_the_file(
    tmp_path: Path,
) -> None:
    reading = unit_reading_from(
        parse_unit_properties(_show()), _cgroup(tmp_path, with_stat=False)
    )
    assert reading.report.anon_bytes is None
    assert reading.report.memory_max_bytes == 27917287424
    assert reading.problems == ("could not read Qdrant's memory: memory.stat FileNotFoundError",)


@pytest.mark.unit
def test_unit_reading_memory_stat_without_an_anon_line_is_a_problem(tmp_path: Path) -> None:
    root = _cgroup(tmp_path)
    (_unit_files(root) / "memory.stat").write_text("file 100\nkernel 5\n", encoding="utf-8")
    reading = unit_reading_from(parse_unit_properties(_show()), root)
    assert reading.report.anon_bytes is None
    assert reading.problems == ("could not read Qdrant's memory: memory.stat has no anon line",)


@pytest.mark.unit
def test_unit_reading_memory_max_that_is_not_a_number_is_a_problem(tmp_path: Path) -> None:
    root = _cgroup(tmp_path)
    (_unit_files(root) / "memory.max").write_text("lots\n", encoding="utf-8")
    reading = unit_reading_from(parse_unit_properties(_show()), root)
    assert reading.report.memory_max_bytes is None
    assert reading.problems == ("could not read Qdrant's memory: memory.max is not a number",)


@pytest.mark.unit
def test_unit_reading_with_no_cgroup_files_at_all_is_one_problem_not_two(tmp_path: Path) -> None:
    root = tmp_path / "empty-cgroup"
    root.mkdir()
    reading = unit_reading_from(parse_unit_properties(_show()), root)
    assert (reading.report.anon_bytes, reading.report.memory_max_bytes) == (None, None)
    assert reading.problems == (
        "could not read Qdrant's memory: memory.stat FileNotFoundError; "
        "memory.max FileNotFoundError",
    )
    assert reading.report.restarts == 1  # the rest of the unit is still reported


@pytest.mark.unit
def test_unit_reading_without_a_control_group_has_no_memory_and_no_extra_problem(
    tmp_path: Path,
) -> None:
    reading = unit_reading_from(
        parse_unit_properties(_show(ControlGroup="")), _cgroup(tmp_path)
    )
    assert (reading.report.anon_bytes, reading.report.memory_max_bytes) == (None, None)
    assert reading.problems == ()


@pytest.mark.unit
@pytest.mark.parametrize("control_group", ["system.slice/x", "/../../etc", "/system.slice/../.."])
def test_unit_reading_control_group_path_that_escapes_the_hierarchy_is_not_followed(
    tmp_path: Path, control_group: str
) -> None:
    show = _show(ControlGroup=control_group)
    reading = unit_reading_from(parse_unit_properties(show), _cgroup(tmp_path))
    assert (reading.report.anon_bytes, reading.report.memory_max_bytes) == (None, None)
    assert reading.problems == ("could not read Qdrant's memory: its cgroup path looks wrong",)


# -- asking systemd -------------------------------------------------------------------------------


@pytest.mark.unit
def test_read_qdrant_unit_asks_systemctl_show_for_the_six_properties_without_a_shell(
    tmp_path: Path,
) -> None:
    systemctl = _Systemctl(stdout=_show())
    reading = read_qdrant_unit(run=systemctl, cgroup_root=_cgroup(tmp_path))
    ((argv, kwargs),) = systemctl.calls
    assert argv == [
        "systemctl",
        "show",
        "wiki-mirror-qdrant.service",
        "--timestamp=unix",
        "-p",
        "LoadState,ActiveState,NRestarts,ExecMainStartTimestamp,InvocationID,ControlGroup",
    ]
    assert kwargs["timeout"] == 10
    assert kwargs["check"] is False
    assert not kwargs.get("shell")
    assert reading.report.restarts == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("systemctl", "sentence"),
    [
        (_Systemctl(error=FileNotFoundError("systemctl")), "systemctl was not found"),
        (
            _Systemctl(error=subprocess.TimeoutExpired("systemctl", 10)),
            "systemctl did not answer within 10 s",
        ),
        (_Systemctl(returncode=1), "systemctl exited with status 1"),
        (
            _Systemctl(error=PermissionError("denied")),
            "systemctl could not be run (PermissionError)",
        ),
    ],
    ids=["missing", "timeout", "non-zero", "not-executable"],
)
def test_read_qdrant_unit_when_systemctl_fails_returns_nothing_and_one_problem(
    tmp_path: Path, systemctl: _Systemctl, sentence: str
) -> None:
    reading = read_qdrant_unit(run=systemctl, cgroup_root=_cgroup(tmp_path))
    # `found` is None, not False: "not installed" is an answer and this is the lack of one.
    assert reading == UnitReading(
        QdrantUnitReport(found=None, active=False),
        (f"could not read Qdrant's unit: {sentence}",),
    )


# -- what is a crash ------------------------------------------------------------------------------


@pytest.mark.unit
def test_crash_on_the_first_run_with_restarts_is_the_current_start_time() -> None:
    assert maintain._last_crash_at(_unit(restarts=1), None) == _RECORDED_START


@pytest.mark.unit
def test_crash_on_the_first_run_without_restarts_is_none() -> None:
    assert maintain._last_crash_at(_unit(restarts=0), None) is None


@pytest.mark.unit
def test_crash_with_a_new_invocation_and_more_restarts_is_the_current_start_time() -> None:
    previous = _record_of(_unit(restarts=1, invocation="inv-a", crash="2026-10-01T00:00:00Z"))
    now = _unit(restarts=2, invocation="inv-b", started="2026-10-23T09:00:00Z")
    assert maintain._last_crash_at(now, previous) == "2026-10-23T09:00:00Z"


@pytest.mark.unit
def test_two_crashes_between_runs_give_the_latest_start_once() -> None:
    previous = _record_of(_unit(restarts=0, invocation="inv-a"))
    now = _unit(restarts=2, invocation="inv-c", started="2026-10-23T09:05:00Z")
    assert maintain._last_crash_at(now, previous) == "2026-10-23T09:05:00Z"


@pytest.mark.unit
def test_a_start_by_hand_back_to_zero_restarts_is_not_a_crash_and_the_old_value_is_kept() -> None:
    previous = _record_of(_unit(restarts=1, invocation="inv-a", crash="2026-10-23T07:12:23Z"))
    now = _unit(restarts=0, invocation="inv-b", started="2026-10-23T10:00:00Z")
    assert maintain._last_crash_at(now, previous) == "2026-10-23T07:12:23Z"


@pytest.mark.unit
def test_a_hand_restart_and_then_a_crash_between_two_runs_is_a_crash_at_the_latest_start() -> None:
    # NRestarts 1 -> 0 (by hand) -> 1 (a crash): the count ends where it began, and only the
    # invocation id shows that anything happened.
    previous = _record_of(_unit(restarts=1, invocation="inv-a", crash="2026-10-23T07:12:23Z"))
    now = _unit(restarts=1, invocation="inv-c", started="2026-10-23T10:00:00Z")
    assert maintain._last_crash_at(now, previous) == "2026-10-23T10:00:00Z"


@pytest.mark.unit
def test_a_hand_restart_and_then_a_crash_that_ends_below_the_old_count_is_still_a_crash() -> None:
    previous = _record_of(_unit(restarts=3, invocation="inv-a", crash="2026-10-23T07:12:23Z"))
    now = _unit(restarts=1, invocation="inv-c", started="2026-10-23T10:00:00Z")
    assert maintain._last_crash_at(now, previous) == "2026-10-23T10:00:00Z"


@pytest.mark.unit
def test_a_reboot_and_then_a_crash_between_two_runs_is_a_crash() -> None:
    # A reboot starts Qdrant afresh (NRestarts 0, a new invocation); the crash after it makes 1.
    previous = _record_of(_unit(restarts=0, invocation="inv-a"))
    now = _unit(restarts=1, invocation="inv-c", started="2026-10-23T10:00:00Z")
    assert maintain._last_crash_at(now, previous) == "2026-10-23T10:00:00Z"


@pytest.mark.unit
def test_a_reboot_alone_is_not_a_crash() -> None:
    previous = _record_of(_unit(restarts=2, invocation="inv-a", crash="2026-10-23T07:12:23Z"))
    now = _unit(restarts=0, invocation="inv-b", started="2026-10-23T10:00:00Z")
    assert maintain._last_crash_at(now, previous) == "2026-10-23T07:12:23Z"


@pytest.mark.unit
def test_the_same_invocation_keeps_the_previous_crash_time() -> None:
    previous = _record_of(_unit(restarts=1, invocation="inv-a", crash="2026-10-23T07:12:23Z"))
    assert maintain._last_crash_at(_unit(restarts=1, invocation="inv-a"), previous) == (
        "2026-10-23T07:12:23Z"
    )


@pytest.mark.unit
def test_a_previous_run_that_could_not_read_the_unit_is_no_baseline_so_restarts_count() -> None:
    previous = _record_of(_unit(restarts=None, invocation=None, started=None))
    assert maintain._last_crash_at(_unit(restarts=3), previous) == _RECORDED_START


@pytest.mark.unit
def test_an_invocation_that_cannot_be_read_now_keeps_the_previous_crash_time() -> None:
    previous = _record_of(_unit(crash="2026-10-23T07:12:23Z"))
    assert maintain._last_crash_at(_unit(restarts=2, invocation=None), previous) == (
        "2026-10-23T07:12:23Z"
    )


@pytest.mark.unit
def test_restarts_that_cannot_be_read_now_keep_the_previous_crash_time() -> None:
    previous = _record_of(_unit(crash="2026-10-23T07:12:23Z"))
    unread = _unit(restarts=None, invocation=None, started=None)
    assert maintain._last_crash_at(unread, previous) == "2026-10-23T07:12:23Z"


# -- a run: the resume ----------------------------------------------------------------------------


@pytest.mark.contract
def test_run_maintenance_grey_collection_is_resumed_with_an_empty_optimizers_update(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    qdrant = _mirror(semantic=semantic_info(status="grey"))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == [
        {"collection_name": SEMANTIC, "optimizers_config": models.OptimizersConfigDiff()}
    ]
    (action,) = record.actions
    assert action == MaintenanceAction(
        at=_NOW,
        collection=SEMANTIC,
        action="resumed",
        detail="status was grey (optimizations pending, not started); resumed them",
    )
    assert record.last_action == action
    assert record.problems == ()
    assert record.collections[1].held is None


@pytest.mark.contract
def test_run_maintenance_yellow_collection_with_the_codes_settings_is_left_alone(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=True)
    qdrant = _mirror()
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    assert record.actions == ()
    assert record.problems == ()
    assert record.last_action is None


@pytest.mark.contract
def test_run_maintenance_reports_each_mirror_collection_as_qdrant_described_it(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    record = _run(config, _mirror(), cgroup_root, frozen_clock)
    assert record.collections == (
        CollectionReport(
            name=LEXICAL,
            alias="wikipedia_lexical",
            status="green",
            optimizer_status="ok",
            points=41590915,
            indexed_vectors=41593444,
            segments=43,
        ),
        CollectionReport(
            name=SEMANTIC,
            alias="wikipedia_semantic",
            status="yellow",
            optimizer_status="ok",
            points=41590915,
            indexed_vectors=25855559,
            segments=85,
        ),
    )


@pytest.mark.contract
def test_run_maintenance_ignores_collections_that_are_not_the_mirrors(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    qdrant = FakeQdrant(
        {SEMANTIC: semantic_info(), LEXICAL: lexical_info(), "somebody_elses": lexical_info()}
    )
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert "somebody_elses" not in qdrant.described
    assert [item.name for item in record.collections] == [LEXICAL, SEMANTIC]


@pytest.mark.contract
def test_run_maintenance_status_qdrant_does_not_know_is_unknown_and_nothing_is_done(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    # `model_copy` skips validation, which is how a status the client's enum lacks would arrive.
    odd = semantic_info().model_copy(update={"status": "purple"})
    qdrant = _mirror(semantic=odd)
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert record.collections[1].status == "unknown"
    assert qdrant.updates == []


@pytest.mark.contract
def test_run_maintenance_grey_after_a_resume_last_time_is_a_problem_and_resumed_again(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _previous_record(config)
    qdrant = _mirror(semantic=semantic_info(status="grey"))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert record.problems == (
        f"{SEMANTIC} is still paused after a resume at 2026-10-23T12:30:04Z",
    )
    assert [action.action for action in record.actions] == ["resumed"]
    assert len(qdrant.updates) == 1


@pytest.mark.contract
def test_run_maintenance_collection_that_recovered_after_a_resume_is_no_problem(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _previous_record(config)
    record = _run(config, _mirror(), cgroup_root, frozen_clock)
    assert record.problems == ()
    assert record.actions == ()


@pytest.mark.contract
def test_run_maintenance_grey_again_after_a_quiet_run_is_not_still_paused(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    # The previous run did nothing; only its carried `last_action` is a resume, from long ago.
    _previous_record(config)
    old = read_maintenance_record(config.data_dir)
    assert old is not None
    quiet = MaintenanceRecord(ran_at=old.ran_at, qdrant=old.qdrant, last_action=old.last_action)
    write_maintenance_record(config.data_dir, quiet)
    qdrant = _mirror(semantic=semantic_info(status="grey"))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert record.problems == ()
    assert [action.action for action in record.actions] == ["resumed"]


@pytest.mark.contract
def test_run_maintenance_resumes_every_grey_collection_once(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    qdrant = FakeQdrant(
        {
            SEMANTIC: semantic_info(status="grey"),
            _OLD_SEMANTIC: semantic_info(status="grey"),
            LEXICAL: lexical_info(),
        }
    )
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert sorted(update["collection_name"] for update in qdrant.updates) == [
        _OLD_SEMANTIC,
        SEMANTIC,
    ]
    assert [action.collection for action in record.actions] == [_OLD_SEMANTIC, SEMANTIC]


# -- a run: the crash-loop guard ------------------------------------------------------------------

#: The command the record gives the operator for a hand resume of the semantic collection.
_HAND_RESUME = (
    "runuser -u wikimirror-sync -- curl -X PATCH "
    f"http://127.0.0.1:6333/collections/{SEMANTIC} -H 'Content-Type: application/json' "
    "-d '{\"optimizers_config\":{}}'"
)


#: What the previous run can have left on disk. The guard reads systemd's restart count and
#: nothing the job wrote, so each hold test runs against every one of these and must get the same
#: answer (`_leave_record`).
_PREVIOUS_RECORDS = pytest.mark.parametrize(
    "record_state", ["absent", "fixture", "damaged", "other-schema"]
)

#: Each way systemd's restart count can be unknown (`_unreadable_count`), and the cause a hold
#: names for it.
_NO_COUNT = pytest.mark.parametrize(
    ("kind", "cause"),
    [
        ("unit-unreadable", "Qdrant's unit could not be read"),
        ("unit-not-installed", "Qdrant's unit wiki-mirror-qdrant.service is not installed"),
        ("no-restart-count", "systemd did not report Qdrant's restart count"),
    ],
)


def _leave_record(config: MirrorConfig, state: str) -> None:
    """Put the named kind of previous record where the job reads it (`_PREVIOUS_RECORDS`)."""
    if state == "absent":
        return
    _previous_record(config)
    path = maintenance_record_file(config.data_dir)
    if state == "damaged":
        path.write_text("{ not json", encoding="utf-8")
    elif state == "other-schema":
        text = path.read_text(encoding="utf-8").replace('"schema": 2', '"schema": 3')
        path.write_text(text, encoding="utf-8")
    # The premise of every test using this: a damaged or foreign record reads as no record.
    assert (read_maintenance_record(config.data_dir) is not None) is (state == "fixture")


def _restarted(times: int) -> str:
    """The recorded `systemctl show` of Qdrant, restarted by systemd `times` times since by hand."""
    return _show(NRestarts=str(times))


def _unreadable_count(kind: str, cgroup_root: Path) -> UnitReader:
    """A unit reader for one of the ways the restart count is unknown (`_NO_COUNT`)."""
    if kind == "unit-unreadable":
        missing = _Systemctl(error=FileNotFoundError("systemctl"))
        return lambda: read_qdrant_unit(run=missing, cgroup_root=cgroup_root)
    if kind == "unit-not-installed":
        show = (_SYSTEMD / "missing_unit_show.txt").read_text(encoding="utf-8")
    else:
        assert kind == "no-restart-count"
        show = _show(NRestarts="many")
    return _reader(show, cgroup_root)


def _grey() -> FakeQdrant:
    """A fake Qdrant whose semantic collection is `grey`, as Qdrant leaves it after a restart."""
    return _mirror(semantic=semantic_info(status="grey"))


def _semantic(config: MirrorConfig) -> CollectionReport:
    """The semantic collection's entry in the record on disk."""
    record = read_maintenance_record(config.data_dir)
    assert record is not None
    (entry,) = [item for item in record.collections if item.name == SEMANTIC]
    return entry


@pytest.mark.contract
@_PREVIOUS_RECORDS
@pytest.mark.parametrize("restarts", [0, 1])
def test_crash_loop_a_grey_collection_is_resumed_while_qdrant_has_restarted_at_most_once(
    config: MirrorConfig,
    cgroup_root: Path,
    frozen_clock: FrozenClock,
    restarts: int,
    record_state: str,
) -> None:
    _leave_record(config, record_state)
    qdrant = _grey()
    record = _run(config, qdrant, cgroup_root, frozen_clock, show=_restarted(restarts))
    assert [update["collection_name"] for update in qdrant.updates] == [SEMANTIC]
    assert [action.action for action in record.actions] == ["resumed"]
    assert _semantic(config).held is None


@pytest.mark.contract
@_PREVIOUS_RECORDS
@pytest.mark.parametrize("restarts", [2, 3])
def test_crash_loop_a_grey_collection_is_held_once_qdrant_has_restarted_itself_twice(
    config: MirrorConfig,
    cgroup_root: Path,
    frozen_clock: FrozenClock,
    restarts: int,
    record_state: str,
) -> None:
    _leave_record(config, record_state)
    qdrant = _grey()
    record = _run(config, qdrant, cgroup_root, frozen_clock, show=_restarted(restarts))
    assert qdrant.updates == []
    assert record.actions == ()
    held = _semantic(config).held
    assert held == (
        f"Qdrant has restarted itself after a failure {restarts} times since it was last started "
        "by hand; the work may be what kills it. Fix the cause (memory cap, disk), then resume "
        f"by hand: {_HAND_RESUME}"
    )
    assert record.problems == (
        f"{SEMANTIC} is held: Qdrant has restarted itself after a failure {restarts} times since "
        f"it was last started by hand (the last at {_RECORDED_START}); resuming it may be what "
        "kills Qdrant again, so it is not resumed by itself",
    )


@pytest.mark.contract
@_PREVIOUS_RECORDS
def test_crash_loop_a_held_collection_is_sent_nothing_not_even_a_settings_update(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock, record_state: str
) -> None:
    # The recorded grey reply has the old layout and the embedding run is complete, so without the
    # hold the job would send the settings update (which also resumes). The real client in front
    # of the recorded files shows what goes over the wire: no PATCH at all.
    _leave_record(config, record_state)
    _mark_load(config, complete=True)
    requests: list[httpx.Request] = []
    client = _real_client("collection_semantic_grey.json", requests)
    try:
        record = _run(config, client, cgroup_root, frozen_clock, show=_restarted(2))
    finally:
        client.close()
    assert _patches(requests) == []
    assert record.actions == ()
    assert _semantic(config).held is not None


@pytest.mark.contract
@_PREVIOUS_RECORDS
@_NO_COUNT
def test_crash_loop_a_grey_collection_is_held_when_the_restart_count_cannot_be_read(
    config: MirrorConfig,
    cgroup_root: Path,
    frozen_clock: FrozenClock,
    kind: str,
    cause: str,
    record_state: str,
) -> None:
    _leave_record(config, record_state)
    qdrant = _grey()
    reader = _unreadable_count(kind, cgroup_root)
    record = _run(config, qdrant, cgroup_root, frozen_clock, reader=reader)
    assert qdrant.updates == []
    assert record.actions == ()
    assert _semantic(config).held == (
        f"{cause}, so a crash loop cannot be ruled out; if Qdrant is healthy, resume by hand: "
        f"{_HAND_RESUME}"
    )
    assert (
        f"{SEMANTIC} is held: {cause}, so a crash loop cannot be ruled out; it is not resumed by "
        "itself until the count can be read"
    ) in record.problems


@pytest.mark.contract
def test_crash_loop_a_hold_lifts_when_qdrant_is_started_by_hand_and_the_count_is_back_at_zero(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    qdrant = _grey()
    _run(config, qdrant, cgroup_root, frozen_clock, show=_restarted(2))
    assert qdrant.updates == []
    record = _run(config, qdrant, cgroup_root, frozen_clock, show=_restarted(0))
    assert [update["collection_name"] for update in qdrant.updates] == [SEMANTIC]
    assert [action.action for action in record.actions] == ["resumed"]
    assert _semantic(config).held is None


@pytest.mark.contract
def test_crash_loop_a_hold_for_an_unreadable_count_lifts_when_the_count_can_be_read_again(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    qdrant = _grey()
    reader = _unreadable_count("unit-unreadable", cgroup_root)
    _run(config, qdrant, cgroup_root, frozen_clock, reader=reader)
    assert qdrant.updates == []
    _run(config, qdrant, cgroup_root, frozen_clock)  # the recorded unit: one restart
    assert [update["collection_name"] for update in qdrant.updates] == [SEMANTIC]


@pytest.mark.contract
def test_crash_loop_a_run_that_could_not_ask_qdrant_leaves_nothing_for_the_guard_to_forget(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    # The guard's input is systemd's count, so a run without Qdrant records no collections and
    # loses nothing: the next run, with Qdrant back, holds on the same count.
    _leave_record(config, "fixture")
    down = _mirror(errors={"get_aliases": _refused()})
    record = _run(config, down, cgroup_root, frozen_clock, show=_restarted(2))
    assert record.collections == ()
    assert record.problems == ("Qdrant did not answer: ResponseHandlingException",)
    up = _grey()
    _run(config, up, cgroup_root, frozen_clock, show=_restarted(2))
    assert up.updates == []
    assert _semantic(config).held is not None


@pytest.mark.contract
@_PREVIOUS_RECORDS
def test_crash_loop_a_storage_change_on_a_yellow_collection_waits_after_two_restarts(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock, record_state: str
) -> None:
    _leave_record(config, record_state)
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="yellow", on_disk=False))
    record = _run(config, qdrant, cgroup_root, frozen_clock, show=_restarted(2))
    assert qdrant.updates == []
    assert record.problems == (
        f"{SEMANTIC}: settings not changed yet: Qdrant has restarted itself after a failure 2 "
        "times since it was last started by hand, and a rebuild may be what kills it; it is "
        "tried again after a person has looked and started Qdrant by hand; they differ from "
        "the code's: graph on disk: false -> true",
    )


@pytest.mark.contract
@_NO_COUNT
def test_crash_loop_a_storage_change_waits_when_the_restart_count_cannot_be_read(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock, kind: str, cause: str
) -> None:
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="yellow", on_disk=False))
    reader = _unreadable_count(kind, cgroup_root)
    record = _run(config, qdrant, cgroup_root, frozen_clock, reader=reader)
    assert qdrant.updates == []
    assert any(
        f"{SEMANTIC}: settings not changed yet: {cause}, so a crash loop cannot be ruled out;"
        in problem
        for problem in record.problems
    )


@pytest.mark.contract
@pytest.mark.parametrize("restarts", [2, 5])
def test_crash_loop_a_threads_only_change_on_a_green_collection_is_applied_after_many_restarts(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock, restarts: int
) -> None:
    # The thread count cannot cause an out-of-memory kill, so the restart count does not gate it.
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", threads=1))
    record = _run(config, qdrant, cgroup_root, frozen_clock, show=_restarted(restarts))
    assert [update["collection_name"] for update in qdrant.updates] == [SEMANTIC]
    assert [action.action for action in record.actions] == ["applied_settings"]


@pytest.mark.contract
@_NO_COUNT
def test_crash_loop_a_threads_only_change_is_applied_when_the_restart_count_cannot_be_read(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock, kind: str, cause: str
) -> None:
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", threads=1))
    reader = _unreadable_count(kind, cgroup_root)
    record = _run(config, qdrant, cgroup_root, frozen_clock, reader=reader)
    assert [action.action for action in record.actions] == ["applied_settings"]


@pytest.mark.contract
@pytest.mark.parametrize("kind", ["restarts", "unit-not-installed"])
def test_crash_loop_the_held_text_with_a_long_qdrant_url_keeps_the_whole_command_at_its_end(
    tmp_path: Path, cgroup_root: Path, frozen_clock: FrozenClock, kind: str
) -> None:
    url = "http://127.0.0.1:6333/" + "p" * 100
    config = make_config(tmp_path, qdrant_url=url)
    command = maintain._resume_command(config.qdrant_url, SEMANTIC)
    assert url in command
    if kind == "restarts":
        _run(config, _grey(), cgroup_root, frozen_clock, show=_restarted(2))
    else:
        reader = _unreadable_count(kind, cgroup_root)
        _run(config, _grey(), cgroup_root, frozen_clock, reader=reader)
    held = _semantic(config).held
    assert held is not None
    # The record cuts text at `MAX_TEXT_CHARS`; a cut command would be worse than none.
    assert len(held) <= MAX_TEXT_CHARS
    assert held.endswith(f"resume by hand: {command}")


@pytest.mark.contract
@pytest.mark.parametrize(
    ("anon", "resumed"),
    [(900_000_000, True), (900_000_001, False)],
    ids=["exactly-90-percent", "just-over-90-percent"],
)
def test_crash_loop_memory_over_90_percent_of_the_cap_holds_a_grey_collection(
    tmp_path: Path, config: MirrorConfig, frozen_clock: FrozenClock, anon: int, resumed: bool
) -> None:
    root = _memory(tmp_path, anon=anon, cap=1_000_000_000)
    qdrant = _mirror(semantic=semantic_info(status="grey"))
    record = _run(config, qdrant, root, frozen_clock)
    assert (len(qdrant.updates) == 1) is resumed
    assert (_semantic(config).held is None) is resumed
    if not resumed:
        assert _semantic(config).held == (
            "Qdrant holds 90.0% of its memory cap; resuming would risk another out-of-memory kill"
        )
        assert record.problems == (f"{SEMANTIC} is held: {_semantic(config).held}",)


@pytest.mark.contract
def test_crash_loop_memory_that_is_not_known_holds_nothing(
    tmp_path: Path, config: MirrorConfig, frozen_clock: FrozenClock
) -> None:
    root = _cgroup(tmp_path)
    (_unit_files(root) / "memory.max").write_text("max\n", encoding="utf-8")
    qdrant = _mirror(semantic=semantic_info(status="grey"))
    _run(config, qdrant, root, frozen_clock)
    assert len(qdrant.updates) == 1


@pytest.mark.contract
def test_crash_loop_a_disk_under_its_floor_holds_a_grey_collection(
    tmp_path: Path, cgroup_root: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path, min_free_disk_gb=20)
    _disk(monkeypatch, 5)
    qdrant = _mirror(semantic=semantic_info(status="grey"))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    held = _semantic(config).held
    assert held is not None
    assert held.startswith("not enough free disk")
    assert record.problems == (f"{SEMANTIC} is held: {held}",)


@pytest.mark.contract
def test_crash_loop_a_disk_that_cannot_be_measured_holds_the_collection_and_the_record_survives(
    tmp_path: Path, cgroup_root: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(_path: Path) -> None:
        raise PermissionError("denied")

    config = make_config(tmp_path, min_free_disk_gb=20)
    monkeypatch.setattr(build.shutil, "disk_usage", unreadable)
    qdrant = _mirror(semantic=semantic_info(status="grey"))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    assert _semantic(config).held == "the free disk space could not be checked (PermissionError)"
    assert read_maintenance_record(config.data_dir) == record


# -- a run: the semantic settings -----------------------------------------------------------------


@pytest.mark.contract
def test_run_maintenance_red_collection_is_a_problem_that_quotes_qdrants_error_and_no_settings_call(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=True)
    red = semantic_info(status="red", threads=1, optimizer_error="No space left on device")
    qdrant = _mirror(semantic=red)
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    assert record.actions == ()
    assert f"{SEMANTIC}: optimizer failed: No space left on device" in record.problems
    assert any(
        "settings not changed yet" in problem and "No space left on device" in problem
        for problem in record.problems
    )
    assert record.collections[1].optimizer_status == "error: No space left on device"


@pytest.mark.contract
def test_run_maintenance_red_collection_with_no_optimizer_error_says_so(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    qdrant = _mirror(semantic=semantic_info(status="red"))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert record.problems == (f"{SEMANTIC}: status is red, and Qdrant gave no optimizer error",)
    assert qdrant.updates == []


@pytest.mark.contract
def test_run_maintenance_threads_only_difference_sends_only_the_thread_count(
    config: MirrorConfig,
    cgroup_root: Path,
    frozen_clock: FrozenClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", threads=1))
    with caplog.at_level(logging.INFO, logger="wiki_mirror.maintain"):
        record = _run(config, qdrant, cgroup_root, frozen_clock)
    (update,) = qdrant.updates
    assert set(update) == {"collection_name", "optimizers_config"}
    assert update["optimizers_config"].model_dump(exclude_none=True) == {
        "max_optimization_threads": 2
    }
    (action,) = record.actions
    assert (action.action, action.collection) == ("applied_settings", SEMANTIC)
    assert action.detail == "optimizer threads: 1 -> 2"
    assert record.problems == ()
    # No storage change, so no rebuild to warn about.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.contract
def test_run_maintenance_threads_only_difference_waits_while_the_collection_is_not_green(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    # Any collection update stops the optimizations in flight: applying it to a yellow
    # collection would throw away the segment being rebuilt.
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="yellow", threads=1))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    assert record.actions == ()
    (problem,) = record.problems
    assert problem.startswith(f"{SEMANTIC}: settings not changed yet: it waits for green (it is")
    assert "(it is yellow)" in problem
    assert "optimizer threads: 1 -> 2" in problem


@pytest.mark.contract
def test_run_maintenance_threads_only_difference_applies_once_the_collection_is_green(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="yellow", threads=1))
    _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    qdrant.set_status(SEMANTIC, "green")
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert [update["collection_name"] for update in qdrant.updates] == [SEMANTIC]
    assert [action.action for action in record.actions] == ["applied_settings"]


@pytest.mark.contract
def test_run_maintenance_threads_only_difference_on_a_grey_collection_is_applied_at_once(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    # Nothing is in flight in a grey collection, and the update is what starts its optimizations.
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="grey", threads=1))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    (update,) = qdrant.updates
    assert set(update) == {"collection_name", "optimizers_config"}
    assert [action.action for action in record.actions] == ["applied_settings"]


@pytest.mark.contract
def test_run_maintenance_a_storage_change_is_applied_to_a_yellow_collection(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    # The rebuild it starts replaces whatever is in flight anyway.
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="yellow", on_disk=False))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert [action.action for action in record.actions] == ["applied_settings"]


@pytest.mark.contract
def test_run_maintenance_a_storage_change_waits_while_qdrant_is_near_its_memory_cap(
    tmp_path: Path, config: MirrorConfig, frozen_clock: FrozenClock
) -> None:
    root = _memory(tmp_path, anon=950_000_000, cap=1_000_000_000)
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", on_disk=False))
    record = _run(config, qdrant, root, frozen_clock)
    assert qdrant.updates == []
    (problem,) = record.problems
    assert "Qdrant holds 95.0% of its memory cap and a rebuild would risk" in problem
    assert "graph on disk: false -> true" in problem


@pytest.mark.contract
def test_run_maintenance_a_threads_only_change_does_not_wait_for_memory(
    tmp_path: Path, config: MirrorConfig, frozen_clock: FrozenClock
) -> None:
    root = _memory(tmp_path, anon=950_000_000, cap=1_000_000_000)
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", threads=1))
    _run(config, qdrant, root, frozen_clock)
    assert len(qdrant.updates) == 1


@pytest.mark.contract
def test_run_maintenance_a_change_the_fake_applies_settles_and_is_not_sent_again(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=True)
    qdrant = FakeQdrant(
        {SEMANTIC: recorded_info("collection_semantic_grey.json"), LEXICAL: lexical_info()},
        applies=True,
    )
    first = _run(config, qdrant, cgroup_root, frozen_clock)
    assert [action.action for action in first.actions] == ["applied_settings"]
    second = _run(config, qdrant, cgroup_root, frozen_clock)
    third = _run(config, qdrant, cgroup_root, frozen_clock)
    assert len(qdrant.updates) == 1
    assert second.actions == third.actions == ()
    assert second.problems == third.problems == ()


@pytest.mark.contract
def test_run_maintenance_a_threads_change_the_fake_applies_settles_too(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", threads=1), applies=True)
    _run(config, qdrant, cgroup_root, frozen_clock)
    _run(config, qdrant, cgroup_root, frozen_clock)
    assert len(qdrant.updates) == 1


@pytest.mark.contract
def test_run_maintenance_grey_collection_with_the_old_layout_gets_one_update_and_one_action(
    config: MirrorConfig,
    cgroup_root: Path,
    frozen_clock: FrozenClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The 06:49 reply (reconstructed, see the fixtures' README): graph in RAM, binary copy
    # pinned, one optimizer thread, grey.
    _mark_load(config, complete=True)
    qdrant = FakeQdrant(
        {SEMANTIC: recorded_info("collection_semantic_grey.json"), LEXICAL: lexical_info()}
    )
    with caplog.at_level(logging.INFO, logger="wiki_mirror.maintain"):
        record = _run(config, qdrant, cgroup_root, frozen_clock)
    (update,) = qdrant.updates
    assert update["collection_name"] == SEMANTIC
    assert update["hnsw_config"].model_dump(exclude_none=True) == {"on_disk": True}
    assert update["quantization_config"].model_dump(exclude_none=True) == {
        "binary": {"always_ram": False}
    }
    assert update["optimizers_config"].model_dump(exclude_none=True) == {
        "max_segment_size": 10_000_000,
        "max_optimization_threads": 2,
    }
    (action,) = record.actions
    assert action.action == "applied_settings"
    assert action.detail.startswith(
        "graph on disk: false -> true; binary copy in RAM: true -> false; "
        "segment size cap: 1000000 KB -> 10000000 KB; optimizer threads: 1 -> 2"
    )
    assert "also resumed" in action.detail
    assert record.last_action == action
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "rebuild" in warnings[0]


@pytest.mark.contract
def test_run_maintenance_a_different_graph_size_is_corrected_with_only_that_field(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=True)
    info = semantic_info()
    odd = info.model_copy(
        update={"config": info.config.model_copy(
            update={"hnsw_config": info.config.hnsw_config.model_copy(update={"m": 32})}
        )}
    )
    qdrant = _mirror(semantic=odd)
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    (update,) = qdrant.updates
    assert set(update) == {"collection_name", "hnsw_config"}
    assert update["hnsw_config"].model_dump(exclude_none=True) == {"m": 16}
    assert record.actions[0].detail == "graph m: 32 -> 16"


@pytest.mark.contract
def test_run_maintenance_a_collection_without_binary_quantization_gets_it_back(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=True)
    info = semantic_info()
    bare = info.model_copy(
        update={"config": info.config.model_copy(update={"quantization_config": None})}
    )
    qdrant = _mirror(semantic=bare)
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    (update,) = qdrant.updates
    assert update["quantization_config"] == embed.SEMANTIC_QUANTIZATION
    assert record.actions[0].detail == "quantization: none -> binary, copy in RAM: false"


@pytest.mark.contract
def test_run_maintenance_a_cap_difference_alone_is_applied_as_a_rebuild_to_a_yellow_collection(
    config: MirrorConfig,
    cgroup_root: Path,
    frozen_clock: FrozenClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # D9: the merges the new cap sets off rewrite every segment, so it is a rebuilding change: it
    # does not wait for green (the merges replace whatever is in flight) and it is warned about.
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="yellow", segment_cap=1_000_000))
    with caplog.at_level(logging.INFO, logger="wiki_mirror.maintain"):
        record = _run(config, qdrant, cgroup_root, frozen_clock)
    (update,) = qdrant.updates
    assert set(update) == {"collection_name", "optimizers_config"}
    assert update["optimizers_config"].model_dump(exclude_none=True) == {
        "max_segment_size": 10_000_000
    }
    (action,) = record.actions
    assert (action.action, action.collection) == ("applied_settings", SEMANTIC)
    assert action.detail == "segment size cap: 1000000 KB -> 10000000 KB"
    assert record.problems == ()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "rebuild" in warnings[0]


@pytest.mark.contract
def test_run_maintenance_threads_and_cap_differing_together_go_in_one_optimizers_diff(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", threads=1, segment_cap=1_000_000))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    (update,) = qdrant.updates
    assert set(update) == {"collection_name", "optimizers_config"}
    assert update["optimizers_config"].model_dump(exclude_none=True) == {
        "max_segment_size": 10_000_000,
        "max_optimization_threads": 2,
    }
    (action,) = record.actions
    assert action.detail == "segment size cap: 1000000 KB -> 10000000 KB; optimizer threads: 1 -> 2"


@pytest.mark.contract
def test_run_maintenance_a_cap_that_is_unset_is_applied(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    # Qdrant reports `null` for a collection on its own default (indexing threads x 200,000 KB),
    # which is never the code's cap.
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", segment_cap=None))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    (update,) = qdrant.updates
    assert update["optimizers_config"].model_dump(exclude_none=True) == {
        "max_segment_size": 10_000_000
    }
    assert record.actions[0].detail == "segment size cap: Qdrant's default -> 10000000 KB"


@pytest.mark.contract
def test_run_maintenance_equal_caps_send_nothing(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=True)
    live = semantic_info(status="yellow").config.optimizer_config.max_segment_size
    assert live == embed.SEMANTIC_MAX_SEGMENT_KB
    qdrant = _mirror(semantic=semantic_info(status="yellow"))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    assert record.actions == ()
    assert record.problems == ()


@pytest.mark.contract
def test_run_maintenance_a_cap_the_fake_applies_settles_and_is_not_sent_again(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", segment_cap=1_000_000), applies=True)
    _run(config, qdrant, cgroup_root, frozen_clock)
    second = _run(config, qdrant, cgroup_root, frozen_clock)
    assert len(qdrant.updates) == 1
    assert second.actions == ()
    assert second.problems == ()


@pytest.mark.contract
@_PREVIOUS_RECORDS
def test_run_maintenance_a_cap_difference_waits_after_two_restarts(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock, record_state: str
) -> None:
    _leave_record(config, record_state)
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="yellow", segment_cap=1_000_000))
    record = _run(config, qdrant, cgroup_root, frozen_clock, show=_restarted(2))
    assert qdrant.updates == []
    assert record.problems == (
        f"{SEMANTIC}: settings not changed yet: Qdrant has restarted itself after a failure 2 "
        "times since it was last started by hand, and a rebuild may be what kills it; it is "
        "tried again after a person has looked and started Qdrant by hand; they differ from "
        "the code's: segment size cap: 1000000 KB -> 10000000 KB",
    )


@pytest.mark.contract
@_NO_COUNT
def test_run_maintenance_a_cap_difference_waits_when_the_restart_count_cannot_be_read(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock, kind: str, cause: str
) -> None:
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="yellow", segment_cap=1_000_000))
    reader = _unreadable_count(kind, cgroup_root)
    record = _run(config, qdrant, cgroup_root, frozen_clock, reader=reader)
    assert qdrant.updates == []
    assert any(
        f"{SEMANTIC}: settings not changed yet: {cause}, so a crash loop cannot be ruled out"
        in problem
        and "segment size cap: 1000000 KB -> 10000000 KB" in problem
        for problem in record.problems
    )


@pytest.mark.contract
@pytest.mark.parametrize(
    ("anon", "applied"),
    [(900_000_000, True), (900_000_001, False)],
    ids=["exactly-90-percent", "just-over-90-percent"],
)
def test_run_maintenance_a_cap_difference_waits_over_90_percent_of_the_memory_cap(
    tmp_path: Path, config: MirrorConfig, frozen_clock: FrozenClock, anon: int, applied: bool
) -> None:
    root = _memory(tmp_path, anon=anon, cap=1_000_000_000)
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", segment_cap=1_000_000))
    record = _run(config, qdrant, root, frozen_clock)
    assert (len(qdrant.updates) == 1) is applied
    if applied:
        assert record.problems == ()
    else:
        (problem,) = record.problems
        assert "Qdrant holds 90.0% of its memory cap and a rebuild would risk" in problem
        assert "segment size cap: 1000000 KB -> 10000000 KB" in problem


@pytest.mark.contract
def test_run_maintenance_settings_difference_waits_for_the_embedding_run_to_finish(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=False)
    qdrant = _mirror(semantic=semantic_info(threads=1))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    assert record.actions == ()
    (problem,) = record.problems
    assert problem.startswith(f"{SEMANTIC}: settings not changed yet: its embedding run has not")
    assert "optimizer threads: 1 -> 2" in problem


@pytest.mark.contract
def test_run_maintenance_settings_difference_without_any_index_state_is_not_applied(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    qdrant = _mirror(semantic=semantic_info(threads=1))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    assert "embedding run has not finished" in record.problems[0]


@pytest.mark.contract
def test_run_maintenance_grey_collection_whose_settings_must_wait_is_still_resumed(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=False)
    qdrant = _mirror(semantic=recorded_info("collection_semantic_grey.json"))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == [
        {"collection_name": SEMANTIC, "optimizers_config": models.OptimizersConfigDiff()}
    ]
    assert [action.action for action in record.actions] == ["resumed"]
    assert len(record.problems) == 1


@pytest.mark.contract
def test_run_maintenance_settings_difference_waits_while_a_build_holds_the_lock(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", threads=1))
    with build_lock(config.data_dir):
        record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    (problem,) = record.problems
    assert "a build or embedding run holds the build lock" in problem


@pytest.mark.contract
def test_run_maintenance_does_not_leave_the_build_lock_held(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", threads=1))
    _run(config, qdrant, cgroup_root, frozen_clock)
    with build_lock(config.data_dir):
        pass  # raises BuildLockedError if the job still held it


@pytest.mark.contract
def test_run_maintenance_a_build_lock_that_cannot_be_checked_postpones_the_change(
    config: MirrorConfig,
    cgroup_root: Path,
    frozen_clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable(data_dir: Path) -> None:
        raise PermissionError(data_dir)

    monkeypatch.setattr(maintain, "build_lock", unreadable)
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", threads=1))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    assert "the build lock could not be checked (PermissionError)" in record.problems[0]


class _Usage(SimpleNamespace):
    """What `shutil.disk_usage` returns."""


def _disk(monkeypatch: pytest.MonkeyPatch, free_gb: float) -> None:
    """Make `build.shutil.disk_usage` report `free_gb` GB free."""
    gb = 1024**3
    usage = _Usage(total=100 * gb, used=0, free=int(free_gb * gb))
    monkeypatch.setattr(build.shutil, "disk_usage", lambda _path: usage)


@pytest.mark.contract
def test_run_maintenance_a_storage_change_waits_when_the_disk_is_below_the_floor(
    tmp_path: Path, cgroup_root: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path, min_free_disk_gb=20)
    _disk(monkeypatch, 5)
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", on_disk=False))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    (problem,) = record.problems
    assert "not enough free disk" in problem
    assert "min_free_disk_gb" in problem


@pytest.mark.contract
def test_run_maintenance_a_storage_change_waits_when_the_disk_cannot_be_measured(
    tmp_path: Path, cgroup_root: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(_path: Path) -> None:
        raise PermissionError("denied")

    config = make_config(tmp_path, min_free_disk_gb=20)
    monkeypatch.setattr(build.shutil, "disk_usage", unreadable)
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", on_disk=False))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    assert "the free disk space could not be checked (PermissionError)" in record.problems[0]
    assert read_maintenance_record(config.data_dir) == record


@pytest.mark.contract
def test_run_maintenance_a_storage_change_is_applied_when_the_disk_has_its_floor(
    tmp_path: Path, cgroup_root: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path, min_free_disk_gb=20)
    _disk(monkeypatch, 500)
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", on_disk=False))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert len(qdrant.updates) == 1
    assert record.problems == ()


@pytest.mark.contract
def test_run_maintenance_a_threads_only_change_needs_no_disk_room(
    tmp_path: Path, cgroup_root: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path, min_free_disk_gb=20)
    _disk(monkeypatch, 5)
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", threads=1))
    _run(config, qdrant, cgroup_root, frozen_clock)
    assert len(qdrant.updates) == 1


@pytest.mark.unit
def test_rebuild_room_is_the_merges_at_once_times_the_cap_times_the_measured_factor() -> None:
    # D9: 2 merges x 10,000,000 KB x 1024 B x 1.27 = 24.2 GB of 1024 cubed bytes, rounded up. The
    # number the operator is told; the pins below keep the units (Qdrant's KB, our GB) honest.
    assert maintain._QDRANT_KB_BYTES == 1024
    assert maintain._GB_BYTES == build._BYTES_PER_GB
    assert maintain._REBUILD_ROOM_GB == 25


@pytest.mark.contract
def test_run_maintenance_a_rebuild_waits_when_the_disk_is_between_the_floor_and_the_room(
    tmp_path: Path, cgroup_root: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path, min_free_disk_gb=20)
    _disk(monkeypatch, 30)  # above the 20 GB floor, below 20 + 25
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", segment_cap=1_000_000))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    (problem,) = record.problems
    assert "not enough free disk to create the rewritten segments of" in problem
    assert "30.0 GB free" in problem
    assert "25 GB of room" in problem
    assert "floor of 20 GB (min_free_disk_gb)" in problem
    assert "segment size cap: 1000000 KB -> 10000000 KB" in problem


@pytest.mark.contract
def test_run_maintenance_a_rebuild_is_applied_when_the_disk_has_the_floor_and_the_room(
    tmp_path: Path, cgroup_root: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path, min_free_disk_gb=20)
    _disk(monkeypatch, 20 + maintain._REBUILD_ROOM_GB)
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", segment_cap=1_000_000))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert len(qdrant.updates) == 1
    assert record.problems == ()


@pytest.mark.contract
def test_run_maintenance_a_threads_only_change_is_not_held_by_the_rebuild_room(
    tmp_path: Path, cgroup_root: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path, min_free_disk_gb=20)
    _disk(monkeypatch, 30)
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(status="green", threads=1))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert len(qdrant.updates) == 1
    assert record.problems == ()


@pytest.mark.contract
def test_crash_loop_a_grey_collection_is_resumed_with_the_floor_but_not_the_rebuild_room(
    tmp_path: Path, cgroup_root: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A resume writes little: only a rebuilding settings change asks for the room.
    config = make_config(tmp_path, min_free_disk_gb=20)
    _disk(monkeypatch, 30)
    qdrant = _mirror(semantic=semantic_info(status="grey"))
    _run(config, qdrant, cgroup_root, frozen_clock)
    assert len(qdrant.updates) == 1
    assert _semantic(config).held is None


@pytest.mark.contract
def test_run_maintenance_only_the_aliased_semantic_collection_has_its_settings_reconciled(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=True)
    _mark_load(config, complete=True, born="20260901")
    qdrant = FakeQdrant(
        {
            SEMANTIC: semantic_info(),
            _OLD_SEMANTIC: semantic_info(threads=1, on_disk=False),
            LEXICAL: lexical_info(),
        }
    )
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    assert record.actions == ()


@pytest.mark.contract
def test_run_maintenance_without_a_semantic_alias_reconciles_nothing(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=True)
    qdrant = _mirror(semantic=semantic_info(threads=1), aliases={"wikipedia_lexical": LEXICAL})
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    assert record.collections[1].alias is None
    assert record.problems == ()


@pytest.mark.contract
def test_run_maintenance_a_refused_update_is_a_problem_and_the_record_is_still_written(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    qdrant = _mirror(
        semantic=semantic_info(status="grey"), errors={"update_collection": _refused()}
    )
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert record.actions == ()
    assert record.problems == (f"{SEMANTIC}: could not resume: ResponseHandlingException",)
    assert read_maintenance_record(config.data_dir) == record


@pytest.mark.contract
def test_run_maintenance_a_refused_settings_update_on_a_grey_collection_is_followed_by_the_resume(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    # The recorded grey reply has the old layout: the settings update goes first, is refused, and
    # the collection must not stay paused for it. Each failure is its own problem.
    _mark_load(config, complete=True)
    qdrant = _mirror(
        semantic=recorded_info("collection_semantic_grey.json"),
        errors={"update_collection:once": _refused()},
    )
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == [
        {"collection_name": SEMANTIC, "optimizers_config": models.OptimizersConfigDiff()}
    ]
    assert [action.action for action in record.actions] == ["resumed"]
    assert record.problems == (f"{SEMANTIC}: could not apply settings: ResponseHandlingException",)


@pytest.mark.contract
def test_run_maintenance_a_refused_settings_update_and_a_refused_resume_are_two_problems(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=True)
    qdrant = _mirror(
        semantic=recorded_info("collection_semantic_grey.json"),
        errors={"update_collection": _refused()},
    )
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    assert record.actions == ()
    assert record.problems == (
        f"{SEMANTIC}: could not apply settings: ResponseHandlingException",
        f"{SEMANTIC}: could not resume: ResponseHandlingException",
    )


# -- a run: Qdrant not answering ------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.parametrize("failing", ["get_aliases", "get_collections"])
@pytest.mark.parametrize(
    "error",
    [_refused(), ResponseHandlingException(httpx.ReadTimeout("timed out"))],
    ids=["refused", "timeout"],
)
def test_run_maintenance_qdrant_not_answering_is_one_problem_and_a_written_record(
    config: MirrorConfig,
    cgroup_root: Path,
    frozen_clock: FrozenClock,
    failing: str,
    error: Exception,
) -> None:
    qdrant = _mirror(semantic=semantic_info(status="grey"), errors={failing: error})
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert record.problems == ("Qdrant did not answer: ResponseHandlingException",)
    assert record.collections == ()
    assert record.actions == ()
    assert qdrant.updates == []
    assert read_maintenance_record(config.data_dir) == record
    assert record.qdrant.restarts == 1  # the unit is still reported


@pytest.mark.contract
def test_run_maintenance_a_collection_deleted_since_the_listing_is_skipped_without_a_problem(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    gone = UnexpectedResponse(404, "Not Found", b"{}", httpx.Headers())
    qdrant = _mirror(errors={f"get_collection:{LEXICAL}": gone})
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert [item.name for item in record.collections] == [SEMANTIC]
    assert record.problems == ()


@pytest.mark.contract
def test_run_maintenance_a_collection_qdrant_will_not_describe_is_a_problem_and_the_rest_go_on(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    refused = UnexpectedResponse(500, "Internal Server Error", b"{}", httpx.Headers())
    qdrant = FakeQdrant(
        {
            SEMANTIC: semantic_info(),
            LEXICAL: lexical_info(),
            _OLD_SEMANTIC: semantic_info(status="grey"),
        },
        errors={f"get_collection:{LEXICAL}": refused},
    )
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert record.problems == (f"{LEXICAL}: Qdrant would not describe it (UnexpectedResponse)",)
    assert [item.name for item in record.collections] == [_OLD_SEMANTIC, SEMANTIC]
    assert [action.collection for action in record.actions] == [_OLD_SEMANTIC]


# -- a run: the unit ------------------------------------------------------------------------------


@pytest.mark.contract
def test_run_maintenance_records_the_unit_with_its_memory_and_first_run_crash_time(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    record = _run(config, _mirror(), cgroup_root, frozen_clock)
    assert record.qdrant == QdrantUnitReport(
        found=True,
        active=True,
        restarts=1,
        invocation_id="eca45b0334914a06bc0c80efc9d8cb79",
        started_at=_RECORDED_START,
        last_crash_at=_RECORDED_START,
        anon_bytes=16908554240,
        memory_max_bytes=27917287424,
    )
    assert record.ran_at == _NOW


@pytest.mark.contract
def test_run_maintenance_a_new_invocation_with_more_restarts_is_recorded_as_a_crash(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _previous_record(config)
    show = _show(NRestarts="2", InvocationID="0" * 32, ExecMainStartTimestamp="@1792750000")
    record = _run(config, _mirror(), cgroup_root, frozen_clock, show=show)
    assert record.qdrant.last_crash_at == "2026-10-23T10:06:40Z"


@pytest.mark.contract
def test_run_maintenance_a_start_by_hand_is_not_a_crash_and_the_old_time_is_kept(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _previous_record(config)
    show = _show(NRestarts="0", InvocationID="0" * 32, ExecMainStartTimestamp="@1792750000")
    record = _run(config, _mirror(), cgroup_root, frozen_clock, show=show)
    assert record.qdrant.last_crash_at == _RECORDED_START
    assert record.qdrant.started_at == "2026-10-23T10:06:40Z"


@pytest.mark.contract
def test_run_maintenance_the_same_invocation_keeps_the_previous_crash_time(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _previous_record(config)
    record = _run(config, _mirror(), cgroup_root, frozen_clock)
    assert record.qdrant.last_crash_at == _RECORDED_START


@pytest.mark.contract
def test_run_maintenance_memory_max_of_max_is_recorded_as_no_cap(
    tmp_path: Path, config: MirrorConfig, frozen_clock: FrozenClock
) -> None:
    root = _cgroup(tmp_path)
    (_unit_files(root) / "memory.max").write_text("max\n", encoding="utf-8")
    record = _run(config, _mirror(), root, frozen_clock)
    assert record.qdrant.memory_max_bytes is None
    assert record.problems == ()


@pytest.mark.contract
def test_run_maintenance_a_unit_that_is_not_installed_is_found_false_and_collections_are_tended(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    # A threads-only change is tended without the unit (it cannot cause an out-of-memory kill);
    # a `grey` collection is not resumed without a restart count (the guard tests).
    _mark_load(config, complete=True)
    show = (_SYSTEMD / "missing_unit_show.txt").read_text(encoding="utf-8")
    qdrant = _mirror(semantic=semantic_info(status="green", threads=1))
    record = _run(config, qdrant, cgroup_root, frozen_clock, show=show)
    assert record.qdrant == QdrantUnitReport(found=False, active=False)
    assert record.problems == ("Qdrant's unit wiki-mirror-qdrant.service is not installed",)
    assert [action.action for action in record.actions] == ["applied_settings"]


@pytest.mark.contract
def test_run_maintenance_a_unit_that_is_not_running_is_a_problem(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    record = _run(config, _mirror(), cgroup_root, frozen_clock, show=_show(ActiveState="failed"))
    assert record.problems == ("Qdrant's unit is not running (ActiveState=failed)",)
    assert record.qdrant.active is False


@pytest.mark.contract
@pytest.mark.parametrize(
    "systemctl",
    [
        _Systemctl(error=FileNotFoundError("systemctl")),
        _Systemctl(error=subprocess.TimeoutExpired("systemctl", 10)),
        _Systemctl(returncode=1),
    ],
    ids=["missing", "timeout", "non-zero"],
)
def test_run_maintenance_when_systemctl_fails_the_run_completes_with_a_problem(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock, systemctl: _Systemctl
) -> None:
    qdrant = _mirror()
    record = run_maintenance(
        config,
        qdrant,
        unit_reader=lambda: read_qdrant_unit(run=systemctl, cgroup_root=cgroup_root),
        clock=frozen_clock.now,
    )
    assert record.qdrant == QdrantUnitReport(found=None, active=False)
    assert len(record.problems) == 1
    assert record.problems[0].startswith("could not read Qdrant's unit: systemctl ")
    assert [item.name for item in record.collections] == [LEXICAL, SEMANTIC]
    assert read_maintenance_record(config.data_dir) == record


# -- a run: the record ----------------------------------------------------------------------------


@pytest.mark.contract
def test_run_maintenance_a_quiet_run_carries_the_previous_last_action(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _previous_record(config)
    before = read_maintenance_record(config.data_dir)
    assert before is not None
    record = _run(config, _mirror(), cgroup_root, frozen_clock)
    assert record.actions == ()
    assert record.last_action == before.last_action
    assert record.last_action is not None


@pytest.mark.contract
def test_run_maintenance_the_record_on_disk_is_the_one_it_returns(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    qdrant = _mirror(semantic=semantic_info(status="grey"))
    record = _run(config, qdrant, cgroup_root, frozen_clock)
    assert read_maintenance_record(config.data_dir) == record


@pytest.mark.contract
def test_run_maintenance_a_damaged_previous_record_is_treated_as_a_first_run(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    path = maintenance_record_file(config.data_dir)
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    record = _run(config, _mirror(), cgroup_root, frozen_clock)
    assert record.qdrant.last_crash_at == _RECORDED_START
    assert read_maintenance_record(config.data_dir) == record


@pytest.mark.contract
def test_run_maintenance_a_record_that_cannot_be_written_raises_oserror(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    config.data_dir.mkdir(parents=True)
    (config.data_dir / "state").write_text("a file where the state directory should be")
    with pytest.raises(OSError):  # noqa: PT011 - any OSError; the command turns it into exit 1
        _run(config, _mirror(), cgroup_root, frozen_clock)


@pytest.mark.contract
def test_run_maintenance_a_record_that_cannot_be_written_stops_the_run_before_anything_is_sent(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    # A run that cannot report must not act: what it did would be invisible to the operator.
    config.data_dir.mkdir(parents=True)
    (config.data_dir / "state").write_text("a file where the state directory should be")
    qdrant = _mirror(semantic=semantic_info(status="grey"))
    with pytest.raises(OSError):  # noqa: PT011 - any OSError; the command turns it into exit 1
        _run(config, qdrant, cgroup_root, frozen_clock)
    assert qdrant.updates == []
    assert qdrant.described == []


@pytest.mark.contract
def test_run_maintenance_leaves_no_probe_file_behind(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _run(config, _mirror(), cgroup_root, frozen_clock)
    assert [path.name for path in (config.data_dir / "state").iterdir()] == ["maintenance.json"]


@pytest.mark.contract
def test_run_maintenance_logs_each_action_and_problem_and_a_summary_with_the_component_key(
    config: MirrorConfig,
    cgroup_root: Path,
    frozen_clock: FrozenClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _previous_record(config)
    qdrant = _mirror(semantic=semantic_info(status="grey"))
    with caplog.at_level(logging.INFO, logger="wiki_mirror.maintain"):
        _run(config, qdrant, cgroup_root, frozen_clock)
    seen = [
        (record.levelno, record.__dict__["component"], record.__dict__["event"])
        for record in caplog.records
    ]
    assert seen == [
        (logging.INFO, "maintain", "resumed"),
        (logging.WARNING, "maintain", "problem"),
        (logging.INFO, "maintain", "run"),
    ]
    assert SEMANTIC in caplog.text


# -- the real client ------------------------------------------------------------------------------


def _real_client(semantic_file: str, requests: list[httpx.Request]) -> QdrantClient:
    """The real client in front of the recorded files, served byte for byte (no socket).

    The 1.12 client (the server's minor, see pyproject.toml) makes no version request on
    construction and has no `check_compatibility` argument, so none is passed.

    Args:
        semantic_file: The recorded reply the semantic collection answers with.
        requests: Receives every request the client sends.
    """
    bodies = {
        SEMANTIC: recorded_bytes(semantic_file),
        LEXICAL: recorded_bytes("collection_lexical_green.json"),
    }
    return QdrantClient(
        url="http://127.0.0.1:6333",
        transport=recorded_transport(bodies, requests),
    )


def _patches(requests: list[httpx.Request]) -> list[tuple[str, Any]]:
    """The path and JSON body of every PATCH the client sent."""
    return [(r.url.path, json.loads(r.content)) for r in requests if r.method == "PATCH"]


@pytest.mark.contract
def test_resume_over_the_real_client_patches_the_collection_with_an_empty_optimizers_config(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    # The index state is absent, so the grey collection's old layout waits and it is only resumed.
    requests: list[httpx.Request] = []
    client = _real_client("collection_semantic_grey.json", requests)
    try:
        record = _run(config, client, cgroup_root, frozen_clock)
    finally:
        client.close()
    # The body Qdrant's documented trigger needs: not an empty body, not nothing at all.
    assert _patches(requests) == [(f"/collections/{SEMANTIC}", {"optimizers_config": {}})]
    assert [(item.name, item.status) for item in record.collections] == [
        (LEXICAL, "green"),
        (SEMANTIC, "grey"),
    ]
    assert [action.action for action in record.actions] == ["resumed"]


@pytest.mark.contract
def test_the_recorded_yellow_reply_is_read_by_the_real_client_into_the_record(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    # The recorded yellow collection has one optimizer thread and the old 1 GB cap (D9). The cap
    # makes the change a rebuild, so it does not wait for green: one update carries both.
    _mark_load(config, complete=True)
    requests: list[httpx.Request] = []
    client = _real_client("collection_semantic_yellow.json", requests)
    try:
        record = _run(config, client, cgroup_root, frozen_clock)
    finally:
        client.close()
    assert _patches(requests) == [
        (
            f"/collections/{SEMANTIC}",
            {"optimizers_config": {"max_segment_size": 10_000_000, "max_optimization_threads": 2}},
        )
    ]
    assert record.collections[1] == CollectionReport(
        name=SEMANTIC,
        alias="wikipedia_semantic",
        status="yellow",
        optimizer_status="ok",
        points=41590915,
        indexed_vectors=25855559,
        segments=85,
    )
    (action,) = record.actions
    assert action.detail == "segment size cap: 1000000 KB -> 10000000 KB; optimizer threads: 1 -> 2"
    assert record.problems == ()


@pytest.mark.contract
def test_settings_over_the_real_client_send_only_the_differing_parts(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    _mark_load(config, complete=True)
    requests: list[httpx.Request] = []
    client = _real_client("collection_semantic_grey.json", requests)
    try:
        _run(config, client, cgroup_root, frozen_clock)
    finally:
        client.close()
    assert _patches(requests) == [
        (
            f"/collections/{SEMANTIC}",
            {
                "hnsw_config": {"on_disk": True},
                "quantization_config": {"binary": {"always_ram": False}},
                "optimizers_config": {
                    "max_segment_size": 10_000_000,
                    "max_optimization_threads": 2,
                },
            },
        )
    ]


@pytest.mark.contract
def test_run_maintenance_over_the_real_client_with_nothing_listening_is_one_problem(
    config: MirrorConfig, cgroup_root: Path, frozen_clock: FrozenClock
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = QdrantClient(url="http://127.0.0.1:6333", transport=httpx.MockTransport(refuse))
    try:
        record = _run(config, client, cgroup_root, frozen_clock)
    finally:
        client.close()
    assert record.problems == ("Qdrant did not answer: ResponseHandlingException",)
    assert record.collections == ()

