"""Qualifies `deploy/wiki-mirror` the way the installer ships it: run through its symlink.

The installer links `/usr/local/bin/wiki-mirror` to `<checkout>/deploy/wiki-mirror`, and the
command finds the checkout, the virtualenv and `update.sh` from where that link points. A
path computed with the wrong number of levels breaks `update`, `key`, `search` and `status` at
once and silently passes every test that only parses the file, which is how it happened after
the deploy files moved up a directory. So the file is copied into a fake checkout
(`app/deploy/wiki-mirror`, a stub `app/.venv/bin/python`, a stub `app/deploy/update.sh`), run
through a symlink in a separate `bin/`, and each verb is asked which stub it reached. `id` is
stubbed, since the command checks for root with `id -u`.

See deploy/README.md and docs/OPERATIONS.md ("The `wiki-mirror` commands").
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
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
