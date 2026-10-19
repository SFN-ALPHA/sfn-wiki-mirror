"""Qualifies the shape of the mirror's deploy files, without a kernel, systemd or a box.

What these guard is the set of decisions a reviewer would otherwise have to re-read the files to
find again: the services run Python in isolated mode from a directory nobody can write; a build
that fails is retried a bounded number of times and then stops; the firewall refuses what it does
not admit instead of dropping it, admits apt's real user, and names only users the installer
checks for; the dependencies are pinned, complete against pyproject.toml, and installed from
wheels; the nightly update timer is at 02:20 local time (no zone is written, so the container's
own decides) and the installer enables it; the
FORMAT file `update.sh` compares across commits equals the line the code builds, so it can never
drift from the constants it names; the firewall file is a template whose only placeholders sit in
its `define` lines and match the keys of `site.env.example`, and it holds no literal address of
anyone's network. Whether nftables or systemd accepts the files is qualified on the box, by the
installer (`nft -c` before it applies, `systemctl daemon-reload`). What `update.sh` does on each
outcome is qualified by running it, in `test_update_script.py`; how the installer reads and
renders the site file is in `test_install_site.py`.

See docs/DESIGN.md sections 6 and 12 and docs/OPERATIONS.md.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from wiki_mirror import index, store

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
UNITS = DEPLOY / "systemd"
BOTH_UNITS = ["wiki-mirror.service", "wiki-mirror-build.service"]

pytestmark = pytest.mark.unit


def _unit(name: str) -> dict[str, dict[str, list[str]]]:
    """A unit file as {section: {key: [values]}} (systemd allows a key to repeat)."""
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] = {}
    for raw in (UNITS / name).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        key, _, value = line.partition("=")
        current.setdefault(key, []).append(value)
    return sections


def _uncommented(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


# -- the services run Python in isolated mode ------------------------------------------------


@pytest.mark.parametrize("name", BOTH_UNITS)
def test_every_exec_start_runs_the_venv_python_in_isolated_mode(name: str) -> None:
    (command,) = _unit(name)["Service"]["ExecStart"]
    interpreter, *arguments = command.split()
    assert interpreter == "/opt/wiki-mirror/.venv/bin/python"
    # `-I` (or at least `-P`) before `-m`: the working directory must not reach sys.path, and
    # the workers a build spawns inherit the flag (docs/OPERATIONS.md; build unit header).
    before_module = arguments[: arguments.index("-m")]
    assert "-I" in before_module or "-P" in before_module, command


def test_the_api_unit_works_from_a_directory_nobody_can_write() -> None:
    assert _unit("wiki-mirror.service")["Service"]["WorkingDirectory"] == ["/"]


@pytest.mark.parametrize("name", BOTH_UNITS)
def test_a_configuration_error_exit_is_never_restarted(name: str) -> None:
    # Exit 2 is the program saying its configuration is wrong; a restart cannot fix it.
    assert _unit(name)["Service"]["RestartPreventExitStatus"] == ["2"]


# -- a failed build is retried a bounded number of times -------------------------------------


def test_a_failed_build_is_retried_every_hour_for_a_bounded_number_of_starts() -> None:
    unit = _unit("wiki-mirror-build.service")
    assert unit["Service"]["Restart"] == ["on-failure"]
    assert unit["Service"]["RestartSec"] == ["1h"]
    # In [Unit], where current systemd reads them; a start limit that is not set retries for ever.
    assert unit["Unit"]["StartLimitIntervalSec"] == ["1d"]
    burst = int(unit["Unit"]["StartLimitBurst"][0])
    assert 2 <= burst <= 10, "a few retries, not none and not a loop"


def test_the_build_has_no_start_timeout_and_the_api_restarts_on_failure() -> None:
    assert _unit("wiki-mirror-build.service")["Service"]["TimeoutStartSec"] == ["infinity"]
    assert _unit("wiki-mirror.service")["Service"]["Restart"] == ["on-failure"]


# -- the firewall -----------------------------------------------------------------------------

NFT = (DEPLOY / "nftables" / "wiki-mirror.nft").read_text(encoding="utf-8")


def _chain(name: str) -> list[str]:
    """The uncommented lines of one chain of the ruleset."""
    body = re.search(rf"chain {name} \{{(.*?)\n    \}}", NFT, re.S)
    assert body is not None, name
    return _uncommented(body.group(1))


def test_the_output_chain_refuses_instead_of_dropping() -> None:
    output = _chain("output")
    verdicts = [line for line in output if re.search(r"\b(drop|reject)\b", line)]
    assert verdicts, "the output chain has no refusal at all"
    assert not [line for line in verdicts if line.endswith("drop")], verdicts
    assert any("reject with tcp reset" in line for line in verdicts)
    assert any("reject with icmpx type admin-prohibited" in line for line in verdicts)


def test_the_final_output_refusals_are_counted_and_the_log_comes_first() -> None:
    output = _chain("output")
    counted = [i for i, line in enumerate(output) if 'counter name "out_refused"' in line]
    logged = [i for i, line in enumerate(output) if line.startswith("limit rate") and "log" in line]
    assert counted
    assert logged
    assert max(logged) < min(counted), "log before the terminal refusals"
    assert output[-1].startswith('counter name "out_refused" reject'), "the catch-all is last"


def test_the_services_that_must_never_dial_out_are_counted_and_refused() -> None:
    lines = [line for line in _chain("output") if '"service_refused"' in line]
    assert len(lines) == 2
    assert all('"wikimirror", "qdrant"' in line and " reject " in f" {line} " for line in lines)
    assert not any(line.endswith("drop") for line in lines)


def test_the_services_refusals_are_logged_before_they_are_refused() -> None:
    output = _chain("output")
    counted = [i for i, line in enumerate(output) if 'counter name "service_refused"' in line]
    logged = [
        i
        for i, line in enumerate(output)
        if '"wikimirror", "qdrant"' in line and "log prefix" in line and "limit rate" in line
    ]
    assert len(logged) == 1
    assert logged[0] < min(counted)


def test_apt_is_admitted_as_its_own_user_on_80_and_443() -> None:
    rules = [line for line in _chain("output") if 'skuid "_apt"' in line]
    assert len(rules) == 1
    assert "tcp dport { 80, 443 } accept" in rules[0]
    assert "meta nfproto ipv4" in rules[0], "ipv4 only: this box has no routed ipv6"


def test_dhcp_renewal_is_admitted_for_root_to_the_lan_only() -> None:
    rules = [line for line in _chain("output") if "udp dport 67" in line]
    assert len(rules) == 1
    rule = rules[0]
    assert 'skuid "root"' in rule
    assert "udp sport 68" in rule
    assert rule.endswith("accept")
    assert "ip daddr { $LAN_V4, 255.255.255.255 }" in rule


#: A placeholder as the installer writes them: upper case between two @ signs. `@github_git` (an
#: nft set reference) is lower case with no closing @, so it is not one.
PLACEHOLDER = re.compile(r"@[A-Z][A-Z0-9_]*@")

#: Any address in a private range, which is where a site's own network lives.
PRIVATE_ADDRESS = re.compile(
    r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"
)


def test_the_firewall_template_holds_no_literal_lan_address() -> None:
    assert not PRIVATE_ADDRESS.findall(NFT)


def test_the_firewall_template_substitutes_only_in_three_define_lines() -> None:
    lines = [line for line in NFT.splitlines() if PLACEHOLDER.search(line)]
    assert [line.split("=")[0].strip() for line in lines] == [
        "define LAN_V4",
        "define EMBEDDING_V4",
        "define EMBEDDING_PORT",
    ]
    assert not [line for line in lines if line.lstrip().startswith("#")], "a comment"
    # Each placeholder once: the rules refer to the defines, so the installer's work stays in
    # one place a reviewer can read.
    assert sorted(PLACEHOLDER.findall(NFT)) == [
        "@EMBEDDING_HOST@",
        "@EMBEDDING_PORT@",
        "@LAN_CIDR@",
    ]


def test_the_site_example_has_exactly_the_keys_the_template_substitutes() -> None:
    keys = {
        line.partition("=")[0]
        for line in _uncommented((DEPLOY / "site.env.example").read_text(encoding="utf-8"))
    }
    assert keys == {placeholder.strip("@") for placeholder in PLACEHOLDER.findall(NFT)}


def test_the_embedding_server_rule_uses_the_site_defines_for_address_and_port() -> None:
    rules = [line for line in _chain("output") if '"wikimirror-sync" ip daddr' in line]
    assert rules == [
        'meta skuid "wikimirror-sync" ip daddr $EMBEDDING_V4 tcp dport $EMBEDDING_PORT accept'
    ]


def test_the_firewall_is_checked_as_a_whole_before_it_is_applied() -> None:
    installer = "\n".join(_uncommented((DEPLOY / "install.sh").read_text(encoding="utf-8")))
    stage = installer[installer.index("install_firewall() {") :]
    assert stage.index('nft -c -f "$conf"') < stage.index('nft -f "$conf"')
    # The rendering is what is installed, and a leftover placeholder stops it.
    assert 'render_firewall "$src" > "$rendered"' in stage
    assert "still holds the placeholder" in installer


def test_every_user_the_ruleset_names_is_one_the_installer_checks_for() -> None:
    named = set(re.findall(r'skuid (?:\{ )?((?:"[^"]+"(?:, )?)+)', "\n".join(_uncommented(NFT))))
    users = {user for group in named for user in re.findall(r'"([^"]+)"', group)}
    assert users == {"root", "_apt", "wikimirror", "wikimirror-sync", "qdrant"}
    installer = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    check = re.search(r"for user in (.*?); do\n\s+id ", installer)
    assert check is not None, "the installer's user-exists check moved"
    checked = set(check.group(1).replace('"$API_USER"', "wikimirror").replace(
        '"$SYNC_USER"', "wikimirror-sync").replace('"$QDRANT_USER"', "qdrant").split())
    assert users == checked, "an unknown name fails the whole ruleset load at boot"


# -- the dependencies are pinned, complete and installed from wheels --------------------------

CONSTRAINTS = DEPLOY / "constraints.txt"


def _pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in _uncommented(CONSTRAINTS.read_text(encoding="utf-8")):
        name, equals, version = line.partition("==")
        assert equals, f"{line!r} is not an exact pin"
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name), line
        assert re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]*", version), line
        assert name.lower() not in pins, f"{name} is pinned twice"
        pins[name.lower().replace("_", "-")] = version
    return pins


def test_every_constraint_is_an_exact_version() -> None:
    assert len(_pins()) > 30


def test_the_direct_dependencies_of_the_install_are_all_pinned() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    wanted = list(project["dependencies"])
    names = {
        re.split(r"[\[<>=!~ ;]", requirement, maxsplit=1)[0].lower().replace("_", "-")
        for requirement in wanted
    }
    assert names <= set(_pins()), sorted(names - set(_pins()))


def test_each_pin_satisfies_the_floor_pyproject_declares() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    pins = _pins()
    for requirement in project["dependencies"]:
        match = re.match(r"([A-Za-z0-9._-]+)(?:\[[^\]]*\])?>=([0-9][^,; ]*)", requirement)
        if match is None:
            continue
        name, floor = match.group(1).lower().replace("_", "-"), match.group(2)
        pinned = tuple(int(part) for part in re.findall(r"\d+", pins[name])[:3])
        wanted = tuple(int(part) for part in re.findall(r"\d+", floor)[:3])
        assert pinned >= wanted, f"{name} is pinned at {pins[name]}, below the floor {floor}"


@pytest.mark.parametrize("script", ["install.sh", "update.sh"])
def test_the_scripts_install_dependencies_from_the_pins_and_from_wheels(script: str) -> None:
    text = (DEPLOY / script).read_text(encoding="utf-8")
    assert "constraints.txt" in text
    assert "--only-binary=:all:" in text
    assert "--no-binary=http-ece" in text, "the one pure-Python sdist-only dependency"
    pip_installs = [line for line in text.splitlines() if '/bin/pip" install' in line]
    # pip upgrades itself before the pins exist; every other install goes through the array.
    unpinned = [
        line
        for line in pip_installs
        if "PIP_PINNED" not in line
        and "--upgrade pip" not in line
        and not line.lstrip().startswith("#")
    ]
    assert not unpinned, unpinned


# -- the nightly update -----------------------------------------------------------------------

UPDATE_TIMER = "wiki-mirror-update.timer"
UPDATE_SERVICE = "wiki-mirror-update.service"


def test_update_timer_fires_at_0220_local_time_with_no_zone_written() -> None:
    timer = _unit(UPDATE_TIMER)["Timer"]
    # No zone on purpose: the container's own time zone decides when 02:20 is, so the file
    # carries nobody's location (docs/OPERATIONS.md, "Time zone"). 02:20 is the main client's
    # nightly 02:00 update + 20 min.
    assert timer["OnCalendar"] == ["*-*-* 02:20:00"]
    assert timer["RandomizedDelaySec"] == ["300"]
    assert timer["Persistent"] == ["true"]
    assert timer["Unit"] == [UPDATE_SERVICE]
    assert _unit(UPDATE_TIMER)["Install"]["WantedBy"] == ["timers.target"]


def test_no_timer_or_script_names_a_regional_time_zone() -> None:
    # A region/city zone is a location. UTC is not (the build timer follows Wikimedia's own UTC
    # publishing schedule, which is why it is the one place a zone is written).
    region = re.compile(r"\b(?:America|Europe|Asia|Africa|Australia|Pacific|Atlantic)/[A-Z]\w+")
    shipped = [
        *UNITS.glob("*.timer"),
        *UNITS.glob("*.service"),
        *DEPLOY.glob("*.sh"),
        DEPLOY / "wiki-mirror",
    ]
    assert shipped
    assert not [path.name for path in shipped if region.search(path.read_text(encoding="utf-8"))]


def test_the_monthly_build_timer_stays_in_utc_with_the_snapshot_schedule() -> None:
    assert _unit("wiki-mirror-build.timer")["Timer"]["OnCalendar"] == ["*-*-02 05:00:00 UTC"]


def test_update_service_runs_the_unattended_updater_once_as_root_without_retry() -> None:
    service = _unit(UPDATE_SERVICE)["Service"]
    assert service["Type"] == ["oneshot"]
    update_sh = "/opt/wiki-mirror/deploy/update.sh"
    assert service["ExecStart"] == [f"{update_sh} --unattended"]
    # The checkout is root-owned on purpose, so root fetches it; and a failed update has already
    # rolled itself back, so a retry would only repeat the failure.
    assert service["User"] == ["root"]
    assert service["Restart"] == ["no"]


def test_update_service_can_write_everything_the_updater_writes() -> None:
    service = _unit(UPDATE_SERVICE)["Service"]
    assert service["ProtectSystem"] == ["strict"]
    writable = service["ReadWritePaths"][0].split()
    # checkout + venv, installed units, the wiki-mirror link, and the build lock / update.json.
    assert writable == [
        "/opt/wiki-mirror",
        "/etc/systemd/system",
        "/usr/local/bin",
        "/var/lib/wiki-mirror/state",
    ]
    # The deploy key is in /root/.ssh: the fetch must be able to read it.
    assert service["ProtectHome"] == ["read-only"]


def test_every_shipped_unit_is_installed_and_the_timers_are_enabled_by_the_installer() -> None:
    installer = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    declared = re.search(r"^UNITS=\((.*?)\)", installer, re.S | re.M)
    assert declared is not None, "the installer's UNITS array moved"
    installed = set(declared.group(1).split())
    assert installed == {unit.name for unit in UNITS.glob("wiki-mirror*")}
    enabled = re.search(r"run systemctl enable ((?:[^\n]*\\\n)*[^\n]*)", installer)
    assert enabled is not None, "the installer's enable step moved"
    assert UPDATE_TIMER in enabled.group(1)
    assert "wiki-mirror-build.timer" in enabled.group(1)
    started = re.search(
        r"for unit in ((?:[^\n]*\\\n)*[^\n]*); do\n\s+run systemctl start", installer
    )
    assert started is not None, "the installer's start loop moved"
    assert UPDATE_TIMER in started.group(1)


def test_the_installers_closing_hint_names_the_full_path_of_the_cli() -> None:
    # `pct enter <ctid>` has no /usr/local/bin on its PATH.
    installer = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    assert "/usr/local/bin/wiki-mirror" in installer.split("${BOLD}Next:${RESET}")[1]


def test_cli_update_documents_and_passes_on_the_format_flag() -> None:
    text = (DEPLOY / "wiki-mirror").read_text(encoding="utf-8")
    assert "--accept-format-change" in text
    assert 'exec "$APP_DIR/deploy/update.sh" "$@"' in text


# -- the format guard -------------------------------------------------------------------------


def _format_line() -> str:
    """The FORMAT line the code builds: the schema, the chunker and the index format."""
    return (
        f"store={store._SCHEMA_VERSION} chunker={index.chunker_fingerprint()}"
        f" index={index.INDEX_FORMAT}"
    )


def test_the_format_file_equals_the_line_built_from_the_code() -> None:
    # update.sh holds an update back when this file changes between two commits. If the file could
    # drift from the constants, a commit that changed the stored data's format could slip through
    # unheld; this test fails that commit until the file says what the code does.
    assert (DEPLOY / "FORMAT").read_text(encoding="utf-8") == _format_line() + "\n"


def test_the_format_file_is_one_line() -> None:
    assert len((DEPLOY / "FORMAT").read_text(encoding="utf-8").splitlines()) == 1


def test_the_format_line_leaves_out_the_parser_version() -> None:
    # A parser change only affects the next build, not what is served: it must not hold an update.
    assert "parser" not in (DEPLOY / "FORMAT").read_text(encoding="utf-8")


def test_update_script_decides_the_format_before_it_merges() -> None:
    text = "\n".join(_uncommented((DEPLOY / "update.sh").read_text(encoding="utf-8")))
    assert text.index('format_of "$CURRENT"') < text.index("git merge --ff-only")
    assert text.index('format_of "$TARGET"') < text.index("git merge --ff-only")
    assert "deploy/FORMAT" in text


def test_update_script_takes_the_builds_own_lock_without_waiting() -> None:
    text = "\n".join(_uncommented((DEPLOY / "update.sh").read_text(encoding="utf-8")))
    assert '"$STATE_DIR/build.lock"' in text
    assert "flock -n" in text
    # The lock is taken before the merge, so a build cannot start in the middle of the swap.
    assert text.index("take_build_lock ||") < text.index("git merge --ff-only")


# -- the shell scripts parse ------------------------------------------------------------------


@pytest.mark.parametrize("script", ["install.sh", "update.sh", "wiki-mirror"])
def test_the_shell_scripts_are_valid_bash(script: str) -> None:
    bash = shutil.which("bash")
    assert bash is not None, "no bash on this machine"
    result = subprocess.run(  # noqa: S603 - fixed argv, a repo file
        [bash, "-n", str(DEPLOY / script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
