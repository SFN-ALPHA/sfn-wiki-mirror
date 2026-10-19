"""Per-agent API keys: which named client may call the mirror, changeable live.

The service answers its main client (its key is `WIKI_MIRROR_API_KEY`, client name `primary`)
and any number of other agents on the LAN over MCP (Claude Code, agents not built yet).
Each of those has its own key so the logs name the caller and one key can be revoked alone.
Their keys live in `clients_file` (`/etc/wiki-mirror/clients.json`) as
`{"<name>": "<sha256 hex of the key>"}`. `api.py` authenticates every request through
`ClientKeys`; `__main__` manages the file (`key add|list|revoke`, reached as `wiki-mirror key ...`).

Non-obvious constraints:

* **Only hashes are stored**, so the API user (`wikimirror`, group-read on the file) can check keys
  but never learn one. Plain unsalted SHA-256 is adequate because a key is
  `secrets.token_urlsafe(48)` (288 bits): there is nothing to guess and so no use for a slow or
  salted hash, and a lookup that costs microseconds matters on every request.
* **Every request compares the presented key's hash against every stored hash** with
  `hmac.compare_digest` and never stops at the first match, so how long a check takes does not
  say which client matched or how many are listed. Comparing digests also means a comparison
  that leaked would leak a hash, not a prefix of a key.
* **The file is re-read when it changes**, checked at most every `RECHECK_SECONDS` (an injected
  clock), so adding or revoking a key needs no restart and a revoked key stops working within
  that interval. A missing file means no extra clients, which is how a fresh install starts.
  A malformed file at runtime is logged and the last good set kept (an editing slip must not
  lock every agent out, nor resurrect a revoked key); at startup it is a hard failure, like any
  invalid configuration.
* **The writer is atomic and keeps the file's group and mode.** `key add` runs as root and the
  API runs as `wikimirror`: a replacement that came out root:root 0600 would silently lock the
  service out of its own client list. A directory lock serialises concurrent `key add`/`key
  revoke`, so a lost update can never put a revoked key back.
* **`primary` is reserved** for the environment key; it is not in the file and cannot be added or
  revoked here (rotate it in `mirror.env`).

See docs/DESIGN.md sections 5 and 6 (docs/DECISIONS.md D2).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

__all__ = [
    "CLIENT_NAME",
    "PRIMARY_CLIENT",
    "RECHECK_SECONDS",
    "ClientKeyError",
    "ClientKeys",
    "ClientNameError",
    "add_client",
    "check_client_name",
    "generate_key",
    "hash_key",
    "list_clients",
    "read_clients",
    "revoke_client",
]

logger = logging.getLogger("wiki_mirror.keys")

#: The client name of the environment key (`WIKI_MIRROR_API_KEY`): the main client.
PRIMARY_CLIENT: Final = "primary"

#: What a client may be called: it appears in logs and in the file, so it stays short and plain.
#: Always applied with `fullmatch`: `$` alone would let a trailing newline through.
CLIENT_NAME: Final = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

#: How often a running service looks at `clients_file` for a change, seconds. A revoked key
#: stops working within this long; the cost of a look is one `stat`.
RECHECK_SECONDS: Final = 5.0

_SHA256_HEX: Final = re.compile(r"^[0-9a-f]{64}$")

#: Mode of a clients file this module creates: root writes, the `wikimirror` group reads.
_NEW_FILE_MODE: Final = 0o640

#: A signature no real file has, so the first look always loads.
_NEVER_LOADED: Final = (-1, -1, -1)


class ClientKeyError(ValueError):
    """The clients file or a requested key change cannot be accepted.

    Messages name the file or the client but never a key, a hash or the text of a bad entry.
    """


class ClientNameError(ClientKeyError):
    """A client name is malformed or reserved (a usage error rather than a file problem)."""


def generate_key() -> str:
    """A new API key: 48 random bytes, URL-safe base64 (64 characters, 288 bits)."""
    return secrets.token_urlsafe(48)


def hash_key(key: str) -> str:
    """The SHA-256 hex digest that stands for a key in the clients file."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def check_client_name(name: str) -> None:
    """Refuse a client name that is malformed or reserved.

    Raises:
        ClientNameError: `name` is not lowercase letters, digits and hyphens starting with a
            letter (1 to 32 characters), or is `primary`.
    """
    if name == PRIMARY_CLIENT:
        msg = (
            f"'{PRIMARY_CLIENT}' is the service's own key (WIKI_MIRROR_API_KEY in mirror.env); "
            "it cannot be added or revoked here"
        )
        raise ClientNameError(msg)
    if not CLIENT_NAME.fullmatch(name):
        msg = (
            "a client name is 1-32 characters: a lowercase letter, then lowercase letters, "
            "digits or '-'"
        )
        raise ClientNameError(msg)


def read_clients(path: Path) -> dict[str, str]:
    """Read the clients file.

    Args:
        path: The file (`MirrorConfig.clients_file`).

    Returns:
        Client name to the SHA-256 hex digest of its key; empty when the file does not exist.

    Raises:
        ClientKeyError: The file cannot be read, is not a JSON object, or holds an invalid or
            reserved name or a value that is not a lowercase SHA-256 hex digest.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        msg = f"cannot read {path}: {type(exc).__name__}"
        raise ClientKeyError(msg) from None
    try:
        data = json.loads(raw)
    except ValueError:
        msg = f"{path} is not valid JSON"
        raise ClientKeyError(msg) from None
    if not isinstance(data, dict):
        msg = f"{path} must hold a JSON object of client name to key hash"
        raise ClientKeyError(msg)
    clients: dict[str, str] = {}
    for name, digest in data.items():
        if not isinstance(name, str) or name == PRIMARY_CLIENT or not CLIENT_NAME.fullmatch(name):
            # Not echoed: a pasted key in the name position must not reach a log.
            msg = f"{path} holds an invalid or reserved client name"
            raise ClientKeyError(msg)
        if not isinstance(digest, str) or not _SHA256_HEX.fullmatch(digest):
            msg = f"{path}: the entry for '{name}' is not a lowercase SHA-256 hex digest"
            raise ClientKeyError(msg)
        clients[name] = digest
    return clients


def list_clients(path: Path) -> list[str]:
    """The names of the extra clients, sorted (never their hashes).

    Raises:
        ClientKeyError: As `read_clients`.
    """
    return sorted(read_clients(path))


def add_client(path: Path, name: str) -> str:
    """Create a key for a new client and store its hash.

    Args:
        path: The clients file; created (root:<directory's group>, 0640) if absent.
        name: The new client's name.

    Returns:
        The key in the clear. This is the only time it exists outside the caller's hands:
        only its hash is written.

    Raises:
        ClientNameError: The name is malformed or reserved.
        ClientKeyError: The name is taken, or the existing file is malformed (it is never
            overwritten). Revoke a name before issuing it a new key.
        OSError: The file or its directory cannot be written.
    """
    check_client_name(name)
    with _exclusive(path.parent):
        clients = read_clients(path)
        if name in clients:
            msg = f"a client named '{name}' already exists; revoke it first to issue a new key"
            raise ClientKeyError(msg)
        key = generate_key()
        clients[name] = hash_key(key)
        _write_clients(path, clients)
    return key


def revoke_client(path: Path, name: str) -> None:
    """Remove a client; its key stops working once the service next looks at the file.

    Raises:
        ClientNameError: The name is malformed or is `primary`.
        ClientKeyError: No such client, or the existing file is malformed.
        OSError: The file or its directory cannot be written.
    """
    check_client_name(name)
    with _exclusive(path.parent):
        clients = read_clients(path)
        if name not in clients:
            msg = f"no client named '{name}'"
            raise ClientKeyError(msg)
        del clients[name]
        _write_clients(path, clients)


@contextlib.contextmanager
def _exclusive(directory: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on the directory while a read-modify-write runs.

    Locked on the directory (not a lock file) so no stray file is left in `/etc`. Closing the
    descriptor releases the lock, including when the process dies.
    """
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _write_clients(path: Path, clients: dict[str, str]) -> None:
    """Replace the clients file atomically, keeping its group and mode.

    The temporary file is created beside the target (same filesystem, so the rename is atomic),
    given the existing file's mode and group - or 0640 and the directory's group for a new file -
    flushed to disk, and renamed over the target. Readers see the old file or the new, never a
    torn one.
    """
    try:
        current = path.stat()
        mode, group = stat.S_IMODE(current.st_mode), current.st_gid
    except FileNotFoundError:
        mode, group = _NEW_FILE_MODE, path.parent.stat().st_gid
    text = json.dumps(dict(sorted(clients.items())), indent=2) + "\n"
    descriptor, scratch = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            os.fchown(handle.fileno(), -1, group)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        Path(scratch).replace(path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            Path(scratch).unlink()
        raise


class ClientKeys:
    """The keys the service accepts: the main client's, plus those in the live clients file.

    One instance serves every request. `authenticate` is safe to call from any thread.
    """

    def __init__(
        self,
        path: Path,
        primary_key: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        recheck_seconds: float = RECHECK_SECONDS,
    ) -> None:
        """Load the clients file once, now.

        Args:
            path: The clients file. May be absent (no extra clients).
            primary_key: The environment key, accepted as client `primary`.
            clock: Monotonic seconds, for the recheck interval; tests inject theirs.
            recheck_seconds: Longest between looks at the file for a change.

        Raises:
            ClientKeyError: The file exists but is malformed. The service must not start on a
                client list it cannot read.
        """
        self._path = path
        self._primary: tuple[str, bytes] = (PRIMARY_CLIENT, hash_key(primary_key).encode("ascii"))
        self._clock = clock
        self._recheck = recheck_seconds
        self._lock = threading.Lock()
        self._signature: tuple[int, int, int] | None = _NEVER_LOADED
        self._entries: tuple[tuple[str, bytes], ...] = (self._primary,)
        self._next_check = clock() + recheck_seconds
        self._refresh(startup=True)

    @property
    def names(self) -> tuple[str, ...]:
        """The accepted client names, `primary` first (as of the last look at the file)."""
        return tuple(name for name, _ in self._entries)

    def authenticate(self, token: str) -> str | None:
        """The name of the client a bearer token belongs to, or None.

        Args:
            token: The presented key, without the `Bearer ` scheme. An empty token matches
                nothing, whatever the file holds.

        Returns:
            The client name (`primary` for the environment key), or None for an unknown key.
        """
        if not token:
            return None
        self._recheck_file()
        digest = hash_key(token).encode("ascii")
        matched: str | None = None
        for name, stored in self._entries:
            # No early exit: the loop takes the same time whoever matches (module docstring).
            if hmac.compare_digest(digest, stored):
                matched = name
        return matched

    def _recheck_file(self) -> None:
        """Look at the file if the interval has passed; one thread looks while the rest go on."""
        if self._clock() < self._next_check or not self._lock.acquire(blocking=False):
            return
        try:
            self._next_check = self._clock() + self._recheck
            self._refresh(startup=False)
        finally:
            self._lock.release()

    def _refresh(self, *, startup: bool) -> None:
        """Reload the entries if the file changed.

        The change signature is (mtime in ns, inode, size): the writer replaces the file by
        rename, so the inode moves even when a coarse filesystem clock leaves the mtime alone.
        A file that fails to load is remembered by its signature, so its error is logged once
        rather than every interval, and is read again as soon as it changes.

        Raises:
            ClientKeyError: Only when `startup` and the file is malformed.
        """
        try:
            info = self._path.stat()
            signature: tuple[int, int, int] | None = (info.st_mtime_ns, info.st_ino, info.st_size)
        except FileNotFoundError:
            signature = None
        except OSError as exc:
            if startup:
                msg = f"cannot read {self._path}: {type(exc).__name__}"
                raise ClientKeyError(msg) from None
            logger.error(
                "the clients file cannot be examined (%s); keeping the last good client list",
                type(exc).__name__,
                extra={"component": "keys", "event": "clients_unreadable"},
            )
            return
        if signature == self._signature:
            return
        try:
            clients = read_clients(self._path)
        except ClientKeyError as exc:
            if startup:
                raise
            self._signature = signature
            logger.error(
                "%s; keeping the last good client list",
                exc,
                extra={"component": "keys", "event": "clients_invalid"},
            )
            return
        previous = self._signature
        self._signature = signature
        self._entries = (
            self._primary,
            *((n, d.encode("ascii")) for n, d in sorted(clients.items())),
        )
        if previous is not _NEVER_LOADED or clients:
            logger.info(
                "client keys loaded: %s",
                ", ".join(self.names),
                extra={"component": "keys", "event": "clients_loaded"},
            )
