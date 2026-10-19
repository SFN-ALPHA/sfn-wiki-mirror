"""Qualifies how `deploy/install.sh` fetches the repository and whether it turns on self-update.

Two decisions for a box that a stranger installs from a public clone. First, the repository is
fetched anonymously over HTTPS by default, and only an SSH URL (a private fork on GitHub) brings the
deploy key with it: an `https://` URL must skip the key check, the host-key pinning, the ssh probe
and the ssh command on the checkout; a `github.com` SSH URL must keep every one of them; SSH to any
other host, any other scheme, and any URL that carries credentials (a user, token or password, a
query string or a fragment) must stop the run without printing the secret. A box that already has a
checkout is treated by that checkout's own origin, the URL update.sh fetches from, and the checkout
is never changed beyond restoring the deploy key's ssh command. The https clone and the update
fetch must never wait at a credentials prompt. Second, the nightly update timer is opt-in: its unit
is always installed, it is enabled only by `--auto-update` or because it already is `enabled`, the
installer never disables it, and the summary says which way it went and how to change it.

The installer is exercised by sourcing it and calling one function (it only runs `main` when
executed, which needs root), with `git`, `ssh`, `ssh-keyscan`, `systemctl` and `install` stood in
for by stubs in front of PATH that log what they were asked to do. Whether git, ssh and systemd do
their part is qualified on the box.

See docs/OPERATIONS.md and docs/DESIGN.md section 12.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "deploy" / "install.sh"
BASH = shutil.which("bash")

#: `contract`, not `unit`: each test spawns a real shell against real files, which is the boundary
#: being qualified (the same tier as the other deploy-script tests).
pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(BASH is None, reason="install.sh needs bash"),
]

HTTPS_URL = "https://git.example.org/team/wiki-mirror.git"
SCP_URL = "git@github.com:team/wiki-mirror.git"
SSH_URL = "ssh://git@github.com/team/wiki-mirror.git"
#: SSH to anywhere but github.com is refused: the key probe, the pinned host key and the firewall's
#: git-over-SSH set are all GitHub's. The last two are look-alikes of the real host.
OTHER_HOST_SSH_URLS = [
    "git@git.example.org:team/wiki-mirror.git",
    "ssh://git@git.example.org/team/wiki-mirror.git",
    "ssh://git@ssh.github.com:443/team/wiki-mirror.git",
    "git@github.com.example.org:team/wiki-mirror.git",
    "ssh://git@github.com@git.example.org/team/wiki-mirror.git",
]
GITHUB_ONLY = "SSH clones are supported from github.com only; use an https URL"
NEITHER = "neither an https:// URL nor an SSH one"
#: The part of a URL that must never be printed, and URLs that carry it: a user and a token in an
#: https URL, a token as the user name, a password in an ssh:// URL, and a secret holding a '/'
#: (which a URL parser would not read as part of the user information at all).
SECRET = "s3cr3t"
CREDENTIAL_URLS = [
    f"https://deploy:{SECRET}-token@git.example.org/team/wiki-mirror.git",
    f"https://{SECRET}-token@git.example.org/team/wiki-mirror.git",
    f"https://deploy:{SECRET}/token@git.example.org/team/wiki-mirror.git",
    f"ssh://deploy:{SECRET}-password@git.example.org/team/wiki-mirror.git",
]
#: A token in a query string or a fragment is as much a credential as one in the authority.
QUERY_URLS = [
    f"https://git.example.org/team/wiki-mirror.git?access_token={SECRET}",
    f"https://git.example.org/team/wiki-mirror.git#{SECRET}",
    f"https://git.example.org/team/wiki-mirror.git?ref=main#{SECRET}",
]
REFUSED_URLS = CREDENTIAL_URLS + QUERY_URLS
CREDENTIALS = "carries credentials"
#: What a blocker may say about any of them: scheme, host and path.
SHOWN_PATH = "git.example.org/team/wiki-mirror.git"
UPDATE_TIMER = "wiki-mirror-update.timer"
ALWAYS_ENABLED = "wiki-mirror-qdrant.service wiki-mirror.service wiki-mirror-build.timer"
#: Every state `systemctl is-enabled` can answer that is not `enabled`; several exit 0.
NOT_ENABLED_STATES = ["disabled", "enabled-runtime", "static", "indirect", "alias", "masked",
                      "not-found"]  # fmt: skip

#: What the installer asks of a checkout without changing it (see Box.writes).
_GIT_QUERIES = ("remote get-url origin", "config --get core.sshCommand")
#: Variables the installer reads, and ones that change how git reaches a remote: the machine
#: running the tests may set any of them, and a test must start from the installer's defaults.
_AMBIENT = frozenset(
    {
        "REPO_URL", "APP_DIR", "DATA_DIR", "CONF_DIR", "SNAPSHOT_MOUNT", "SNAPSHOT_DIR",
        "DEPLOY_KEY", "KNOWN_HOSTS", "BRANCH", "NFT_INSTALLED", "NFT_CONF", "RESOLV_CONF",
        "UNIT_DIR", "IP_BIN", "SITE_OWNER_UID", "SITE_OWNER_GID", "PYTHON", "SSH_CONNECTION",
        "GIT_SSH_COMMAND", "GIT_SSH", "GIT_TERMINAL_PROMPT",
    }
)  # fmt: skip

#: Logs each call and, in a second file, the git environment it ran with (`args|ssh|prompt`).
#: The two read-only questions the installer asks of a checkout are answered from STUB_ORIGIN and
#: STUB_SSHCOMMAND (unset: no origin, no ssh command, as in git); `clone` makes the checkout
#: directory (its last argument); every other call only logs.
_GIT = """#!/bin/sh
echo "git $*" >> "$STUB_LOG"
echo "git $*|${GIT_SSH_COMMAND-unset}|${GIT_TERMINAL_PROMPT-unset}" >> "$STUB_ENV"
case "$*" in
    *" remote get-url origin")
        [ -n "${STUB_ORIGIN-}" ] && { echo "$STUB_ORIGIN"; exit 0; }
        echo "error: No such remote 'origin'" >&2; exit 2 ;;
    *" config --get core.sshCommand")
        [ -n "${STUB_SSHCOMMAND-}" ] && { echo "$STUB_SSHCOMMAND"; exit 0; }
        exit 1 ;;
esac
if [ "$1" = clone ]; then for last; do :; done; mkdir -p "$last/.git"; fi
exit 0
"""
_SSH = (
    '#!/bin/sh\necho "ssh $*" >> "$STUB_LOG"\n'
    'echo "Hi test! You have successfully authenticated, but no shell access is provided."\n'
    "exit 1\n"
)
_SSH_KEYSCAN = (
    '#!/bin/sh\necho "ssh-keyscan $*" >> "$STUB_LOG"\n'
    'echo "github.com ssh-ed25519 AAAAstub"\nexit 0\n'
)
#: Answers `is-enabled` for the update timer only, from STUB_UPDATE_TIMER_STATE, the way systemctl
#: does (exit 0 for every enabled-ish state, 1 for disabled and masked, 4 for an unknown unit). A
#: question about any other unit is logged as UNEXPECTED and fails: the `box` fixture then fails
#: the test, so a quiet change in which units the installer asks about cannot go unseen.
_SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >> "$STUB_LOG"
if [ "$1" = is-enabled ]; then
    if [ "$2" != wiki-mirror-update.timer ]; then
        echo "UNEXPECTED systemctl $*" >> "$STUB_LOG"
        echo "systemctl stub: no answer for is-enabled $2" >&2
        exit 99
    fi
    state="${STUB_UPDATE_TIMER_STATE:-disabled}"
    case "$state" in
        enabled|enabled-runtime|static|indirect|alias) echo "$state"; exit 0 ;;
        disabled|masked) echo "$state"; exit 1 ;;
        *) echo "Failed to get unit file state for $2: No such file or directory" >&2; exit 4 ;;
    esac
fi
exit 0
"""
#: Logs and does nothing: the pinning of the host key runs `install -d -m 0700 /root/.ssh`.
_INSTALL_NOOP = '#!/bin/sh\necho "install $*" >> "$STUB_LOG"\nexit 0\n'


@dataclass
class Box:
    """A scratch box for the installer: a checkout path, a unit directory and a stub log."""

    root: Path

    @property
    def app(self) -> Path:
        """Where the installer would clone to (APP_DIR); absent until a clone stub makes it."""
        return self.root / "app"

    @property
    def units(self) -> Path:
        """Where the installer puts the systemd units (UNIT_DIR)."""
        return self.root / "units"

    @property
    def key(self) -> Path:
        """Where the deploy key would be (DEPLOY_KEY); absent unless a test creates it."""
        return self.root / "ssh" / "deploy_key"

    @property
    def known_hosts(self) -> Path:
        """The pinned host keys (KNOWN_HOSTS)."""
        return self.root / "ssh" / "known_hosts"

    @property
    def log(self) -> Path:
        """What the stubs were asked to do, one line each."""
        return self.root / "stub.log"

    @property
    def env_log(self) -> Path:
        """The git environment of each git call: `args|GIT_SSH_COMMAND|GIT_TERMINAL_PROMPT`."""
        return self.root / "stub-env.log"

    @property
    def ssh_command(self) -> str:
        """The ssh command the installer sets on an SSH checkout."""
        return (
            f"ssh -i {self.key} -o IdentitiesOnly=yes -o UserKnownHostsFile={self.known_hosts}"
            " -o StrictHostKeyChecking=yes"
        )

    def calls(self) -> list[str]:
        """The stubs' log lines, in order (empty if nothing was called)."""
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def writes(self) -> list[str]:
        """The stub calls that could change something: all but git's two read-only questions."""
        return [call for call in self.calls() if not call.endswith(_GIT_QUERIES)]

    def git_env(self, prefix: str) -> tuple[str, str]:
        """GIT_SSH_COMMAND and GIT_TERMINAL_PROMPT of the one git call that starts with `prefix`.

        Either is the word `unset` when the call had no such variable.
        """
        lines = self.env_log.read_text(encoding="utf-8").splitlines()
        (line,) = [line for line in lines if line.startswith(prefix)]
        _, ssh_command, prompt = line.rsplit("|", 2)
        return ssh_command, prompt

    def checkout(self) -> None:
        """Make APP_DIR look like a checkout: the installer only looks for `.git`."""
        (self.app / ".git").mkdir(parents=True)

    def stub(self, name: str, body: str) -> None:
        """Put an executable stand-in for `name` in front of PATH."""
        stubs = self.root / "stubs"
        stubs.mkdir(exist_ok=True)
        (stubs / name).write_text(body, encoding="utf-8")
        (stubs / name).chmod(0o755)

    def call(
        self, code: str, *args: str, repo: str | None = None, **env: str
    ) -> subprocess.CompletedProcess[str]:
        """Source the installer (with `args`, as its own arguments) and run `code` in that shell.

        `repo` sets REPO_URL; left out, REPO_URL is removed from the environment so the
        installer's own default applies. `env` adds to the environment, which is otherwise the
        test runner's less every variable in `_AMBIENT`.
        """
        assert BASH is not None
        inherited = {k: v for k, v in os.environ.items() if k not in _AMBIENT}
        environment = {
            **inherited,
            "PATH": f"{self.root / 'stubs'}:{os.environ['PATH']}",
            "APP_DIR": str(self.app),
            "CONF_DIR": str(self.root / "conf"),
            "UNIT_DIR": str(self.units),
            "DEPLOY_KEY": str(self.key),
            "KNOWN_HOSTS": str(self.known_hosts),
            "STUB_LOG": str(self.log),
            "STUB_ENV": str(self.env_log),
            "INSTALLER": str(INSTALL_SH),
            **({"REPO_URL": repo} if repo is not None else {}),
            **env,
        }
        return subprocess.run(  # noqa: S603 - fixed argv, a repo script sourced by path
            [BASH, "-c", f'source "$INSTALLER" {" ".join(args)}; {code}'],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )


@pytest.fixture
def box(tmp_path: Path) -> Iterator[Box]:
    """A scratch box with an empty SSH directory and the stubs every test may need.

    After the test, fails it if the systemctl stub was asked a question it has no answer for.
    """
    scratch = Box(tmp_path)
    (tmp_path / "ssh").mkdir()
    scratch.stub("git", _GIT)
    scratch.stub("ssh", _SSH)
    scratch.stub("ssh-keyscan", _SSH_KEYSCAN)
    scratch.stub("systemctl", _SYSTEMCTL)
    yield scratch
    unexpected = [call for call in scratch.calls() if call.startswith("UNEXPECTED")]
    assert not unexpected, unexpected


def _enable_call(calls: list[str]) -> str:
    """The one `systemctl enable` call among the stub's log lines."""
    (enable,) = [call for call in calls if call.startswith("systemctl enable ")]
    return enable


# -- the default is anonymous HTTPS -----------------------------------------------------------


def test_installer_default_repository_is_an_anonymous_https_url(box: Box) -> None:
    result = box.call('printf "%s\\n" "$REPO_URL" "$(repo_transport)"')
    assert result.returncode == 0, result.stderr
    url, transport = result.stdout.splitlines()
    assert url.startswith("https://")
    assert "@" not in url, "an anonymous URL carries no user or token"
    assert transport == "https"


def test_installer_repo_option_sets_the_url_the_transport_follows(box: Box) -> None:
    result = box.call('printf "%s\\n" "$REPO_URL" "$(repo_transport)"', "--repo", SCP_URL)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [SCP_URL, "ssh"]


@pytest.mark.parametrize(
    ("url", "transport"),
    [
        (HTTPS_URL, "https"),
        ("https://github.com/team/wiki-mirror", "https"),
        (SCP_URL, "ssh"),
        (SSH_URL, "ssh"),
        ("ssh://git@github.com:22/team/wiki-mirror.git", "ssh"),
        ("ssh://github.com/team/wiki-mirror.git", "ssh"),
        ("ssh://deploy@github.com/team/wiki-mirror.git", "ssh"),
        ("ssh://git@GitHub.com/team/wiki-mirror.git", "ssh"),
        *[(url, "credentials") for url in REFUSED_URLS],
        ("ssh://:token@github.com/team/wiki-mirror.git", "credentials"),
        ("ssh://git:token@github.com/team/wiki-mirror.git", "credentials"),
        *[(url, "unsupported") for url in OTHER_HOST_SSH_URLS],
        ("http://git.example.org/team/wiki-mirror.git", "unsupported"),
        ("git://git.example.org/team/wiki-mirror.git", "unsupported"),
        ("file:///srv/git/wiki-mirror.git", "unsupported"),
        ("/srv/git/wiki-mirror.git", "unsupported"),
        ("ftp://git.example.org/wiki-mirror.git", "unsupported"),
        ("github.com/team/wiki-mirror", "unsupported"),
    ],
)
def test_repo_transport_is_decided_by_the_form_of_the_url(
    box: Box, url: str, transport: str
) -> None:
    result = box.call("repo_transport", repo=url)
    assert result.returncode == 0, result.stderr
    assert result.stdout == transport


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        (HTTPS_URL, HTTPS_URL),
        (SCP_URL, SCP_URL),
        (CREDENTIAL_URLS[0], "https://" + SHOWN_PATH),
        (CREDENTIAL_URLS[2], "https://" + SHOWN_PATH),
        (CREDENTIAL_URLS[3], "ssh://" + SHOWN_PATH),
        *[(url, "https://" + SHOWN_PATH) for url in QUERY_URLS],
        ("git@git.example.org:team/wiki-mirror.git", "...@git.example.org:team/wiki-mirror.git"),
    ],
)
def test_url_shown_keeps_the_host_and_path_and_drops_anything_that_could_hold_a_secret(
    box: Box, url: str, shown: str
) -> None:
    result = box.call(f'url_shown "{url}"')
    assert result.returncode == 0, result.stderr
    assert result.stdout == shown


# -- a box with a checkout is treated by that checkout's origin --------------------------------


def test_repo_transport_existing_checkout_origin_wins_over_repo_url(box: Box) -> None:
    box.checkout()
    result = box.call('printf "%s\\n" "$(repo_url)" "$(repo_transport)"', repo=HTTPS_URL,
                      STUB_ORIGIN=SCP_URL)  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [SCP_URL, "ssh"]


def test_repo_transport_existing_checkout_without_an_origin_falls_back_to_repo_url(
    box: Box,
) -> None:
    box.checkout()
    result = box.call("repo_transport", repo=HTTPS_URL)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "https"


def test_repo_transport_without_a_checkout_asks_git_nothing(box: Box) -> None:
    result = box.call("repo_transport", repo=SCP_URL, STUB_ORIGIN=HTTPS_URL)
    assert result.stdout == "ssh", "no .git yet: REPO_URL decides, whatever git would say"
    assert box.calls() == []


def test_check_repo_access_ssh_checkout_without_a_key_stops_even_with_the_https_default(
    box: Box,
) -> None:
    # An SSH box re-run with no --repo: update.sh will fetch through the deploy key at 02:20, so
    # the installer has to check it now.
    box.checkout()
    result = box.call("check_repo_access", STUB_ORIGIN=SCP_URL)
    assert result.returncode == 1
    assert f"no deploy key at {box.key}" in result.stderr


def test_verify_deploy_key_ssh_checkout_probes_the_key_even_with_the_https_default(
    box: Box,
) -> None:
    box.checkout()
    box.key.write_text("not a real key\n", encoding="utf-8")
    box.stub("install", _INSTALL_NOOP)
    result = box.call("verify_deploy_key", STUB_ORIGIN=SCP_URL)
    assert result.returncode == 0, result.stderr
    assert "deploy key is registered on GitHub" in result.stdout
    assert any(call.startswith("ssh ") and "git@github.com" in call for call in box.calls())


def test_check_repo_access_https_checkout_needs_no_key_whatever_repo_url_says(box: Box) -> None:
    box.checkout()
    result = box.call("check_repo_access", repo=SCP_URL, STUB_ORIGIN=HTTPS_URL)
    assert result.returncode == 0, result.stderr
    assert "anonymously over HTTPS" in result.stdout
    assert "origin of the checkout" in result.stdout


def test_check_repo_access_checkout_origin_with_credentials_stops_and_tells_how_to_fix_it(
    box: Box,
) -> None:
    box.checkout()
    result = box.call("check_repo_access", STUB_ORIGIN=CREDENTIAL_URLS[0])
    assert result.returncode == 1
    assert CREDENTIALS in result.stderr
    assert f"git -C {box.app} remote set-url origin" in result.stderr
    assert SECRET not in result.stdout + result.stderr
    assert box.writes() == [], "the installer reports; it does not rewrite the origin"


def test_check_repo_access_checkout_origin_on_another_ssh_host_stops_and_tells_how_to_fix_it(
    box: Box,
) -> None:
    box.checkout()
    result = box.call("check_repo_access", STUB_ORIGIN=OTHER_HOST_SSH_URLS[0])
    assert result.returncode == 1
    assert GITHUB_ONLY in result.stderr
    assert f"git -C {box.app} remote set-url origin" in result.stderr
    assert box.writes() == []


def test_check_repo_access_checkout_origin_wins_even_over_a_repo_url_that_would_be_refused(
    box: Box,
) -> None:
    # REPO_URL is only the URL to clone from; with a checkout there is nothing to clone.
    box.checkout()
    result = box.call("check_repo_access", repo=CREDENTIAL_URLS[0], STUB_ORIGIN=HTTPS_URL)
    assert result.returncode == 0, result.stderr
    assert SECRET not in result.stdout + result.stderr


# -- preflight: no key for https, the key for ssh, a stop for anything else -------------------


def test_check_repo_access_https_url_needs_no_deploy_key_and_says_so(box: Box) -> None:
    assert not box.key.exists()
    result = box.call("check_repo_access", repo=HTTPS_URL)
    assert result.returncode == 0, result.stderr
    assert "anonymously over HTTPS" in result.stdout
    assert "deploy key" not in (result.stdout + result.stderr).lower()


def test_check_repo_access_default_url_needs_no_deploy_key(box: Box) -> None:
    result = box.call("check_repo_access")
    assert result.returncode == 0, result.stderr
    assert "anonymously over HTTPS" in result.stdout


@pytest.mark.parametrize("url", [SCP_URL, SSH_URL])
def test_check_repo_access_ssh_url_without_a_key_stops_and_prints_the_commands(
    box: Box, url: str
) -> None:
    result = box.call("check_repo_access", repo=url)
    assert result.returncode == 1
    assert f"no deploy key at {box.key}" in result.stderr
    assert "ssh-keygen" in result.stderr
    assert "Allow write access" in result.stderr


def test_check_repo_access_ssh_url_without_a_key_is_a_dry_run_blocker(box: Box) -> None:
    result = box.call(
        'DRY_RUN=true; check_repo_access; printf "%s\\n" "${BLOCKERS[@]}"', repo=SCP_URL
    )
    assert result.returncode == 0, result.stderr
    assert "no deploy key at" in result.stdout


def test_check_repo_access_ssh_url_with_a_key_passes(box: Box) -> None:
    box.key.write_text("not a real key\n", encoding="utf-8")
    result = box.call("check_repo_access", repo=SCP_URL)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("url", OTHER_HOST_SSH_URLS)
def test_check_repo_access_ssh_url_for_another_host_stops_and_says_github_only(
    box: Box, url: str
) -> None:
    box.key.write_text("not a real key\n", encoding="utf-8")
    result = box.call("check_repo_access", repo=url)
    assert result.returncode == 1
    assert GITHUB_ONLY in result.stderr
    assert "deploy key" not in result.stderr, "a key would not help: the host is the problem"
    assert box.calls() == []


def test_check_repo_access_ssh_url_for_another_host_is_a_dry_run_blocker(box: Box) -> None:
    result = box.call(
        'DRY_RUN=true; check_repo_access; printf "%s\\n" "${BLOCKERS[@]}"',
        repo=OTHER_HOST_SSH_URLS[0],
    )
    assert result.returncode == 0, result.stderr
    assert GITHUB_ONLY in result.stdout


@pytest.mark.parametrize(
    "url",
    ["http://git.example.org/x.git", "git://git.example.org/x.git", "file:///srv/x.git", "x.git"],
)
def test_check_repo_access_any_other_scheme_stops_and_names_the_accepted_forms(
    box: Box, url: str
) -> None:
    result = box.call("check_repo_access", repo=url)
    assert result.returncode == 1
    assert NEITHER in result.stderr
    assert url in result.stderr


def test_check_repo_access_unknown_scheme_is_a_dry_run_blocker(box: Box) -> None:
    result = box.call(
        'DRY_RUN=true; check_repo_access; printf "%s\\n" "${BLOCKERS[@]}"',
        repo="http://git.example.org/x.git",
    )
    assert result.returncode == 0, result.stderr
    assert NEITHER in result.stdout


# -- credentials in the URL are refused, and never printed ------------------------------------


@pytest.mark.parametrize("url", REFUSED_URLS)
def test_check_repo_access_url_with_credentials_stops_and_never_prints_them(
    box: Box, url: str
) -> None:
    result = box.call("check_repo_access", repo=url)
    assert result.returncode == 1
    assert CREDENTIALS in result.stderr
    assert SHOWN_PATH in result.stderr, "it still says which repository it refused"
    output = result.stdout + result.stderr
    assert SECRET not in output
    assert "access_token" not in output, "nor the name of the query parameter"
    assert "deploy:" not in output, "the user name is user information too"
    assert "deploy@" not in output
    assert box.calls() == []


@pytest.mark.parametrize("url", REFUSED_URLS)
def test_check_repo_access_url_with_credentials_is_a_dry_run_blocker_that_never_prints_them(
    box: Box, url: str
) -> None:
    result = box.call('DRY_RUN=true; check_repo_access; printf "%s\\n" "${BLOCKERS[@]}"', repo=url)
    assert result.returncode == 0, result.stderr
    assert "would STOP here" in result.stdout
    assert CREDENTIALS in result.stdout
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("url", REFUSED_URLS)
def test_a_dry_run_through_the_repository_steps_never_prints_credentials_or_clones(
    box: Box, url: str
) -> None:
    # Every step that touches the URL, in the installer's order: none may print the secret, and
    # none may plan a clone or a key check for a URL that already stopped the run.
    result = box.call(
        "DRY_RUN=true; check_repo_access; verify_deploy_key; clone_checkout; summary", repo=url
    )
    assert result.returncode == 0, result.stderr
    assert SECRET not in result.stdout + result.stderr
    assert "git clone" not in result.stdout
    assert "would not clone" in result.stdout
    assert "would verify" not in result.stdout
    assert box.calls() == []


@pytest.mark.parametrize("url", REFUSED_URLS)
def test_clone_checkout_url_with_credentials_clones_nothing_and_never_prints_them(
    box: Box, url: str
) -> None:
    result = box.call("clone_checkout", repo=url)
    assert result.returncode == 1
    assert CREDENTIALS in result.stderr
    assert SECRET not in result.stdout + result.stderr
    assert box.calls() == [], "git was never run with the URL"


def test_credentials_in_an_unsupported_url_are_not_printed_either(box: Box) -> None:
    url = f"http://deploy:{SECRET}@git.example.org/team/wiki-mirror.git"
    result = box.call("check_repo_access", repo=url)
    assert result.returncode == 1
    assert NEITHER in result.stderr
    assert "http://" + SHOWN_PATH in result.stderr
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "url", ["ssh://git@github.com:22/team/wiki-mirror.git", SSH_URL, SCP_URL]
)
def test_check_repo_access_ssh_url_naming_only_a_user_is_not_credentials(
    box: Box, url: str
) -> None:
    # The deploy key is the credential for SSH; `git@` and a port are ordinary.
    box.key.write_text("not a real key\n", encoding="utf-8")
    result = box.call("check_repo_access", repo=url)
    assert result.returncode == 0, result.stderr


# -- the deploy-key stage: silent for https, as before for ssh --------------------------------


def test_verify_deploy_key_https_url_does_nothing_at_all(box: Box) -> None:
    result = box.call("verify_deploy_key", repo=HTTPS_URL)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert box.calls() == [], "no ssh-keyscan, no ssh probe, no host key pinned"
    assert not box.known_hosts.exists()


def test_verify_deploy_key_https_url_prints_nothing_in_a_dry_run_either(box: Box) -> None:
    result = box.call("DRY_RUN=true; verify_deploy_key", repo=HTTPS_URL)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_verify_deploy_key_ssh_url_pins_the_host_key_and_probes_with_the_key(box: Box) -> None:
    box.key.write_text("not a real key\n", encoding="utf-8")
    box.stub("install", _INSTALL_NOOP)
    result = box.call("verify_deploy_key", repo=SCP_URL)
    assert result.returncode == 0, result.stderr
    assert "deploy key is registered on GitHub" in result.stdout
    assert box.known_hosts.read_text(encoding="utf-8").startswith("github.com ")
    calls = box.calls()
    assert any(call.startswith("ssh-keyscan ") for call in calls)
    (probe,) = [call for call in calls if call.startswith("ssh ")]
    assert f"-i {box.key}" in probe
    assert "git@github.com" in probe


def test_verify_deploy_key_ssh_url_without_a_key_dry_run_says_it_would_verify(box: Box) -> None:
    result = box.call("DRY_RUN=true; verify_deploy_key", repo=SCP_URL)
    assert result.returncode == 0, result.stderr
    assert "would verify" in result.stdout
    assert box.calls() == []


def test_verify_deploy_key_ssh_url_for_another_host_does_nothing(box: Box) -> None:
    result = box.call("verify_deploy_key", repo=OTHER_HOST_SSH_URLS[0])
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert box.calls() == [], "no host key is pinned for a host the installer does not support"


# -- the clone --------------------------------------------------------------------------------


def test_clone_checkout_https_url_dry_run_shows_a_plain_clone_with_no_ssh_command(
    box: Box,
) -> None:
    result = box.call("DRY_RUN=true; clone_checkout", repo=HTTPS_URL)
    assert result.returncode == 0, result.stderr
    (planned,) = [line for line in result.stdout.splitlines() if "would run:" in line]
    assert planned.strip().endswith(
        f"env GIT_TERMINAL_PROMPT=0 git clone --quiet --branch main {HTTPS_URL} {box.app}"
    )
    # The scratch path is named after the test, so it is taken out before looking for words.
    out = result.stdout.replace(str(box.root), "").lower()
    assert "ssh" not in out
    assert "deploy" not in out


def test_clone_checkout_https_url_clones_plainly_and_sets_no_ssh_command(box: Box) -> None:
    result = box.call("clone_checkout", repo=HTTPS_URL)
    assert result.returncode == 0, result.stderr
    assert box.calls() == [f"git clone --quiet --branch main {HTTPS_URL} {box.app}"]
    assert box.git_env("git clone")[0] == "unset"
    assert "cloned" in result.stdout


def test_clone_checkout_https_url_never_waits_at_a_credentials_prompt(box: Box) -> None:
    # The URL is anonymous, so a 401 (a repository that is not public, a mistyped name) has to
    # fail at once and say so, not stop at git's "Username for" prompt on a terminal.
    assert box.call("clone_checkout", repo=HTTPS_URL).returncode == 0
    assert box.git_env("git clone")[1] == "0"


@pytest.mark.parametrize("url", [SCP_URL, SSH_URL])
def test_clone_checkout_ssh_url_clones_with_the_deploy_key_and_pins_it_on_the_checkout(
    box: Box, url: str
) -> None:
    result = box.call("clone_checkout", repo=url)
    assert result.returncode == 0, result.stderr
    assert box.calls() == [
        f"git clone --quiet --branch main {url} {box.app}",
        f"git -C {box.app} config --get core.sshCommand",
        f"git -C {box.app} config core.sshCommand {box.ssh_command}",
    ]
    assert box.git_env("git clone")[0] == box.ssh_command


def test_clone_checkout_ssh_url_dry_run_shows_the_ssh_command(box: Box) -> None:
    result = box.call("DRY_RUN=true; clone_checkout", repo=SCP_URL)
    assert result.returncode == 0, result.stderr
    assert f"GIT_SSH_COMMAND=ssh -i {box.key}" in result.stdout


def test_clone_checkout_existing_https_checkout_is_left_exactly_as_it_is(box: Box) -> None:
    # A box installed over SSH keeps its origin when the installer is re-run with the (now https)
    # default; update.sh reads the checkout, never REPO_URL.
    box.checkout()
    result = box.call("clone_checkout", STUB_ORIGIN=HTTPS_URL)
    assert result.returncode == 0, result.stderr
    assert "already done" in result.stdout
    assert box.writes() == [], "no clone, no `git config`: git was only asked questions"


def test_clone_checkout_existing_ssh_checkout_with_the_deploy_keys_command_is_not_touched(
    box: Box,
) -> None:
    box.checkout()
    result = box.call("clone_checkout", STUB_ORIGIN=SCP_URL, STUB_SSHCOMMAND=box.ssh_command)
    assert result.returncode == 0, result.stderr
    assert "already done" in result.stdout
    assert f"git -C {box.app} config --get core.sshCommand" in box.calls()
    assert box.writes() == []


@pytest.mark.parametrize("held", ["", "ssh -i /old/key"])
def test_clone_checkout_existing_ssh_checkout_with_another_ssh_command_gets_the_deploy_keys(
    box: Box, held: str
) -> None:
    # The one thing a re-run restores on a checkout: the command its fetches read the key with.
    box.checkout()
    result = box.call("clone_checkout", STUB_ORIGIN=SCP_URL, STUB_SSHCOMMAND=held)
    assert result.returncode == 0, result.stderr
    assert box.writes() == [f"git -C {box.app} config core.sshCommand {box.ssh_command}"]


def test_clone_checkout_unknown_scheme_clones_nothing(box: Box) -> None:
    result = box.call("clone_checkout", repo="http://git.example.org/x.git")
    assert result.returncode == 1
    assert NEITHER in result.stderr
    assert box.calls() == []
    dry = box.call("DRY_RUN=true; clone_checkout", repo="http://git.example.org/x.git")
    assert dry.returncode == 0, dry.stderr
    assert "would not clone" in dry.stdout


def test_clone_checkout_ssh_url_for_another_host_clones_nothing(box: Box) -> None:
    result = box.call("clone_checkout", repo=OTHER_HOST_SSH_URLS[0])
    assert result.returncode == 1
    assert GITHUB_ONLY in result.stderr
    assert box.calls() == []


# -- the update timer is opt-in ---------------------------------------------------------------


def _install_units(
    box: Box, *, auto_update: bool, state: str = "disabled", dry: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run install_units against the real unit files, writing to the scratch unit directory.

    `state` is what `systemctl is-enabled wiki-mirror-update.timer` answers.
    """
    box.units.mkdir(exist_ok=True)
    settings = f"AUTO_UPDATE={str(auto_update).lower()}; DRY_RUN={str(dry).lower()}; "
    return box.call(
        f"{settings}install_units",
        APP_DIR=str(ROOT),
        STUB_UPDATE_TIMER_STATE=state,
    )


def test_install_units_by_default_enables_everything_but_the_update_timer(box: Box) -> None:
    result = _install_units(box, auto_update=False)
    assert result.returncode == 0, result.stderr
    assert _enable_call(box.calls()) == f"systemctl enable {ALWAYS_ENABLED}"
    started = [call for call in box.calls() if call.startswith("systemctl start ")]
    assert started == [f"systemctl start {unit}" for unit in ALWAYS_ENABLED.split()]
    assert (
        "qdrant, the API and the monthly build timer enabled (no build started)" in result.stdout
    )


def test_install_units_asks_systemctl_only_about_the_update_timer(box: Box) -> None:
    assert _install_units(box, auto_update=False).returncode == 0
    asked = [call for call in box.calls() if call.startswith("systemctl is-enabled")]
    assert asked == [f"systemctl is-enabled {UPDATE_TIMER}"]


def test_install_units_by_default_still_installs_the_update_timer_unit(box: Box) -> None:
    assert _install_units(box, auto_update=False).returncode == 0
    assert (box.units / UPDATE_TIMER).is_file()
    assert (box.units / "wiki-mirror-update.service").is_file()


def test_install_units_by_default_says_the_timer_is_off_and_how_to_turn_it_on(box: Box) -> None:
    result = _install_units(box, auto_update=False)
    assert "NOT enabled" in result.stdout
    assert f"systemctl enable --now {UPDATE_TIMER}" in result.stdout


def test_install_units_with_auto_update_enables_and_starts_the_update_timer(box: Box) -> None:
    result = _install_units(box, auto_update=True)
    assert result.returncode == 0, result.stderr
    assert _enable_call(box.calls()) == f"systemctl enable {ALWAYS_ENABLED} {UPDATE_TIMER}"
    assert f"systemctl start {UPDATE_TIMER}" in box.calls()
    assert (
        "qdrant, the API and the monthly build timer and the nightly update timer enabled"
        in result.stdout
    )
    assert "NOT enabled" not in result.stdout


def test_install_units_with_the_update_timer_already_enabled_keeps_it_enabled(box: Box) -> None:
    result = _install_units(box, auto_update=False, state="enabled")
    assert result.returncode == 0, result.stderr
    assert _enable_call(box.calls()) == f"systemctl enable {ALWAYS_ENABLED} {UPDATE_TIMER}"
    assert "the nightly update timer enabled" in result.stdout
    assert "NOT enabled" not in result.stdout


@pytest.mark.parametrize("state", NOT_ENABLED_STATES)
def test_install_units_update_timer_in_any_state_but_enabled_is_not_enabled_by_a_rerun(
    box: Box, state: str
) -> None:
    # `systemctl is-enabled` exits 0 for enabled-runtime, static, indirect and alias too, so the
    # exit status cannot tell an operator's `enable` from a timer that is merely linked or
    # transient; the state word can.
    result = _install_units(box, auto_update=False, state=state)
    assert result.returncode == 0, result.stderr
    assert _enable_call(box.calls()) == f"systemctl enable {ALWAYS_ENABLED}"
    assert "NOT enabled" in result.stdout


@pytest.mark.parametrize(
    ("auto_update", "state"), [(False, "disabled"), (False, "enabled"), (True, "disabled")]
)
def test_install_units_never_disables_or_stops_anything(
    box: Box, auto_update: bool, state: str
) -> None:
    assert _install_units(box, auto_update=auto_update, state=state).returncode == 0
    assert not [call for call in box.calls() if " disable" in call or " stop" in call]


def test_install_units_dry_run_by_default_plans_no_update_timer(box: Box) -> None:
    result = _install_units(box, auto_update=False, dry=True)
    assert result.returncode == 0, result.stderr
    assert f"would run: systemctl enable {ALWAYS_ENABLED}\n" in result.stdout
    planned = [line for line in result.stdout.splitlines() if "would run: systemctl" in line]
    assert not [line for line in planned if UPDATE_TIMER in line]
    assert "NOT enabled" in result.stdout
    assert not (box.units / UPDATE_TIMER).exists(), "a dry run installs nothing"


def test_install_units_dry_run_with_auto_update_plans_the_update_timer(box: Box) -> None:
    result = _install_units(box, auto_update=True, dry=True)
    assert result.returncode == 0, result.stderr
    assert f"would run: systemctl enable {ALWAYS_ENABLED} {UPDATE_TIMER}" in result.stdout
    assert f"would run: systemctl start {UPDATE_TIMER}" in result.stdout
    assert not (box.units / UPDATE_TIMER).exists(), "a dry run installs nothing"


def test_installer_auto_update_flag_is_off_unless_given(box: Box) -> None:
    off = box.call('echo "$AUTO_UPDATE"')
    on = box.call('echo "$AUTO_UPDATE"', "--auto-update")
    assert (off.stdout.strip(), on.stdout.strip()) == ("false", "true")


# -- the closing summary says which way it went -----------------------------------------------


def test_summary_with_the_update_timer_off_says_so_and_gives_the_enable_command(box: Box) -> None:
    result = box.call("DRY_RUN=true; UPDATES_ENABLED=false; summary")
    assert result.returncode == 0, result.stderr
    assert "Automatic nightly updates are OFF" in result.stdout
    assert f"systemctl enable --now {UPDATE_TIMER}" in result.stdout


def test_summary_with_the_update_timer_on_says_when_it_runs(box: Box) -> None:
    result = box.call("DRY_RUN=true; UPDATES_ENABLED=true; summary")
    assert result.returncode == 0, result.stderr
    assert "updates itself at 02:20 local time" in result.stdout
    assert "OFF" not in result.stdout


# -- usage ------------------------------------------------------------------------------------


def _help() -> str:
    assert BASH is not None
    result = subprocess.run(  # noqa: S603 - fixed argv, a repo script
        [BASH, str(INSTALL_SH), "--help"], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0
    return result.stdout


def test_help_documents_auto_update_and_the_two_kinds_of_repository_url() -> None:
    text = _help()
    assert "--auto-update" in text
    assert "systemctl enable --now wiki-mirror-update.timer" in text
    assert "https://" in text
    assert "ssh://" in text


def test_help_says_ssh_is_github_only_and_credentials_are_refused() -> None:
    text = " ".join(_help().split())
    assert "git@github.com:path or ssh://git@github.com/path" in text
    assert "SSH to any other host is refused" in text
    assert "a user name, token, query string or fragment in the URL is refused" in text


def test_help_does_not_advertise_the_paths_the_units_hard_code() -> None:
    overrides = _help().split("Environment overrides:")[1]
    for name in ("APP_DIR", "DATA_DIR", "CONF_DIR", "SNAPSHOT_MOUNT"):
        assert name not in overrides
    assert "REPO_URL" in overrides
