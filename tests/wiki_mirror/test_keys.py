"""Qualifies the per-agent keys: hashing, the clients file, the live reload and the refusals.

Everything runs against a temporary clients file and an injected clock. Failure paths named
here: a bad or reserved client name, a taken name, a malformed file (refused at startup, ignored
with the last good set kept at runtime, logged once, never overwritten by `add_client`), a key
revoked or added while the service runs, a deleted file, an empty token, and a comparison that
must not stop at the first match.

See docs/DESIGN.md sections 5 and 6 (docs/DECISIONS.md D2).
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from tests.fixtures.clock import FrozenClock
from wiki_mirror import keys
from wiki_mirror.keys import (
    PRIMARY_CLIENT,
    RECHECK_SECONDS,
    ClientKeyError,
    ClientKeys,
    ClientNameError,
    add_client,
    check_client_name,
    generate_key,
    hash_key,
    list_clients,
    read_clients,
    revoke_client,
)

pytestmark = pytest.mark.unit

_PRIMARY_KEY = "c" * 48


def _edit(path: Path, text: str) -> None:
    """Rewrite the file in place and move its mtime, as an editor would."""
    path.write_text(text, encoding="utf-8")
    later = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(later, later))


def _keys(path: Path, clock: FrozenClock) -> ClientKeys:
    """The registry under test: the primary key plus the file, on the test clock."""
    return ClientKeys(path, _PRIMARY_KEY, clock=clock.monotonic)


# -- hashing and names ------------------------------------------------------------------------


def test_hash_key_is_the_sha256_hex_of_the_utf8_key() -> None:
    assert hash_key("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_generate_key_is_long_and_never_repeats() -> None:
    first, second = generate_key(), generate_key()
    assert len(first) >= 64
    assert first != second


@pytest.mark.parametrize("name", ["a", "claude-code", "worker", "agent-2", "a" + "b" * 31])
def test_check_client_name_accepts_plain_names(name: str) -> None:
    check_client_name(name)


@pytest.mark.parametrize(
    "name", ["", "Claude", "1abc", "-a", "a_b", "a b", "a.b", "a" * 33, "é", "a\n"]
)
def test_check_client_name_refuses_malformed_names(name: str) -> None:
    with pytest.raises(ClientNameError):
        check_client_name(name)


def test_check_client_name_refuses_primary_and_says_where_its_key_lives() -> None:
    with pytest.raises(ClientNameError, match=r"mirror\.env"):
        check_client_name(PRIMARY_CLIENT)


# -- the clients file -------------------------------------------------------------------------


def test_read_clients_missing_file_is_no_clients(tmp_path: Path) -> None:
    assert read_clients(tmp_path / "clients.json") == {}


def test_read_clients_valid_file_returns_name_to_digest(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    path.write_text(json.dumps({"worker": hash_key("k")}), encoding="utf-8")
    assert read_clients(path) == {"worker": hash_key("k")}


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '"text"',
        json.dumps({"Worker": "a" * 64}),
        json.dumps({"primary": "a" * 64}),
        json.dumps({"worker": "short"}),
        json.dumps({"worker": "A" * 64}),
        json.dumps({"worker": "a" * 64 + "\n"}),
        json.dumps({"worker\n": "a" * 64}),
        json.dumps({"worker": 7}),
    ],
)
def test_read_clients_malformed_file_raises(tmp_path: Path, text: str) -> None:
    path = tmp_path / "clients.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ClientKeyError):
        read_clients(path)


def test_read_clients_error_never_repeats_what_the_file_held(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    path.write_text(json.dumps({"A-PASTED-RAW-KEY-VALUE": "a" * 64}), encoding="utf-8")
    with pytest.raises(ClientKeyError) as raised:
        read_clients(path)
    assert "PASTED" not in str(raised.value)


def test_add_client_stores_only_the_hash_and_returns_the_key(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    key = add_client(path, "claude-code")
    assert key not in path.read_text(encoding="utf-8")
    assert read_clients(path) == {"claude-code": hash_key(key)}


def test_add_client_keeps_the_other_clients(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    add_client(path, "worker")
    add_client(path, "claude-code")
    assert list_clients(path) == ["claude-code", "worker"]


def test_add_client_taken_name_is_refused_and_the_old_hash_stays(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    add_client(path, "worker")
    before = read_clients(path)
    with pytest.raises(ClientKeyError, match="already exists"):
        add_client(path, "worker")
    assert read_clients(path) == before


def test_add_client_primary_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    with pytest.raises(ClientNameError):
        add_client(path, "primary")
    assert not path.exists()


def test_add_client_over_a_malformed_file_refuses_and_leaves_it_untouched(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ClientKeyError):
        add_client(path, "worker")
    assert path.read_text(encoding="utf-8") == "{not json"


def test_add_client_new_file_is_0640_in_the_directorys_group(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    add_client(path, "worker")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.stat().st_gid == tmp_path.stat().st_gid


def test_add_client_keeps_the_mode_of_the_file_it_replaces(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)
    add_client(path, "worker")
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_add_client_leaves_no_temporary_file_when_the_group_cannot_be_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object) -> None:
        raise PermissionError("not permitted")

    monkeypatch.setattr(keys.os, "fchown", refuse)
    with pytest.raises(PermissionError):
        add_client(tmp_path / "clients.json", "worker")
    assert list(tmp_path.iterdir()) == []


def test_revoke_client_removes_only_that_client(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    add_client(path, "worker")
    add_client(path, "claude-code")
    revoke_client(path, "worker")
    assert list_clients(path) == ["claude-code"]


def test_revoke_client_unknown_name_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ClientKeyError, match="no client named"):
        revoke_client(tmp_path / "clients.json", "nobody")


def test_revoke_client_primary_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ClientNameError):
        revoke_client(tmp_path / "clients.json", "primary")


def test_list_clients_returns_names_sorted_and_no_hashes(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    add_client(path, "worker")
    add_client(path, "claude-code")
    assert list_clients(path) == ["claude-code", "worker"]


# -- authentication ---------------------------------------------------------------------------


def test_authenticate_primary_key_is_client_primary(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    assert _keys(tmp_path / "clients.json", frozen_clock).authenticate(_PRIMARY_KEY) == "primary"


def test_authenticate_listed_key_is_its_clients_name(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    path = tmp_path / "clients.json"
    key = add_client(path, "claude-code")
    registry = _keys(path, frozen_clock)
    assert registry.authenticate(key) == "claude-code"
    assert registry.authenticate(_PRIMARY_KEY) == "primary"


def test_authenticate_unknown_key_is_none(tmp_path: Path, frozen_clock: FrozenClock) -> None:
    path = tmp_path / "clients.json"
    add_client(path, "claude-code")
    assert _keys(path, frozen_clock).authenticate("x" * 48) is None


def test_authenticate_empty_token_matches_nothing_even_if_its_hash_is_listed(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    path = tmp_path / "clients.json"
    path.write_text(json.dumps({"oops": hash_key("")}), encoding="utf-8")
    assert _keys(path, frozen_clock).authenticate("") is None


def test_authenticate_compares_every_entry_whoever_matches_first(
    tmp_path: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "clients.json"
    add_client(path, "aaa")
    add_client(path, "bbb")
    registry = _keys(path, frozen_clock)
    calls: list[int] = []
    real = keys.hmac.compare_digest

    def counting(left: bytes, right: bytes) -> bool:
        calls.append(1)
        return real(left, right)

    monkeypatch.setattr(keys.hmac, "compare_digest", counting)
    registry.authenticate(_PRIMARY_KEY)  # the first entry matches
    assert len(calls) == 3


def test_names_lists_primary_first_then_the_clients(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    path = tmp_path / "clients.json"
    add_client(path, "worker")
    assert _keys(path, frozen_clock).names == ("primary", "worker")


# -- live reload ------------------------------------------------------------------------------


def test_missing_file_at_start_means_only_the_primary_key(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    assert _keys(tmp_path / "clients.json", frozen_clock).names == ("primary",)


def test_key_added_while_running_works_after_the_recheck_interval_not_before(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    path = tmp_path / "clients.json"
    registry = _keys(path, frozen_clock)
    key = add_client(path, "worker")
    frozen_clock.advance(RECHECK_SECONDS - 1)
    assert registry.authenticate(key) is None
    frozen_clock.advance(2)
    assert registry.authenticate(key) == "worker"


def test_key_revoked_while_running_stops_working_without_a_restart(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    path = tmp_path / "clients.json"
    key = add_client(path, "worker")
    registry = _keys(path, frozen_clock)
    assert registry.authenticate(key) == "worker"
    revoke_client(path, "worker")
    frozen_clock.advance(RECHECK_SECONDS + 1)
    assert registry.authenticate(key) is None
    assert registry.authenticate(_PRIMARY_KEY) == "primary"


def test_file_edited_in_place_with_a_new_mtime_is_reloaded(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    path = tmp_path / "clients.json"
    path.write_text("{}", encoding="utf-8")
    registry = _keys(path, frozen_clock)
    _edit(path, json.dumps({"worker": hash_key("secret-key")}))
    frozen_clock.advance(RECHECK_SECONDS + 1)
    assert registry.authenticate("secret-key") == "worker"


def test_malformed_file_at_startup_refuses(tmp_path: Path, frozen_clock: FrozenClock) -> None:
    path = tmp_path / "clients.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ClientKeyError):
        _keys(path, frozen_clock)


def test_malformed_file_at_runtime_keeps_the_last_good_set_and_logs_once(
    tmp_path: Path, frozen_clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "clients.json"
    key = add_client(path, "worker")
    registry = _keys(path, frozen_clock)
    _edit(path, "{not json")
    with caplog.at_level(logging.ERROR, logger="wiki_mirror.keys"):
        for _ in range(3):
            frozen_clock.advance(RECHECK_SECONDS + 1)
            assert registry.authenticate(key) == "worker"
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def test_file_repaired_after_a_malformed_edit_is_picked_up(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    path = tmp_path / "clients.json"
    registry = _keys(path, frozen_clock)
    _edit(path, "{not json")
    frozen_clock.advance(RECHECK_SECONDS + 1)
    registry.authenticate("anything")
    _edit(path, json.dumps({"worker": hash_key("fixed-key")}))
    frozen_clock.advance(RECHECK_SECONDS + 1)
    assert registry.authenticate("fixed-key") == "worker"


def test_file_deleted_at_runtime_leaves_only_the_primary_key(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    path = tmp_path / "clients.json"
    key = add_client(path, "worker")
    registry = _keys(path, frozen_clock)
    path.unlink()
    frozen_clock.advance(RECHECK_SECONDS + 1)
    assert registry.authenticate(key) is None
    assert registry.authenticate(_PRIMARY_KEY) == "primary"


def test_unchanged_file_is_not_read_again(
    tmp_path: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "clients.json"
    add_client(path, "worker")
    registry = _keys(path, frozen_clock)
    reads: list[Path] = []
    real = keys.read_clients
    monkeypatch.setattr(keys, "read_clients", lambda p: (reads.append(p), real(p))[1])
    for _ in range(3):
        frozen_clock.advance(RECHECK_SECONDS + 1)
        registry.authenticate("anything")
    assert reads == []
