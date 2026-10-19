#!/usr/bin/env bash
#
# Update the mirror on its LXC from GitHub, or leave it exactly as it was.
#
# Run as root: by hand through `wiki-mirror update`, or every night at 02:20 local time by
# wiki-mirror-update.timer with `--unattended`. Decided on 2026-10-17: the
# mirror follows main by itself, around the time the main client's own nightly update runs. The checkout
# is root-owned and the services only read it, so root fetches it, and a service account never
# holds a credential that can change the code it runs.
#
# Options:
#   --unattended           the timer's mode. A running build is a quiet "deferred" (exit 0), and
#                          a format change over built data is a "held" (exit 0): neither is a
#                          failure, and a unit that went red every night of a multi-day semantic
#                          build would teach everyone to ignore it. Failures still exit 1.
#   --accept-format-change go ahead with a commit that changes the stored data's format, over
#                          built data, knowing a rebuild (`wiki-mirror build-now`) is needed after.
#
# What it does, in order, stopping at the first thing that is wrong:
#   1. `git fetch`. Nothing at the target: records "current" and stops.
#   2. Refuses if a build is running, by the unit's state OR by the build lock
#      (`<data_dir>/state/build.lock`, the non-blocking flock `build_lock` in build.py takes), and
#      holds that lock until it exits, so a build cannot start in the middle of a package swap.
#      A restart mid-build is safe (the build is checkpointed) but an installed-package swap
#      under a running parse is not worth the argument. A build that tries to start during the
#      swap exits 1 on the held lock ("a build is already running"); its unit's hourly retry
#      then starts it. Unattended this is "deferred"; by hand it is a refusal.
#   3. The format guard (below), then a fast-forward only. A diverged or locally edited checkout
#      is refused, never merged or discarded: someone debugging in production owns that tree.
#   4. Reinstalls the package into the venv (`pip install "."` then a no-deps force
#      reinstall, because the package version is fixed and pip cannot see a new commit). Every
#      dependency is held to deploy/constraints.txt and comes from a wheel, so an
#      update changes a dependency only when a commit changes that file, and never compiles.
#   5. Syncs the systemd units from the checkout (root can). A
#      NEW timer is installed but not enabled, and the log and the record say so: enabling is
#      `systemctl enable --now <timer>`, once.
#   6. Restarts the API - not Qdrant, not a build - and waits for /v1/wiki/health.
#   7. If the new version does not become healthy, rolls back to the previous commit,
#      reinstalls it, restarts, and exits non-zero. The same happens if the run stops for any
#      other reason between the merge and the healthy exit (a failed unit install, a signal):
#      the exit trap rolls back, so an unattended box is never left on unchecked code.
#
# The format guard. deploy/FORMAT is one line - `store=<schema> chunker=<fingerprint>
# index=<format>` - kept equal to the code's constants by a test. A commit that changes any of
# them makes the data already built unreadable or misaligned the moment the API restarts on it,
# until a rebuild of many hours. So the FORMAT of the current commit is compared with the
# target's BEFORE anything is merged. If they differ (or the current commit has no FORMAT file at
# all, so nothing is known), and there is built data to protect (any *.db under
# <data_dir>/articles), the update is HELD: unattended it records "held" and exits 0 (a human has
# to decide, and a red unit every night until they do would add nothing the record does not
# already say); by hand it refuses unless --accept-format-change. With no built data a format
# change just proceeds: nothing exists to break. The guard protects the update AFTER the one that
# brings it: that one runs the old script (this file replaces itself on the merge).
#
# Every run writes <data_dir>/state/update.json (atomically, root:wikimirror 0640) - when, from and to
# which commit, the outcome (updated, current, deferred, held, failed, rolled_back), a short
# detail, and whether it ran unattended. /v1/wiki/health and `wiki-mirror status` show it.
#
# Firewall changes are NOT applied here: nftables is an operator action. If the ruleset the
# checkout would produce differs from the installed one this says so; `install.sh` applies it.
# The checkout holds a TEMPLATE (the site's LAN and embedding server are in
# /etc/wiki-mirror/site.env), so the comparison is against the template rendered for this site by
# the installer's own code (`install.sh --show-firewall`), never against the raw template. That
# note is advisory: a site file that cannot be rendered is reported and the update carries on,
# because failing here, after the merge, would roll back an otherwise good update.
#
# See docs/DESIGN.md section 12, docs/OPERATIONS.md (R.9, R.10) and
# deploy/README.md.

set -euo pipefail

# Run from a stable copy of this script: `git merge` below replaces this very file while bash
# is still reading it, and bash resumes at the same byte offset in different content.
if [[ -z "${WIKI_MIRROR_UPDATE_SELF_COPY:-}" ]]; then
    _self_copy="$(mktemp)"
    cat "${BASH_SOURCE[0]}" > "$_self_copy"
    WIKI_MIRROR_UPDATE_SELF_COPY="$_self_copy" exec bash "$_self_copy" "$@"
fi

APP_DIR="${APP_DIR:-/opt/wiki-mirror}"
CONF_DIR="${CONF_DIR:-/etc/wiki-mirror}"
DATA_DIR="${DATA_DIR:-/var/lib/wiki-mirror}"
BRANCH="${BRANCH:-main}"
VENV="$APP_DIR/.venv"
SRC_DIR="$APP_DIR/deploy"
MIRROR_ENV="$CONF_DIR/mirror.env"
STATE_DIR="$DATA_DIR/state"
API_PORT=8300
# Where the checkout's files are installed. Variables so a test can point them at a scratch
# directory; on the box they are always these defaults.
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
BIN_LINK="${BIN_LINK:-/usr/local/bin/wiki-mirror}"
NFT_INSTALLED="${NFT_INSTALLED:-/etc/nftables.d/wiki-mirror.nft}"
# The identity the build runs as and the group it shares with the API (install.sh): the lock
# file belongs to them, and update.json is root's, readable by the group.
SYNC_USER="${SYNC_USER:-wikimirror-sync}"
SHARED_GROUP="${SHARED_GROUP:-wikimirror}"
RECORD_OWNER="${RECORD_OWNER:-root:$SHARED_GROUP}"
# The tested dependency versions, wheels only (http-ece, pure Python and sdist-only, excepted);
# the same flags install.sh uses. See the header of constraints.txt.
PIP_PINNED=(-c "$SRC_DIR/constraints.txt" --only-binary=:all: --no-binary=http-ece)

UNATTENDED=false
ACCEPT_FORMAT_CHANGE=false

# What the exit trap records if the run ends in a non-zero status nobody recorded. FAIL_OUTCOME
# becomes rolled_back once a rollback has fully succeeded; OUTCOME_ARMED turns the trap on once
# there is a `from` commit and somewhere to write (before that, nothing is knowable).
OUTCOME_ARMED=false
RECORDED=false
FAIL_OUTCOME=failed
FAIL_DETAIL=""
ROLLBACK_NOTE=""
# MERGED: the checkout has moved to the target and the "healthy" exit has not happened yet; a
# non-zero exit in that window is rolled back by the exit trap. ROLLBACK_TRIED: `rollback` has
# run (from a failure path or the trap), so it never runs twice.
MERGED=false
ROLLBACK_TRIED=false
CURRENT=""
TARGET=""
BUILD_LOCK_FD=""

log() { printf '%s update: %s\n' "$(date --iso-8601=seconds)" "$*"; }
die() { FAIL_DETAIL="$*"; log "FAILED: $*" >&2; exit 1; }

usage() {
    sed -n '/^# Options:/,/^#$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# A string as a JSON string literal: control characters dropped, backslash and quote escaped,
# cut to 300 characters. The details written here are fixed text and commit ids, never a URL, a
# key or a command's output, so this is a safety net rather than the control.
json_text() {
    local text="${1:0:300}"
    text="$(printf '%s' "$text" | tr -d '\000-\037')"
    text="${text//\\/\\\\}"
    text="${text//\"/\\\"}"
    printf '"%s"' "$text"
}

# Write <data_dir>/state/update.json: temp file in the same directory, then mv, so a reader (the
# API, on every health poll) never sees half a file. $1 is the outcome, $2 the detail. Never
# fails the run: an unwritable record is a warning, not a reason to die inside a die.
record_outcome() {
    local outcome="$1" detail="$2" target_json=null tmp
    RECORDED=true
    if [[ -n "$TARGET" ]]; then
        target_json="$(json_text "${TARGET:0:12}")"
    fi
    if [[ ! -d "$STATE_DIR" ]]; then
        log "WARNING: no $STATE_DIR; the outcome '$outcome' is not recorded" >&2
        return 0
    fi
    tmp="$(mktemp "$STATE_DIR/.update.json.XXXXXX")" || {
        log "WARNING: cannot create a file in $STATE_DIR; the outcome '$outcome' is not recorded" >&2
        return 0
    }
    if printf '{"at": %s, "from": %s, "to": %s, "outcome": %s, "detail": %s, "unattended": %s}\n' \
            "$(json_text "$(date --iso-8601=seconds)")" "$(json_text "${CURRENT:0:12}")" \
            "$target_json" "$(json_text "$outcome")" "$(json_text "$detail")" "$UNATTENDED" \
            > "$tmp" \
       && chown "$RECORD_OWNER" "$tmp" && chmod 0640 "$tmp" \
       && mv -f "$tmp" "$STATE_DIR/update.json"; then
        return 0
    fi
    rm -f "$tmp"
    log "WARNING: could not write $STATE_DIR/update.json; the outcome '$outcome' is not recorded" >&2
}

# Every exit path that did not record an outcome and is not a success is a failure to record:
# `die`, a `set -e` stop in the middle of the run, or a signal. A `die` has already set the
# detail; an unexpected stop gets its exit status as the only fact there is.
#
# A non-zero exit after the merge and before the healthy exit (a `set -e` stop, a signal) would
# leave an unattended box on code nobody checked, so the trap rolls it back first, exactly as the
# planned failure paths do. errexit is off in here: a rollback step that fails must be reported,
# not end the trap, and the exit status stays the original one.
on_exit() {
    local status=$?
    set +e
    if $MERGED && ! $ROLLBACK_TRIED && [[ $status -ne 0 ]]; then
        log "stopped after the merge (exit $status); rolling back" >&2
        rollback
    fi
    if $OUTCOME_ARMED && ! $RECORDED && [[ $status -ne 0 ]]; then
        record_outcome "$FAIL_OUTCOME" \
            "${FAIL_DETAIL:-stopped unexpectedly (exit $status)}$ROLLBACK_NOTE"
    fi
    rm -f "$WIKI_MIRROR_UPDATE_SELF_COPY"
}
trap on_exit EXIT

for argument in "$@"; do
    case "$argument" in
        --unattended) UNATTENDED=true ;;
        --accept-format-change) ACCEPT_FORMAT_CHANGE=true ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option '$argument' (--unattended, --accept-format-change)" ;;
    esac
done

# `id -u` rather than $EUID: the variable is read-only in bash, and a test cannot stand in for it.
[[ "$(id -u)" -eq 0 ]] || die "run as root (wiki-mirror update)"
[[ -d "$APP_DIR/.git" ]] || die "$APP_DIR is not a git checkout"
[[ -x "$VENV/bin/python" ]] || die "no virtualenv at $VENV (run install.sh)"
[[ -d "$STATE_DIR" ]] || die "no state directory $STATE_DIR (run install.sh)"

# A oneshot unit is "activating", not "active", while its process runs, so `is-active --quiet`
# (which succeeds only for "active") never saw a running build. It is also "activating" while
# it waits out RestartSec after a failure (SubState auto-restart), which is not a build running.
# Prints a reason when the unit says a build is running, nothing otherwise.
build_unit_running() {
    local build_unit=wiki-mirror-build.service build_state
    build_state="$(systemctl show "$build_unit" -p ActiveState --value)/$(systemctl show "$build_unit" -p SubState --value)"
    case "$build_state" in
        activating/auto-restart|inactive/*|failed/*) ;;
        active/*|activating/*|reloading/*) printf 'the build unit is %s' "$build_state" ;;
    esac
}

# Take the build lock without waiting and keep it, on a descriptor, until this script exits. It
# is the lock `build_lock` (build.py) takes, so "held" means a build - or a stage run by hand -
# is going, whatever systemd thinks. The file is created the way the build creates it (owned by
# the build user, 0640): one that root created would be one the build user could not open.
# Returns 0 when taken, 75 when someone else holds it; anything else is a failure of this box.
take_build_lock() {
    local lock="$STATE_DIR/build.lock" status=0
    if [[ ! -e "$lock" ]]; then
        install -m 0640 -o "$SYNC_USER" -g "$SHARED_GROUP" /dev/null "$lock" \
            || die "cannot create the build lock $lock"
    fi
    exec {BUILD_LOCK_FD}>>"$lock" || die "cannot open the build lock $lock"
    flock -n -E 75 "$BUILD_LOCK_FD" || status=$?
    return "$status"
}

# An update that cannot go ahead now, for a reason that is nobody's fault, and has been recorded.
# Unattended it is a clean exit 0; by hand it is a refusal (exit 1) with $3 as the message.
# $1 is the outcome, $2 the detail for the record, $3 the message for a person at a terminal.
stand_down() {
    record_outcome "$1" "$2"
    if $UNATTENDED; then
        log "$1: $2"
        exit 0
    fi
    die "$3"
}

# The bearer key is read from the env file into a curl config on a pipe, so it is never in a
# process listing or in this script's output.
api_healthy() {
    local key
    key="$(sed -n 's/^WIKI_MIRROR_API_KEY=//p' "$MIRROR_ENV" | head -1)"
    [[ -n "$key" ]] || return 1
    curl -fsS --max-time 5 -K <(printf 'header = "Authorization: Bearer %s"\n' "$key") \
        "http://127.0.0.1:$API_PORT/v1/wiki/health" >/dev/null 2>&1
}

install_package() {
    "$VENV/bin/pip" install --quiet "${PIP_PINNED[@]}" "${APP_DIR}" \
        && "$VENV/bin/pip" install --quiet "${PIP_PINNED[@]}" --force-reinstall --no-deps "$APP_DIR"
}

# Restore the previous commit and its install. The outcome is `rolled_back` only if every step
# worked; if one did not, the box may be in neither state and the record says `failed` plus why.
rollback() {
    local complete=true
    ROLLBACK_TRIED=true
    log "rolling back to ${CURRENT:0:12}"
    git -C "$APP_DIR" reset --hard --quiet "$CURRENT" || { log "WARNING: could not reset to $CURRENT"; complete=false; }
    install_package || { log "WARNING: reinstall of $CURRENT failed"; complete=false; }
    systemctl restart wiki-mirror.service || { log "WARNING: restart after rollback failed"; complete=false; }
    if $complete; then
        FAIL_OUTCOME=rolled_back
        log "rolled back; the previous version is running"
    else
        ROLLBACK_NOTE="; the rollback itself was incomplete - check the box"
    fi
}

# Say so if the firewall the checkout would produce for this site is not the installed one. It
# never fails the update, whatever happens inside it: the installer runs in its own process
# (a broken one is just a failed render), and every status is handled. Its output, stdout and
# stderr together, goes to one file: on success that is the rendered ruleset and nothing else,
# on failure it is the reason.
firewall_note() {
    local rendered status=0
    rendered="$(mktemp)" || { log "note: cannot compare the firewall (no temporary file)"; return 0; }
    APP_DIR="$APP_DIR" CONF_DIR="$CONF_DIR" bash "$SRC_DIR/install.sh" --show-firewall \
        > "$rendered" 2>&1 || status=$?
    if [[ $status -ne 0 ]]; then
        log "note: the firewall for this site cannot be rendered from the checkout, so it was not compared:"
        sed 's/^/    /' "$rendered" >&2 || true
        log "      fix $CONF_DIR/site.env if that is the cause, then apply with: $SRC_DIR/install.sh"
    elif ! cmp -s "$rendered" "$NFT_INSTALLED"; then
        log "note: the firewall rendered from the checkout differs from the installed one;"
        log "      apply it deliberately with: $SRC_DIR/install.sh"
    fi
    rm -f "$rendered"
}

# The one-line FORMAT of a commit, or "(none)" when that commit has no such file.
format_of() {
    local text
    if text="$(git -C "$APP_DIR" show "$1:deploy/FORMAT" 2>/dev/null)"; then
        printf '%s' "${text%%$'\n'*}"
    else
        printf '(none)'
    fi
}

cd "$APP_DIR"
CURRENT="$(git rev-parse HEAD)"
OUTCOME_ARMED=true

# git's own words on failure, not a guess at them (blaming the deploy key for what was an
# ownership error cost a week once). They go to the log, never into the record: they can name
# the remote.
if ! FETCH_ERROR="$(git fetch --quiet origin "$BRANCH" 2>&1)"; then
    [[ -n "$FETCH_ERROR" ]] && printf '%s\n' "$FETCH_ERROR" | sed 's/^/    /' >&2
    die "could not fetch from origin. If git over SSH to GitHub is refused, the github_git set in the firewall may be stale; root also has 443 - see docs/OPERATIONS.md failure modes"
fi
TARGET="$(git rev-parse "origin/$BRANCH")"

if [[ "$CURRENT" == "$TARGET" ]]; then
    log "already at ${CURRENT:0:12} ($BRANCH); nothing to do"
    record_outcome current "already at ${CURRENT:0:12} ($BRANCH)"
    exit 0
fi

# A build is running, by the unit or by the lock. The lock is then kept for the whole update.
busy="$(build_unit_running)"
if [[ -z "$busy" ]]; then
    lock_status=0
    take_build_lock || lock_status=$?
    case "$lock_status" in
        0) ;;
        75) busy="the build lock is held (a build, or a stage run by hand)" ;;
        *) die "could not test the build lock (flock exit $lock_status)" ;;
    esac
fi
if [[ -n "$busy" ]]; then
    stand_down deferred "update to ${TARGET:0:12} waits: $busy" \
        "a build is running ($busy; wiki-mirror logs build). Wait for it, or stop it with: systemctl stop wiki-mirror-build"
fi

# Local edits on a deployment host are somebody debugging in production; refuse rather than
# discard them. `--ff-only` also refuses a checkout that has diverged from origin.
git diff --quiet HEAD -- || die "the checkout has local changes; commit, stash or discard them first"

# The format guard (header). Decided before the merge, from the two commits' own FORMAT files.
format_note=""
current_format="$(format_of "$CURRENT")"
target_format="$(format_of "$TARGET")"
if [[ "$current_format" != "$target_format" || "$current_format" == "(none)" ]]; then
    if compgen -G "$DATA_DIR/articles/*.db" >/dev/null; then
        format_description="format $current_format -> $target_format"
        if $ACCEPT_FORMAT_CHANGE; then
            format_note="; $format_description, rebuild needed: wiki-mirror build-now"
            log "accepting a format change over built data ($format_description); the data must be rebuilt afterwards: wiki-mirror build-now"
        else
            stand_down held \
                "update to ${TARGET:0:12} held: $format_description would break the built data; by hand: wiki-mirror update --accept-format-change, then wiki-mirror build-now" \
                "the stored data's format changes ($format_description) and built data exists. Applying it means a rebuild of many hours (wiki-mirror build-now) after: if that is intended, run: wiki-mirror update --accept-format-change"
        fi
    else
        log "format changes ($current_format -> $target_format); no built data to protect, going ahead"
    fi
fi

log "updating ${CURRENT:0:12} -> ${TARGET:0:12} ($BRANCH)"
git merge --ff-only --quiet "origin/$BRANCH" || die "cannot fast-forward $BRANCH to ${TARGET:0:12} (diverged?)"
MERGED=true

if ! install_package; then
    rollback
    die "package install failed (PyPI reachable? root holds tcp 443. Or a version pinned in constraints.txt has no wheel for this Python - the pip error above names it)"
fi
printf '%s\n' "$TARGET" > "$VENV/.installed-commit"

# Units: installed from the checkout whenever they differ. A unit change that deploys but
# never reaches /etc is a feature that is silently inert. A timer that was not there before is installed but NOT enabled: enabling is a
# decision, so it is said out loud here and in the record.
units_changed=false
new_timers=""
for unit in "$SRC_DIR"/systemd/*.service "$SRC_DIR"/systemd/*.timer; do
    [[ -f "$unit" ]] || continue
    if ! cmp -s "$unit" "$UNIT_DIR/$(basename "$unit")"; then
        if [[ "$unit" == *.timer && ! -e "$UNIT_DIR/$(basename "$unit")" ]]; then
            new_timers="$new_timers $(basename "$unit")"
        fi
        install -m 0644 "$unit" "$UNIT_DIR/"
        log "installed changed unit $(basename "$unit")"
        units_changed=true
    fi
done
$units_changed && systemctl daemon-reload
for timer in $new_timers; do
    log "note: $timer is new and is NOT enabled; enable it once with: systemctl enable --now $timer"
    format_note="$format_note; new timer not enabled: $timer"
done

firewall_note
# The symlink is re-asserted, in case /usr/local/bin was wiped.
ln -sfn "$SRC_DIR/wiki-mirror" "$BIN_LINK"

log "restarting the API"
systemctl restart wiki-mirror.service || { rollback; die "restart failed"; }

for _ in $(seq 1 30); do
    if api_healthy; then
        MERGED=false
        log "healthy at $(git log --oneline -1)"
        record_outcome updated "healthy at $(git log --oneline -1)$format_note"
        exit 0
    fi
    sleep 2
done

rollback
die "the new version did not become healthy within 60 s (journalctl -u wiki-mirror -n 50)"
