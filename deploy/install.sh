#!/usr/bin/env bash
#
# Provision the mirror's LXC (the Wikipedia mirror) from a bare Debian container to a
# running API, a running Qdrant, a monthly build timer and a nightly update timer. Idempotent:
# run it again after a failure, a restore or a template change and it repairs what is missing without touching what
# is already right. `--dry-run` shows every step and what would stop the real run.
#
# Run as root on the box, either from a copy (`scp deploy/install.sh deploy/site.env.example
# root@<mirror-host>:/root/`) or from the checkout (`/opt/wiki-mirror/deploy/install.sh`). The
# steps, in order, each logged and skipped when already done:
#
#   1. Preflight      Debian 12/13, x86_64, disk, the site file, resolver, the NAS mount, the
#                     deploy key. Everything that can stop the run is checked BEFORE the
#                     20-minute Python build, not after it.
#   2. Packages       git, nftables, sqlite3, zstd, and (only if Python must be built) the
#                     compiler and -dev libraries.
#   3. Users, dirs    wikimirror (API), wikimirror-sync (build jobs), qdrant; the directory contract.
#   4. Deploy key     verified registered on GitHub (never generated here; see below).
#   5. Python         the newest interpreter >= 3.12, else CPython 3.13 built from source.
#   6. Checkout       /opt/wiki-mirror, root-owned, via the read-only deploy key;
#                     a venv with `pip install "."` (not editable), every dependency
#                     held to the version in deploy/constraints.txt and installed from
#                     wheels only (no compiler on the box beyond a one-off Python build).
#   7. Qdrant         the pinned release, checksum-verified, and its config.
#   8. Config         mirror.yaml, mirror.env with a generated API key, and an empty
#                     clients.json (the other agents' key hashes; `wiki-mirror key add`).
#   9. Units          the six systemd units; qdrant + API + the monthly build timer + the nightly
#                     update timer (02:20 local time) enabled.
#  10. Firewall       nftables per-user egress, rendered from the template and the site file,
#                     checked with `nft -c` before it is applied. Transactional: the file it
#                     replaces is kept until the whole ruleset checks clean, and put back (or the
#                     new one removed) if it does not, so the next boot never loads a ruleset
#                     that failed its check.
#  11. Operator CLI   /usr/local/bin/wiki-mirror.
#  12. Verify         Qdrant and the API answer; the firewall admits and refuses what it should.
#
# **Per-site settings.** The firewall names the LAN, the embedding server and its port, which
# differ at every site, so they are not in the repository: they live in /etc/wiki-mirror/site.env
# (root:root 0644, no secrets; the template is deploy/site.env.example). A first run installs the
# example there and STOPS; so does a file that still equals the example or holds anything but
# three valid literals. The file is parsed as data, never sourced, and each value must match a
# strict IPv4 / CIDR / port pattern before it is substituted into a ruleset that loads as root.
# The LAN must be /8 or longer and inside RFC 1918 or 100.64.0.0/10, since SSH and the API are
# admitted from everything in it. The file must be root's and not writable by anyone else, and
# the installer refuses a LAN that this box, or the SSH session running it, is not inside (the
# firewall would lock the next connection out). See check_site.
# This script is self-contained for that (it runs before any checkout exists, from a lone copy),
# and `--show-firewall` prints the rendered ruleset without changing anything: update.sh uses
# it to tell whether a commit changed the firewall.
#
# **Deliberately not automated**, because each is a human decision or a secret that must not
# pass through a script's arguments:
#
#   * Registering the deploy key on GitHub. The script prints the exact commands and stops.
#   * The values in site.env. The example is installed; choosing your network is yours.
#   * The Wikimedia Enterprise sign-in (/etc/wiki-mirror/enterprise.env). docs/OPERATIONS.md
#     has the `read -r -s` command; the installer only fixes its owner and mode.
#   * The first build. `wiki-mirror build-now` is the operator's call: it downloads about 150 GB and runs
#     for hours.
#
# What it will not do: overwrite an existing secret, clobber edited config (use
# --adopt-config to replace it from the templates, with a dated backup), or continue past a
# stage it could not verify. An installer that half-succeeds silently is worse than one that
# stops and says where. Secrets (the API key, the Enterprise password) are never printed.
#
# Pinned downloads (versions AND sha256 are constants below; the source of each is stated at
# the constant). A download whose checksum does not match is deleted and the run stops.
#
# See docs/DESIGN.md sections 4-6 and docs/OPERATIONS.md.

set -euo pipefail

REPO_URL="${REPO_URL:-git@github.com:SFN-ALPHA/sfn-wiki-mirror.git}"
APP_DIR="${APP_DIR:-/opt/wiki-mirror}"
DATA_DIR="${DATA_DIR:-/var/lib/wiki-mirror}"
CONF_DIR="${CONF_DIR:-/etc/wiki-mirror}"
SNAPSHOT_MOUNT="${SNAPSHOT_MOUNT:-/mnt/wikipedia}"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-$SNAPSHOT_MOUNT/snapshots}"
DEPLOY_KEY="${DEPLOY_KEY:-/root/.ssh/wiki_mirror_deploy}"
KNOWN_HOSTS="${KNOWN_HOSTS:-/root/.ssh/known_hosts}"
BRANCH="${BRANCH:-main}"
API_PORT=8300
QDRANT_PORT=6333
# Where the rendered ruleset is installed, the nftables configuration that includes it, and the
# resolver list the preflight reads. Variables so a test can point them at a scratch directory;
# on the box they are always these defaults.
NFT_INSTALLED="${NFT_INSTALLED:-/etc/nftables.d/wiki-mirror.nft}"
NFT_CONF="${NFT_CONF:-/etc/nftables.conf}"
RESOLV_CONF="${RESOLV_CONF:-/etc/resolv.conf}"
# `ip`, which lists the box's own addresses for the site check (a test points it at a stub), and
# the uid and gid the site file must belong to: root, always, on a real box. Tests, which cannot
# be root, set them to their own ids; nothing else does.
IP_BIN="${IP_BIN:-ip}"
SITE_OWNER_UID="${SITE_OWNER_UID:-0}"
SITE_OWNER_GID="${SITE_OWNER_GID:-0}"

# The lowest-privilege identities the services run as (docs/DESIGN.md section 6).
API_USER=wikimirror
SYNC_USER=wikimirror-sync
QDRANT_USER=qdrant
SHARED_GROUP=wikimirror

# CPython, built from source because Debian 12 ships 3.11 and the project needs >= 3.12
# (installed as /usr/local/bin/python3.13). 3.13.16 was released 2026-09-30,
# 16 days before this was pinned (2026-10-16): the newest 3.13 patch release that is at least
# two weeks old. The sha256 is the value python.org's release page lists for the "XZ
# compressed source tarball" (https://www.python.org/downloads/release/python-31316/), and
# was independently recomputed from a download of the file on 2026-10-16 - the two agree.
# Bump both together, from that page, never from a mirror.
PYTHON_VERSION="3.13.16"
PYTHON_SHA256="f4b1bfb3c79b5bb11b8d228a12504163b4c0dab4d679828d8f5f26b6cb6ab35d"

# Qdrant v1.12.4 (published 2024-11-18). The release
# assets carry no publisher checksum, so this sha256 is of
# `qdrant-x86_64-unknown-linux-gnu.tar.gz` downloaded from
# https://github.com/qdrant/qdrant/releases/download/v1.12.4/ over TLS on 2026-10-16 (the
# gnu build). It pins the artifact against a later swap of
# the release asset; it does not prove the original was untampered.
QDRANT_VERSION="1.12.4"
QDRANT_SHA256="8b65c40a2b3ded3881813dda48be34050c558c20b0deaa02e7a51d7888ca1913"

# Interpreter: empty means "find or build one" (resolve_python). PYTHON=python3.13 forces one.
PYTHON="${PYTHON:-}"
NEED_PYTHON_BUILD=false
NEED_VENV_PACKAGE=false

DRY_RUN=false
ADOPT_CONFIG=false
SHOW_FIREWALL=false

# The validated site settings (read_site_file). Empty until the site file has been read and passed;
# a dry run that found the file unusable leaves them empty and says so.
LAN_CIDR=""
EMBEDDING_HOST=""
EMBEDDING_PORT=""
declare -a SITE_PROBLEMS=()

# Collected as the run proceeds and printed at the end. An installer that scrolls three
# hundred lines past and exits 0 has told the operator nothing they can act on.
declare -a DONE_STEPS=()
declare -a REMAINING=()
declare -a BLOCKERS=()

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
    YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi

step()  { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$RESET"; }
info()  { printf '    %s\n' "$*"; }
skip()  { printf '    %salready done: %s%s\n' "$DIM" "$*" "$RESET"; }
warn()  { printf '    %s! %s%s\n' "$YELLOW" "$*" "$RESET"; }
ok()    { printf '    %s+ %s%s\n' "$GREEN" "$*" "$RESET"; DONE_STEPS+=("$*"); }
die()   { printf '\n%serror: %s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }

# Stop the real run; in a dry run record the problem and carry on, so one dry run lists
# everything that would stop the real one instead of only the first.
blocker() {
    if $DRY_RUN; then
        warn "dry run: the real run would STOP here:"
        printf '%s\n' "$1" | sed 's/^/        /'
        BLOCKERS+=("${1%%$'\n'*}")
        return 0
    fi
    die "$1"
}

run() {
    if $DRY_RUN; then
        printf '    %swould run: %s%s\n' "$DIM" "$*" "$RESET"
        return 0
    fi
    "$@"
}

usage() {
    cat <<'USAGE'
Provision the mirror's container (Wikipedia mirror). Idempotent - safe to re-run.

  --dry-run         show every step and what would stop the real run; touch nothing
  --adopt-config    replace an edited mirror.yaml / qdrant.yaml with the checkout's
                    template (the old file is kept as <name>.bak-<timestamp>)
  --show-firewall   print the nftables ruleset the checkout's template and the site file
                    produce, and exit; needs no root and changes nothing
  --repo URL        clone from somewhere other than the default origin
  -h, --help        this

Per-site settings (the LAN, the embedding server) are read from CONF_DIR/site.env, by default
/etc/wiki-mirror/site.env; a first run installs deploy/site.env.example there and stops so you
can edit it.

Environment overrides: REPO_URL APP_DIR DATA_DIR CONF_DIR SNAPSHOT_MOUNT DEPLOY_KEY BRANCH
PYTHON (force an interpreter, e.g. PYTHON=python3.13).
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        --adopt-config) ADOPT_CONFIG=true ;;
        --show-firewall) SHOW_FIREWALL=true ;;
        --repo) REPO_URL="${2:?--repo needs a URL}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option $1 (try --help)" ;;
    esac
    shift
done

SRC_DIR="$APP_DIR/deploy"
VENV="$APP_DIR/.venv"
# The tested set of dependency versions. `--only-binary=:all:` makes a pin with no wheel for
# this Python fail the install instead of compiling on the box; http-ece (pure Python, published
# only as an sdist) is the one package built locally. See the header of constraints.txt.
CONSTRAINTS="$SRC_DIR/constraints.txt"
PIP_PINNED=(-c "$CONSTRAINTS" --only-binary=:all: --no-binary=http-ece)
ENTERPRISE_ENV="$CONF_DIR/enterprise.env"
MIRROR_ENV="$CONF_DIR/mirror.env"
CLIENTS_FILE="$CONF_DIR/clients.json"
SITE_FILE="$CONF_DIR/site.env"
# Where this script lives, so a lone copy (scp'd before any checkout exists) can still find the
# site.env.example scp'd beside it.
SCRIPT_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
APT_UPDATED=false

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

apt_update_once() {
    $APT_UPDATED && return 0
    run apt-get update -qq
    APT_UPDATED=true
}

package_installed() {
    [[ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)" == "install ok installed" ]]
}

# Make a directory exist with exactly this owner, group and mode, saying so only when it had
# to change something (a re-run of a configured host stays quiet). chmod is a separate step
# because GNU install's -m drops setgid on directories in some coreutils versions.
ensure_dir() {
    local mode="$1" owner="$2" group="$3" path="$4"
    if [[ -d "$path" && "$(stat -c '%a %U:%G' "$path")" == "${mode#0} $owner:$group" ]]; then
        return 0
    fi
    run install -d -o "$owner" -g "$group" "$path"
    run chmod "$mode" "$path"
    info "$path  ($owner:$group $mode)"
}

# Install a template as a config file the operator may edit. Absent: installed. Identical:
# skipped. Edited: left alone with a pointer to the diff, unless --adopt-config, which keeps a
# dated backup. Never silently clobbers, never silently drifts.
install_template() {
    local src="$1" dst="$2" mode="$3" owner="$4" group="$5" backup
    if [[ ! -e "$dst" ]]; then
        run install -m "$mode" -o "$owner" -g "$group" "$src" "$dst"
        ok "installed $dst"
    elif $DRY_RUN && [[ ! -f "$src" ]]; then
        skip "$dst (template not readable until the checkout exists)"
    elif cmp -s "$src" "$dst"; then
        skip "$dst matches the template"
        run chown "$owner:$group" "$dst"
        run chmod "$mode" "$dst"
    elif $ADOPT_CONFIG; then
        backup="$dst.bak-$(date +%Y%m%d-%H%M%S)"
        run cp -p "$dst" "$backup"
        run install -m "$mode" -o "$owner" -g "$group" "$src" "$dst"
        ok "replaced $dst from the template (old copy: $backup)"
    else
        warn "$dst differs from the template and was left alone"
        info "see the difference:  diff -u $dst $src"
        info "take the template:   $0 --adopt-config"
        run chown "$owner:$group" "$dst"
        run chmod "$mode" "$dst"
    fi
}

# Run curl for a mirror API call with the bearer key supplied on stdin as a curl config,
# so the key never appears in a process listing or in this script's output.
api_get() {
    local path="$1" key
    key="$(sed -n 's/^WIKI_MIRROR_API_KEY=//p' "$MIRROR_ENV" | head -1)"
    [[ -n "$key" ]] || return 1
    curl -fsS --max-time 5 -K <(printf 'header = "Authorization: Bearer %s"\n' "$key") \
        "http://127.0.0.1:$API_PORT$path"
}

# ---------------------------------------------------------------------------
# Site settings: /etc/wiki-mirror/site.env
# ---------------------------------------------------------------------------
#
# The LAN, the embedding server's address and its port differ at every site and reach the
# firewall. They are read here as DATA and checked against strict literal patterns, because the
# firewall stage substitutes them into a ruleset that nft loads as root: a value that is not
# one of these literals must never get that far (docs/OPERATIONS.md "The site file").

# An IPv4 octet: 0, or 1-255 with no leading zero (some parsers read a leading zero as octal).
# The match allows up to 999; valid_ipv4 checks the range.
OCTET_RE='(0|[1-9][0-9]{0,2})'
IPV4_RE="^${OCTET_RE}\.${OCTET_RE}\.${OCTET_RE}\.${OCTET_RE}\$"
# Prefix length 0-32 without a leading zero. Octets are groups 1-4, the length group 5.
CIDR_RE="^${OCTET_RE}\.${OCTET_RE}\.${OCTET_RE}\.${OCTET_RE}/([0-9]|[12][0-9]|3[0-2])\$"

# True when $1 is a dotted-quad IPv4 address, every octet 0-255, no leading zeros.
valid_ipv4() {
    local octet
    [[ "$1" =~ $IPV4_RE ]] || return 1
    for octet in "${BASH_REMATCH[@]:1:4}"; do
        (( octet <= 255 )) || return 1
    done
}

# The 32-bit value of a dotted quad that valid_ipv4 has already passed.
ipv4_to_int() {
    local a b c d
    IFS=. read -r a b c d <<< "$1"
    printf '%s' "$(( (a << 24) | (b << 16) | (c << 8) | d ))"
}

# The netmask for a prefix length 0-32, as a 32-bit value.
cidr_mask() {
    printf '%s' "$(( $1 == 0 ? 0 : (0xFFFFFFFF << (32 - $1)) & 0xFFFFFFFF ))"
}

# True when IPv4 address $1 lies inside CIDR $2; both must already have passed validation.
ip_in_cidr() {
    local mask
    mask="$(cidr_mask "${2#*/}")"
    (( ($(ipv4_to_int "$1") & mask) == ($(ipv4_to_int "${2%/*}") & mask) ))
}

# The only networks a LAN can be: RFC 1918 private space and RFC 6598 shared address space (what
# carrier-grade NAT and Tailscale-style overlays use). LAN_CIDR must lie wholly inside one of
# them. The firewall admits SSH and the API port from everything in LAN_CIDR, and that is only
# sound for an address block nobody on the internet can use as a source address.
PRIVATE_NETWORKS=(10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10)

# True when CIDR $1 (already validated, no host bits) lies wholly inside a PRIVATE_NETWORKS entry:
# its prefix is at least as long as that network's, and its address is inside it.
inside_private_network() {
    local network
    for network in "${PRIVATE_NETWORKS[@]}"; do
        if (( ${1#*/} >= ${network#*/} )) && ip_in_cidr "${1%/*}" "$network"; then
            return 0
        fi
    done
    return 1
}

# Print why $1 is not an acceptable LAN_CIDR, or nothing when it is. Beyond the pattern:
#   * a prefix shorter than /8 is refused: no LAN has 16 million addresses, and 0.0.0.0/0 or
#     128.0.0.0/1 would admit the whole internet, or half of it, to SSH and the API;
#   * an address with host bits set is refused, because how nft reads 192.168.1.5/24 is not worth
#     betting a default-deny firewall on;
#   * a network that is not private (PRIVATE_NETWORKS) is refused, with the reason, because a
#     public block as "the LAN" is the same hole at a smaller size.
lan_cidr_problem() {
    local value="$1" address bits network
    if [[ ! "$value" =~ $CIDR_RE ]] || ! valid_ipv4 "${value%/*}"; then
        printf 'LAN_CIDR must be an IPv4 network such as 192.168.1.0/24, got %q' "${value:0:40}"
        return 0
    fi
    address="${value%/*}"
    bits="${value#*/}"
    if (( bits < 8 )); then
        printf 'LAN_CIDR %s is wider than /8, which is no LAN: SSH and the API are admitted from everything in it, so a /0 opens them to the whole internet and a /1 to half of it. Give your own network, such as 192.168.1.0/24' "$value"
        return 0
    fi
    network=$(( $(ipv4_to_int "$address") & $(cidr_mask "$bits") ))
    if (( network != $(ipv4_to_int "$address") )); then
        printf 'LAN_CIDR %s has host bits set; the network address is %d.%d.%d.%d/%s' "$value" \
            $(( network >> 24 )) $(( (network >> 16) & 255 )) $(( (network >> 8) & 255 )) \
            $(( network & 255 )) "$bits"
        return 0
    fi
    if ! inside_private_network "$value"; then
        printf 'LAN_CIDR %s is not a private network. SSH and the API are admitted from everything in it, so it must be your own LAN: inside 10.0.0.0/8, 172.16.0.0/12 or 192.168.0.0/16 (private addresses), or 100.64.0.0/10 (shared address space, used by carrier-grade NAT and Tailscale-style overlays)' "$value"
    fi
}

# Print why $1 is not an acceptable EMBEDDING_HOST, or nothing. An address, never a name: a
# name would be resolved by nft when the ruleset loads, through whatever DNS exists by then.
embedding_host_problem() {
    valid_ipv4 "$1" || printf 'EMBEDDING_HOST must be an IPv4 address such as 192.168.1.60, got %q' "${1:0:40}"
}

# Print why $1 is not an acceptable EMBEDDING_PORT, or nothing: digits only, 1-65535.
embedding_port_problem() {
    if [[ ! "$1" =~ ^[1-9][0-9]{0,4}$ ]] || (( $1 > 65535 )); then
        printf 'EMBEDDING_PORT must be a TCP port number, 1-65535, got %q' "${1:0:40}"
    fi
}

# Read $SITE_FILE into LAN_CIDR, EMBEDDING_HOST and EMBEDDING_PORT. Every problem is collected
# in SITE_PROBLEMS rather than stopping at the first, so one pass tells the operator all there is
# to fix; if there is any, the three variables are left empty so nothing half-valid can be used.
#
# The file is never sourced or evaluated. A line is accepted only if it is blank, a comment, or
# exactly one of the three known keys, `=`, and a value with no whitespace; the value must then
# pass the strict check for its key. No quoting, expansion or continuation exists: a quote is
# simply a character that fails the pattern.
read_site_file() {
    local line key value problem lineno=0 seen=" "
    SITE_PROBLEMS=()
    LAN_CIDR=""
    EMBEDDING_HOST=""
    EMBEDDING_PORT=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        lineno=$((lineno + 1))
        [[ "$line" =~ ^[[:space:]]*(#.*)?$ ]] && continue
        if [[ ! "$line" =~ ^(LAN_CIDR|EMBEDDING_HOST|EMBEDDING_PORT)=([^[:space:]]*)$ ]]; then
            SITE_PROBLEMS+=("line $lineno is not LAN_CIDR=, EMBEDDING_HOST= or EMBEDDING_PORT= followed by a value with no spaces or quotes: $(printf '%q' "${line:0:60}")")
            continue
        fi
        key="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        if [[ "$seen" == *" $key "* ]]; then
            SITE_PROBLEMS+=("line $lineno: $key is set more than once")
            continue
        fi
        seen+="$key "
        case "$key" in
            LAN_CIDR) problem="$(lan_cidr_problem "$value")" ;;
            EMBEDDING_HOST) problem="$(embedding_host_problem "$value")" ;;
            EMBEDDING_PORT) problem="$(embedding_port_problem "$value")" ;;
        esac
        if [[ -n "$problem" ]]; then
            SITE_PROBLEMS+=("line $lineno: $problem")
        else
            printf -v "$key" '%s' "$value"
        fi
    done < "$SITE_FILE"
    for key in LAN_CIDR EMBEDDING_HOST EMBEDDING_PORT; do
        [[ "$seen" == *" $key "* ]] || SITE_PROBLEMS+=("$key is missing")
    done
    if [[ ${#SITE_PROBLEMS[@]} -gt 0 ]]; then
        LAN_CIDR=""
        EMBEDDING_HOST=""
        EMBEDDING_PORT=""
    fi
}

# The problems read_site_file found, one per line, indented for a message.
site_problem_lines() {
    local problem
    for problem in "${SITE_PROBLEMS[@]}"; do
        printf '      - %s\n' "$problem"
    done
}

# Where the example site file is, if anywhere: in the checkout (every run after the first), or
# beside this script (a lone copy scp'd to the box before any checkout exists).
find_site_example() {
    local candidate
    for candidate in "$SRC_DIR/site.env.example" "$SCRIPT_DIR/site.env.example"; do
        if [[ -f "$candidate" ]]; then
            printf '%s' "$candidate"
            return 0
        fi
    done
}

# Print, one per line and indented for a message, why $SITE_FILE cannot be trusted to decide
# what a firewall applied as root admits; nothing when it can. Anyone who can write the file can
# open SSH and the API to a network of their choosing, so it must be a regular file (a link's
# target is not checked), owned by root ($SITE_OWNER_UID) and not writable by group or others.
site_file_trust_problems() {
    local owner mode
    if [[ -L "$SITE_FILE" ]]; then
        printf '      - it is a symbolic link; the file itself must be here, as the target is not checked\n'
        return 0
    fi
    read -r owner mode < <(stat -c '%u %a' "$SITE_FILE" 2>/dev/null) || true
    if [[ -z "${owner:-}" ]]; then
        printf '      - its owner and mode cannot be read\n'
        return 0
    fi
    if [[ "$owner" != "$SITE_OWNER_UID" ]]; then
        printf '      - it belongs to uid %s, not to root (uid %s)\n' "$owner" "$SITE_OWNER_UID"
    fi
    if (( 8#$mode & 8#022 )); then
        printf '      - its mode is %s: its group or everyone can write it\n' "$mode"
    fi
}

# Two ways a well-formed LAN_CIDR still locks the operator out; the firewall would turn each into
# a refused connection with no error to read. Both are caught before anything is applied.
#   (a) the SSH session running this installer comes from outside LAN_CIDR: it would survive
#       (established flows are accepted first) but no new one from there could start;
#   (b) the box has no global IPv4 address inside LAN_CIDR, so no client on that network could
#       reach it, and its DNS, NTP and DHCP rules would name a network it is not on.
# An IPv6 or absent SSH client is skipped, not guessed at. If `ip` cannot be run, (b) is skipped
# with a warning: the check guards against a mistake, and a missing tool is not one.
check_lan_reach() {
    local client addresses address listed="" found=false
    if [[ -n "${SSH_CONNECTION:-}" ]]; then
        client="${SSH_CONNECTION%% *}"
        if valid_ipv4 "$client" && ! ip_in_cidr "$client" "$LAN_CIDR"; then
            blocker "you are connected over SSH from $client, which is outside LAN_CIDR ($LAN_CIDR).
    The firewall admits new SSH only from LAN_CIDR, so once it is applied this session would
    survive but no new connection from $client could start. Correct LAN_CIDR in $SITE_FILE,
    or connect from inside it, and run this installer again. Nothing has been changed."
        fi
    fi

    if ! addresses="$("$IP_BIN" -4 -o addr show scope global 2>/dev/null)"; then
        warn "could not list this box's addresses ($IP_BIN failed), so LAN_CIDR was not compared with them"
        return 0
    fi
    while read -r address; do
        [[ -n "$address" ]] || continue
        listed+=" $address"
        if valid_ipv4 "$address" && ip_in_cidr "$address" "$LAN_CIDR"; then
            found=true
        fi
    done < <(awk '{for (i = 1; i < NF; i++) if ($i == "inet") { split($(i + 1), a, "/"); print a[1] }}' <<< "$addresses")
    if ! $found; then
        blocker "this box has no global IPv4 address inside LAN_CIDR ($LAN_CIDR); its global IPv4 addresses:${listed:- none}.
    The firewall admits clients, DNS, NTP and DHCP only on that network, so a box that is not
    on it would be unreachable. Either LAN_CIDR is wrong, or this box's address is not assigned
    yet (a DHCP lease, a bridge that is not up). Fix that and run this installer again.
    Nothing has been changed."
    fi
}

# Preflight: the site file exists, is not the unedited example, belongs to root and cannot be
# written by anyone else, holds three valid values, and a LAN_CIDR that this box and the SSH
# session running the installer are actually inside. On a first run it installs the example and
# stops, because the firewall built from example values would lock the real LAN out. Run before
# the long steps for the usual reason: an operator who has to edit a file should learn that now,
# not after a compile.
check_site() {
    local example trust
    example="$(find_site_example)"

    if [[ ! -e "$SITE_FILE" ]]; then
        if [[ -z "$example" ]]; then
            blocker "no $SITE_FILE, and site.env.example is neither beside this script nor in $SRC_DIR.
    Create $SITE_FILE (root:root 0644) with these three lines, using your own values:

      LAN_CIDR=192.168.1.0/24
      EMBEDDING_HOST=192.168.1.60
      EMBEDDING_PORT=8080

    or copy deploy/site.env.example from the repository next to this installer, and run it
    again. Nothing has been changed."
            return 0
        fi
        # CONF_DIR may not exist yet (setup_users_and_dirs has not run); it fixes the group.
        [[ -d "$CONF_DIR" ]] || run install -d -m 0751 "$CONF_DIR"
        run install -m 0644 -o "$SITE_OWNER_UID" -g "$SITE_OWNER_GID" "$example" "$SITE_FILE"
        ok "installed $SITE_FILE from $example"
        blocker "no site file: the shipped example $($DRY_RUN && echo 'would be' || echo 'was') installed as $SITE_FILE and must be edited first.
    It holds the three values that differ between sites and that the firewall is built from:
      LAN_CIDR         your IPv4 network, e.g. 192.168.1.0/24 (SSH, the API and DNS use it)
      EMBEDDING_HOST   IPv4 address of the embedding server the build user may reach
      EMBEDDING_PORT   that server's TCP port
    Edit all three, then run this installer again. Nothing else has been changed."
        return 0
    fi

    if [[ -n "$example" ]] && cmp -s "$example" "$SITE_FILE"; then
        blocker "$SITE_FILE is still the shipped example, which holds example values for someone else's network.
    Edit LAN_CIDR, EMBEDDING_HOST and EMBEDDING_PORT for this site, then run this installer
    again. No firewall is built from example values. Nothing has been changed."
        return 0
    fi

    if [[ ! -f "$SITE_FILE" || ! -r "$SITE_FILE" ]]; then
        blocker "$SITE_FILE is not a readable file; replace it with one (see site.env.example)."
        return 0
    fi
    trust="$(site_file_trust_problems)"
    if [[ -n "$trust" ]]; then
        blocker "$SITE_FILE cannot be trusted, so no firewall is built from it:
$trust
    It decides what a firewall applied as root admits; anyone who can write it can open SSH and
    the API to a network of their choosing. Fix it with: chown root:root $SITE_FILE && chmod 0644 $SITE_FILE
    (check its contents first), then run this installer again. Nothing has been changed."
        return 0
    fi
    read_site_file
    if [[ ${#SITE_PROBLEMS[@]} -gt 0 ]]; then
        blocker "$SITE_FILE has ${#SITE_PROBLEMS[@]} problem(s), so no firewall is built from it:
$(site_problem_lines)
    Fix them (site.env.example shows the format) and run this installer again. Nothing has
    been changed."
        return 0
    fi
    info "site file $SITE_FILE: LAN $LAN_CIDR, embedding server $EMBEDDING_HOST port $EMBEDDING_PORT"
    check_lan_reach
}

# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------

python_is_new_enough() {
    # Asked of the interpreter itself rather than parsed from its name, because `python3`
    # is 3.11 on one Debian release and 3.13 on the next.
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null
}

python_can_make_venvs() {
    # Actually built, not merely importable: Debian splits `ensurepip` into the venv package,
    # so `-m venv --help` succeeds on an interpreter that cannot create one, and the failure
    # surfaces later as a confusing pip error.
    local probe status
    probe="$(mktemp -d)"
    "$1" -m venv "$probe/v" >/dev/null 2>&1 && [[ -x "$probe/v/bin/python" ]]
    status=$?
    rm -rf "$probe"
    return $status
}

resolve_python() {
    local candidate
    if [[ -n "$PYTHON" ]]; then
        command -v "$PYTHON" >/dev/null || die "PYTHON=$PYTHON is not on PATH"
        python_is_new_enough "$PYTHON" || die "$PYTHON is $("$PYTHON" -V 2>&1); this needs 3.12 or newer"
        info "using $PYTHON ($("$PYTHON" -V 2>&1), set explicitly)"
        return
    fi
    for candidate in /usr/local/bin/python3.14 /usr/local/bin/python3.13 python3.14 python3.13 \
                     python3.12 python3; do
        command -v "$candidate" >/dev/null || continue
        python_is_new_enough "$candidate" || continue
        if python_can_make_venvs "$candidate"; then
            PYTHON="$(command -v "$candidate")"
            info "using $PYTHON ($("$PYTHON" -V 2>&1))"
            return
        fi
        if [[ "$candidate" == python3 ]]; then
            # Debian 13: python3 is 3.13 but ships its venv support separately.
            PYTHON="$(command -v python3)"
            NEED_VENV_PACKAGE=true
            info "using $PYTHON ($("$PYTHON" -V 2>&1)); python3-venv will be installed"
            return
        fi
    done
    NEED_PYTHON_BUILD=true
    PYTHON=/usr/local/bin/python3.13
    info "no usable Python >= 3.12 here (Debian 12 ships 3.11); CPython $PYTHON_VERSION will be built from source"
}

check_nameserver() {
    # DNS is allowed to the LAN only (nftables). A public resolver in resolv.conf would make
    # lookups fail or stall once the firewall is applied. Two writers can put one there:
    # Proxmox (from the container config, on every start) and dhclient (from the DHCP lease; a
    # router often hands out its own address followed by public resolvers), so the hint names
    # both fixes. "The LAN" is the site file's LAN_CIDR; loopback is always fine.
    local ns bad=""
    if [[ -z "$LAN_CIDR" ]]; then
        info "resolver not checked: there is no usable LAN_CIDR yet (see the site file above)"
        return 0
    fi
    while read -r ns; do
        case "$ns" in
            127.*|::1) continue ;;
        esac
        if valid_ipv4 "$ns" && ip_in_cidr "$ns" "$LAN_CIDR"; then
            continue
        fi
        bad="$bad $ns"
    done < <(awk '$1 == "nameserver" {print $2}' "$RESOLV_CONF")
    # A blocker, not a warning: a container is often created with public resolvers (1.1.1.1,
    # 8.8.8.8), and continuing would apply a firewall that breaks every lookup mid-install.
    if [[ -n "$bad" ]]; then
        blocker "resolv.conf names a resolver outside the LAN ($LAN_CIDR):$bad
the firewall allows DNS only to $LAN_CIDR. If the interface uses DHCP, pin the resolver:
  echo 'supersede domain-name-servers <your LAN resolver>;' >> /etc/dhcp/dhclient.conf
otherwise, on the Proxmox host:
  pct set <ctid> --nameserver <your LAN resolver>
then, on the Proxmox host: pct reboot <ctid>
(If the resolver IS on your LAN, LAN_CIDR in $SITE_FILE is wrong.)"
    fi
}

check_deploy_key() {
    if [[ -f "$DEPLOY_KEY" ]]; then
        return 0
    fi
    blocker "no deploy key at $DEPLOY_KEY.
    Create one, register it on GitHub, then run this installer again. Nothing else has
    been changed. As root on this box:

      install -d -m 0700 /root/.ssh
      ssh-keygen -t ed25519 -N '' -C \"wiki-mirror-\$(hostname)\" -f $DEPLOY_KEY
      cat ${DEPLOY_KEY}.pub

    GitHub -> sfn-wiki-mirror -> Settings -> Deploy keys -> Add deploy key. Paste the line
    printed by 'cat', leave \"Allow write access\" UNCHECKED (this host only ever reads).
    GitHub refuses a key already used on any repository, so this must be a new key, not
    any other deploy key on this host."
}

preflight() {
    step "Preflight"
    [[ $EUID -eq 0 ]] || die "run as root; the service accounts are created below"
    command -v apt-get >/dev/null || die "this expects Debian (no apt-get found)"

    local os_id os_version
    os_id="$(. /etc/os-release && printf '%s' "${ID:-}")"
    os_version="$(. /etc/os-release && printf '%s' "${VERSION_ID:-}")"
    case "$os_id:$os_version" in
        debian:12|debian:13) info "Debian $os_version" ;;
        *) die "this is $os_id $os_version; the mirror's container is Debian 12 or 13" ;;
    esac

    # The pinned Qdrant checksum is for the x86_64 build.
    [[ "$(uname -m)" == x86_64 ]] || die "unsupported architecture $(uname -m); the pinned Qdrant build is x86_64"

    local free_gb
    free_gb="$(df --output=avail -BG / | tail -1 | tr -dc '0-9')"
    if [[ ${free_gb:-0} -lt 100 ]]; then
        warn "only ${free_gb}G free on / - the article store and the Qdrant index need about 110 GB,"
        warn "and about 150 GB at peak while a refresh builds beside the serving index (docs/DESIGN.md section 9)"
    else
        info "${free_gb}G free on /"
    fi

    check_site
    check_nameserver

    # The NAS holds the downloaded snapshots. An unprivileged container cannot mount NFS, so
    # it is mounted on the Proxmox host and bind-mounted in; this checks only that it arrived.
    if mountpoint -q "$SNAPSHOT_MOUNT"; then
        info "$SNAPSHOT_MOUNT is a mount point"
    else
        blocker "$SNAPSHOT_MOUNT is not a mount point.
    The snapshot dataset must be bind-mounted into this container from the Proxmox host
    (an unprivileged container cannot mount NFS itself). On the Proxmox HOST, once:

      mkdir -p /mnt/wikipedia-mirror
      echo '<nas-ip>:/<export-path>/wikipedia-mirror /mnt/wikipedia-mirror nfs defaults,_netdev 0 0' >> /etc/fstab
      mount /mnt/wikipedia-mirror
      pct set <ctid> -mp1 /mnt/wikipedia-mirror,mp=$SNAPSHOT_MOUNT,backup=0
      pct reboot <ctid>

    Then run this installer again. (Writes from the container arrive on the NAS as uid
    100000+N; the NFS share must map every user to the dataset's owner, \"Mapall\" on TrueNAS -
    docs/OPERATIONS.md \"The NAS mount\".)"
    fi

    # Present is enough here; owner and mode are fixed once the users exist. Absent is a
    # warning, not a stop: everything but the first download works without it.
    if [[ -f "$ENTERPRISE_ENV" ]]; then
        info "$ENTERPRISE_ENV exists"
    else
        warn "no $ENTERPRISE_ENV - the download stage needs it (docs/OPERATIONS.md: the read -r -s command)"
        REMAINING+=("Create $ENTERPRISE_ENV (Enterprise username and password) before the first build")
    fi

    # Before the long steps: a missing or unregistered deploy key is the likeliest reason for
    # a first run to stop, and it must stop before a 20-minute compile, not after.
    check_deploy_key
    resolve_python

    ok "preflight"
}

# ---------------------------------------------------------------------------
# 2. Packages
# ---------------------------------------------------------------------------

#: Always needed. `openssh-client` for the deploy key and ssh-keyscan, `util-linux` (runuser)
#: is essential on Debian. `openssl` is not needed: the API key comes from Python's secrets.
BASE_PACKAGES=(git curl ca-certificates openssh-client nftables zstd sqlite3 xz-utils)

#: Needed only to compile CPython with ssl, sqlite3, lzma, bz2, ctypes, uuid, zlib and
#: readline. tk, ncurses-dev and gdbm are left out: nothing here uses tkinter/curses/dbm.
BUILD_PACKAGES=(build-essential pkg-config libssl-dev zlib1g-dev libbz2-dev libsqlite3-dev
                libffi-dev liblzma-dev uuid-dev libreadline-dev)

install_packages() {
    step "System packages"
    local wanted=("${BASE_PACKAGES[@]}") missing=() package
    $NEED_PYTHON_BUILD && wanted+=("${BUILD_PACKAGES[@]}")
    $NEED_VENV_PACKAGE && wanted+=(python3-venv)

    for package in "${wanted[@]}"; do
        package_installed "$package" || missing+=("$package")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        skip "packages (${#wanted[@]} checked)"
        return
    fi
    apt_update_once
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
    ok "packages: ${missing[*]}"
}

# ---------------------------------------------------------------------------
# 3. Users and directories
# ---------------------------------------------------------------------------

ensure_group() {
    getent group "$1" >/dev/null && return 0
    run groupadd --system "$1"
}

# A login-less system account. The two wikimirror accounts share the primary group `wikimirror`, which
# is what lets the API read what the build user writes without either being root.
ensure_user() {
    local user="$1" group="$2"
    if id "$user" >/dev/null 2>&1; then
        if [[ "$(id -gn "$user")" != "$group" ]]; then
            run usermod -g "$group" "$user"
            ok "primary group of $user set to $group"
        fi
        return 0
    fi
    run useradd --system --gid "$group" --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin "$user"
    ok "created system user $user"
}

setup_users_and_dirs() {
    step "Users and directories"
    ensure_group "$SHARED_GROUP"
    ensure_user "$API_USER" "$SHARED_GROUP"
    ensure_user "$SYNC_USER" "$SHARED_GROUP"
    # Qdrant's own user and group: a compromise of a parse job (wikimirror-sync) must not be able
    # to write the index directly, and Qdrant gets no network egress at all.
    if ! id "$QDRANT_USER" >/dev/null 2>&1; then
        run useradd --system --user-group --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin "$QDRANT_USER"
        ok "created system user $QDRANT_USER"
    fi

    # Local state: written by wikimirror-sync, read (never written) by the API through group
    # wikimirror. Mode 2751: the leading 2 (setgid) keeps new files in group wikimirror, and the final
    # 1 is search-only for others - it exists so `qdrant` can reach its own directory below,
    # nothing else, since every child has its own tighter mode.
    ensure_dir 2751 "$SYNC_USER" "$SHARED_GROUP" "$DATA_DIR"
    ensure_dir 2750 "$SYNC_USER" "$SHARED_GROUP" "$DATA_DIR/articles"
    ensure_dir 2750 "$SYNC_USER" "$SHARED_GROUP" "$DATA_DIR/state"
    local sub
    for sub in "" /storage /snapshots /tmp; do
        ensure_dir 0700 "$QDRANT_USER" "$QDRANT_USER" "$DATA_DIR/qdrant$sub"
    done

    # Configuration: world-searchable (so qdrant can read qdrant.yaml), every file inside has
    # its own mode. See install_config.
    ensure_dir 0751 root "$SHARED_GROUP" "$CONF_DIR"

    # The Enterprise sign-in belongs to the build user alone. It is root:root 0600 when the operator
    # writes it; ownership moves here, now that the account exists. Contents are checked
    # for shape, never printed.
    if [[ -f "$ENTERPRISE_ENV" ]]; then
        run chown "$SYNC_USER:$SHARED_GROUP" "$ENTERPRISE_ENV"
        run chmod 0600 "$ENTERPRISE_ENV"
        if grep -q '^WME_USERNAME=.' "$ENTERPRISE_ENV" && grep -q '^WME_PASSWORD=.' "$ENTERPRISE_ENV"; then
            info "$ENTERPRISE_ENV: owner $SYNC_USER, mode 0600, both WME_ lines present"
        else
            warn "$ENTERPRISE_ENV lacks a WME_USERNAME= or WME_PASSWORD= line"
            REMAINING+=("Fix $ENTERPRISE_ENV: it needs WME_USERNAME= and WME_PASSWORD= lines")
        fi
    fi

    # The snapshot directory on the NAS is created BY wikimirror-sync rather than chowned to it:
    # NFS with the share's users mapped (e.g. TrueNAS Mapall) refuses chown from a non-root client,
    # and the identity that must write there is exactly the one that creates it. A failure
    # here is almost always the share's UID mapping, so say so.
    if $DRY_RUN || ! id "$SYNC_USER" >/dev/null 2>&1; then
        info "would create $SNAPSHOT_DIR as $SYNC_USER and test a write"
    elif mountpoint -q "$SNAPSHOT_MOUNT"; then
        # shellcheck disable=SC2016  # $1 and $f are for the inner sh, not this shell
        if ! runuser -u "$SYNC_USER" -- mkdir -p "$SNAPSHOT_DIR" 2>/dev/null \
           || ! runuser -u "$SYNC_USER" -- sh -c 'f="$(mktemp -p "$1" .writetest.XXXXXX)" && rm -f "$f"' _ "$SNAPSHOT_DIR" 2>/dev/null; then
            blocker "$SYNC_USER cannot write to $SNAPSHOT_DIR.
    The container's UIDs reach the NAS as 100000+N (unprivileged container). On the NFS share
    for the snapshot dataset, map every user and group to the dataset's owner (on TrueNAS: set
    Mapall user and group) so that every write lands as that user, and restrict the share's
    authorized hosts to the Proxmox host. (Not Maproot: it maps only requests from uid 0, and
    these arrive as uid 100000+N.) Then re-run this installer (docs/OPERATIONS.md)."
        else
            info "$SNAPSHOT_DIR is writable by $SYNC_USER"
        fi
    fi
    ok "users and directories"
}

# ---------------------------------------------------------------------------
# 4. Deploy key: registered, and the host key pinned
# ---------------------------------------------------------------------------

verify_deploy_key() {
    step "Deploy key"
    if $DRY_RUN && [[ ! -f "$DEPLOY_KEY" ]]; then
        info "would verify $DEPLOY_KEY is registered on GitHub"
        return 0
    fi

    if ! grep -q '^github.com ' "$KNOWN_HOSTS" 2>/dev/null; then
        run install -d -m 0700 /root/.ssh
        # Trust on first use. After this the key is pinned and
        # every later fetch uses StrictHostKeyChecking=yes.
        $DRY_RUN || ssh-keyscan -t rsa,ed25519 github.com >> "$KNOWN_HOSTS" 2>/dev/null
        ok "github host key pinned"
    fi
    $DRY_RUN && { info "would test the key against git@github.com"; return 0; }

    # Captured then matched: `ssh -T` exits 1 even on success ("does not provide shell
    # access"), which `set -o pipefail` would turn into a failure.
    local reply
    reply="$(ssh -i "$DEPLOY_KEY" -o IdentitiesOnly=yes -o BatchMode=yes \
        -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$KNOWN_HOSTS" \
        -o ConnectTimeout=15 -T git@github.com 2>&1 || true)"
    if [[ "$reply" == *"successfully authenticated"* ]]; then
        ok "deploy key is registered on GitHub"
        return 0
    fi
    blocker "GitHub does not accept $DEPLOY_KEY.
    Add ${DEPLOY_KEY}.pub as a deploy key on sfn-wiki-mirror (Settings -> Deploy keys, write
    access UNCHECKED) and run this installer again. ssh said: ${reply##*$'\n'}"
}

# ---------------------------------------------------------------------------
# 5. Python
# ---------------------------------------------------------------------------

build_python() {
    step "Python $PYTHON_VERSION (from source)"
    if ! $NEED_PYTHON_BUILD; then skip "interpreter $PYTHON"; return; fi

    local tarball="Python-${PYTHON_VERSION}.tar.xz" url work log status=0
    url="https://www.python.org/ftp/python/${PYTHON_VERSION}/${tarball}"
    log=/var/log/wiki-mirror-python-build.log

    # --enable-optimizations --with-lto: profile-guided and link-time optimisation, about 10-15%
    # faster Python. It costs a one-time 15-25 minutes on 6 cores; the parse stage is hours of
    # CPU-bound Python on every monthly build, so the compile pays for itself on the first.
    # `make altinstall` (never `install`) so it cannot take over /usr/bin/python3 or the name
    # `python3` that Debian's own tools use.
    info "building to /usr/local (15-25 minutes; log: $log)"
    if $DRY_RUN; then
        info "would download $url, verify sha256 $PYTHON_SHA256, ./configure --enable-optimizations --with-lto, make -j\$(nproc), make altinstall"
        return 0
    fi

    work="$(mktemp -d /var/tmp/wiki-mirror-python-build.XXXXXX)"
    : > "$log"
    curl -fsSL --retry 3 -o "$work/$tarball" "$url" \
        || { rm -rf "$work"; die "could not download $url"; }
    if ! printf '%s  %s\n' "$PYTHON_SHA256" "$work/$tarball" | sha256sum -c --status; then
        local actual
        actual="$(sha256sum "$work/$tarball" | awk '{print $1}')"
        rm -rf "$work"
        die "checksum mismatch for $tarball: expected $PYTHON_SHA256, got $actual. The file was deleted;
    do not continue until python.org's release page and this script agree."
    fi

    # An && chain, not a subshell with set -e: errexit is disabled inside a command list that
    # is itself the left side of `||`, so a failing step would not stop the next one.
    ( cd "$work" && tar -xJf "$tarball" && cd "Python-${PYTHON_VERSION}" \
        && ./configure --prefix=/usr/local --enable-optimizations --with-lto --with-ensurepip=install \
        && make -j"$(nproc)" && make altinstall ) >> "$log" 2>&1 || status=$?
    rm -rf "$work"
    if [[ $status -ne 0 ]]; then
        tail -n 40 "$log" >&2
        die "the Python build failed (exit $status); the full log is $log"
    fi

    # The modules the service needs, imported rather than assumed: a CPython built without a
    # -dev library compiles cleanly and only fails at import, at 03:00 on a build night.
    local module
    for module in ssl sqlite3 lzma bz2 ctypes uuid zlib hashlib; do
        "$PYTHON" -I -c "import $module" 2>/dev/null \
            || die "the new $PYTHON cannot import '$module'; a -dev library was missing at build time (see $log)"
    done
    "$PYTHON" -I -c "import readline" 2>/dev/null || warn "python has no readline (only the interactive prompt is affected)"
    ok "built $("$PYTHON" -V 2>&1) at $PYTHON"
}

# ---------------------------------------------------------------------------
# 6. Checkout and virtualenv
# ---------------------------------------------------------------------------

clone_checkout() {
    step "Repository"
    local ssh_cmd="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=$KNOWN_HOSTS -o StrictHostKeyChecking=yes"

    if [[ -e "$APP_DIR/.git" ]]; then
        skip "checkout at $APP_DIR"
    else
        if [[ -e "$APP_DIR" && -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]]; then
            blocker "$APP_DIR exists, is not empty, and is not a git checkout; move it aside and re-run"
        fi
        run env GIT_SSH_COMMAND="$ssh_cmd" git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR" \
            || blocker "clone failed; the key authenticated but $REPO_URL did not resolve"
        ok "cloned $REPO_URL ($BRANCH)"
    fi

    # Set on the checkout so every later fetch - update.sh's - uses the deploy key with the
    # pinned host key, without an environment variable anyone has to remember. The checkout
    # stays root-owned and read-only to the service accounts: the services run its code, so
    # a service account that could write it could run its own code as another service.
    if [[ -e "$APP_DIR/.git" ]]; then
        run git -C "$APP_DIR" config core.sshCommand "$ssh_cmd"
    fi
}

setup_venv() {
    step "Virtualenv"
    if $DRY_RUN && [[ ! -e "$APP_DIR/.git" ]]; then
        info "would create $VENV and pip install \"${APP_DIR}\" (non-editable)"
        return 0
    fi
    python_can_make_venvs "$PYTHON" || $DRY_RUN || die "$PYTHON cannot create a virtualenv"
    [[ -f "$CONSTRAINTS" ]] || $DRY_RUN || die "missing $CONSTRAINTS - the checkout is incomplete"

    if [[ -x "$VENV/bin/python" ]] && python_is_new_enough "$VENV/bin/python"; then
        skip "venv at $VENV"
    else
        run "$PYTHON" -m venv "$VENV"
        ok "created $VENV"
    fi

    # Stamped with the commit it was installed from: the package version is fixed at 0.1.0,
    # so pip cannot tell two commits apart, and "already satisfied" would otherwise leave
    # yesterday's code installed. Non-editable on purpose - the services run an installed copy,
    # so a stray edit in the checkout cannot change what is running.
    local head stamp="$VENV/.installed-commit"
    head="$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
    if [[ -f "$stamp" && "$(cat "$stamp")" == "$head" ]]; then
        skip "dependencies and package at ${head:0:12}"
    else
        info "installing dependencies at their pinned versions (a few minutes)"
        run "$VENV/bin/pip" install --quiet --upgrade pip
        run "$VENV/bin/pip" install --quiet "${PIP_PINNED[@]}" "${APP_DIR}" \
            || die "dependency install failed. PyPI unreachable (root holds tcp 443 in the firewall)? Or a version pinned in $CONSTRAINTS has no wheel for $("$PYTHON" -V 2>&1) - the pip error above names it."
        run "$VENV/bin/pip" install --quiet "${PIP_PINNED[@]}" --force-reinstall --no-deps "$APP_DIR" \
            || die "package install failed"
        $DRY_RUN || printf '%s\n' "$head" > "$stamp"
        ok "package at ${head:0:12} and its dependencies"
    fi
    # Run from / so nothing in a working directory can shadow an import, and -I for the same.
    $DRY_RUN || (cd / && "$VENV/bin/python" -I -c 'import wiki_mirror.text.sparse, selectolax, zstandard, qdrant_client, fastapi') \
        || die "the venv cannot import the mirror's dependencies"
}

# ---------------------------------------------------------------------------
# 7. Qdrant
# ---------------------------------------------------------------------------

install_qdrant() {
    step "Qdrant $QDRANT_VERSION"
    local current=""
    if [[ -x /usr/local/bin/qdrant ]]; then
        current="$(/usr/local/bin/qdrant --version 2>/dev/null | head -1 || true)"
        if [[ "$current" == *"$QDRANT_VERSION"* ]]; then
            skip "qdrant ($current)"
            return
        fi
        # A different version may not read this storage format. Not replaced silently, and not
        # a failure: an operator who upgraded on purpose should not be undone by a re-run.
        warn "/usr/local/bin/qdrant is '${current:-unknown}', not $QDRANT_VERSION; left alone (remove it to reinstall)"
        return
    fi

    local tarball="qdrant-x86_64-unknown-linux-gnu.tar.gz" url work
    url="https://github.com/qdrant/qdrant/releases/download/v${QDRANT_VERSION}/${tarball}"
    if $DRY_RUN; then
        info "would download $url, verify sha256 $QDRANT_SHA256, install to /usr/local/bin/qdrant"
        return 0
    fi
    work="$(mktemp -d)"
    curl -fsSL --retry 3 -o "$work/$tarball" "$url" || { rm -rf "$work"; die "could not download $url"; }
    if ! printf '%s  %s\n' "$QDRANT_SHA256" "$work/$tarball" | sha256sum -c --status; then
        local actual
        actual="$(sha256sum "$work/$tarball" | awk '{print $1}')"
        rm -rf "$work"
        die "checksum mismatch for $tarball: expected $QDRANT_SHA256, got $actual. The file was deleted."
    fi
    tar -xzf "$work/$tarball" -C "$work" qdrant
    install -m 0755 -o root -g root "$work/qdrant" /usr/local/bin/qdrant
    rm -rf "$work"
    ok "qdrant $QDRANT_VERSION at /usr/local/bin/qdrant"
}

# ---------------------------------------------------------------------------
# 8. Configuration and the API key
# ---------------------------------------------------------------------------

install_config() {
    step "Configuration"
    # mirror.yaml: group wikimirror (both service users read it). qdrant.yaml: world-readable
    # because the `qdrant` user is in neither service group; it holds no secret.
    install_template "$SRC_DIR/mirror.yaml" "$CONF_DIR/mirror.yaml" 0640 root "$SHARED_GROUP"
    install_template "$SRC_DIR/qdrant.yaml" "$CONF_DIR/qdrant.yaml" 0644 root root

    # The API key the main client presents. Generated here and nowhere else, never regenerated (a
    # new key would silently lock the client out), never printed. Root-owned 0600: systemd
    # reads EnvironmentFile as root before dropping to `wikimirror`, so the service itself, and
    # the build user, never need to read it.
    if [[ -f "$MIRROR_ENV" ]] && grep -q '^WIKI_MIRROR_API_KEY=.' "$MIRROR_ENV"; then
        skip "WIKI_MIRROR_API_KEY in $MIRROR_ENV"
        run chown root:root "$MIRROR_ENV"
        run chmod 0600 "$MIRROR_ENV"
    elif $DRY_RUN; then
        info "would generate WIKI_MIRROR_API_KEY into $MIRROR_ENV (0600, root)"
    else
        local key
        key="$("$PYTHON" -I -c 'import secrets; print(secrets.token_urlsafe(48))')"
        [[ ${#key} -ge 32 ]] || die "key generation produced a short key"
        # Appended, never rewritten: a file that already holds other settings keeps them.
        ( umask 077; printf 'WIKI_MIRROR_API_KEY=%s\n' "$key" >> "$MIRROR_ENV" )
        chown root:root "$MIRROR_ENV"
        chmod 0600 "$MIRROR_ENV"
        unset key
        ok "generated WIKI_MIRROR_API_KEY (not shown; docs/OPERATIONS.md has the copy-to-client command)"
        REMAINING+=("Copy WIKI_MIRROR_API_KEY to the main client and point it at this API (docs/OPERATIONS.md)")
    fi

    # The other agents' keys (Claude Code, ...), managed with `wiki-mirror key add|revoke`.
    # SHA-256 hashes only, so the API user may read it: root:wikimirror 0640 (the API unit already
    # has /etc/wiki-mirror read-only). Created empty ONCE and never overwritten or
    # emptied - it is the record of who holds a key. The service re-reads it when it changes.
    if [[ -f "$CLIENTS_FILE" ]]; then
        skip "$CLIENTS_FILE"
        run chown "root:$SHARED_GROUP" "$CLIENTS_FILE"
        run chmod 0640 "$CLIENTS_FILE"
    elif $DRY_RUN; then
        info "would create $CLIENTS_FILE (empty, root:$SHARED_GROUP 0640)"
    else
        ( umask 037; printf '{}\n' > "$CLIENTS_FILE" )
        chown "root:$SHARED_GROUP" "$CLIENTS_FILE"
        chmod 0640 "$CLIENTS_FILE"
        ok "created $CLIENTS_FILE (no agent has a key yet: wiki-mirror key add <name>)"
    fi
}

# ---------------------------------------------------------------------------
# 9. Units
# ---------------------------------------------------------------------------

UNITS=(wiki-mirror-qdrant.service wiki-mirror.service
       wiki-mirror-build.service wiki-mirror-build.timer
       wiki-mirror-update.service wiki-mirror-update.timer)

install_units() {
    step "Services"
    local unit changed=false
    for unit in "${UNITS[@]}"; do
        if $DRY_RUN && [[ ! -f "$SRC_DIR/systemd/$unit" ]]; then
            info "would install $unit"
            continue
        fi
        [[ -f "$SRC_DIR/systemd/$unit" ]] || die "missing unit $SRC_DIR/systemd/$unit - the checkout is incomplete"
        if ! cmp -s "$SRC_DIR/systemd/$unit" "/etc/systemd/system/$unit"; then
            run install -m 0644 "$SRC_DIR/systemd/$unit" /etc/systemd/system/
            changed=true
        fi
    done
    if $changed; then
        run systemctl daemon-reload
        ok "units installed"
    else
        skip "units"
    fi

    # Qdrant, the API and the two timers. NOT the build or update services: they are started by
    # their timers (or by the operator: `wiki-mirror build-now`, `wiki-mirror update`), never by this installer.
    run systemctl enable wiki-mirror-qdrant.service wiki-mirror.service \
        wiki-mirror-build.timer wiki-mirror-update.timer
    # Started separately and without `--now` semantics that abort the run: if the API cannot
    # start (bad config, a missing key) the installer must still finish the firewall and then
    # say so in the verify step, not die half-configured with the box wide open.
    for unit in wiki-mirror-qdrant.service wiki-mirror.service \
                wiki-mirror-build.timer wiki-mirror-update.timer; do
        run systemctl start "$unit" || warn "$unit did not start - journalctl -u ${unit%.*} -n 50"
    done
    ok "qdrant, the API, the monthly build timer and the nightly update timer enabled (no build started)"
}

# ---------------------------------------------------------------------------
# 10. Firewall
# ---------------------------------------------------------------------------

# Print the firewall template $1 with the validated site values (LAN_CIDR, EMBEDDING_HOST,
# EMBEDDING_PORT) in place of its three placeholders. Returns 1, printing nothing to stdout, if the
# template is missing or empty, or any placeholder is left afterwards (a template that gained
# one this script does not know would otherwise load as a syntax error, or worse).
#
# The substitution is plain parameter expansion, so nothing is evaluated; and the values are
# digits, dots and one slash by construction (read_site_file), so none of them can carry
# the `&` that bash 5.2 treats specially in a replacement.
render_firewall() {
    local template="$1" text
    if [[ -z "$LAN_CIDR" || -z "$EMBEDDING_HOST" || -z "$EMBEDDING_PORT" ]]; then
        echo "render_firewall: the site settings are not loaded" >&2
        return 1
    fi
    if [[ ! -s "$template" ]]; then
        echo "render_firewall: $template is missing or empty" >&2
        return 1
    fi
    # -d '' reads to end of file keeping every newline; read returns 1 at the end, which is
    # expected here and not an error.
    IFS= read -r -d '' text < "$template" || true
    text="${text//@LAN_CIDR@/$LAN_CIDR}"
    text="${text//@EMBEDDING_HOST@/$EMBEDDING_HOST}"
    text="${text//@EMBEDDING_PORT@/$EMBEDDING_PORT}"
    if [[ "$text" =~ (@[A-Z][A-Z0-9_]*@) ]]; then
        echo "render_firewall: $template still holds the placeholder ${BASH_REMATCH[1]} after rendering" >&2
        return 1
    fi
    printf '%s' "$text"
}

# --show-firewall: print the ruleset this checkout and this site file produce, then stop. Needs
# no root and changes nothing; update.sh compares its output with the installed file. Exits 1
# with the reason on stderr if the site file is missing or not valid.
show_firewall() {
    local src="$SRC_DIR/nftables/wiki-mirror.nft"
    [[ -f "$SITE_FILE" && -r "$SITE_FILE" ]] || die "no readable $SITE_FILE (see site.env.example)"
    read_site_file
    if [[ ${#SITE_PROBLEMS[@]} -gt 0 ]]; then
        die "$SITE_FILE has ${#SITE_PROBLEMS[@]} problem(s):
$(site_problem_lines)"
    fi
    render_firewall "$src" || die "cannot render $src"
}

# Put back what install_firewall changed in this run, so that a ruleset which fails its check is
# not left on disk for the next boot to load. nftables.service loads /etc/nftables.conf at boot,
# and a file that fails to load there leaves the box with NO ruleset, which on a default-deny
# design means open. $1 is the installed file, $2 the backup of the file it replaced, $3 the
# nftables configuration, $4 the include line this installer owns in it, and $5 a space-separated
# list of what was done: file-replaced (the previous file is in $2), file-created (there was none
# before), conf-created, include-added. Says what it did on stdout; returns 1, after saying so
# loudly, if any step could not be undone (the backup is then left in place for a hand repair).
undo_firewall_install() {
    local dst="$1" backup="$2" conf="$3" include="$4" changes=" $5 " status=0 kept
    if [[ "$changes" == *" file-replaced "* ]]; then
        if mv -f "$backup" "$dst"; then
            warn "put the previous $dst back, byte for byte"
        else
            warn "COULD NOT put the previous $dst back; it is saved as $backup - copy it over $dst by hand BEFORE the next boot"
            status=1
        fi
    fi
    if [[ "$changes" == *" file-created "* ]]; then
        if rm -f "$dst"; then
            warn "removed the new $dst (there was none before)"
        else
            warn "COULD NOT remove the new $dst - delete it by hand BEFORE the next boot"
            status=1
        fi
    fi
    if [[ "$changes" == *" conf-created "* ]]; then
        if rm -f "$conf"; then
            warn "removed $conf, which this run had created"
        else
            warn "COULD NOT remove $conf, which this run created - delete it by hand BEFORE the next boot"
            status=1
        fi
    elif [[ "$changes" == *" include-added "* ]]; then
        # grep -v exits 1 when no line is left, which only means an empty file here; the include
        # line is the one thing this run appended.
        kept="$(grep -vxF "$include" "$conf" || true)"
        if printf '%s\n' "$kept" > "$conf"; then
            warn "took the include line for $dst out of $conf again"
        else
            warn "COULD NOT take '$include' out of $conf - remove that line by hand BEFORE the next boot"
            status=1
        fi
    fi
    return "$status"
}

install_firewall() {
    step "Firewall (nftables)"
    local src="$SRC_DIR/nftables/wiki-mirror.nft"
    local dst="$NFT_INSTALLED" conf="$NFT_CONF"
    local include="include \"$dst\""
    # The file this run replaces is kept here until the whole ruleset has checked clean (below).
    local backup="$dst.previous" changes=""

    if $DRY_RUN && [[ ! -f "$src" ]]; then
        info "would render the firewall template with $SITE_FILE, install it as $dst, include it from $conf, check with nft -c, apply"
        return 0
    fi
    [[ -f "$src" ]] || die "missing $src - the checkout is incomplete"
    if [[ -z "$LAN_CIDR" ]]; then
        # Only a dry run may get here: a real run died in check_site if the site file was
        # unusable. Anything else is a bug, and a firewall is never built from nothing.
        $DRY_RUN || die "the site settings are not loaded; no firewall is built without them"
        info "would render $src with $SITE_FILE once that file is valid (see above)"
        return 0
    fi

    # nft resolves the user names in `meta skuid` rules when it loads the file, and one
    # unknown name fails the whole load - which at boot would leave the box with NO ruleset.
    # setup_users_and_dirs has made ours; `_apt` is apt's sandbox user, created by the apt
    # package itself (uid 42 on Debian), and the ruleset needs it because apt downloads as `_apt`.
    # This is the check that all of them exist.
    local user
    for user in root _apt "$API_USER" "$SYNC_USER" "$QDRANT_USER"; do
        id "$user" >/dev/null 2>&1 || $DRY_RUN || die "user $user is missing; the ruleset names it"
    done

    # A backup left by a run that stopped between replacing the file and finishing its check
    # means the installed file may be one that never passed. It is not overwritten with that
    # file's copy (the only good version could be lost), so the run stops until a person says
    # which one stands.
    if [[ -e "$backup" ]]; then
        blocker "$backup exists: an earlier run stopped after replacing $dst and before its check finished, so $dst may be a ruleset that never passed.
    Nothing has been changed. If $dst is the version you want, delete $backup; if not, put
    the old one back with: mv -f $backup $dst
    Then run this installer again."
    fi

    # The installed file is the RENDERED template, so "unchanged" means the rendering equals it
    # byte for byte: a changed template or a changed site file both show up as a difference,
    # and an identical one is not rewritten. The ruleset is still checked and applied below
    # either way (loading it is idempotent), as before the site file existed.
    run install -d -m 0755 "$(dirname "$dst")"
    if $DRY_RUN; then
        info "would render $src with $SITE_FILE; the site-specific lines would read:"
        render_firewall "$src" | grep -E '^[[:space:]]*define (LAN_V4|EMBEDDING_V4|EMBEDDING_PORT) ' \
            | sed 's/^[[:space:]]*/        /' || true
        if [[ -f "$dst" ]] && render_firewall "$src" | cmp -s - "$dst"; then
            skip "$dst matches the rendered template"
        else
            info "would install the rendered ruleset as $dst, keeping any previous file as $backup until the whole ruleset checks clean"
        fi
    else
        local rendered
        rendered="$(mktemp)"
        render_firewall "$src" > "$rendered" || { rm -f "$rendered"; die "cannot render the firewall; nothing was installed"; }
        if [[ -f "$dst" ]] && cmp -s "$rendered" "$dst"; then
            skip "$dst matches the template rendered from $SITE_FILE"
        else
            # Transactional: the previous file is kept (mode, owner and times too) until the check
            # below passes, and put back if it does not. A rendering that fails `nft -c` must
            # never be what the next boot loads.
            if [[ -f "$dst" ]]; then
                cp -p "$dst" "$backup" || { rm -f "$rendered" "$backup"; die "cannot back up $dst as $backup; nothing was changed"; }
                changes+=" file-replaced"
            else
                changes+=" file-created"
            fi
            if ! install -m 0644 -o root -g root "$rendered" "$dst"; then
                rm -f "$rendered"
                undo_firewall_install "$dst" "$backup" "$conf" "$include" "$changes" || true
                die "cannot install $dst; what this run had changed was put back (see above)"
            fi
            info "installed $dst (the template rendered from $SITE_FILE); it stays only if the ruleset checks clean"
        fi
        rm -f "$rendered"
    fi
    if [[ ! -f "$conf" ]]; then
        $DRY_RUN || printf '#!/usr/sbin/nft -f\nflush ruleset\n' > "$conf"
        changes+=" conf-created"
    fi
    if ! grep -qxF "$include" "$conf" 2>/dev/null; then
        $DRY_RUN || printf '%s\n' "$include" >> "$conf"
        changes+=" include-added"
        info "$($DRY_RUN && echo 'would add' || echo 'added') '$include' to $conf"
    fi

    # Check the WHOLE ruleset first, then apply. /etc/nftables.conf flushes before it loads, so
    # a file that fails halfway would leave no table at all - which on a default-drop design
    # means open, not sealed. Existing connections, including this SSH session, survive: the
    # first input rule accepts established flows.
    if $DRY_RUN; then
        info "would run: nft -c -f $conf, then nft -f $conf, then enable nftables.service"
        return 0
    fi
    local check_err
    check_err="$(nft -c -f "$conf" 2>&1)" || {
        printf '%s\n' "$check_err" | sed 's/^/    /' >&2
        # Fail closed: the check failed, so whatever this run wrote is taken back before
        # anything else, and nft -f is never run. A failure to take it back is said first
        # and loudest, because the next boot would then load the bad file.
        if undo_firewall_install "$dst" "$backup" "$conf" "$include" "$changes"; then
            die "the ruleset does not check clean; NOTHING WAS APPLIED and the firewall files are back as they were before this run."
        fi
        die "the ruleset does not check clean, nothing was applied, AND this run could not put back everything it changed (see the lines above marked COULD NOT). Repair those BEFORE the next boot or nftables.service will load a ruleset that fails."
    }
    rm -f "$backup"
    if [[ "$changes" == *file-* ]]; then
        ok "installed $dst (the template rendered from $SITE_FILE)"
    fi
    nft -f "$conf" || die "nft -f $conf failed"
    systemctl enable --quiet nftables.service
    ok "firewall applied (input: SSH and :$API_PORT from the LAN; output: per-user allowlist, refused with a reset)"
}

# ---------------------------------------------------------------------------
# 11. Operator CLI
# ---------------------------------------------------------------------------

install_cli() {
    step "Operator CLI"
    local target="$SRC_DIR/wiki-mirror" link=/usr/local/bin/wiki-mirror
    if [[ "$(readlink -f "$link" 2>/dev/null || true)" == "$(readlink -f "$target" 2>/dev/null || echo none)" ]]; then
        skip "wiki-mirror on PATH"
        return
    fi
    # A symlink into the checkout, so a command change is live as soon as update.sh pulls it.
    run ln -sfn "$target" "$link"
    ok "wiki-mirror -> $target"
}

# ---------------------------------------------------------------------------
# 12. Prove it
# ---------------------------------------------------------------------------

verify() {
    step "Verifying"
    if $DRY_RUN; then
        info "would poll Qdrant :$QDRANT_PORT/healthz and the API :$API_PORT/v1/wiki/health, then probe the firewall"
        return 0
    fi

    local up=false
    for _ in $(seq 1 30); do
        if curl -fsS --max-time 3 "http://127.0.0.1:$QDRANT_PORT/healthz" >/dev/null 2>&1; then up=true; break; fi
        sleep 2
    done
    if $up; then
        ok "Qdrant answers on 127.0.0.1:$QDRANT_PORT"
    else
        warn "Qdrant did not answer within 60 s - journalctl -u wiki-mirror-qdrant -n 50"
        REMAINING+=("Diagnose why Qdrant is not answering (wiki-mirror logs qdrant)")
    fi

    up=false
    for _ in $(seq 1 30); do
        if api_get /v1/wiki/health >/dev/null 2>&1; then up=true; break; fi
        sleep 2
    done
    if $up; then
        ok "the API answers on :$API_PORT (an empty mirror reports status 'empty' until the first build)"
    else
        warn "the API did not answer within 60 s - journalctl -u wiki-mirror -n 50"
        REMAINING+=("Diagnose why the API is not answering (wiki-mirror logs api)")
    fi

    # The firewall, by behaviour rather than by reading the rules. Any HTTP status counts as
    # "connected"; only a refused/timed-out connection is a failure.
    if curl -sS --max-time 10 -o /dev/null https://github.com 2>/dev/null; then
        ok "root can reach the internet on 443 (pip, GitHub)"
    else
        warn "root cannot reach https://github.com - check DNS (the resolver must be on the LAN) and the firewall"
        REMAINING+=("Root has no outbound 443: check the resolver and 'nft list ruleset'")
    fi
    # apt fetches as `_apt`, not root, and over plain http on a stock Debian: probe exactly that.
    if runuser -u _apt -- curl -sS --max-time 10 -o /dev/null http://deb.debian.org 2>/dev/null; then
        ok "_apt can reach the Debian mirror on 80 (apt update, apt install)"
    else
        warn "_apt cannot reach http://deb.debian.org - apt will fail; check DNS and the firewall"
        REMAINING+=("The apt user (_apt) has no outbound 80/443: check the resolver and 'nft list ruleset'")
    fi
    if runuser -u "$SYNC_USER" -- curl -sS --max-time 10 -o /dev/null https://api.enterprise.wikimedia.com 2>/dev/null; then
        ok "$SYNC_USER can reach api.enterprise.wikimedia.com"
    else
        warn "$SYNC_USER cannot reach api.enterprise.wikimedia.com - the first build would fail"
        REMAINING+=("$SYNC_USER has no route to Wikimedia Enterprise: check DNS and the firewall")
    fi
    if runuser -u "$API_USER" -- curl -sS --max-time 5 -o /dev/null https://github.com 2>/dev/null; then
        warn "$API_USER REACHED the internet - the egress rule for it is not in force"
        REMAINING+=("The API user can reach the internet: inspect 'nft list chain inet wiki_mirror output'")
    else
        ok "$API_USER is refused outbound connections, as designed"
    fi
}

summary() {
    printf '\n%s%s%s\n' "$BOLD" "================================================" "$RESET"
    printf '%sMirror install %s%s\n' "$BOLD" "$($DRY_RUN && echo 'dry run complete' || echo complete)" "$RESET"
    printf '%s%s%s\n\n' "$BOLD" "================================================" "$RESET"

    local item
    if [[ ${#DONE_STEPS[@]} -gt 0 ]]; then
        printf '%s%s%s\n' "$BOLD" "$($DRY_RUN && echo 'Would be done (nothing was changed):' || echo 'Done:')" "$RESET"
        for item in "${DONE_STEPS[@]}"; do printf '  %s+%s %s\n' "$GREEN" "$RESET" "$item"; done
    fi
    if [[ ${#BLOCKERS[@]} -gt 0 ]]; then
        printf '\n%sWould stop the real run:%s\n' "$RED" "$RESET"
        for item in "${BLOCKERS[@]}"; do printf '  %s!%s %s\n' "$RED" "$RESET" "$item"; done
    fi
    if [[ ${#REMAINING[@]} -gt 0 ]]; then
        printf '\n%sStill needs you:%s\n' "$YELLOW" "$RESET"
        for item in "${REMAINING[@]}"; do printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$item"; done
    fi

    cat <<EOF

${BOLD}Next:${RESET}
  Status        wiki-mirror status
  First build   wiki-mirror build-now      (about 150 GB of downloads; run it when you choose)
  Logs          wiki-mirror logs api|build|qdrant
  Update        wiki-mirror update         (the box also updates itself at 02:20 local time; timedatectl shows its zone)
  Firewall      nft list counters    (what has been refused)
  Site values   $SITE_FILE; after editing it, run this installer again to re-render the firewall

  From \`pct enter <ctid>\` the shell's PATH lacks /usr/local/bin, so \`wiki-mirror\` is "command not
  found" there: run \`exec bash -l\` first, or use the full path, /usr/local/bin/wiki-mirror.
  (Over SSH it is found as usual.)
EOF
}

# ---------------------------------------------------------------------------

main() {
    # Before the banner, the root check and every step: this mode only prints, and update.sh
    # reads its output.
    if $SHOW_FIREWALL; then
        show_firewall
        return 0
    fi

    printf '%sMirror installer%s%s\n' "$BOLD" "$RESET" \
        "$($DRY_RUN && printf ' (dry run - nothing will change)')"

    preflight
    install_packages
    setup_users_and_dirs
    verify_deploy_key
    build_python
    clone_checkout
    setup_venv
    install_qdrant
    install_config
    install_units
    install_firewall
    install_cli
    verify
    summary
}

# Run only when executed, not when sourced: tests/deploy/ sources this file to qualify the
# site-settings functions on their own, without root or a box.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
