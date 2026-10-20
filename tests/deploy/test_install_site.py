"""Qualifies how `deploy/install.sh` reads, validates and renders the per-site settings.

The firewall names the LAN, the embedding server and its port, which differ at every site; they
come from `/etc/wiki-mirror/site.env` and are substituted into a ruleset that nft loads as root.
What is under test is the part that must not be wrong: the file is parsed as data and never
evaluated; every value must be a strict literal or the installer stops (a hostile or
mistyped value never reaches the ruleset); the embedding server is optional, its host and port
come as a pair or not at all, and a site without one gets a ruleset with no rule for it and no
variable that rule would need; a first run installs the example and stops; an
unedited example is refused; an edited file is never overwritten; the rendered ruleset has no
placeholder left and differs from the template only in its `define` lines; LAN_CIDR must be
a private network of /8 or longer; the SSH session running the installer and the box itself must
be inside it; the site file must belong to root and not be writable by anyone else; the resolver
preflight follows the site's LAN instead of a fixed network; the firewall install puts the
previous file back when the check fails; and a dry run reports each of these as a blocker and
touches nothing.

The installer is exercised two ways, both without root or a box: `install.sh --show-firewall`
as a child process (the mode `update.sh` uses), and by sourcing the script and calling one
function (it only runs `main` when executed). `ip`, `nft`, `systemctl` and the file owner the
installer expects are stood in for through its test seams. Whether nft accepts the rendered
ruleset is qualified on the box (`nft -c` before it applies). Addresses are the shared address
space (100.64.0.0/10), documentation ranges, or the guide's example values.

See docs/OPERATIONS.md ("The site file") and docs/DESIGN.md section 6.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
INSTALL_SH = DEPLOY / "install.sh"
EXAMPLE = DEPLOY / "site.env.example"
TEMPLATE = DEPLOY / "nftables" / "wiki-mirror.nft"
BASH = shutil.which("bash")
INSTALL_BIN = shutil.which("install")

#: `contract`, not `unit`: each test spawns a real shell against real files, which is the
#: boundary being qualified (the same tier as the other deploy-script tests).
pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(BASH is None or INSTALL_BIN is None, reason="install.sh needs bash"),
]

#: A complete, valid site file in the shared address space (100.64.0.0/10), which the installer
#: accepts as a LAN and which is nobody's own network; deliberately not the shipped example's
#: values, so a render that fell back to the example would be caught.
LAN = "100.64.1.0/24"
HOST = "100.64.1.77"
PORT = "11434"
#: What the stubbed `ip -4 -o addr show scope global` prints by default: an address inside LAN.
BOX_ADDRESS_LINE = "2: eth0    inet 100.64.1.50/24 brd 100.64.1.255 scope global eth0"
VALID = f"LAN_CIDR={LAN}\nEMBEDDING_HOST={HOST}\nEMBEDDING_PORT={PORT}\n"
#: A valid site file for a site with no embedding server: the LAN alone.
NO_EMBEDDING = f"LAN_CIDR={LAN}\n"

#: Every variable `install.sh` reads from its environment, plus the ones git and ssh would act on
#: if a test reached them: none is inherited from the machine the suite runs on.
_INSTALLER_ENVIRONMENT = frozenset(
    {
        "SSH_CONNECTION", "REPO_URL", "APP_DIR", "DATA_DIR", "CONF_DIR", "SNAPSHOT_MOUNT",
        "SNAPSHOT_DIR", "DEPLOY_KEY", "KNOWN_HOSTS", "BRANCH", "NFT_INSTALLED", "NFT_CONF",
        "RESOLV_CONF", "UNIT_DIR", "IP_BIN", "SITE_OWNER_UID", "SITE_OWNER_GID", "PYTHON",
        "GIT_SSH_COMMAND", "GIT_TERMINAL_PROMPT",
    }
)  # fmt: skip

PLACEHOLDER = re.compile(r"@[A-Z][A-Z0-9_]*@")
#: The comment lines that bracket the embedding server's `define`s and rule in the template.
EMBEDDING_MARKER = re.compile(r"^\s*# [<>]{3} embedding-server$", re.M)


@dataclass
class Site:
    """A scratch box for the installer: a checkout's deploy files and a config directory."""

    root: Path

    @property
    def app(self) -> Path:
        """The checkout (APP_DIR), holding the deploy files the installer reads."""
        return self.root / "app"

    @property
    def conf(self) -> Path:
        """The configuration directory (CONF_DIR) holding `site.env`."""
        return self.root / "conf"

    @property
    def site_file(self) -> Path:
        """Where the installer looks for the site file."""
        return self.conf / "site.env"

    @property
    def installed(self) -> Path:
        """Where the installer would put the rendered ruleset (NFT_INSTALLED)."""
        return self.root / "etc-nftables.d" / "wiki-mirror.nft"

    @property
    def nft_conf(self) -> Path:
        """The nftables configuration that includes the ruleset (NFT_CONF)."""
        return self.root / "nftables.conf"

    @property
    def resolv(self) -> Path:
        """The resolver list the preflight reads (RESOLV_CONF)."""
        return self.root / "resolv.conf"

    @property
    def log(self) -> Path:
        """What the stubbed `nft` and `systemctl` were asked to do, one line each."""
        return self.root / "stub.log"

    def calls(self) -> list[str]:
        """The stubs' log lines, in order (empty if nothing was called)."""
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def write(self, text: str) -> None:
        """Write the site file."""
        self.site_file.write_text(text, encoding="utf-8")

    @property
    def ip_stub(self) -> Path:
        """The stand-in for `ip` (IP_BIN): prints $STUB_IP_LINES, by default one address in LAN."""
        return self.root / "ip-stub"

    def environment(self, installer: Path, **env: str) -> dict[str, str]:
        """The installer's variables, pointed at this scratch box.

        SSH_CONNECTION is dropped from the inherited environment (the suite may itself be run
        over ssh), and so is every other variable the installer reads and this fixture does not
        set, so a developer's own BRANCH or REPO_URL cannot change what a test sees; a test that
        wants one passes it. The owner the site file must have is the test's own uid and gid,
        since a test cannot be root.
        """
        inherited = {k: v for k, v in os.environ.items() if k not in _INSTALLER_ENVIRONMENT}
        return {
            **inherited,
            "APP_DIR": str(self.app),
            "CONF_DIR": str(self.conf),
            "NFT_INSTALLED": str(self.installed),
            "NFT_CONF": str(self.nft_conf),
            "RESOLV_CONF": str(self.resolv),
            "IP_BIN": str(self.ip_stub),
            "SITE_OWNER_UID": str(os.getuid()),
            "SITE_OWNER_GID": str(os.getgid()),
            "STUB_LOG": str(self.log),
            "INSTALLER": str(installer),
            **env,
        }

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        """Run `install.sh` with `args` (only modes that need no root: --show-firewall)."""
        assert BASH is not None
        return subprocess.run(  # noqa: S603 - fixed argv, a repo script
            [BASH, str(INSTALL_SH), *args],
            env=self.environment(INSTALL_SH, **env),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def call(
        self, code: str, *, installer: Path = INSTALL_SH, **env: str
    ) -> subprocess.CompletedProcess[str]:
        """Source the installer and run `code` in that shell (its functions, no `main`)."""
        assert BASH is not None
        return subprocess.run(  # noqa: S603 - fixed argv, a repo script sourced by path
            [BASH, "-c", f'source "$INSTALLER"; {code}'],
            env=self.environment(installer, **env),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )


@pytest.fixture
def site(tmp_path: Path) -> Site:
    """A scratch box with the checkout's template and example, and an empty config directory."""
    box = Site(tmp_path)
    (box.app / "deploy" / "nftables").mkdir(parents=True)
    shutil.copy(TEMPLATE, box.app / "deploy" / "nftables" / TEMPLATE.name)
    shutil.copy(EXAMPLE, box.app / "deploy" / EXAMPLE.name)
    box.conf.mkdir()
    box.installed.parent.mkdir()
    box.ip_stub.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"${{STUB_IP_LINES-{BOX_ADDRESS_LINE}}}\"\n", encoding="utf-8"
    )
    box.ip_stub.chmod(0o755)
    return box


def _defines(ruleset: str) -> dict[str, str]:
    return dict(re.findall(r"^\s*define (\w+) = (\S+)$", ruleset, re.M))


def _with(key: str, value: str) -> str:
    """The valid site file with one line's value replaced."""
    return "".join(
        f"{name}={value if name == key else good}\n"
        for name, good in (("LAN_CIDR", LAN), ("EMBEDDING_HOST", HOST), ("EMBEDDING_PORT", PORT))
    )


# -- rendering --------------------------------------------------------------------------------


def test_show_firewall_valid_site_file_renders_the_expected_defines(site: Site) -> None:
    site.write(VALID)
    result = site.run("--show-firewall")
    assert result.returncode == 0, result.stderr
    assert _defines(result.stdout) == {
        "LAN_V4": LAN,
        "EMBEDDING_V4": HOST,
        "EMBEDDING_PORT": PORT,
    }


def test_show_firewall_output_is_the_template_with_only_the_placeholders_replaced(
    site: Site,
) -> None:
    site.write(VALID)
    result = site.run("--show-firewall")
    template = TEMPLATE.read_text(encoding="utf-8")
    expected = (
        template.replace("@LAN_CIDR@", LAN)
        .replace("@EMBEDDING_HOST@", HOST)
        .replace("@EMBEDDING_PORT@", PORT)
    )
    assert result.stdout == expected


def test_show_firewall_output_has_no_placeholder_left(site: Site) -> None:
    site.write(VALID)
    result = site.run("--show-firewall")
    assert result.returncode == 0, result.stderr
    assert not PLACEHOLDER.findall(result.stdout)


def test_show_firewall_the_shipped_example_with_lan_cidr_uncommented_is_itself_valid(
    site: Site,
) -> None:
    # A shipped example the installer rejects once it is filled in would stop every first
    # install for the wrong reason. The one edit it asks for is removing "# " from LAN_CIDR.
    site.write(EXAMPLE.read_text(encoding="utf-8").replace("# LAN_CIDR=", "LAN_CIDR="))
    result = site.run("--show-firewall")
    assert result.returncode == 0, result.stderr
    assert not PLACEHOLDER.findall(result.stdout)
    # The embedding pair ships commented out, so the example alone renders a ruleset without it.
    assert _defines(result.stdout) == {"LAN_V4": "192.168.1.0/24"}


def test_show_firewall_the_shipped_example_unedited_sets_nothing_and_is_refused(
    site: Site,
) -> None:
    # Every setting is commented out as shipped, so a site on 192.168.1.0/24 has to write the
    # line as well: nothing is assumed about the network.
    shutil.copy(EXAMPLE, site.site_file)
    result = site.run("--show-firewall")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "LAN_CIDR is missing" in result.stderr
    assert "commented out" in result.stderr


def test_show_firewall_comments_and_blank_lines_are_ignored(site: Site) -> None:
    site.write("# a header\n\n   # indented comment\n" + VALID + "\n# trailer\n")
    result = site.run("--show-firewall")
    assert result.returncode == 0, result.stderr
    assert _defines(result.stdout)["EMBEDDING_PORT"] == PORT


def test_show_firewall_last_line_without_a_newline_is_still_read(site: Site) -> None:
    site.write(VALID.rstrip("\n"))
    result = site.run("--show-firewall")
    assert result.returncode == 0, result.stderr
    assert _defines(result.stdout)["EMBEDDING_PORT"] == PORT


def test_show_firewall_no_site_file_is_refused_with_nothing_on_stdout(site: Site) -> None:
    result = site.run("--show-firewall")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "site.env" in result.stderr


def test_show_firewall_changes_nothing_on_disk(site: Site) -> None:
    site.write(VALID)
    before = sorted(path.relative_to(site.root) for path in site.root.rglob("*"))
    assert site.run("--show-firewall").returncode == 0
    assert sorted(path.relative_to(site.root) for path in site.root.rglob("*")) == before
    assert not site.installed.exists()


# -- a value is one of three strict literals, or the installer stops --------------------------

BAD_VALUES = [
    ("LAN_CIDR", "192.168.1.0/33"),
    ("LAN_CIDR", "192.168.1.0"),
    ("LAN_CIDR", "192.168.1.0/24;flush"),
    ("LAN_CIDR", "192.168.1.0/24,198.51.100.0/24"),
    ("LAN_CIDR", "256.1.1.0/24"),
    ("LAN_CIDR", "198.051.100.0/24"),
    ("LAN_CIDR", "192.168.1.0/024"),
    ("LAN_CIDR", '"192.168.1.0/24"'),
    ("LAN_CIDR", "192.168.1.5/24"),
    ("LAN_CIDR", "0.0.0.0/0"),
    ("LAN_CIDR", "$(id)"),
    ("LAN_CIDR", "fd00::/8"),
    ("LAN_CIDR", ""),
    ("EMBEDDING_HOST", "192.168.1.60/32"),
    ("EMBEDDING_HOST", "300.1.1.1"),
    ("EMBEDDING_HOST", "embed.example.org"),
    ("EMBEDDING_HOST", "198.51.100"),
    ("EMBEDDING_HOST", "192.168.1.60:8080"),
    ("EMBEDDING_HOST", "192.168.1.60;accept"),
    ("EMBEDDING_HOST", "192.168.1.060"),
    ("EMBEDDING_HOST", ""),
    ("EMBEDDING_PORT", "0"),
    ("EMBEDDING_PORT", "65536"),
    ("EMBEDDING_PORT", "80;"),
    ("EMBEDDING_PORT", "08080"),
    ("EMBEDDING_PORT", "-1"),
    ("EMBEDDING_PORT", "http"),
    ("EMBEDDING_PORT", "8080,22"),
    ("EMBEDDING_PORT", "99999999999999999999"),
    ("EMBEDDING_PORT", ""),
]


@pytest.mark.parametrize(("key", "value"), BAD_VALUES, ids=[f"{k}={v}" for k, v in BAD_VALUES])
def test_show_firewall_invalid_value_is_refused_and_nothing_is_rendered(
    site: Site, key: str, value: str
) -> None:
    site.write(_with(key, value))
    result = site.run("--show-firewall")
    assert result.returncode == 1
    assert result.stdout == "", "a refused value must not reach any output the caller could use"
    assert key in result.stderr


@pytest.mark.parametrize(("key", "value"), BAD_VALUES, ids=[f"{k}={v}" for k, v in BAD_VALUES])
def test_check_site_invalid_value_blocks_the_real_run_and_the_dry_run(
    site: Site, key: str, value: str
) -> None:
    site.write(_with(key, value))
    real = site.call("check_site")
    assert real.returncode == 1
    assert key in real.stderr
    dry = site.call('DRY_RUN=true; check_site; printf "%s\\n" "${BLOCKERS[@]}"; echo "[$LAN_CIDR]"')
    assert dry.returncode == 0, dry.stderr
    assert "problem(s)" in dry.stdout, "the dry run lists it as a blocker"
    assert "[]" in dry.stdout, "no half-valid value is left loaded"


def test_show_firewall_a_prefix_with_host_bits_names_the_network_address(site: Site) -> None:
    site.write(_with("LAN_CIDR", "192.168.1.5/24"))
    assert "192.168.1.0/24" in site.run("--show-firewall").stderr


# -- LAN_CIDR is a private network of /8 or longer --------------------------------------------

#: Prefixes shorter than /8. The firewall admits SSH and the API from everything in LAN_CIDR, so
#: each of these opens them to a large part of the internet.
TOO_WIDE = ["0.0.0.0/0", "0.0.0.0/1", "128.0.0.0/1", "192.0.0.0/2", "172.0.0.0/7", "8.0.0.0/5"]

#: Well-formed networks of /8 or longer that are nobody's private LAN, with the edges of each
#: private block just outside it: 172.16/12, 192.168/16 and 100.64/10 end where these begin.
NOT_PRIVATE = [
    "8.8.8.0/24",
    "11.0.0.0/8",
    "172.15.0.0/16",
    "172.32.0.0/16",
    "192.167.0.0/16",
    "192.169.0.0/16",
    "100.63.0.0/16",
    "100.128.0.0/16",
    "100.0.0.0/8",
    "172.0.0.0/8",
    "192.0.0.0/8",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "198.51.100.0/24",
    "203.0.113.0/24",
]

#: Inside RFC 1918 or RFC 6598 (100.64.0.0/10): each whole block, networks at its far end, and
#: a single host.
PRIVATE = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "172.31.255.0/24",
    "192.168.0.0/16",
    "192.168.1.0/24",
    "192.168.255.0/24",
    "100.64.0.0/10",
    "100.64.1.0/24",
    "100.127.255.0/24",
    "100.64.1.77/32",
]


@pytest.mark.parametrize("network", TOO_WIDE)
def test_show_firewall_lan_cidr_wider_than_slash_8_is_refused_and_says_why(
    site: Site, network: str
) -> None:
    site.write(_with("LAN_CIDR", network))
    result = site.run("--show-firewall")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "wider than /8" in result.stderr
    assert network in result.stderr


@pytest.mark.parametrize("network", NOT_PRIVATE)
def test_show_firewall_lan_cidr_outside_private_space_is_refused_and_says_why(
    site: Site, network: str
) -> None:
    site.write(_with("LAN_CIDR", network))
    result = site.run("--show-firewall")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "not a private network" in result.stderr
    assert "100.64.0.0/10" in result.stderr, "the message names what would be accepted"


@pytest.mark.parametrize("network", PRIVATE)
def test_show_firewall_lan_cidr_inside_private_space_is_accepted_and_rendered(
    site: Site, network: str
) -> None:
    site.write(_with("LAN_CIDR", network))
    result = site.run("--show-firewall")
    assert result.returncode == 0, result.stderr
    assert _defines(result.stdout)["LAN_V4"] == network


@pytest.mark.parametrize("network", TOO_WIDE + NOT_PRIVATE)
def test_check_site_lan_cidr_that_is_no_lan_blocks_the_real_run_and_the_dry_run(
    site: Site, network: str
) -> None:
    site.write(_with("LAN_CIDR", network))
    real = site.call("check_site")
    assert real.returncode == 1
    assert "LAN_CIDR" in real.stderr
    dry = site.call('DRY_RUN=true; check_site; printf "%s\\n" "${BLOCKERS[@]}"; echo "[$LAN_CIDR]"')
    assert dry.returncode == 0, dry.stderr
    assert "problem(s)" in dry.stdout, "the dry run lists it as a blocker"
    assert "[]" in dry.stdout, "a refused network is not left loaded for the firewall"


def test_show_firewall_every_problem_is_listed_in_one_pass(site: Site) -> None:
    site.write("LAN_CIDR=nonsense\nEMBEDDING_HOST=nonsense\nEMBEDDING_PORT=nonsense\n")
    stderr = site.run("--show-firewall").stderr
    assert "LAN_CIDR must" in stderr
    assert "EMBEDDING_HOST must" in stderr
    assert "EMBEDDING_PORT must" in stderr


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("LAN_CIDR", "LAN_CIDR is missing"),
        # The embedding pair is optional, but only as a pair: losing one half is refused.
        ("EMBEDDING_HOST", "EMBEDDING_PORT is set but EMBEDDING_HOST is not"),
        ("EMBEDDING_PORT", "EMBEDDING_HOST is set but EMBEDDING_PORT is not"),
    ],
)
def test_show_firewall_a_missing_key_is_refused(site: Site, missing: str, message: str) -> None:
    site.write("".join(line for line in VALID.splitlines(keepends=True) if missing not in line))
    result = site.run("--show-firewall")
    assert result.returncode == 1
    assert result.stdout == ""
    assert message in result.stderr


def test_show_firewall_a_key_set_twice_is_refused(site: Site) -> None:
    site.write(VALID + "EMBEDDING_PORT=22\n")
    result = site.run("--show-firewall")
    assert result.returncode == 1
    assert "EMBEDDING_PORT is set more than once" in result.stderr


@pytest.mark.parametrize(
    "line",
    [
        "FOO=bar",
        "EMBEDDING_PORTS=22",
        "export LAN_CIDR=198.51.100.0/24",
        " LAN_CIDR=198.51.100.0/24",
        "LAN_CIDR =198.51.100.0/24",
        "LAN_CIDR=198.51.100.0/24 # trailing",
        "LAN_CIDR=198.51.100.0/24\r",
        "just words",
    ],
)
def test_show_firewall_a_line_that_is_not_one_of_the_three_keys_is_refused(
    site: Site, line: str
) -> None:
    site.write(VALID + line + "\n")
    result = site.run("--show-firewall")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "line 4" in result.stderr


@pytest.mark.parametrize(
    "payload",
    ["$(touch${IFS}MARKER)", "`touch${IFS}MARKER`", ";touch${IFS}MARKER", "${IFS}MARKER"],
)
@pytest.mark.parametrize("key", ["LAN_CIDR", "EMBEDDING_HOST", "EMBEDDING_PORT"])
def test_show_firewall_the_site_file_is_data_and_a_substitution_in_it_is_never_run(
    site: Site, key: str, payload: str
) -> None:
    # No whitespace in the payloads, so each reaches the value check instead of failing the line
    # pattern first: they are rejected as values, and nothing is evaluated on the way.
    marker = site.root / "pwned"
    site.write(_with(key, payload.replace("MARKER", str(marker))))
    result = site.run("--show-firewall")
    assert result.returncode == 1
    assert result.stdout == ""
    assert not marker.exists()


def test_show_firewall_a_whole_line_of_shell_is_never_run(site: Site) -> None:
    marker = site.root / "pwned"
    shell = f"$(touch${{IFS}}{marker})\n`touch${{IFS}}{marker}`\nsource${{IFS}}{marker}\n"
    site.write(VALID + shell)
    result = site.run("--show-firewall")
    assert result.returncode == 1
    assert not marker.exists()


# -- the embedding server is optional, but only as a pair ------------------------------------


def _without_the_embedding_block(template: str) -> str:
    """The template with everything between the embedding markers (and the markers) removed."""
    return re.sub(
        r"^[ \t]*# >>> embedding-server\n.*?^[ \t]*# <<< embedding-server\n",
        "",
        template,
        flags=re.M | re.S,
    )


def test_show_firewall_without_an_embedding_server_renders_no_embedding_rule_or_define(
    site: Site,
) -> None:
    site.write(NO_EMBEDDING)
    result = site.run("--show-firewall")
    assert result.returncode == 0, result.stderr
    assert _defines(result.stdout) == {"LAN_V4": LAN}
    # Nothing that could name an undefined variable, and no placeholder: nft rejects both.
    assert "EMBEDDING" not in result.stdout
    assert "tcp dport 443 accept" in result.stdout, "the build user's public 443 rule stays"
    assert not PLACEHOLDER.findall(result.stdout)
    assert not EMBEDDING_MARKER.search(result.stdout), "the markers go with what they bracket"


def test_show_firewall_without_an_embedding_server_is_the_template_less_the_marked_block(
    site: Site,
) -> None:
    site.write(NO_EMBEDDING)
    template = TEMPLATE.read_text(encoding="utf-8")
    expected = _without_the_embedding_block(template).replace("@LAN_CIDR@", LAN)
    assert site.run("--show-firewall").stdout == expected


def test_show_firewall_with_an_embedding_server_keeps_the_rule_and_resolves_its_defines(
    site: Site,
) -> None:
    site.write(VALID)
    result = site.run("--show-firewall")
    assert result.returncode == 0, result.stderr
    assert _defines(result.stdout) == {
        "LAN_V4": LAN,
        "EMBEDDING_V4": HOST,
        "EMBEDDING_PORT": PORT,
    }
    rule = 'meta skuid "wikimirror-sync" ip daddr $EMBEDDING_V4 tcp dport $EMBEDDING_PORT accept'
    assert rule in result.stdout
    assert len(EMBEDDING_MARKER.findall(result.stdout)) == 4, "kept, as comments"


@pytest.mark.parametrize(
    "text",
    ["EMBEDDING_HOST=\nEMBEDDING_PORT=\n", "EMBEDDING_HOST=\n", "EMBEDDING_PORT=\n"],
)
def test_show_firewall_embedding_keys_with_empty_values_count_as_not_set(
    site: Site, text: str
) -> None:
    site.write(NO_EMBEDDING + text)
    result = site.run("--show-firewall")
    assert result.returncode == 0, result.stderr
    assert _defines(result.stdout) == {"LAN_V4": LAN}


#: One embedding value given and its partner absent or empty, with the problem it must report.
ONLY_HOST = "EMBEDDING_HOST is set but EMBEDDING_PORT is not"
ONLY_PORT = "EMBEDDING_PORT is set but EMBEDDING_HOST is not"
HALF_SET = [
    (f"EMBEDDING_HOST={HOST}\n", ONLY_HOST),
    (f"EMBEDDING_PORT={PORT}\n", ONLY_PORT),
    (f"EMBEDDING_HOST={HOST}\nEMBEDDING_PORT=\n", ONLY_HOST),
    (f"EMBEDDING_HOST=\nEMBEDDING_PORT={PORT}\n", ONLY_PORT),
]


@pytest.mark.parametrize(("text", "message"), HALF_SET)
def test_show_firewall_exactly_one_embedding_value_is_refused_and_says_both_or_neither(
    site: Site, text: str, message: str
) -> None:
    site.write(NO_EMBEDDING + text)
    result = site.run("--show-firewall")
    assert result.returncode == 1
    assert result.stdout == "", "half an egress rule is never rendered"
    assert message in result.stderr
    assert "set both, or neither" in result.stderr


@pytest.mark.parametrize(("text", "message"), HALF_SET)
def test_check_site_exactly_one_embedding_value_blocks_the_real_run_and_the_dry_run(
    site: Site, text: str, message: str
) -> None:
    site.write(NO_EMBEDDING + text)
    real = site.call("check_site")
    assert real.returncode == 1
    assert message in real.stderr
    dry = site.call('DRY_RUN=true; check_site; printf "%s\\n" "${BLOCKERS[@]}"; echo "[$LAN_CIDR]"')
    assert dry.returncode == 0, dry.stderr
    assert "problem(s)" in dry.stdout, "the dry run lists it as a blocker"
    assert "[]" in dry.stdout, "no half-valid value is left loaded"


def test_show_firewall_an_invalid_embedding_value_with_no_partner_reports_both_problems(
    site: Site,
) -> None:
    site.write(NO_EMBEDDING + "EMBEDDING_HOST=not-an-address\n")
    stderr = site.run("--show-firewall").stderr
    assert "EMBEDDING_HOST must be an IPv4 address" in stderr, "a typo is reported as a typo"
    assert "EMBEDDING_HOST is set but EMBEDDING_PORT is not" in stderr


def test_check_site_without_an_embedding_server_says_so_and_loads_no_embedding_values(
    site: Site,
) -> None:
    site.write(NO_EMBEDDING)
    result = site.call('check_site; echo "[$EMBEDDING_HOST][$EMBEDDING_PORT]"')
    assert result.returncode == 0, result.stderr
    assert f"LAN {LAN}, no embedding server" in result.stdout
    assert "embedding server  port" not in result.stdout, "the line must not print empty values"
    assert result.stdout.splitlines()[-1] == "[][]"


def test_check_site_with_an_embedding_server_names_its_address_and_port(site: Site) -> None:
    site.write(VALID)
    result = site.call("check_site")
    assert result.returncode == 0, result.stderr
    assert f"embedding server {HOST} port {PORT}" in result.stdout


def _nft_runner() -> list[str] | None:
    """The argv prefix that runs `nft` able to check a ruleset here, or None if nothing does.

    `nft -c` still talks to the kernel, which needs CAP_NET_ADMIN. Tried in order: a throwaway
    user and network namespace (`unshare -Urn`, no privilege, nothing of the host's touched), then
    passwordless `sudo -n` (GitHub's runners restrict unprivileged user namespaces but give the
    runner user sudo; `-c` loads nothing, so this changes nothing on the host either). Each is
    probed with an empty table before it is trusted.
    """
    nft = shutil.which("nft") or next(
        (path for path in ("/usr/sbin/nft", "/sbin/nft") if Path(path).exists()), None
    )
    if nft is None:
        return None
    candidates = [
        [tool, *flags, nft]
        for tool, flags in (
            (shutil.which("unshare"), ["-Urn"]),
            (shutil.which("sudo"), ["-n"]),
        )
        if tool is not None
    ]
    for argv in candidates:
        probe = subprocess.run(  # noqa: S603 - fixed argv, a stdin ruleset that is an empty table
            [*argv, "-c", "-f", "/dev/stdin"],
            input="table inet probe { }\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if probe.returncode == 0:
            return argv
    return None


@pytest.fixture(scope="module")
def nft_runner() -> list[str]:
    """The argv prefix for a working `nft`; skips if there is none, but fails when `CI` is set.

    A skip on a developer's machine is honest; a skip in CI would let a ruleset that nft rejects
    pass the build, which is the one thing these tests exist to catch.
    """
    argv = _nft_runner()
    if argv is None:
        reason = "needs nft, and either an unprivileged user namespace or passwordless sudo"
        if os.environ.get("CI"):
            pytest.fail(f"CI must be able to run `nft -c`, and cannot: {reason}")
        pytest.skip(reason)
    return argv


#: The users the ruleset names, as the numbers nft accepts, since none of them exist on a test
#: machine and nft refuses a name it cannot resolve.
_RULESET_USERS = {
    "wikimirror-sync": "1002",
    "wikimirror": "1001",
    "qdrant": "1003",
    "_apt": "42",
    "root": "0",
}


def _nft_check(
    runner: list[str], ruleset: str, tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    """Run `nft -c` on a rendered ruleset, with its users written as the numbers nft accepts."""
    numeric = re.sub(
        r'"(wikimirror-sync|wikimirror|qdrant|_apt|root)"',
        lambda match: _RULESET_USERS[match.group(1)],
        ruleset,
    )
    path = tmp_path / "check.nft"
    path.write_text(numeric, encoding="utf-8")
    return subprocess.run(  # noqa: S603 - fixed argv, a scratch file
        [*runner, "-c", "-f", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("settings", [VALID, NO_EMBEDDING], ids=["embedding", "no-embedding"])
def test_rendered_ruleset_passes_nft_check_with_and_without_an_embedding_server(
    site: Site, tmp_path: Path, nft_runner: list[str], settings: str
) -> None:
    site.write(settings)
    rendered = site.run("--show-firewall").stdout
    result = _nft_check(nft_runner, rendered, tmp_path)
    assert result.returncode == 0, result.stderr


def test_nft_check_rejects_the_embedding_rule_when_its_defines_are_missing(
    site: Site, tmp_path: Path, nft_runner: list[str]
) -> None:
    # The failure the markers exist to prevent: a rule naming a variable nobody defined.
    site.write(VALID)
    rendered = site.run("--show-firewall").stdout
    broken = "".join(
        line for line in rendered.splitlines(keepends=True) if "define EMBEDDING_" not in line
    )
    result = _nft_check(nft_runner, broken, tmp_path)
    assert result.returncode != 0
    assert "EMBEDDING_V4" in result.stderr


def _render(site: Site, template: Path, settings: str) -> subprocess.CompletedProcess[str]:
    return site.call(f'{settings}render_firewall "{template}"')


def test_render_firewall_with_only_one_embedding_value_returns_one_and_prints_nothing(
    site: Site,
) -> None:
    template = site.app / "deploy" / "nftables" / TEMPLATE.name
    result = _render(site, template, f"LAN_CIDR={LAN}; EMBEDDING_HOST={HOST}; ")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "must be set together or not at all" in result.stderr


def test_render_firewall_without_embedding_and_an_unclosed_marker_renders_nothing(
    site: Site,
) -> None:
    template = site.app / "deploy" / "nftables" / TEMPLATE.name
    text = template.read_text(encoding="utf-8")
    head, _, tail = text.rpartition("        # <<< embedding-server\n")
    template.write_text(head + tail, encoding="utf-8")
    result = _render(site, template, f"LAN_CIDR={LAN}; ")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "never closes it" in result.stderr


def test_render_firewall_without_embedding_and_a_closing_marker_alone_renders_nothing(
    site: Site,
) -> None:
    template = site.app / "deploy" / "nftables" / TEMPLATE.name
    template.write_text(
        template.read_text(encoding="utf-8") + "# <<< embedding-server\n", encoding="utf-8"
    )
    result = _render(site, template, f"LAN_CIDR={LAN}; ")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "with no '# >>> embedding-server' before it" in result.stderr


def test_render_firewall_without_embedding_and_a_rule_outside_the_markers_renders_nothing(
    site: Site,
) -> None:
    template = site.app / "deploy" / "nftables" / TEMPLATE.name
    template.write_text(
        template.read_text(encoding="utf-8") + "ip daddr $EMBEDDING_V4 accept\n", encoding="utf-8"
    )
    result = _render(site, template, f"LAN_CIDR={LAN}; ")
    assert result.returncode == 1
    assert result.stdout == "", "an undefined variable would fail the whole ruleset load"
    assert "outside its" in result.stderr


# -- the first install, the unedited example, an edited file ----------------------------------


def test_check_site_missing_file_installs_the_example_and_blocks(site: Site) -> None:
    result = site.call("check_site")
    assert result.returncode == 1
    assert site.site_file.read_text(encoding="utf-8") == EXAMPLE.read_text(encoding="utf-8")
    assert stat.S_IMODE(site.site_file.stat().st_mode) == 0o644
    assert "must be edited" in result.stderr


def test_check_site_missing_file_dry_run_blocks_and_creates_nothing(site: Site) -> None:
    result = site.call('DRY_RUN=true; check_site; printf "%s\\n" "${BLOCKERS[@]}"')
    assert result.returncode == 0, result.stderr
    assert "would be installed" in result.stdout
    assert not site.site_file.exists()


def test_check_site_missing_file_creates_the_config_directory_when_there_is_none(
    site: Site,
) -> None:
    shutil.rmtree(site.conf)
    result = site.call("check_site")
    assert result.returncode == 1
    assert site.site_file.exists()


def test_check_site_missing_file_in_a_lone_copy_uses_the_example_beside_the_script(
    site: Site, tmp_path: Path
) -> None:
    lone = tmp_path / "lone"
    lone.mkdir()
    shutil.copy(INSTALL_SH, lone / "install.sh")
    shutil.copy(EXAMPLE, lone / "site.env.example")
    shutil.rmtree(site.app)
    result = site.call("check_site", installer=lone / "install.sh")
    assert result.returncode == 1
    assert site.site_file.read_text(encoding="utf-8") == EXAMPLE.read_text(encoding="utf-8")


def test_check_site_missing_file_and_no_example_anywhere_blocks_with_the_lines_to_write(
    site: Site, tmp_path: Path
) -> None:
    lone = tmp_path / "lone"
    lone.mkdir()
    shutil.copy(INSTALL_SH, lone / "install.sh")
    shutil.rmtree(site.app)
    result = site.call("check_site", installer=lone / "install.sh")
    assert result.returncode == 1
    assert not site.site_file.exists()
    for key in ("LAN_CIDR=", "EMBEDDING_HOST=", "EMBEDDING_PORT="):
        assert key in result.stderr


def test_check_site_unedited_example_blocks_and_changes_nothing(site: Site) -> None:
    shutil.copy(EXAMPLE, site.site_file)
    before = site.site_file.read_bytes()
    result = site.call("check_site")
    assert result.returncode == 1
    assert "still the shipped example" in result.stderr
    assert site.site_file.read_bytes() == before


def test_check_site_unedited_example_blocks_a_dry_run_too(site: Site) -> None:
    shutil.copy(EXAMPLE, site.site_file)
    result = site.call('DRY_RUN=true; check_site; printf "%s\\n" "${BLOCKERS[@]}"')
    assert result.returncode == 0, result.stderr
    assert "still the shipped example" in result.stdout


def test_check_site_edited_valid_file_passes_loads_the_values_and_is_left_alone(
    site: Site,
) -> None:
    site.write(VALID)
    result = site.call('check_site; echo "$LAN_CIDR $EMBEDDING_HOST $EMBEDDING_PORT"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == f"{LAN} {HOST} {PORT}"
    assert site.site_file.read_text(encoding="utf-8") == VALID


def test_check_site_the_example_with_one_value_changed_counts_as_edited(site: Site) -> None:
    # The optional pair is commented out in the example; uncommenting it, with one value
    # changed, is the edit an operator with an embedding server makes.
    text = (
        EXAMPLE.read_text(encoding="utf-8")
        .replace("# LAN_CIDR=", "LAN_CIDR=")
        .replace("# EMBEDDING_HOST=", "EMBEDDING_HOST=")
        .replace("# EMBEDDING_PORT=11434", "EMBEDDING_PORT=9090")
    )
    site.write(text)
    # The example's own LAN is 192.168.1.0/24, so the box (as the stub reports it) is on that LAN.
    result = site.call(
        'check_site; echo "$EMBEDDING_PORT"',
        STUB_IP_LINES="2: eth0    inet 192.168.1.50/24 brd 192.168.1.255 scope global eth0",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "9090"


def test_check_site_a_site_on_the_examples_own_lan_passes_once_it_uncomments_lan_cidr(
    site: Site,
) -> None:
    # A box really on 192.168.1.0/24 with no embedding server: the shipped example needs one
    # edit to be right, and that edit has to be possible (it used to need none and block anyway).
    site.write(EXAMPLE.read_text(encoding="utf-8").replace("# LAN_CIDR=", "LAN_CIDR="))
    result = site.call(
        'check_site; echo "[$LAN_CIDR]"',
        STUB_IP_LINES="2: eth0    inet 192.168.1.50/24 brd 192.168.1.255 scope global eth0",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "[192.168.1.0/24]"


def test_check_site_unedited_example_says_to_remove_the_hash_and_write_a_network(
    site: Site,
) -> None:
    shutil.copy(EXAMPLE, site.site_file)
    result = site.call("check_site")
    assert result.returncode == 1
    assert "every setting in it is commented out" in result.stderr
    assert "remove the leading '# ' from the LAN_CIDR line" in result.stderr
    assert "192.168.1.0/24 shown is only an example" in result.stderr


# -- the installer must not lock the operator out ---------------------------------------------

#: Run in a dry run: what the real run would stop for, one line each, then what stayed loaded.
DRY = 'DRY_RUN=true; check_site; printf "%s\\n" "${BLOCKERS[@]}"; echo "[$LAN_CIDR]"'


def _ssh(client: str) -> str:
    """An SSH_CONNECTION as sshd sets it: client address and port, then server address and port."""
    return f"{client} 50000 100.64.1.50 22"


def test_check_site_ssh_client_outside_the_lan_blocks_and_names_the_client(site: Site) -> None:
    site.write(VALID)
    result = site.call("check_site", SSH_CONNECTION=_ssh("100.64.2.9"))
    assert result.returncode == 1
    assert "100.64.2.9" in result.stderr
    assert "outside LAN_CIDR" in result.stderr
    assert "no new connection" in result.stderr, "it says what the firewall would do"


def test_check_site_ssh_client_outside_the_lan_is_a_dry_run_blocker_and_stops_nothing_else(
    site: Site,
) -> None:
    site.write(VALID)
    result = site.call(DRY, SSH_CONNECTION=_ssh("8.8.4.4"))
    assert result.returncode == 0, result.stderr
    assert "connected over SSH from 8.8.4.4" in result.stdout
    assert f"[{LAN}]" in result.stdout, "the values stay loaded, so later steps are still checked"


@pytest.mark.parametrize("client", ["100.64.1.0", "100.64.1.9", "100.64.1.255"])
def test_check_site_ssh_client_inside_the_lan_passes(site: Site, client: str) -> None:
    site.write(VALID)
    result = site.call("check_site", SSH_CONNECTION=_ssh(client))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("client", ["100.64.0.255", "100.64.2.0"])
def test_check_site_ssh_client_one_address_outside_the_lan_blocks(site: Site, client: str) -> None:
    site.write(VALID)
    assert site.call("check_site", SSH_CONNECTION=_ssh(client)).returncode == 1


def test_check_site_ssh_client_over_ipv6_is_skipped_not_guessed_at(site: Site) -> None:
    site.write(VALID)
    result = site.call("check_site", SSH_CONNECTION="2001:db8::9 50000 2001:db8::1 22")
    assert result.returncode == 0, result.stderr


def test_check_site_without_an_ssh_connection_skips_the_client_check(site: Site) -> None:
    site.write(VALID)
    result = site.call("check_site")
    assert result.returncode == 0, result.stderr
    assert "SSH" not in result.stderr + result.stdout


def test_check_site_an_ssh_connection_that_is_not_an_address_is_skipped_and_never_run(
    site: Site,
) -> None:
    marker = site.root / "pwned"
    site.write(VALID)
    result = site.call("check_site", SSH_CONNECTION=f"$(touch {marker}) 1 2 3")
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_check_site_a_box_with_no_address_inside_the_lan_blocks_and_lists_its_addresses(
    site: Site,
) -> None:
    site.write(VALID)
    other = "2: eth0    inet 100.64.9.50/24 brd 100.64.9.255 scope global eth0"
    result = site.call("check_site", STUB_IP_LINES=other)
    assert result.returncode == 1
    assert "no global IPv4 address inside LAN_CIDR" in result.stderr
    assert "100.64.9.50" in result.stderr


def test_check_site_a_box_with_no_address_inside_the_lan_is_a_dry_run_blocker(site: Site) -> None:
    site.write(VALID)
    result = site.call(DRY, STUB_IP_LINES="2: eth0    inet 100.64.9.50/24 scope global eth0")
    assert result.returncode == 0, result.stderr
    assert "no global IPv4 address inside LAN_CIDR" in result.stdout


def test_check_site_a_box_with_no_global_address_at_all_blocks_and_says_none(site: Site) -> None:
    site.write(VALID)
    result = site.call("check_site", STUB_IP_LINES="")
    assert result.returncode == 1
    assert "addresses: none" in result.stderr


def test_check_site_a_box_with_one_address_inside_the_lan_among_others_passes(site: Site) -> None:
    site.write(VALID)
    lines = (
        "2: bridge0    inet 100.65.0.1/16 brd 100.65.255.255 scope global bridge0\n"
        "3: eth1    inet 100.64.1.50/24 brd 100.64.1.255 scope global eth1"
    )
    result = site.call("check_site", STUB_IP_LINES=lines)
    assert result.returncode == 0, result.stderr


def test_check_site_a_box_address_just_outside_the_lan_does_not_count(site: Site) -> None:
    site.write(VALID)
    just_outside = "2: eth0    inet 100.64.2.1/24 brd 100.64.2.255 scope global eth0"
    assert site.call("check_site", STUB_IP_LINES=just_outside).returncode == 1


def test_check_site_when_ip_cannot_be_run_the_box_check_is_skipped_with_a_warning(
    site: Site,
) -> None:
    site.write(VALID)
    failing = shutil.which("false")
    assert failing is not None
    result = site.call("check_site", IP_BIN=failing)
    assert result.returncode == 0, result.stderr
    assert "could not list this box's addresses" in result.stdout


# -- the site file decides what root admits, so it must be root's and nobody else's ---------


def test_check_site_a_site_file_owned_by_another_user_blocks_and_says_who(site: Site) -> None:
    site.write(VALID)
    stranger = str(os.getuid() + 1)
    result = site.call("check_site", SITE_OWNER_UID=stranger)
    assert result.returncode == 1
    assert "cannot be trusted" in result.stderr
    assert f"belongs to uid {os.getuid()}, not to root (uid {stranger})" in result.stderr


def test_check_site_a_site_file_owned_by_another_user_is_a_dry_run_blocker_and_loads_nothing(
    site: Site,
) -> None:
    site.write(VALID)
    result = site.call(DRY, SITE_OWNER_UID=str(os.getuid() + 1))
    assert result.returncode == 0, result.stderr
    assert "cannot be trusted" in result.stdout
    assert "[]" in result.stdout, "an untrusted file's values are never loaded"


@pytest.mark.parametrize("mode", [0o664, 0o646, 0o666, 0o620, 0o602])
def test_check_site_a_site_file_writable_by_group_or_others_blocks(site: Site, mode: int) -> None:
    site.write(VALID)
    site.site_file.chmod(mode)
    result = site.call("check_site")
    assert result.returncode == 1
    assert "cannot be trusted" in result.stderr
    assert f"its mode is {mode:o}" in result.stderr


def test_check_site_a_site_file_writable_by_others_is_a_dry_run_blocker_and_loads_nothing(
    site: Site,
) -> None:
    site.write(VALID)
    site.site_file.chmod(0o666)
    result = site.call(DRY)
    assert result.returncode == 0, result.stderr
    assert "cannot be trusted" in result.stdout
    assert "[]" in result.stdout


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o600, 0o444])
def test_check_site_a_site_file_only_its_owner_can_write_passes(site: Site, mode: int) -> None:
    site.write(VALID)
    site.site_file.chmod(mode)
    result = site.call("check_site")
    assert result.returncode == 0, result.stderr


def test_check_site_a_site_file_that_is_a_symbolic_link_blocks(site: Site) -> None:
    real = site.root / "elsewhere.env"
    real.write_text(VALID, encoding="utf-8")
    site.site_file.symlink_to(real)
    result = site.call("check_site")
    assert result.returncode == 1
    assert "symbolic link" in result.stderr


def test_check_site_every_way_the_file_is_untrusted_is_listed_in_one_pass(site: Site) -> None:
    site.write(VALID)
    site.site_file.chmod(0o666)
    result = site.call("check_site", SITE_OWNER_UID=str(os.getuid() + 1))
    assert "belongs to uid" in result.stderr
    assert "its mode is 666" in result.stderr


def test_check_site_the_example_is_installed_owned_by_root_with_mode_0644(site: Site) -> None:
    # Not root here, so the install is only planned (a dry run); what matters is the command line,
    # and that the owner it names is root unless a test seam says otherwise.
    result = site.call("DRY_RUN=true; check_site", SITE_OWNER_UID="", SITE_OWNER_GID="")
    assert result.returncode == 0, result.stderr
    assert "install -m 0644 -o 0 -g 0 " in result.stdout


def test_check_site_the_example_is_installed_for_the_owner_the_seam_names(site: Site) -> None:
    result = site.call("check_site")
    assert result.returncode == 1
    assert site.site_file.stat().st_uid == os.getuid()
    assert stat.S_IMODE(site.site_file.stat().st_mode) == 0o644


# -- the resolver follows the site's LAN ------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "network", "inside"),
    [
        ("198.51.100.1", "198.51.100.0/24", True),
        ("198.51.100.255", "198.51.100.0/24", True),
        ("198.51.101.1", "198.51.100.0/24", False),
        ("203.0.113.5", "203.0.113.5/32", True),
        ("203.0.113.6", "203.0.113.5/32", False),
        ("192.0.2.130", "192.0.2.128/25", True),
        ("192.0.2.127", "192.0.2.128/25", False),
        ("192.0.2.200", "192.0.0.0/16", True),
        ("193.0.2.200", "192.0.0.0/16", False),
        ("1.1.1.1", "198.51.100.0/24", False),
    ],
)
def test_ip_in_cidr_decides_membership_by_prefix(
    site: Site, address: str, network: str, inside: bool
) -> None:
    result = site.call(f"ip_in_cidr {address} {network} && echo yes || echo no")
    assert result.stdout.strip() == ("yes" if inside else "no")


def _check_nameserver(site: Site, resolv: str, lan: str = LAN) -> subprocess.CompletedProcess[str]:
    site.resolv.write_text(resolv, encoding="utf-8")
    return site.call(f"LAN_CIDR={lan}; check_nameserver")


def test_check_nameserver_resolver_inside_the_site_lan_passes(site: Site) -> None:
    result = _check_nameserver(site, "nameserver 100.64.1.1\nnameserver 127.0.0.53\n")
    assert result.returncode == 0, result.stderr


def test_check_nameserver_loopback_resolver_passes_whatever_the_lan(site: Site) -> None:
    assert _check_nameserver(site, "nameserver 127.0.0.1\nnameserver ::1\n").returncode == 0


def test_check_nameserver_public_resolver_blocks_and_names_the_site_lan(site: Site) -> None:
    result = _check_nameserver(site, "nameserver 100.64.1.1\nnameserver 1.1.1.1\n")
    assert result.returncode == 1
    assert "1.1.1.1" in result.stderr
    assert LAN in result.stderr
    assert "100.64.1.1" not in result.stderr, "the resolver that is fine is not blamed"


def test_check_nameserver_resolver_on_another_private_network_blocks(site: Site) -> None:
    # The preflight used to accept one fixed /24; a resolver on a neighbouring network is
    # outside this site's LAN and DNS to it would be refused by the firewall.
    result = _check_nameserver(site, "nameserver 192.0.2.1\n")
    assert result.returncode == 1
    assert "192.0.2.1" in result.stderr


def test_check_nameserver_without_a_usable_lan_checks_nothing_instead_of_blaming_the_resolver(
    site: Site,
) -> None:
    site.resolv.write_text("nameserver 1.1.1.1\n", encoding="utf-8")
    result = site.call("DRY_RUN=true; check_nameserver")
    assert result.returncode == 0, result.stderr
    assert "not checked" in result.stdout


def test_check_nameserver_hint_tells_the_operator_to_use_their_own_resolver(site: Site) -> None:
    result = _check_nameserver(site, "nameserver 1.1.1.1\n")
    assert "supersede domain-name-servers <your LAN resolver>;" in result.stderr
    assert "--nameserver <your LAN resolver>" in result.stderr


# -- the firewall stage: dry run, and when the installed file counts as unchanged ------------


def _dry_run_firewall(site: Site) -> subprocess.CompletedProcess[str]:
    return site.call(
        f"LAN_CIDR={LAN}; EMBEDDING_HOST={HOST}; EMBEDDING_PORT={PORT}; DRY_RUN=true; "
        "install_firewall"
    )


def test_install_firewall_dry_run_shows_the_rendered_defines_and_writes_nothing(
    site: Site,
) -> None:
    result = _dry_run_firewall(site)
    assert result.returncode == 0, result.stderr
    assert f"define LAN_V4 = {LAN}" in result.stdout
    assert f"define EMBEDDING_V4 = {HOST}" in result.stdout
    assert f"define EMBEDDING_PORT = {PORT}" in result.stdout
    assert "would install the rendered ruleset" in result.stdout
    assert not site.installed.exists()


def test_install_firewall_dry_run_with_an_installed_file_equal_to_the_rendering_is_unchanged(
    site: Site,
) -> None:
    site.write(VALID)
    site.installed.write_text(site.run("--show-firewall").stdout, encoding="utf-8")
    result = _dry_run_firewall(site)
    assert result.returncode == 0, result.stderr
    assert "matches the rendered template" in result.stdout
    assert "would install the rendered ruleset" not in result.stdout


def test_install_firewall_dry_run_with_an_installed_file_for_another_lan_is_changed(
    site: Site,
) -> None:
    site.write(_with("LAN_CIDR", "100.64.2.0/24"))
    site.installed.write_text(site.run("--show-firewall").stdout, encoding="utf-8")
    result = _dry_run_firewall(site)
    assert "would install the rendered ruleset" in result.stdout


def test_install_firewall_dry_run_with_a_blocked_site_file_renders_nothing(site: Site) -> None:
    result = site.call("DRY_RUN=true; install_firewall")
    assert result.returncode == 0, result.stderr
    assert "once that file is valid" in result.stdout


# -- the firewall stage for real, against stubbed nft and systemctl ---------------------------

#: Stubs first on PATH. `id` finds every user (the box's users do not exist here), `nft` and
#: `systemctl` log their arguments (and `nft -c` can be told to fail), and `install` drops the
#: -o/-g that only root may use and runs the real one.
_STUBS = {
    "id": "#!/bin/sh\nexit 0\n",
    "nft": (
        '#!/bin/sh\necho "nft $*" >> "$STUB_LOG"\n'
        'if [ "$1" = "-c" ] && [ "${STUB_NFT_CHECK_EXIT:-0}" != 0 ]; then\n'
        # A failing check can also make a directory unwritable, so that undoing the install fails.
        '  [ -n "${STUB_NFT_LOCK_DIR:-}" ] && chmod 555 "$STUB_NFT_LOCK_DIR"\n'
        '  echo "Error: stub refuses the ruleset" >&2; exit "$STUB_NFT_CHECK_EXIT"\nfi\nexit 0\n'
    ),
    "systemctl": '#!/bin/sh\necho "systemctl $*" >> "$STUB_LOG"\nexit 0\n',
    "install": (
        "#!/usr/bin/env bash\nargs=()\n"
        "while [[ $# -gt 0 ]]; do\n"
        '  case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac\n'
        f'done\nexec {INSTALL_BIN} "${{args[@]}}"\n'
    ),
}


def _apply_firewall(
    site: Site, *, loaded: bool = True, port: str = PORT, embedding: bool = True, **env: str
) -> subprocess.CompletedProcess[str]:
    """Run the real (not dry) firewall stage with the stubs in front of PATH.

    `embedding=False` loads a site with no embedding server (only LAN_CIDR is set).
    """
    stubs = site.root / "stubs"
    stubs.mkdir(exist_ok=True)
    for name, body in _STUBS.items():
        (stubs / name).write_text(body, encoding="utf-8")
        (stubs / name).chmod(0o755)
    settings = f"LAN_CIDR={LAN}; "
    if embedding:
        settings += f"EMBEDDING_HOST={HOST}; EMBEDDING_PORT={port}; "
    if not loaded:
        settings = ""
    return site.call(
        f"{settings}install_firewall", PATH=f"{stubs}:{os.environ['PATH']}", **env
    )


def test_install_firewall_installs_the_rendering_checks_the_whole_ruleset_then_applies_it(
    site: Site,
) -> None:
    site.write(VALID)
    result = _apply_firewall(site)
    assert result.returncode == 0, result.stderr
    assert site.installed.read_text(encoding="utf-8") == site.run("--show-firewall").stdout
    assert f'include "{site.installed}"' in site.nft_conf.read_text(encoding="utf-8").splitlines()
    assert site.calls() == [
        f"nft -c -f {site.nft_conf}",
        f"nft -f {site.nft_conf}",
        "systemctl enable --quiet nftables.service",
    ]
    assert stat.S_IMODE(site.installed.stat().st_mode) == 0o644


def _backup(site: Site) -> Path:
    """Where the installer keeps the file it replaces until the check has passed."""
    return site.installed.with_name(site.installed.name + ".previous")


def test_install_firewall_check_fails_with_a_previous_file_restores_it_byte_for_byte(
    site: Site,
) -> None:
    site.write(VALID)
    assert _apply_firewall(site).returncode == 0
    before = site.installed.read_bytes()
    mtime = site.installed.stat().st_mtime_ns
    conf_before = site.nft_conf.read_bytes()
    applied = len(site.calls())

    result = _apply_firewall(site, port="9090", STUB_NFT_CHECK_EXIT="1")

    assert result.returncode == 1
    assert "does not check clean" in result.stderr
    assert "NOTHING WAS APPLIED" in result.stderr
    assert "put the previous" in result.stdout
    assert site.installed.read_bytes() == before, "the previous file is back, byte for byte"
    assert site.installed.stat().st_mtime_ns == mtime
    assert stat.S_IMODE(site.installed.stat().st_mode) == 0o644
    assert not _backup(site).exists()
    assert site.nft_conf.read_bytes() == conf_before
    assert site.calls()[applied:] == [f"nft -c -f {site.nft_conf}"], "nft -f was never called"


def test_install_firewall_check_fails_with_no_previous_file_removes_the_new_one(
    site: Site,
) -> None:
    site.write(VALID)
    result = _apply_firewall(site, STUB_NFT_CHECK_EXIT="1")
    assert result.returncode == 1
    assert "does not check clean" in result.stderr
    assert "removed the new" in result.stdout
    assert not site.installed.exists()
    assert not _backup(site).exists()
    assert site.calls() == [f"nft -c -f {site.nft_conf}"], "check ran, apply did not"


def test_install_firewall_check_fails_with_no_previous_file_leaves_no_dangling_include(
    site: Site,
) -> None:
    # The next boot must load exactly what it loaded before: a configuration that includes a file
    # that is no longer there would fail the whole load.
    original = "flush ruleset\ntable inet other { }\n"
    site.nft_conf.write_text(original, encoding="utf-8")
    site.write(VALID)
    result = _apply_firewall(site, STUB_NFT_CHECK_EXIT="1")
    assert result.returncode == 1
    assert not site.installed.exists()
    assert site.nft_conf.read_text(encoding="utf-8") == original
    assert "COULD NOT" not in result.stdout


def test_install_firewall_check_fails_with_no_previous_file_removes_the_configuration_it_created(
    site: Site,
) -> None:
    site.write(VALID)
    assert not site.nft_conf.exists()
    assert _apply_firewall(site, STUB_NFT_CHECK_EXIT="1").returncode == 1
    assert not site.nft_conf.exists()


def test_install_firewall_check_passes_leaves_the_new_file_in_place_and_no_backup(
    site: Site,
) -> None:
    site.write(VALID)
    assert _apply_firewall(site).returncode == 0
    result = _apply_firewall(site, port="9090")
    assert result.returncode == 0, result.stderr
    assert _defines(site.installed.read_text(encoding="utf-8"))["EMBEDDING_PORT"] == "9090"
    assert not _backup(site).exists(), "the backup is removed once the check has passed"
    assert site.calls()[-2:] == [
        f"nft -f {site.nft_conf}",
        "systemctl enable --quiet nftables.service",
    ]


def test_install_firewall_check_fails_on_an_unchanged_file_keeps_it(site: Site) -> None:
    site.write(VALID)
    assert _apply_firewall(site).returncode == 0
    before = site.installed.read_bytes()
    result = _apply_firewall(site, STUB_NFT_CHECK_EXIT="1")
    assert result.returncode == 1
    assert site.installed.read_bytes() == before, "nothing this run wrote, so nothing is undone"
    assert not _backup(site).exists()
    assert "include" in site.nft_conf.read_text(encoding="utf-8")


def test_install_firewall_a_backup_left_by_an_interrupted_run_stops_it_before_any_change(
    site: Site,
) -> None:
    site.write(VALID)
    assert _apply_firewall(site).returncode == 0
    _backup(site).write_text("an older ruleset\n", encoding="utf-8")
    before = site.installed.read_bytes()
    applied = len(site.calls())
    result = _apply_firewall(site, port="9090")
    assert result.returncode == 1
    assert "an earlier run stopped" in result.stderr
    assert site.installed.read_bytes() == before
    assert _backup(site).read_text(encoding="utf-8") == "an older ruleset\n"
    assert len(site.calls()) == applied


def test_install_firewall_a_backup_left_by_an_interrupted_run_is_a_dry_run_blocker(
    site: Site,
) -> None:
    _backup(site).write_text("an older ruleset\n", encoding="utf-8")
    result = site.call(
        f"LAN_CIDR={LAN}; EMBEDDING_HOST={HOST}; EMBEDDING_PORT={PORT}; DRY_RUN=true; "
        'install_firewall; printf "%s\\n" "${BLOCKERS[@]}"'
    )
    assert result.returncode == 0, result.stderr
    assert "an earlier run stopped" in result.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write into a read-only directory")
def test_install_firewall_failed_restore_is_loud_and_keeps_the_backup(site: Site) -> None:
    site.write(VALID)
    assert _apply_firewall(site).returncode == 0
    directory = site.installed.parent
    try:
        result = _apply_firewall(
            site, port="9090", STUB_NFT_CHECK_EXIT="1", STUB_NFT_LOCK_DIR=str(directory)
        )
    finally:
        directory.chmod(0o755)
    assert result.returncode == 1
    assert "COULD NOT put the previous" in result.stdout
    assert "BEFORE the next boot" in result.stdout
    assert "could not put back everything" in result.stderr
    assert _backup(site).exists(), "the good file is still saved for a hand repair"


def test_install_firewall_second_run_leaves_the_installed_file_alone_and_says_so(
    site: Site,
) -> None:
    site.write(VALID)
    assert _apply_firewall(site).returncode == 0
    first = site.installed.stat().st_mtime_ns
    result = _apply_firewall(site)
    assert result.returncode == 0, result.stderr
    assert "matches the template rendered" in result.stdout
    assert site.installed.stat().st_mtime_ns == first, "an identical rendering is not rewritten"
    assert site.nft_conf.read_text(encoding="utf-8").count("include") == 1


def test_install_firewall_a_changed_site_value_replaces_the_installed_file(site: Site) -> None:
    site.write(VALID)
    assert _apply_firewall(site).returncode == 0
    result = _apply_firewall(site, port="9090")
    assert result.returncode == 0, result.stderr
    assert _defines(site.installed.read_text(encoding="utf-8"))["EMBEDDING_PORT"] == "9090"
    assert "installed" in result.stdout


def test_install_firewall_dry_run_without_an_embedding_server_says_there_is_no_rule_for_one(
    site: Site,
) -> None:
    result = site.call(f"LAN_CIDR={LAN}; DRY_RUN=true; install_firewall")
    assert result.returncode == 0, result.stderr
    assert f"define LAN_V4 = {LAN}" in result.stdout
    assert "EMBEDDING_V4" not in result.stdout
    assert "no embedding server" in result.stdout
    assert "would install the rendered ruleset" in result.stdout
    assert not site.installed.exists()


def test_install_firewall_without_an_embedding_server_installs_a_ruleset_with_no_rule_for_one(
    site: Site,
) -> None:
    site.write(NO_EMBEDDING)
    result = _apply_firewall(site, embedding=False)
    assert result.returncode == 0, result.stderr
    installed = site.installed.read_text(encoding="utf-8")
    assert installed == site.run("--show-firewall").stdout
    assert "EMBEDDING" not in installed
    assert site.calls() == [
        f"nft -c -f {site.nft_conf}",
        f"nft -f {site.nft_conf}",
        "systemctl enable --quiet nftables.service",
    ]


def test_install_firewall_a_site_that_drops_its_embedding_server_replaces_the_installed_file(
    site: Site,
) -> None:
    site.write(VALID)
    assert _apply_firewall(site).returncode == 0
    assert "EMBEDDING_V4" in site.installed.read_text(encoding="utf-8")
    site.write(NO_EMBEDDING)
    result = _apply_firewall(site, embedding=False)
    assert result.returncode == 0, result.stderr
    assert "EMBEDDING" not in site.installed.read_text(encoding="utf-8")
    assert not _backup(site).exists()


def test_install_firewall_without_loaded_site_settings_builds_nothing_and_dies(site: Site) -> None:
    result = _apply_firewall(site, loaded=False)
    assert result.returncode == 1
    assert "site settings are not loaded" in result.stderr
    assert not site.installed.exists()
    assert site.calls() == []


def test_install_firewall_template_with_an_unknown_placeholder_installs_nothing(
    site: Site,
) -> None:
    template = site.app / "deploy" / "nftables" / TEMPLATE.name
    template.write_text(
        template.read_text(encoding="utf-8").replace("@EMBEDDING_PORT@", "@EMBEDDING_PORTS@"),
        encoding="utf-8",
    )
    result = _apply_firewall(site)
    assert result.returncode == 1
    assert "still holds the placeholder @EMBEDDING_PORTS@" in result.stderr
    assert not site.installed.exists()
    assert site.calls() == []


# -- usage ------------------------------------------------------------------------------------


def test_installer_help_lists_show_firewall_and_the_site_file() -> None:
    assert BASH is not None
    result = subprocess.run(  # noqa: S603 - fixed argv, a repo script
        [BASH, str(INSTALL_SH), "--help"], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0
    assert "--show-firewall" in result.stdout
    assert "site.env" in result.stdout
