"""Qualifies the shape of the mirror's deploy files, without a kernel, systemd or a box.

What these guard is the set of decisions a reviewer would otherwise have to re-read the files to
find again: the services run Python in isolated mode from a directory nobody can write; a build
that fails is retried a bounded number of times and then stops; the firewall refuses what it does
not admit instead of dropping it, admits apt's real user, and names only users the installer
checks for; the dependencies are pinned, complete against pyproject.toml, and installed from
wheels; the nightly update timer is at 02:20 local time (no zone is written, so the container's
own decides) and the installer enables it only on request; the
FORMAT file `update.sh` compares across commits equals the line the code builds, so it can never
drift from the constants it names; the firewall file is a template whose only placeholders sit in
its `define` lines and match the keys of `site.env.example`, whose embedding-server lines are
bracketed by the markers the installer removes when a site has none, and which holds no literal
address of anyone's network. Whether nftables or systemd accepts the files is qualified on the
box, by the installer (`nft -c` before it applies, `systemctl daemon-reload`). What `update.sh`
does on each outcome is qualified by running it, in `test_update_script.py`; how the installer
reads and renders the site file is in `test_install_site.py`; how it fetches the repository and
whether it enables the update timer is in `test_install_repo_and_updates.py`.

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


#: The private and shared blocks as the network ranges they are: not anyone's network, and
#: exactly what the build user's tcp 443 rule excludes (below).
PRIVATE_BLOCKS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")


def test_the_firewall_template_holds_no_literal_lan_address() -> None:
    without_the_ranges = NFT
    for block in PRIVATE_BLOCKS:
        without_the_ranges = without_the_ranges.replace(block, "<range>")
    assert not PRIVATE_ADDRESS.findall(without_the_ranges)


def _build_user_443_rule() -> str:
    rules = [
        line
        for line in _chain("output")
        if 'skuid "wikimirror-sync"' in line and "tcp dport 443" in line
    ]
    assert len(rules) == 1, rules
    return rules[0]


def test_the_build_users_443_rule_excludes_every_private_and_shared_range() -> None:
    # The parse job handles attacker-editable HTML: it may reach public hosts on 443 (Enterprise,
    # S3) but not a host on the LAN or any other private network.
    rule = _build_user_443_rule()
    excluded = re.search(r"ip daddr != \{([^}]*)\}", rule)
    assert excluded is not None, rule
    assert set(re.findall(r"\d+\.\d+\.\d+\.\d+/\d+", excluded.group(1))) == set(PRIVATE_BLOCKS)
    assert rule.startswith("meta nfproto ipv4 ")
    assert rule.endswith("tcp dport 443 accept")


def test_the_private_ranges_the_443_rule_excludes_are_the_ones_a_lan_can_be_in() -> None:
    # LAN_V4 is not in the exclusion set because the installer only accepts a LAN inside these
    # blocks; if that list ever grew, the rule would silently let the parse job reach the new one.
    installer = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    declared = re.search(r"^PRIVATE_NETWORKS=\((.*?)\)", installer, re.M)
    assert declared is not None, "the installer's PRIVATE_NETWORKS moved"
    assert set(declared.group(1).split()) == set(PRIVATE_BLOCKS)


def test_the_embedding_accept_comes_before_the_build_users_443_rule() -> None:
    # The embedding server is on the LAN, which the 443 rule excludes, so it needs its own accept
    # first; if the order flipped, an embedding server on port 443 would be refused.
    output = _chain("output")
    embedding = [i for i, line in enumerate(output) if "$EMBEDDING_V4" in line]
    assert embedding == [output.index(_build_user_443_rule()) - 1]


def test_root_and_apt_keep_80_and_443_to_anywhere_so_a_lan_mirror_still_works() -> None:
    # Deliberate (see the template header): they parse nothing an attacker edits, and a site may
    # run an apt or PyPI mirror, or a proxy, on its own LAN.
    for user in ("root", "_apt"):
        (rule,) = [
            line for line in _chain("output") if f'skuid "{user}"' in line and "dport" in line
            and "@github_git" not in line and "udp" not in line
        ]
        assert "daddr" not in rule, rule


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
    text = (DEPLOY / "site.env.example").read_text(encoding="utf-8")
    # Nothing is assumed about a site: every setting ships commented out, in the `# KEY=value`
    # shape an operator uncomments (a network that merely happens to equal the example's would
    # otherwise be unfixable: the installer refuses an unedited example). Each is still a key the
    # template knows.
    assert not _uncommented(text)
    shipped = set(re.findall(r"^# ([A-Z][A-Z_]*)=", text, re.M))
    assert shipped == {"LAN_CIDR", "EMBEDDING_HOST", "EMBEDDING_PORT"}
    assert shipped == {placeholder.strip("@") for placeholder in PLACEHOLDER.findall(NFT)}


EMBEDDING_BEGIN = "# >>> embedding-server"
EMBEDDING_END = "# <<< embedding-server"


def _outside_and_inside_the_embedding_markers() -> tuple[list[str], list[str]]:
    """The template's lines outside, and between, the markers the installer strips."""
    outside: list[str] = []
    inside: list[str] = []
    open_block = False
    for raw in NFT.splitlines():
        line = raw.strip()
        if line == EMBEDDING_BEGIN:
            assert not open_block, "markers nest"
            open_block = True
        elif line == EMBEDDING_END:
            assert open_block, "a closing marker with no opening one"
            open_block = False
        else:
            (inside if open_block else outside).append(line)
    assert not open_block, "an opening marker is never closed"
    return outside, inside


def test_the_embedding_defines_and_rule_are_the_only_lines_between_the_markers() -> None:
    _, inside = _outside_and_inside_the_embedding_markers()
    assert _uncommented("\n".join(inside)) == [
        "define EMBEDDING_V4 = @EMBEDDING_HOST@",
        "define EMBEDDING_PORT = @EMBEDDING_PORT@",
        'meta skuid "wikimirror-sync" ip daddr $EMBEDDING_V4 tcp dport $EMBEDDING_PORT accept',
    ]


def test_nothing_outside_the_embedding_markers_names_the_embedding_values() -> None:
    # nft rejects a rule that names an undefined variable; with no embedding server the installer
    # removes the bracketed lines, so anything else that named them would break the whole ruleset.
    outside, _ = _outside_and_inside_the_embedding_markers()
    assert not [line for line in outside if "EMBEDDING_" in line]


def test_the_embedding_server_rule_uses_the_site_defines_for_address_and_port() -> None:
    rules = [line for line in _chain("output") if "$EMBEDDING_" in line]
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


def test_no_pin_is_left_for_a_package_this_project_does_not_use() -> None:
    # Pins that once belonged to a web-push dependency and a password hasher no longer in the
    # tree; a constraint on a package pip does not install is inert and only misleads.
    leftovers = {"pywebpush", "http-ece", "py-vapid", "argon2-cffi", "argon2-cffi-bindings"}
    assert not leftovers & set(_pins())


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
    # No exception: every pinned dependency has a wheel, so nothing is ever built on the box.
    assert "--no-binary" not in text
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
    # carries nobody's location (docs/OPERATIONS.md, "Time zone"). 02:20 is a quiet hour.
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
    # A checkout cloned over SSH (a private fork) keeps its deploy key in /root/.ssh: the fetch
    # must be able to read it.
    assert service["ProtectHome"] == ["read-only"]


def test_every_shipped_unit_is_installed_and_the_update_timer_is_opt_in() -> None:
    installer = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    declared = re.search(r"^UNITS=\((.*?)\)", installer, re.S | re.M)
    assert declared is not None, "the installer's UNITS array moved"
    installed = set(declared.group(1).split())
    assert installed == {unit.name for unit in UNITS.glob("wiki-mirror*")}
    # What is always enabled and started: the services and the monthly build timer, not the update
    # timer, which is added only on --auto-update or when it is already enabled
    # (test_install_repo_and_updates.py runs both ways).
    always = re.search(r"local to_enable=\((.*?)\)", installer)
    assert always is not None, "the installer's enable list moved"
    assert set(always.group(1).split()) == {
        "wiki-mirror-qdrant.service",
        "wiki-mirror.service",
        "wiki-mirror-build.timer",
    }
    assert f"to_enable+=({UPDATE_TIMER})" in installer
    assert 'run systemctl enable "${to_enable[@]}"' in installer
    assert 'for unit in "${to_enable[@]}"; do' in installer


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
