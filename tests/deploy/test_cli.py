"""Qualifies `deploy/wiki-mirror` the way the installer ships it: run through its symlink.

The installer links `/usr/local/bin/wiki-mirror` to `<checkout>/deploy/wiki-mirror`, and the
command finds the checkout, the virtualenv and `update.sh` from where that link points. A
path computed with the wrong number of levels breaks `update`, `key`, `search` and `status` at
once and silently passes every test that only parses the file, which is how it happened after
the deploy files moved up a directory. So the file is copied into a fake checkout
(`app/deploy/wiki-mirror`, a stub `app/.venv/bin/python`, a stub `app/deploy/update.sh`), run
through a symlink in a separate `bin/`, and each verb is asked which stub it reached. `id` is
stubbed, since the command checks for root with `id -u`. `status` is run over stubbed `curl`
(the health reply), `systemctl` and `nft` (real `nft list counters` output from
`tests/fixtures/deploy/`), with the real interpreter standing in for the virtualenv's, and its
output is read: the semantic index, the maintenance record, the schedule and the firewall counters.
The only wall clock is the child process's own, in "minutes ago".

See deploy/README.md and docs/OPERATIONS.md ("The `wiki-mirror` commands").
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
CLI = DEPLOY / "wiki-mirror"
BASH = shutil.which("bash")

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(BASH is None, reason="wiki-mirror needs bash"),
]


@dataclass
class Installed:
    """A scratch box laid out as the installer lays it out."""

    root: Path

    @property
    def app(self) -> Path:
        """The checkout the command must find from its own location."""
        return self.root / "app"

    @property
    def link(self) -> Path:
        """The symlink standing in for /usr/local/bin/wiki-mirror."""
        return self.root / "bin" / "wiki-mirror"

    def run(self, *args: str, uid: str = "0") -> subprocess.CompletedProcess[str]:
        """Run the command through its symlink as user `uid` (what the stub `id -u` says)."""
        environment = {
            **os.environ,
            "PATH": f"{self.root / 'stubs'}:{os.environ['PATH']}",
            "CONF_DIR": str(self.root / "conf"),
            "STUB_UID": uid,
        }
        environment.pop("APP_DIR", None)
        assert BASH is not None
        return subprocess.run(  # noqa: S603 - fixed argv, a repo script run through a symlink
            [str(self.link), *args],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )


def _executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def installed(tmp_path: Path) -> Installed:
    """The command in a fake checkout, reachable only through a symlink in another directory."""
    box = Installed(tmp_path)
    (box.app / "deploy").mkdir(parents=True)
    shutil.copy(CLI, box.app / "deploy" / "wiki-mirror")
    (box.app / "deploy" / "wiki-mirror").chmod(0o755)
    _executable(box.app / "deploy" / "update.sh", '#!/bin/sh\necho "update-stub $0 $*"\n')
    _executable(box.app / ".venv" / "bin" / "python", '#!/bin/sh\necho "python-stub $0 $*"\n')
    _executable(tmp_path / "stubs" / "id", '#!/bin/sh\necho "${STUB_UID:-0}"\n')
    (tmp_path / "conf").mkdir()
    box.link.parent.mkdir()
    box.link.symlink_to("../app/deploy/wiki-mirror")
    return box


def test_cli_update_runs_the_update_script_of_the_checkout_it_is_linked_into(
    installed: Installed,
) -> None:
    result = installed.run("update", "--accept-format-change")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        f"update-stub {installed.app / 'deploy' / 'update.sh'} --accept-format-change"
    )


@pytest.mark.parametrize(
    "verb", [("key", "list"), ("key", "add", "agent"), ("key", "revoke", "agent")]
)
def test_cli_key_runs_the_virtualenv_python_of_the_checkout_it_is_linked_into(
    installed: Installed, verb: tuple[str, ...]
) -> None:
    result = installed.run(*verb)
    assert result.returncode == 0, result.stderr
    interpreter = installed.app / ".venv" / "bin" / "python"
    assert result.stdout.strip() == (
        f"python-stub {interpreter} -I -m wiki_mirror "
        f"--config {installed.root / 'conf' / 'mirror.yaml'} {' '.join(verb)}"
    )


def test_cli_without_root_refuses_the_verbs_that_need_it_and_runs_nothing(
    installed: Installed,
) -> None:
    for verb in (("update",), ("key", "list"), ("status",)):
        result = installed.run(*verb, uid="1000")
        assert result.returncode == 1
        assert "needs root" in result.stderr
        assert result.stdout == ""


def test_cli_help_needs_no_root_and_prints_the_usage_block(installed: Installed) -> None:
    result = installed.run("--help", uid="1000")
    assert result.returncode == 0, result.stderr
    assert "wiki-mirror update" in result.stdout
    assert "wiki-mirror key add <name>" in result.stdout


def _systemd_stubs(installed: Installed, *, active: str = "inactive") -> Path:
    """Stub `systemctl` (recording its calls) and `journalctl`; return the call log's path."""
    stubs = installed.root / "stubs"
    log = stubs / "calls.log"
    _executable(
        stubs / "systemctl",
        '#!/bin/sh\necho "systemctl $*" >> "$(dirname "$0")/calls.log"\n'
        f'case "$1" in is-active) echo {active} ;; show) echo ;; esac\n',
    )
    _executable(stubs / "journalctl", '#!/bin/sh\necho "journalctl $*"\n')
    return log


def test_cli_embed_now_resets_the_start_limit_and_starts_the_embed_unit(
    installed: Installed,
) -> None:
    log = _systemd_stubs(installed)
    result = installed.run("embed-now")
    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls[-2:] == [
        "systemctl reset-failed wiki-mirror-embed.service",
        "systemctl start --no-block wiki-mirror-embed.service",
    ]
    assert "wiki-mirror logs embed" in result.stdout


def test_cli_embed_now_while_a_run_is_going_refuses_and_starts_nothing(
    installed: Installed,
) -> None:
    log = _systemd_stubs(installed, active="active")
    result = installed.run("embed-now")
    assert result.returncode == 1
    assert "already going" in result.stderr
    assert "start" not in log.read_text(encoding="utf-8")


def test_cli_embed_now_needs_root(installed: Installed) -> None:
    result = installed.run("embed-now", uid="1000")
    assert result.returncode == 1
    assert "needs root" in result.stderr


def test_cli_logs_embed_shows_the_embed_units_journal(installed: Installed) -> None:
    _systemd_stubs(installed)
    result = installed.run("logs", "embed")
    assert result.returncode == 0, result.stderr
    # An idle oneshot has nothing to follow: its recent history, like the build unit's.
    assert result.stdout.strip() == "journalctl -u wiki-mirror-embed.service -n 100 --no-pager"


def test_cli_an_unknown_command_names_embed_now_among_the_commands(installed: Installed) -> None:
    result = installed.run("frobnicate")
    assert result.returncode == 1
    assert "embed-now" in result.stderr


def test_cli_help_lists_embed_now(installed: Installed) -> None:
    result = installed.run("--help", uid="1000")
    assert "wiki-mirror embed-now" in result.stdout
    assert "embed" in result.stdout


def test_cli_app_dir_override_still_wins_over_the_location_it_was_found_at(
    installed: Installed, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    _executable(other / "deploy" / "update.sh", '#!/bin/sh\necho "other-stub"\n')
    environment = {
        **os.environ,
        "PATH": f"{tmp_path / 'stubs'}:{os.environ['PATH']}",
        "APP_DIR": str(other),
    }
    result = subprocess.run(  # noqa: S603 - fixed argv, a repo script run through a symlink
        [str(installed.link), "update"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.stdout.strip() == "other-stub"


def test_cli_logs_maintain_shows_the_maintain_units_journal(installed: Installed) -> None:
    _systemd_stubs(installed)
    result = installed.run("logs", "maintain")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "journalctl -u wiki-mirror-maintain.service -n 100 --no-pager"


def test_cli_logs_names_maintain_among_the_logs_it_knows(installed: Installed) -> None:
    result = installed.run("logs", "frobnicate")
    assert result.returncode == 1
    assert "maintain" in result.stderr


def test_cli_help_lists_the_maintain_log(installed: Installed) -> None:
    result = installed.run("--help", uid="1000")
    assert "maintain" in result.stdout


# -- `wiki-mirror status` ----------------------------------------------------------------------

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_API_KEY = "k" * 40


def _health_body(**overrides: object) -> dict[str, object]:
    """A health reply in the shape the API returns, healthy, with no semantic index."""
    body: dict[str, object] = {
        "status": "ok",
        "reasons": [],
        "snapshot": "2026-10-01",
        "applied_snapshot": "20261001",
        "articles": 7000000,
        "passages": 41590915,
        "collection": "wikipedia_lexical_20261001",
        "build": {"phase": "serving"},
        "update": None,
        "semantic": None,
        "maintenance": None,
    }
    return {**body, **overrides}


def _fresh_record(**unit: object) -> dict[str, object]:
    """The fixture maintenance record, run just now (the CLI measures its age on the real clock)."""
    record = json.loads(
        (_FIXTURES / "maintenance" / "maintenance_after_crash.json").read_text(encoding="utf-8")
    )
    record["ran_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    record["qdrant"].update(unit)
    assert isinstance(record, dict)
    return record


def _semantic(**fields: object) -> dict[str, object]:
    """A `semantic` block as the API returns it for the recorded yellow collection."""
    block: dict[str, object] = {
        "collection": "wikipedia_semantic_20261001",
        "status": "yellow",
        "optimizer_status": "ok",
        "points": 41590915,
        "indexed_vectors": 25855559,
        "segments": 85,
        "complete": True,
    }
    return {**block, **fields}


def _status_box(installed: Installed, health: dict[str, object], *, enabled: str = "") -> None:
    """Stub everything `status` reaches: a real Python, the key file, curl, systemctl, nft.

    Args:
        installed: The scratch box.
        health: What the API's health route answers.
        enabled: Space-separated unit names `systemctl is-enabled` reports enabled.
    """
    stubs = installed.root / "stubs"
    # `status` parses the health JSON with the venv's Python; the default stub only echoes.
    _executable(
        installed.app / ".venv" / "bin" / "python", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'
    )
    (installed.root / "conf" / "mirror.env").write_text(
        f"WIKI_MIRROR_API_KEY={_API_KEY}\n", encoding="utf-8"
    )
    body = stubs / "health.json"
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_text(json.dumps(health), encoding="utf-8")
    _executable(
        stubs / "curl",
        f'#!/bin/sh\necho "$*" >> "$(dirname "$0")/curl.args"\ncat "{body}"\n',
    )
    _executable(
        stubs / "systemctl",
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  is-active) echo active ;;\n"
        f'  is-enabled) case " {enabled} " in *" $2 "*) echo enabled ;;'
        " *) echo disabled ;; esac ;;\n"
        "  show) echo ;;\n"
        "esac\n",
    )
    _executable(
        stubs / "nft",
        f'#!/bin/sh\ncat "{_FIXTURES / "deploy" / "nft_list_counters.txt"}"\n',
    )


def _status(installed: Installed, health: dict[str, object], **kwargs: str) -> str:
    """Run `status` over a box whose API answers `health`; return stdout."""
    _status_box(installed, health, **kwargs)
    result = installed.run("status")
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_cli_status_semantic_green_is_ready_with_its_passage_count(installed: Installed) -> None:
    out = _status(installed, _health_body(semantic=_semantic(status="green")))
    assert "ready (41,590,915 passages)" in out


def test_cli_status_semantic_yellow_shows_how_far_the_graph_is(installed: Installed) -> None:
    out = _status(installed, _health_body(semantic=_semantic()))
    assert "building its search graph (25,855,559 of 41,590,915 indexed)" in out
    assert "embedding run complete" in out


def test_cli_status_semantic_grey_says_the_maintenance_job_resumes_it(installed: Installed) -> None:
    out = _status(installed, _health_body(semantic=_semantic(status="grey")))
    assert "paused - the maintenance job resumes it within 15 minutes" in out


def test_cli_status_semantic_optimizer_error_shows_qdrants_text(installed: Installed) -> None:
    out = _status(
        installed,
        _health_body(
            semantic=_semantic(status="red", optimizer_status="error: no space left on device")
        ),
    )
    assert "optimizer failed: no space left on device" in out


def test_cli_status_semantic_incomplete_run_is_said(installed: Installed) -> None:
    out = _status(installed, _health_body(semantic=_semantic(complete=False)))
    assert "embedding run not complete" in out


def test_cli_status_without_a_semantic_index_says_so(installed: Installed) -> None:
    out = _status(installed, _health_body())
    assert "no semantic index yet" in out


def test_cli_status_without_a_maintenance_record_says_how_to_enable_the_timer(
    installed: Installed,
) -> None:
    out = _status(installed, _health_body())
    assert "no maintenance record - enable the timer once: " in out
    assert "systemctl enable --now wiki-mirror-maintain.timer" in out


def test_cli_status_maintenance_shows_run_restarts_memory_and_last_action(
    installed: Installed,
) -> None:
    out = _status(installed, _health_body(maintenance=_fresh_record()))
    assert "last run " in out
    assert " minutes ago)" in out
    assert "Qdrant restarts after a failure: 1, last at 2026-10-23T07:12:23Z" in out
    assert "Qdrant memory 16.9 GB of 27.9 GB (61%)" in out
    assert "last action 2026-10-23T12:30:04Z on wikipedia_semantic_20261001: status was grey" in out


def test_cli_status_maintenance_without_a_memory_cap_says_no_cap(installed: Installed) -> None:
    out = _status(installed, _health_body(maintenance=_fresh_record(memory_max_bytes=None)))
    assert "Qdrant memory 16.9 GB (no cap)" in out


def test_cli_status_maintenance_record_from_long_ago_says_the_job_has_stopped(
    installed: Installed,
) -> None:
    # The fixture's own run time is 2026-10-23: long past by the time the CLI reads the real clock.
    record = json.loads(
        (_FIXTURES / "maintenance" / "maintenance_after_crash.json").read_text(encoding="utf-8")
    )
    record["ran_at"] = "2026-01-01T00:00:00Z"
    out = _status(installed, _health_body(maintenance=record))
    assert "the timer or the job has stopped: wiki-mirror logs maintain" in out


def test_cli_status_maintenance_problems_are_listed(installed: Installed) -> None:
    record = _fresh_record()
    record["problems"] = ["wikipedia_semantic_20261001 stayed grey after a resume"]
    out = _status(installed, _health_body(maintenance=record))
    assert "wikipedia_semantic_20261001 stayed grey after a resume" in out


def test_cli_status_unreadable_unit_is_not_called_not_installed(installed: Installed) -> None:
    out = _status(installed, _health_body(maintenance=_fresh_record(found=None)))
    assert "the Qdrant unit could not be read" in out
    assert "not installed" not in out


def test_cli_status_unit_systemd_says_is_missing_is_called_not_installed(
    installed: Installed,
) -> None:
    out = _status(installed, _health_body(maintenance=_fresh_record(found=False)))
    assert "the Qdrant unit is not installed" in out


def test_cli_status_a_held_collection_shows_its_reason(installed: Installed) -> None:
    record = _fresh_record()
    record["collections"][1]["held"] = "Qdrant holds 95% of its memory cap"  # type: ignore[index]
    out = _status(installed, _health_body(maintenance=record))
    assert "wikipedia_semantic_20261001 is held: Qdrant holds 95% of its memory cap" in out


def test_cli_status_no_longer_prints_a_healing_since_line(installed: Installed) -> None:
    out = _status(installed, _health_body(maintenance=_fresh_record()))
    assert "healing since" not in out


def test_cli_status_prints_every_reason_degraded_gives(installed: Installed) -> None:
    out = _status(
        installed,
        _health_body(
            status="degraded",
            reasons=[
                "Qdrant was restarted after a failure at 2026-10-23T07:12:23Z",
                "the last build failed",
            ],
        ),
    )
    assert "Qdrant was restarted after a failure at 2026-10-23T07:12:23Z" in out
    assert "the last build failed" in out


def test_cli_status_warns_when_the_maintain_timer_is_not_enabled(installed: Installed) -> None:
    out = _status(installed, _health_body())
    assert "wiki-mirror-maintain.timer is not enabled" in out
    assert "systemctl enable --now wiki-mirror-maintain.timer" in out


def test_cli_status_shows_the_maintain_timer_when_it_is_enabled(installed: Installed) -> None:
    out = _status(installed, _health_body(), enabled="wiki-mirror-maintain.timer")
    assert "wiki-mirror-maintain.timer is not enabled" not in out
    assert "wiki-mirror-maintain.timer     " in out


def test_cli_status_firewall_shows_exactly_the_four_counters_with_their_packets(
    installed: Installed,
) -> None:
    out = _status(installed, _health_body())
    counters = out.split("Firewall", 1)[1].splitlines()[1:]
    shown = [line.split() for line in counters if line.strip()]
    assert shown == [
        ["in_refused", "165446", "packets"],
        ["in_invalid", "923", "packets"],
        ["out_refused", "1", "packets"],
        ["service_refused", "4", "packets"],
    ]


def test_cli_status_keeps_the_key_out_of_the_curl_arguments(installed: Installed) -> None:
    _status(installed, _health_body())
    arguments = (installed.root / "stubs" / "curl.args").read_text(encoding="utf-8")
    assert _API_KEY not in arguments
