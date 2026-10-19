"""Qualifies `deploy/update.sh` by running it: every outcome, the lock and the guard.

The script is run for real against a scratch box: a bare "origin" and a checkout cloned from it,
a fake virtualenv whose `pip` records its calls, and stubs on PATH for `id` (so a non-root test
user passes the root check), `systemctl`, `curl` and `sleep`. Everything it installs goes to
scratch directories through the script's own path variables. What is under test is the script's
decisions: it records `current`, `updated`, `deferred`, `held`, `failed` and `rolled_back` in
`state/update.json`; it holds the build's lock for the whole swap and treats a held lock exactly
like a running build; a build is a quiet deferral unattended and a refusal by hand; a commit that
changes `deploy/FORMAT` is held when built data exists and not otherwise; and an exit it
did not plan still leaves a `failed` record; the firewall the checkout would produce for the
site is compared with the installed one (through the installer's own `--show-firewall`) and only
ever reported, never applied, and never fails the update. What systemd, pip and the real API do
is qualified on the box.

See docs/DESIGN.md section 12 and docs/OPERATIONS.md (R.9, R.10).
"""

from __future__ import annotations

import fcntl
import grp
import json
import os
import pwd
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
UPDATE_SH = DEPLOY / "update.sh"
BASH = shutil.which("bash")
GIT = shutil.which("git")

#: `contract`, not `unit`: each test spawns a real shell and real git against real files, which
#: is the boundary being qualified (the same tier as the other deploy-script tests).
pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(
        BASH is None or shutil.which("flock") is None or shutil.which("git") is None,
        reason="update.sh needs bash, flock and git",
    ),
]

OLD_FORMAT = "store=2 chunker=v1:512/50 index=1"
NEW_FORMAT = "store=3 chunker=v1:512/50 index=1"
KEY = "test-key-not-a-secret-0123456789"
#: A valid site file in the shared address space (100.64.0.0/10), as the firewall note renders it.
SITE = "LAN_CIDR=100.64.1.0/24\nEMBEDDING_HOST=100.64.1.77\nEMBEDDING_PORT=11434\n"

_STUBS = {
    # `id -u` says 0 so the root check passes; anything else is the real `id`.
    "id": '#!/bin/sh\n[ "$1" = "-u" ] && echo 0 && exit 0\nexec /usr/bin/id "$@"\n',
    # `show <unit> -p <Property> --value` answers from the environment; every call is logged.
    "systemctl": (
        '#!/bin/sh\necho "systemctl $*" >> "$STUB_LOG"\n'
        'if [ "$1" = show ]; then\n'
        '  case "$4" in\n'
        '    ActiveState) echo "${STUB_ACTIVE:-inactive}" ;;\n'
        '    SubState) echo "${STUB_SUB:-dead}" ;;\n'
        "  esac\nfi\nexit 0\n"
    ),
    "curl": '#!/bin/sh\nexit "${STUB_CURL_EXIT:-0}"\n',
    "sleep": "#!/bin/sh\nexit 0\n",
}

#: pip logs its call and whether it could take the build lock (it cannot while update.sh holds
#: it). With STUB_PIP_FAIL=1 the first call fails and later ones succeed, so a rollback's
#: reinstall works; with STUB_PIP_FAIL=always every call fails.
_PIP = (
    '#!/bin/sh\necho "pip $*" >> "$STUB_LOG"\n'
    'if flock -n "$STUB_LOCK" true; then echo "pip lock free" >> "$STUB_LOG"; '
    'else echo "pip lock held" >> "$STUB_LOG"; fi\n'
    '[ "${STUB_PIP_FAIL:-0}" = always ] && exit 1\n'
    # "after": the update's own two pip calls pass, every later call (a rollback's) fails.
    'if [ "${STUB_PIP_FAIL:-0}" = after ] && '
    '[ "$(grep -c "^pip install" "$STUB_LOG")" -gt 2 ]; then exit 1; fi\n'
    'if [ "${STUB_PIP_FAIL:-0}" = 1 ] && [ ! -e "$STUB_PIP_MARK" ]; then\n'
    '  touch "$STUB_PIP_MARK"\n  exit 1\nfi\nexit 0\n'
)


@dataclass
class Box:
    """A scratch mirror box: an origin, a checkout cloned from it, and the data directory."""

    root: Path

    @property
    def app(self) -> Path:
        """The root-owned checkout (APP_DIR)."""
        return self.root / "app"

    @property
    def origin(self) -> Path:
        """The bare repository `git fetch origin main` reads."""
        return self.root / "origin.git"

    @property
    def work(self) -> Path:
        """A second clone the test commits to and pushes from, standing in for GitHub."""
        return self.root / "work"

    @property
    def data(self) -> Path:
        """The data directory (DATA_DIR) with `state` and `articles`."""
        return self.root / "data"

    @property
    def state(self) -> Path:
        """`<data_dir>/state`: the build lock and `update.json`."""
        return self.data / "state"

    @property
    def log(self) -> Path:
        """What the stubs were asked to do, one line each."""
        return self.root / "stub.log"

    @property
    def lock(self) -> Path:
        """The build lock file."""
        return self.state / "build.lock"

    def git_env(self) -> dict[str, str]:
        """An environment in which git has an identity and ignores the machine's config."""
        return {
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }

    def git(self, cwd: Path, *args: str) -> str:
        """Run git in `cwd` and return its stdout, failing the test on an error."""
        assert GIT is not None
        done = subprocess.run(  # noqa: S603 - fixed argv, scratch repositories
            [GIT, *args], cwd=cwd, env=self.git_env(), capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    def publish(self, files: dict[str, str]) -> str:
        """Commit `files` (path -> content) to origin's main from the work clone; return the id."""
        for relative, content in files.items():
            path = self.work / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "-m", "change")
        self.git(self.work, "push", "-q", "origin", "main")
        return self.git(self.work, "rev-parse", "HEAD")

    def head(self) -> str:
        """The checkout's current commit."""
        return self.git(self.app, "rev-parse", "HEAD")

    def build_data(self) -> None:
        """Pretend a build finished: one article store exists, so there is data to protect."""
        (self.data / "articles" / "wikipedia_20261001.db").write_bytes(b"x")

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        """Run update.sh with the box's paths and the stubs first on PATH."""
        me = pwd.getpwuid(os.getuid()).pw_name
        group = grp.getgrgid(os.getgid()).gr_name
        environment = {
            **self.git_env(),
            "PATH": f"{self.root / 'stubs'}:{os.environ['PATH']}",
            "APP_DIR": str(self.app),
            "CONF_DIR": str(self.root / "conf"),
            "DATA_DIR": str(self.data),
            "UNIT_DIR": str(self.root / "units"),
            "BIN_LINK": str(self.root / "bin" / "wiki-mirror"),
            "NFT_INSTALLED": str(self.root / "installed.nft"),
            "BRANCH": "main",
            # The real script chowns update.json to root:wikimirror and creates the lock for
            # wikimirror-sync.
            "SYNC_USER": me,
            "SHARED_GROUP": group,
            "RECORD_OWNER": f"{me}:{group}",
            "STUB_LOG": str(self.log),
            "STUB_LOCK": str(self.lock),
            "STUB_PIP_MARK": str(self.root / "pip.failed"),
            **env,
        }
        environment.pop("WIKI_MIRROR_UPDATE_SELF_COPY", None)
        assert BASH is not None
        return subprocess.run(  # noqa: S603 - fixed argv, a repo script
            [BASH, str(UPDATE_SH), *args],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def record(self) -> dict[str, Any]:
        """What the run left in `update.json`."""
        loaded = json.loads((self.state / "update.json").read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def calls(self) -> list[str]:
        """The stubs' log lines, in order (empty if nothing was called)."""
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def show_firewall(self) -> subprocess.CompletedProcess[str]:
        """What the checkout's installer renders for this box's site file (what update.sh reads)."""
        assert BASH is not None
        return subprocess.run(  # noqa: S603 - fixed argv, a repo script in the scratch checkout
            [BASH, str(self.app / "deploy" / "install.sh"), "--show-firewall"],
            env={**os.environ, "APP_DIR": str(self.app), "CONF_DIR": str(self.root / "conf")},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )


def _make_box(root: Path, base_format: str | None) -> Box:
    """A box whose checkout and origin are at commit A, with `base_format` as FORMAT (or none)."""
    box = Box(root)
    box.work.mkdir(parents=True)
    box.git(box.work, "init", "-q", "-b", "main")
    files = {
        "deploy/constraints.txt": "",
        "deploy/wiki-mirror": "#!/bin/sh\n",
        "deploy/nftables/wiki-mirror.nft": "# rules\n",
        "deploy/systemd/wiki-mirror.service": "[Unit]\n",
    }
    if base_format is not None:
        files["deploy/FORMAT"] = base_format + "\n"
    for relative, content in files.items():
        path = box.work / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    box.git(box.work, "add", "-A")
    box.git(box.work, "commit", "-q", "-m", "first")
    box.git(root, "clone", "-q", "--bare", str(box.work), str(box.origin))
    box.git(box.work, "remote", "add", "origin", str(box.origin))
    box.git(root, "clone", "-q", str(box.origin), str(box.app))

    (box.state).mkdir(parents=True)
    (box.data / "articles").mkdir()
    (root / "conf").mkdir()
    (root / "conf" / "mirror.env").write_text(f"WIKI_MIRROR_API_KEY={KEY}\n", encoding="utf-8")
    (root / "units").mkdir()
    (root / "bin").mkdir()
    stubs = root / "stubs"
    stubs.mkdir()
    for name, body in _STUBS.items():
        (stubs / name).write_text(body, encoding="utf-8")
        (stubs / name).chmod(0o755)
    venv_bin = box.app / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (venv_bin / "pip").write_text(_PIP, encoding="utf-8")
    for tool in ("python", "pip"):
        (venv_bin / tool).chmod(0o755)
    # The venv is not tracked, and a checkout with an untracked directory is still clean to
    # `git diff --quiet HEAD`, which is all update.sh asks.
    return box


@pytest.fixture
def box(tmp_path: Path) -> Box:
    """A box with a FORMAT file at the first commit and no built data."""
    return _make_box(tmp_path, OLD_FORMAT)


def _pip_calls(box: Box) -> list[str]:
    return [line for line in box.calls() if line.startswith("pip install")]


# -- the record -------------------------------------------------------------------------------


def test_update_nothing_new_records_current_and_changes_nothing(box: Box) -> None:
    result = box.run("--unattended")
    record = box.record()
    assert result.returncode == 0, result.stdout + result.stderr
    assert record["outcome"] == "current"
    assert record["from"] == record["to"] == box.head()[:12]
    assert record["unattended"] is True
    assert _pip_calls(box) == []


def test_update_record_is_complete_private_and_written_atomically(box: Box) -> None:
    box.publish({"README.md": "new\n"})
    result = box.run("--unattended")
    record = box.record()
    assert result.returncode == 0, result.stdout + result.stderr
    assert set(record) == {"at", "from", "to", "outcome", "detail", "unattended"}
    datetime.fromisoformat(record["at"])
    assert len(record["from"]) == len(record["to"]) == 12
    path = box.state / "update.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert not list(box.state.glob(".update.json.*")), "a temp file was left behind"


def test_update_record_never_holds_the_api_key_or_a_url(box: Box) -> None:
    box.publish({"README.md": "new\n"})
    result = box.run("--unattended")
    written = (box.state / "update.json").read_text(encoding="utf-8")
    assert KEY not in written + result.stdout + result.stderr
    assert "://" not in written


# -- updated ----------------------------------------------------------------------------------


def test_update_new_commit_is_merged_installed_restarted_and_recorded(box: Box) -> None:
    new = box.publish({"README.md": "new\n"})
    result = box.run("--unattended")
    record = box.record()
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.head() == new
    assert (record["outcome"], record["to"]) == ("updated", new[:12])
    assert any(call == "systemctl restart wiki-mirror.service" for call in box.calls())
    assert (box.app / ".venv" / ".installed-commit").read_text(encoding="utf-8").strip() == new


def test_update_by_hand_records_that_it_was_not_unattended(box: Box) -> None:
    box.publish({"README.md": "new\n"})
    assert box.run().returncode == 0
    assert box.record()["unattended"] is False


def test_update_new_timer_is_installed_but_not_enabled_and_the_record_says_so(box: Box) -> None:
    box.publish({"deploy/systemd/wiki-mirror-update.timer": "[Timer]\n"})
    result = box.run("--unattended")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (box.root / "units" / "wiki-mirror-update.timer").exists()
    assert "new timer not enabled: wiki-mirror-update.timer" in box.record()["detail"]
    assert not any("enable" in call for call in box.calls())


# -- the build lock and a running build -------------------------------------------------------


def test_update_holds_the_build_lock_while_it_swaps_the_package(box: Box) -> None:
    box.publish({"README.md": "new\n"})
    assert box.run("--unattended").returncode == 0
    assert "pip lock held" in box.calls()
    assert "pip lock free" not in box.calls()


def test_update_creates_the_build_lock_when_there_is_none(box: Box) -> None:
    box.publish({"README.md": "new\n"})
    assert not box.lock.exists()
    assert box.run("--unattended").returncode == 0
    assert stat.S_IMODE(box.lock.stat().st_mode) == 0o640


def test_update_unattended_with_the_build_unit_running_defers_and_exits_zero(box: Box) -> None:
    before = box.head()
    new = box.publish({"README.md": "new\n"})
    result = box.run("--unattended", STUB_ACTIVE="activating", STUB_SUB="start")
    record = box.record()
    assert result.returncode == 0, result.stdout + result.stderr
    assert (record["outcome"], record["unattended"]) == ("deferred", True)
    assert (record["from"], record["to"]) == (before[:12], new[:12])
    assert box.head() == before, "a deferred update must not touch the checkout"
    assert _pip_calls(box) == []


def test_update_unattended_with_the_build_lock_held_defers_like_a_running_build(
    box: Box,
) -> None:
    before = box.head()
    box.publish({"README.md": "new\n"})
    box.lock.write_text("", encoding="utf-8")
    with box.lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = box.run("--unattended")
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.record()["outcome"] == "deferred"
    assert box.head() == before
    assert _pip_calls(box) == []


def test_update_by_hand_with_a_build_running_refuses_and_still_records_it(box: Box) -> None:
    before = box.head()
    box.publish({"README.md": "new\n"})
    result = box.run(STUB_ACTIVE="active", STUB_SUB="running")
    assert result.returncode == 1
    assert "a build is running" in result.stderr
    assert (box.record()["outcome"], box.record()["unattended"]) == ("deferred", False)
    assert box.head() == before


def test_update_build_waiting_for_its_retry_is_not_a_running_build(box: Box) -> None:
    box.publish({"README.md": "new\n"})
    result = box.run("--unattended", STUB_ACTIVE="activating", STUB_SUB="auto-restart")
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.record()["outcome"] == "updated"


def test_update_with_nothing_new_and_a_build_running_says_current_not_deferred(box: Box) -> None:
    result = box.run("--unattended", STUB_ACTIVE="active", STUB_SUB="running")
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.record()["outcome"] == "current"


# -- the format guard -------------------------------------------------------------------------


def test_update_unattended_format_change_over_built_data_is_held_and_exits_zero(box: Box) -> None:
    before = box.head()
    box.build_data()
    new = box.publish({"deploy/FORMAT": NEW_FORMAT + "\n"})
    result = box.run("--unattended")
    record = box.record()
    assert result.returncode == 0, result.stdout + result.stderr
    assert (record["outcome"], record["to"]) == ("held", new[:12])
    assert OLD_FORMAT in record["detail"]
    assert NEW_FORMAT in record["detail"]
    assert "wiki-mirror update --accept-format-change" in record["detail"]
    assert box.head() == before, "a held update must not merge"
    assert _pip_calls(box) == []


def test_update_by_hand_format_change_over_built_data_refuses_without_the_flag(box: Box) -> None:
    before = box.head()
    box.build_data()
    box.publish({"deploy/FORMAT": NEW_FORMAT + "\n"})
    result = box.run()
    assert result.returncode == 1
    assert "--accept-format-change" in result.stderr
    assert "wiki-mirror build-now" in result.stderr
    assert (box.record()["outcome"], box.record()["unattended"]) == ("held", False)
    assert box.head() == before


def test_update_by_hand_accepting_a_format_change_updates_and_says_to_rebuild(box: Box) -> None:
    box.build_data()
    new = box.publish({"deploy/FORMAT": NEW_FORMAT + "\n"})
    result = box.run("--accept-format-change")
    record = box.record()
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.head() == new
    assert record["outcome"] == "updated"
    assert "wiki-mirror build-now" in record["detail"]


def test_update_format_change_with_no_built_data_just_proceeds(box: Box) -> None:
    new = box.publish({"deploy/FORMAT": NEW_FORMAT + "\n"})
    result = box.run("--unattended")
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.head() == new
    assert box.record()["outcome"] == "updated"


def test_update_unchanged_format_over_built_data_proceeds(box: Box) -> None:
    box.build_data()
    new = box.publish({"README.md": "new\n"})
    assert box.run("--unattended").returncode == 0
    assert (box.head(), box.record()["outcome"]) == (new, "updated")


def test_update_running_commit_without_a_format_file_is_held_over_built_data(
    tmp_path: Path,
) -> None:
    box = _make_box(tmp_path, None)
    before = box.head()
    box.build_data()
    box.publish({"deploy/FORMAT": OLD_FORMAT + "\n"})
    result = box.run("--unattended")
    record = box.record()
    assert result.returncode == 0, result.stdout + result.stderr
    assert record["outcome"] == "held"
    assert "(none)" in record["detail"]
    assert box.head() == before


# -- failures ---------------------------------------------------------------------------------


def test_update_unreachable_origin_is_failed_and_the_record_has_no_target(box: Box) -> None:
    before = box.head()
    shutil.rmtree(box.origin)
    result = box.run("--unattended")
    record = box.record()
    assert result.returncode == 1
    assert (record["outcome"], record["to"]) == ("failed", None)
    assert "could not fetch from origin" in record["detail"]
    assert str(box.origin) not in record["detail"], "git's own words name the remote"
    assert box.head() == before


def test_update_local_changes_in_the_checkout_are_failed_and_never_discarded(box: Box) -> None:
    box.publish({"README.md": "new\n"})
    (box.app / "deploy/wiki-mirror").write_text("# edited in production\n", encoding="utf-8")
    result = box.run("--unattended")
    assert result.returncode == 1
    assert box.record()["outcome"] == "failed"
    assert "local changes" in box.record()["detail"]
    edited = box.app / "deploy/wiki-mirror"
    assert edited.read_text(encoding="utf-8") == "# edited in production\n"


def test_update_package_install_failure_rolls_back_and_records_rolled_back(box: Box) -> None:
    before = box.head()
    box.publish({"README.md": "new\n"})
    result = box.run("--unattended", STUB_PIP_FAIL="1")
    record = box.record()
    assert result.returncode == 1
    assert (record["outcome"], record["from"]) == ("rolled_back", before[:12])
    assert "package install failed" in record["detail"]
    assert box.head() == before


def test_update_new_version_that_never_becomes_healthy_rolls_back(box: Box) -> None:
    before = box.head()
    box.publish({"README.md": "new\n"})
    result = box.run("--unattended", STUB_CURL_EXIT="22")
    record = box.record()
    assert result.returncode == 1
    assert record["outcome"] == "rolled_back"
    assert "did not become healthy" in record["detail"]
    assert box.head() == before
    restarts = [call for call in box.calls() if call.startswith("systemctl restart")]
    assert len(restarts) == 2, "one for the new version, one after the rollback"


def test_update_stop_nobody_planned_after_the_merge_is_rolled_back_and_recorded(box: Box) -> None:
    # The wiki-mirror link cannot be made (no such directory): `set -e` ends the run after the
    # merge, a path no `die` covers. The exit trap rolls back, records it, and keeps the exit
    # status.
    before = box.head()
    box.publish({"README.md": "new\n"})
    result = box.run("--unattended", BIN_LINK=str(box.root / "missing" / "wiki-mirror"))
    record = box.record()
    assert result.returncode != 0
    assert (record["outcome"], record["from"]) == ("rolled_back", before[:12])
    assert "stopped unexpectedly" in record["detail"]
    assert box.head() == before


def test_update_unit_install_failing_after_the_merge_is_rolled_back(box: Box) -> None:
    before = box.head()
    box.publish({"README.md": "new\n"})
    not_a_directory = box.root / "units-file"
    not_a_directory.write_text("", encoding="utf-8")
    result = box.run("--unattended", UNIT_DIR=str(not_a_directory))
    record = box.record()
    assert result.returncode != 0
    assert record["outcome"] == "rolled_back"
    assert box.head() == before
    assert (box.app / ".venv" / ".installed-commit").exists()
    restarts = [call for call in box.calls() if call.startswith("systemctl restart")]
    assert len(restarts) == 1, "the rollback restarts the API once"


def test_update_trap_rollback_that_fails_is_recorded_failed_and_keeps_the_exit_status(
    box: Box,
) -> None:
    before = box.head()
    new = box.publish({"README.md": "new\n"})
    result = box.run(
        "--unattended", BIN_LINK=str(box.root / "missing" / "wiki-mirror"), STUB_PIP_FAIL="after"
    )
    record = box.record()
    assert result.returncode == 1, "the original non-zero exit survives the trap"
    assert record["outcome"] == "failed"
    assert "rollback itself was incomplete" in record["detail"]
    assert "stopped unexpectedly" in record["detail"]
    assert box.head() == before, "the reset itself worked"
    assert record["to"] == new[:12]
    assert len(_pip_calls(box)) == 3, "the rollback ran once and did not recurse"


def test_update_rollback_that_cannot_reinstall_is_failed_with_rollback_incomplete(
    box: Box,
) -> None:
    box.publish({"README.md": "new\n"})
    result = box.run("--unattended", STUB_PIP_FAIL="always")
    record = box.record()
    assert result.returncode == 1
    assert record["outcome"] == "failed"
    assert "rollback itself was incomplete" in record["detail"]
    installs = _pip_calls(box)
    assert len(installs) == 2, "one install of the new version, one attempt at the rollback"


# -- the firewall note ------------------------------------------------------------------------


def _publish_firewall_files(box: Box) -> str:
    """Commit the real installer and firewall template, as a release that changes the firewall."""
    return box.publish(
        {
            "deploy/install.sh": (DEPLOY / "install.sh").read_text(encoding="utf-8"),
            "deploy/nftables/wiki-mirror.nft": (DEPLOY / "nftables" / "wiki-mirror.nft").read_text(
                encoding="utf-8"
            ),
        }
    )


def _write_site(box: Box, text: str) -> None:
    (box.root / "conf" / "site.env").write_text(text, encoding="utf-8")


def test_update_firewall_rendered_for_this_site_that_differs_is_noted_and_not_applied(
    box: Box,
) -> None:
    _publish_firewall_files(box)
    _write_site(box, SITE)
    result = box.run("--unattended")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "differs from the installed one" in result.stdout
    assert "install.sh" in result.stdout
    assert box.record()["outcome"] == "updated"
    assert not (box.root / "installed.nft").exists(), "update.sh never applies the firewall"


def test_update_firewall_equal_to_the_installed_rendering_is_not_noted(box: Box) -> None:
    _publish_firewall_files(box)
    _write_site(box, SITE)
    assert box.run("--unattended").returncode == 0
    rendered = box.show_firewall()
    assert rendered.returncode == 0, rendered.stderr
    (box.root / "installed.nft").write_text(rendered.stdout, encoding="utf-8")
    box.publish({"README.md": "another change\n"})
    result = box.run("--unattended")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "firewall" not in result.stdout
    assert box.record()["outcome"] == "updated"


def test_update_firewall_for_a_changed_site_file_is_noted_against_the_installed_one(
    box: Box,
) -> None:
    _publish_firewall_files(box)
    _write_site(box, SITE)
    assert box.run("--unattended").returncode == 0
    (box.root / "installed.nft").write_text(box.show_firewall().stdout, encoding="utf-8")
    _write_site(box, SITE.replace("EMBEDDING_PORT=11434", "EMBEDDING_PORT=9090"))
    box.publish({"README.md": "another change\n"})
    assert "differs from the installed one" in box.run("--unattended").stdout


def test_update_firewall_without_a_site_file_is_noted_and_the_update_still_succeeds(
    box: Box,
) -> None:
    _publish_firewall_files(box)
    result = box.run("--unattended")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cannot be rendered" in result.stdout
    assert "site.env" in result.stdout + result.stderr
    assert box.record()["outcome"] == "updated"


def test_update_firewall_with_an_invalid_site_file_names_the_problem_and_still_succeeds(
    box: Box,
) -> None:
    _publish_firewall_files(box)
    _write_site(box, SITE.replace("100.64.1.77", "not-an-address"))
    result = box.run("--unattended")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cannot be rendered" in result.stdout
    assert "EMBEDDING_HOST must be an IPv4 address" in result.stderr
    assert box.record()["outcome"] == "updated"


def test_update_firewall_check_with_no_installer_in_the_checkout_does_not_fail_the_update(
    box: Box,
) -> None:
    box.publish({"README.md": "new\n"})
    result = box.run("--unattended")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cannot be rendered" in result.stdout
    assert box.record()["outcome"] == "updated"


# -- usage ------------------------------------------------------------------------------------


def test_update_unknown_option_is_refused_before_anything_is_touched(box: Box) -> None:
    result = box.run("--frobnicate")
    assert result.returncode == 1
    assert "unknown option" in result.stderr
    assert not (box.state / "update.json").exists()


def test_update_help_lists_both_options(box: Box) -> None:
    result = box.run("--help")
    assert result.returncode == 0
    assert "--unattended" in result.stdout
    assert "--accept-format-change" in result.stdout
