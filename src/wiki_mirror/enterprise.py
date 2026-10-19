"""The Wikimedia Enterprise client: sign in, list a snapshot, download its chunks to the NAS.

Used only by the download stage (`build.run_download`), on the mirror's container, by the
`wikimirror-sync` user; it is the one component that holds the Enterprise credentials and the
only one that talks to Wikimedia. A synchronous `httpx.Client` is enough: the build is a batch
job with a handful of download threads and no event loop.

The protocol, as measured on the live service on 2026-10-16 (W1 contract):

* `POST auth.enterprise.wikimedia.com/v1/login` with the lowercase username and the password
  returns an access token (24 h), an id token and a refresh token (90 days or 90 uses).
  `/v1/token-refresh` trades the refresh token for a new access token; any 401 means "sign in
  again".
* `GET api.enterprise.wikimedia.com/v2/snapshots/<id>` describes a snapshot and lists its chunks.
* `GET .../snapshots/<id>/chunks/<chunk>/download` answers **307** with a presigned S3 URL that
  expires in 60 seconds. `HEAD` on the same path returns `content-length`, `etag` and
  `accept-ranges`.

Non-obvious constraints:

* **The bearer token must never reach S3.** S3 answers 400 to a request that carries both the
  presigned signature and an `Authorization` header (observed). Redirects are therefore switched
  off, the `Location` is read by hand, and the S3 request is built with no auth header at all.
* **A presigned URL is a credential.** Its query string carries the signature. It is never
  logged, never put in an exception message, and `httpx`'s own request logger (which prints full
  URLs at INFO) is held at WARNING by this module. Messages name hosts, chunk ids, byte counts
  and status codes, nothing else. Exceptions from `httpx` are re-raised `from None` for the same
  reason: their text can include the URL.
* **Resume asks for a fresh URL every time.** A 60-second URL cannot outlive a retry, so each
  attempt is HEAD-free but presign-new, with `Range: bytes=<have>-`. The `.etag` sidecar records
  which object the `.part` file belongs to; a different etag (a re-published chunk) discards it.
* **The download budget is enforced here, before Enterprise does it.** One presign is one
  chunk download against the free tier's 1,500 a month; `ChunkBudget` counts them in a JSON file
  keyed by UTC month, so a retry loop gone wrong stops at our ceiling, not at the account's.
* **Credentials come from the env file** (`WME_USERNAME=`, `WME_PASSWORD=`), read by this module
  itself. The value is the raw rest of the line (no quotes, no escapes), because a password may
  contain any character.

See docs/DESIGN.md sections 5 and 6.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final
from urllib.parse import quote, urlsplit

import httpx

from wiki_mirror.records import SnapshotMeta

__all__ = ["ChunkBudget", "EnterpriseClient", "EnterpriseError"]

logger = logging.getLogger("wiki_mirror.enterprise")

#: The sign-in service and the snapshot API (W1 contract; both HTTPS).
AUTH_BASE: Final = "https://auth.enterprise.wikimedia.com/v1"
API_BASE: Final = "https://api.enterprise.wikimedia.com/v2"

#: Renew the access token this long before it expires, so a request never races the deadline.
_REFRESH_MARGIN_SECONDS: Final = 300.0

#: Access-token life when the refresh reply does not say (the login reply says 86400).
_DEFAULT_TOKEN_SECONDS: Final = 86_400

#: Longest wait honoured from a `Retry-After` header; a longer one is a service in trouble and
#: the retry budget below is a better judge than a header.
_MAX_RETRY_AFTER_SECONDS: Final = 120.0

#: Chunk and snapshot ids end up in URLs and file names; what Enterprise sends is validated.
_SAFE_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_SNAPSHOT_DATE: Final = re.compile(r"^(\d{4}-\d{2}-\d{2})")

_CONTENT_RANGE: Final = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")

_REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})

#: `httpx` logs every request at INFO, URL included. A presigned URL is a credential.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


class EnterpriseError(RuntimeError):
    """A problem talking to Wikimedia Enterprise. The message never holds a secret."""


class ChunkBudget:
    """A per-month count of chunk downloads, persisted in a small JSON file.

    One `charge` is one presigned-URL request. The file maps `YYYY-MM` (UTC) to a count; a new
    month starts at zero without any cleanup. Writes are atomic (temp file plus rename) and
    serialised by a lock, so the download threads share one budget safely. It is not safe
    against two processes at once; the build runs one at a time.
    """

    def __init__(
        self,
        path: Path,
        limit: int,
        *,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Create a budget.

        Args:
            path: The JSON counter file (`data_dir/state/enterprise_budget.json`). Created on the
                first charge.
            limit: Chunk downloads allowed per UTC month (`MirrorConfig.monthly_chunk_budget`).
            utc_now: The clock that picks the month; tests inject theirs.
        """
        self._path = path
        self._limit = limit
        self._utc_now = utc_now
        self._lock = threading.Lock()

    def charge(self) -> int:
        """Count one download against this month's budget.

        Returns:
            Downloads used this month, this one included.

        Raises:
            EnterpriseError: The month's budget is already spent. Nothing is counted.
        """
        month = self._utc_now().strftime("%Y-%m")
        with self._lock:
            counts = self._read()
            used = counts.get(month, 0)
            if used >= self._limit:
                msg = (
                    f"the monthly chunk-download budget of {self._limit} is spent for {month}"
                    " (monthly_chunk_budget); raise it in the settings or wait for next month"
                )
                raise EnterpriseError(msg)
            counts[month] = used + 1
            self._write(counts)
            return used + 1

    def _read(self) -> dict[str, int]:
        """Load the counters; a missing file is a fresh month, an unreadable one is an error."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            # Resetting to zero here would let a corrupt file disable the guard it exists for.
            msg = f"the download budget file {self._path} is unreadable; fix or delete it"
            raise EnterpriseError(msg) from None
        if not isinstance(raw, dict):
            msg = f"the download budget file {self._path} does not hold a JSON object"
            raise EnterpriseError(msg)
        return {key: value for key, value in raw.items() if isinstance(value, int)}

    def _write(self, counts: Mapping[str, int]) -> None:
        """Replace the counter file atomically."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_name(self._path.name + ".tmp")
        temp.write_text(json.dumps(dict(sorted(counts.items()))), encoding="utf-8")
        temp.replace(self._path)


class _RetryTransfer(Exception):  # noqa: N818 - control flow inside this module, not an error type
    """A download attempt that failed in a way worth another try with a fresh URL."""


class EnterpriseClient:
    """Signs in to Wikimedia Enterprise, describes snapshots and downloads their chunks.

    Thread-safe: the download threads share one instance. Token renewal is serialised, and a
    401 seen by several threads at once costs one sign-in, not one each.
    """

    def __init__(
        self,
        env_file: Path,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 4,
        backoff_seconds: float = 2.0,
    ) -> None:
        """Create a client.

        Args:
            env_file: The 0600 file with `WME_USERNAME=` and `WME_PASSWORD=` lines.
            transport: An `httpx` transport; tests inject a `MockTransport`.
            clock: A monotonic clock in seconds, for token expiry; tests inject theirs.
            sleep: What backoff calls to wait; tests inject a recorder so nothing really sleeps.
            max_attempts: Tries per request (and per chunk transfer) before giving up.
            backoff_seconds: The first retry's wait; each later one doubles it.

        Raises:
            EnterpriseError: The env file is missing or lacks a username or password. The
                message names the file and the key, never a value.
        """
        self._username, self._password = _read_credentials(env_file)
        self._clock = clock
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._client = httpx.Client(
            transport=transport,
            # Off on purpose: the 307 from the download path points at S3 and the bearer token
            # must not follow it (module docstring).
            follow_redirects=False,
            timeout=httpx.Timeout(30.0, read=120.0),
            headers={"User-Agent": "sfn-wiki-mirror/1 (+https://github.com/SFN-ALPHA/sfn-wiki-mirror)"},
        )
        self._lock = threading.RLock()
        self._access: str | None = None
        self._refresh: str | None = None
        self._expires_at = 0.0
        #: Bumped on every new access token; lets threads tell "my token was rejected" from
        #: "someone already replaced it".
        self._generation = 0

    def __repr__(self) -> str:
        """A representation without the credentials."""
        return f"EnterpriseClient(user_set={bool(self._username)})"

    def __enter__(self) -> EnterpriseClient:
        """Use the client as a context manager that closes its connections."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the client."""
        self.close()

    def close(self) -> None:
        """Close the HTTP connections."""
        self._client.close()

    # -- the snapshot API -------------------------------------------------------------------

    def snapshot(self, identifier: str) -> SnapshotMeta:
        """Describe a snapshot: its date, record count and chunk ids.

        Args:
            identifier: The snapshot id, e.g. `enwiki_namespace_0`.

        Returns:
            What a build records about the snapshot.

        Raises:
            EnterpriseError: The API answered with an error or a body that is not a snapshot.
        """
        _check_id(identifier, "snapshot")
        body = self._get_json(f"/snapshots/{identifier}", "snapshot description")
        if not isinstance(body, dict):
            raise EnterpriseError("the snapshot description is not a JSON object")
        modified = body.get("date_modified")
        date_match = _SNAPSHOT_DATE.match(modified) if isinstance(modified, str) else None
        records = body.get("record_count")
        if date_match is None or not isinstance(records, int) or isinstance(records, bool):
            raise EnterpriseError("the snapshot description lacks a date_modified or record_count")
        chunk_ids = _chunk_ids(body.get("chunks"))
        if not chunk_ids:
            # The description is meant to list them; the listing endpoint is the documented
            # source, so fall back to it rather than fail a build over a shape change.
            listing = self._get_json(f"/snapshots/{identifier}/chunks", "chunk listing")
            chunk_ids = _chunk_ids(listing)
        if not chunk_ids:
            raise EnterpriseError("the snapshot lists no chunks")
        return SnapshotMeta(
            identifier=identifier,
            date=date_match.group(1),
            record_count=records,
            chunk_ids=chunk_ids,
        )

    def download_chunk(
        self,
        identifier: str,
        chunk_id: str,
        dest: Path,
        *,
        budget: ChunkBudget | None = None,
    ) -> Path:
        """Download one chunk to `dest`, resuming a partial download if one is there.

        The bytes stream to `<dest>.part` and are verified against the size the API reports
        before an atomic rename, so `dest` only ever exists complete. A `dest` that already has
        the right size is left alone.

        Args:
            identifier: The snapshot id.
            chunk_id: The chunk id.
            dest: Final path, e.g. `.../enwiki_namespace_0_chunk_1.tar.gz`. Its directory must
                exist.
            budget: The monthly download counter; charged once per presigned-URL request.

        Returns:
            `dest`.

        Raises:
            EnterpriseError: A request failed past its retries, the budget is spent, or the
                finished file's size disagrees with the API's. A partial file is kept for the
                next run unless it is known to be wrong.
        """
        _check_id(identifier, "snapshot")
        _check_id(chunk_id, "chunk")
        path = f"/snapshots/{quote(identifier, safe='')}/chunks/{quote(chunk_id, safe='')}/download"
        head = self._api("HEAD", path)
        if head.status_code != 200:
            msg = f"chunk {chunk_id}: size lookup answered HTTP {head.status_code}"
            raise EnterpriseError(msg)
        total = _int_header(head, "content-length")
        if total is None or total <= 0:
            msg = f"chunk {chunk_id}: the API reported no usable content-length"
            raise EnterpriseError(msg)
        etag = head.headers.get("etag", "")

        if dest.exists() and dest.stat().st_size == total:
            logger.info(
                "chunk already present",
                extra={"component": "enterprise", "event": "chunk_present", "chunk": chunk_id},
            )
            return dest

        part = dest.with_name(dest.name + ".part")
        sidecar = dest.with_name(dest.name + ".etag")
        self._reconcile_part(part, sidecar, etag, total)

        attempts = 0
        while True:
            have = part.stat().st_size if part.exists() else 0
            if have == total:
                break
            if have > total:
                # More bytes than the API promised: the object changed under us or the server
                # misbehaved. Resuming would only stack more wrong bytes, so fail loudly.
                part.unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)
                msg = f"chunk {chunk_id}: downloaded {have} bytes where the API reported {total}"
                raise EnterpriseError(msg)
            attempts += 1
            if attempts > self._max_attempts:
                msg = (
                    f"chunk {chunk_id}: incomplete after {self._max_attempts} attempts"
                    f" ({have} of {total} bytes kept for the next run)"
                )
                raise EnterpriseError(msg)
            try:
                location = self._presign(path, chunk_id, budget)
                self._transfer(location, part, have, chunk_id)
            except _RetryTransfer:
                self._backoff(attempts)

        # The loop only exits on an exact size match with the API's content-length (verified).
        _fsync(part)
        part.replace(dest)
        sidecar.unlink(missing_ok=True)
        logger.info(
            "downloaded a chunk",
            extra={
                "component": "enterprise",
                "event": "chunk_downloaded",
                "chunk": chunk_id,
                "bytes": total,
            },
        )
        return dest

    # -- downloading ------------------------------------------------------------------------

    @staticmethod
    def _reconcile_part(part: Path, sidecar: Path, etag: str, total: int) -> None:
        """Keep a partial file only if it provably belongs to this version of the chunk."""
        recorded = None
        if sidecar.exists():
            recorded = sidecar.read_text(encoding="utf-8")
        if part.exists() and (recorded != etag or part.stat().st_size > total):
            part.unlink()
        sidecar.write_text(etag, encoding="utf-8")

    def _presign(self, path: str, chunk_id: str, budget: ChunkBudget | None) -> str:
        """Ask the API for a fresh presigned S3 URL (a counted download).

        Raises:
            EnterpriseError: The budget is spent, or the API did not answer with a redirect.
        """
        if budget is not None:
            budget.charge()
        response = self._api("GET", path)
        location = response.headers.get("location")
        if response.status_code not in _REDIRECTS or not location:
            msg = f"chunk {chunk_id}: expected a redirect, got HTTP {response.status_code}"
            raise EnterpriseError(msg)
        return str(location)

    def _transfer(self, location: str, part: Path, have: int, chunk_id: str) -> None:
        """Fetch the rest of a chunk from its presigned URL into the part file.

        Raises:
            _RetryTransfer: A failure worth another attempt with a new URL: a transport error
                mid-body, an expired URL (403), a refused range, a 5xx.
            EnterpriseError: A failure no retry will fix (a 4xx other than those above).
        """
        headers = {"Range": f"bytes={have}-"} if have else {}
        host = urlsplit(location).hostname or "?"
        try:
            # No Authorization header on this request, by construction (module docstring).
            response = self._send("GET", location, headers=headers, stream=True)
        except EnterpriseError:
            raise _RetryTransfer from None
        try:
            status = response.status_code
            if status == 416:
                # "Range not satisfiable". A part that already held every byte never gets here
                # (the caller's loop ends on the size match), so this offset is past the end of
                # the object: the part is wrong and the next attempt starts from zero.
                part.unlink(missing_ok=True)
                raise _RetryTransfer
            if status in (400, 403, 404):
                # S3 answers 403 once the 60 s URL has expired; the next attempt presigns anew.
                logger.warning(
                    "the download URL was refused (HTTP %d) for %s on %s",
                    status,
                    chunk_id,
                    host,
                    extra={"component": "enterprise", "event": "presign_refused"},
                )
                raise _RetryTransfer
            if status not in (200, 206):
                msg = f"chunk {chunk_id}: {host} answered HTTP {status}"
                raise EnterpriseError(msg)
            mode = "ab"
            if status == 200 and have:
                # The server ignored the Range header and is sending the whole object.
                mode = "wb"
            elif status == 206 and not _range_starts_at(response, have):
                part.unlink(missing_ok=True)
                raise _RetryTransfer
            try:
                with part.open(mode) as handle:
                    # Raw, not decoded: the object is a .tar.gz and must land byte for byte, and
                    # un-rechunked, so what arrived before a cut is on disk to resume from.
                    for block in response.iter_raw():
                        handle.write(block)
            except httpx.TransportError:
                logger.warning(
                    "the transfer of %s from %s broke off; will resume",
                    chunk_id,
                    host,
                    extra={"component": "enterprise", "event": "transfer_broken"},
                )
                raise _RetryTransfer from None
        finally:
            response.close()

    # -- authentication and requests --------------------------------------------------------

    def _get_json(self, path: str, what: str) -> object:
        """GET an API path and decode its JSON body (the caller checks the shape)."""
        response = self._api("GET", path)
        if response.status_code != 200:
            msg = f"{what}: HTTP {response.status_code}"
            raise EnterpriseError(msg)
        try:
            return response.json()
        except ValueError:
            msg = f"{what}: the reply is not JSON"
            raise EnterpriseError(msg) from None

    def _api(self, method: str, path: str) -> httpx.Response:
        """Send an authenticated API request, signing in again once on a 401."""
        url = API_BASE + path
        token, generation = self._token()
        response = self._send(method, url, headers={"Authorization": f"Bearer {token}"})
        if response.status_code != 401:
            return response
        self._sign_in_again(generation)
        token, _ = self._token()
        response = self._send(method, url, headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 401:
            msg = f"{method} {urlsplit(url).hostname}: HTTP 401 after a fresh sign-in"
            raise EnterpriseError(msg)
        return response

    def _token(self) -> tuple[str, int]:
        """The current access token and its generation, renewing it first if it is stale."""
        with self._lock:
            if self._access is None:
                self._login()
            elif self._clock() >= self._expires_at - _REFRESH_MARGIN_SECONDS:
                self._refresh_or_login()
            if self._access is None:
                raise EnterpriseError("sign-in produced no access token")
            return self._access, self._generation

    def _sign_in_again(self, rejected_generation: int) -> None:
        """Replace a rejected token, unless another thread already has."""
        with self._lock:
            if self._generation == rejected_generation:
                self._login()

    def _refresh_or_login(self) -> None:
        """Renew the access token with the refresh token; sign in afresh if that fails."""
        if self._refresh:
            try:
                response = self._send(
                    "POST",
                    f"{AUTH_BASE}/token-refresh",
                    json_body={"username": self._username, "refresh_token": self._refresh},
                )
                body = response.json() if response.status_code == 200 else None
            except (EnterpriseError, ValueError):
                body = None
            if isinstance(body, dict) and isinstance(body.get("access_token"), str):
                self._store_tokens(body, keep_refresh=True)
                return
            logger.info(
                "the token refresh did not work; signing in again",
                extra={"component": "enterprise", "event": "refresh_failed"},
            )
        self._login()

    def _login(self) -> None:
        """Sign in with the username and password and keep the tokens.

        Raises:
            EnterpriseError: The service rejected the credentials or sent an unusable reply.
        """
        response = self._send(
            "POST",
            f"{AUTH_BASE}/login",
            json_body={"username": self._username, "password": self._password},
        )
        if response.status_code != 200:
            msg = f"sign-in was refused (HTTP {response.status_code}); check the credentials file"
            raise EnterpriseError(msg)
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("access_token"), str):
            raise EnterpriseError("sign-in reply has no access token")
        self._store_tokens(body, keep_refresh=False)

    def _store_tokens(self, body: Mapping[str, object], *, keep_refresh: bool) -> None:
        """Record a token reply: the tokens, their expiry and a new generation."""
        access = body["access_token"]
        refresh = body.get("refresh_token")
        expires = body.get("expires_in")
        if not isinstance(access, str):
            raise EnterpriseError("sign-in reply has no access token")
        self._access = access
        if isinstance(refresh, str) and refresh:
            self._refresh = refresh
        elif not keep_refresh:
            self._refresh = None
        lifetime = expires if isinstance(expires, int) and expires > 0 else _DEFAULT_TOKEN_SECONDS
        self._expires_at = self._clock() + lifetime
        self._generation += 1

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        """Send one request, retrying transport errors, 429 and 5xx with backoff.

        Args:
            method: HTTP method.
            url: Absolute URL (possibly a presigned one: never put in a message).
            headers: Request headers.
            json_body: A JSON body, for the auth calls.
            stream: Leave the body unread, for the chunk download.

        Returns:
            The first response that is not a retryable failure. Its status is the caller's to
            judge, except that retry exhaustion raises.

        Raises:
            EnterpriseError: Out of attempts. The message holds the host and a status or an
                exception class name.
        """
        host = urlsplit(url).hostname or "?"
        for attempt in range(1, self._max_attempts + 1):
            try:
                request = self._client.build_request(
                    method, url, headers=dict(headers) if headers else None, json=json_body
                )
                response = self._client.send(request, stream=stream)
            except httpx.TransportError as exc:
                if attempt == self._max_attempts:
                    msg = f"{method} {host}: {type(exc).__name__} after {attempt} attempts"
                    raise EnterpriseError(msg) from None
                self._backoff(attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = _retry_after(response)
                status = response.status_code
                response.close()
                if attempt == self._max_attempts:
                    msg = f"{method} {host}: HTTP {status} after {attempt} attempts"
                    raise EnterpriseError(msg)
                self._backoff(attempt, retry_after)
                continue
            return response
        raise EnterpriseError(f"{method} {host}: no attempts were made")  # max_attempts < 1

    def _backoff(self, attempt: int, retry_after: float = 0.0) -> None:
        """Wait before the next try: exponential from `backoff_seconds`, or the server's ask."""
        self._sleep(max(self._backoff_seconds * 2 ** (attempt - 1), retry_after))


def _read_credentials(env_file: Path) -> tuple[str, str]:
    """Read `WME_USERNAME` and `WME_PASSWORD` from the env file.

    The value is the raw rest of the line after the first `=`; nothing is unquoted, unescaped or
    stripped except the line ending, because passwords may contain any character, including
    quotes and trailing spaces. The username is lowercased: the login endpoint requires it.
    """
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read the Enterprise credentials file {env_file}: {type(exc).__name__}"
        raise EnterpriseError(msg) from None
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, equals, value = line.partition("=")
        if equals and not key.lstrip().startswith("#"):
            values[key.strip()] = value
    username = values.get("WME_USERNAME", "").strip()
    password = values.get("WME_PASSWORD", "")
    for key, value in (("WME_USERNAME", username), ("WME_PASSWORD", password)):
        if not value:
            msg = f"{env_file} has no {key}= line"
            raise EnterpriseError(msg)
    return username.lower(), password


def _check_id(value: str, what: str) -> None:
    """Refuse an id that could not be a snapshot or chunk id (it goes into a URL and a path)."""
    if not _SAFE_ID.match(value):
        msg = f"not a valid {what} id"
        raise EnterpriseError(msg)


def _chunk_ids(value: object) -> tuple[str, ...]:
    """Chunk ids from a list of strings or of `{identifier: ...}` objects; others are ignored."""
    if not isinstance(value, list):
        return ()
    seen: dict[str, None] = {}
    for entry in value:
        chunk = entry.get("identifier") if isinstance(entry, Mapping) else entry
        if isinstance(chunk, str) and _SAFE_ID.match(chunk):
            seen[chunk] = None
    return tuple(seen)


def _int_header(response: httpx.Response, name: str) -> int | None:
    """A header as a non-negative integer, or None."""
    value = response.headers.get(name, "")
    return int(value) if value.isdigit() else None


def _retry_after(response: httpx.Response) -> float:
    """Seconds a `Retry-After` header asks for (digits only), capped; 0 when absent."""
    value = response.headers.get("retry-after", "")
    return min(float(value), _MAX_RETRY_AFTER_SECONDS) if value.isdigit() else 0.0


def _range_starts_at(response: httpx.Response, offset: int) -> bool:
    """Whether a 206 reply's `Content-Range` starts where the request asked.

    A reply without the header is believed: S3 always sends it, and refusing a reply for a
    missing optional header would turn a working resume into an endless restart.
    """
    header = response.headers.get("content-range")
    if header is None:
        return True
    match = _CONTENT_RANGE.match(header)
    return match is not None and int(match.group(1)) == offset


def _fsync(path: Path) -> None:
    """Flush a finished file to disk before it is renamed into place (it is on an NFS share)."""
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
