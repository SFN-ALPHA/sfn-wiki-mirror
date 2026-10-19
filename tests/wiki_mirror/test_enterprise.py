"""Qualifies the Wikimedia Enterprise client against a scripted fake of its published protocol.

`FakeWME` below speaks the three services the client uses (sign-in, the snapshot API with its
307-to-S3 download path, and S3 with `Range`), as measured on the live service on 2026-10-16.
Nothing touches the network and nothing sleeps: the clock and the backoff sleep are injected.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from tests.fixtures.clock import FrozenClock
from wiki_mirror.enterprise import ChunkBudget, EnterpriseClient, EnterpriseError

PASSWORD = 'p"a\'ss=w\\ord é '  # quotes, equals, backslash, non-ASCII, trailing space
SIGNATURE = "SECRETSIGNATURE123"
SNAPSHOT = "enwiki_namespace_0"
CHUNK = "enwiki_namespace_0_chunk_1"
DATA = bytes(range(256)) * 40  # 10,240 bytes

S3_HOST = "wme-eks-data-pr.s3.amazonaws.com"


def _body(status: int, data: bytes, headers: dict[str, str] | None = None) -> httpx.Response:
    """A response whose body is an unread stream, as a real S3 download is.

    `httpx.Response(content=...)` reads its body at construction, which a streamed download
    (`iter_raw`) would then refuse to read a second time.
    """
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(data))


class FakeWME:
    """A scripted Wikimedia Enterprise: auth, snapshot API and presigned S3 in one handler."""

    def __init__(self, data: bytes = DATA, etag: str = '"v1"') -> None:
        self.data = data
        self.etag = etag
        self.requests: list[httpx.Request] = []
        self.logins = 0
        self.refreshes = 0
        self.presigns = 0
        self.expires_in = 86_400
        self.refresh_ok = True
        self.token_serial = 0
        self.valid: set[str] = set()
        #: Answer this many API requests with 401 regardless of the token.
        self.reject_api = 0
        #: One-shot S3 responses, consumed in order before the normal behaviour.
        self.s3_script: list[Callable[[httpx.Request], httpx.Response]] = []
        #: Raised by the next API request when set (a transport failure).
        self.api_failure: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        """The transport handler."""
        self.requests.append(request)
        host = request.url.host
        path = request.url.path
        if host == "auth.enterprise.wikimedia.com":
            return self._auth(request, path)
        if host == S3_HOST:
            return self._s3(request)
        return self._api(request, path)

    def _new_token(self) -> str:
        self.token_serial += 1
        token = f"access-token-{self.token_serial}"
        self.valid.add(token)
        return token

    def _auth(self, request: httpx.Request, path: str) -> httpx.Response:
        body = json.loads(request.content)
        if path.endswith("/login"):
            self.logins += 1
            if body["password"] != PASSWORD:
                return httpx.Response(401, json={"message": "bad credentials"})
            return httpx.Response(
                200,
                json={
                    "access_token": self._new_token(),
                    "id_token": "id-token",
                    "refresh_token": "refresh-token-1",
                    "expires_in": self.expires_in,
                },
            )
        self.refreshes += 1
        if not self.refresh_ok or body["refresh_token"] != "refresh-token-1":
            return httpx.Response(401, json={"message": "expired"})
        return httpx.Response(200, json={"access_token": self._new_token()})

    def _api(self, request: httpx.Request, path: str) -> httpx.Response:
        if self.api_failure is not None:
            failure, self.api_failure = self.api_failure, None
            raise failure
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if self.reject_api > 0:
            self.reject_api -= 1
            return httpx.Response(401)
        if token not in self.valid:
            return httpx.Response(401)
        if path.endswith("/download"):
            if request.method == "HEAD":
                return httpx.Response(
                    200,
                    headers={
                        "content-length": str(len(self.data)),
                        "etag": self.etag,
                        "accept-ranges": "bytes",
                    },
                )
            self.presigns += 1
            return httpx.Response(
                307, headers={"location": f"https://{S3_HOST}/obj/{CHUNK}?sig={SIGNATURE}"}
            )
        if path.endswith("/chunks"):
            return httpx.Response(200, json=[{"identifier": CHUNK}, {"identifier": "other_2"}])
        return httpx.Response(
            200,
            json={
                "identifier": SNAPSHOT,
                "date_modified": "2026-10-01T01:17:10.982940484Z",
                "record_count": 7338688,
                "chunks": [CHUNK, "other_2"],
            },
        )

    def _s3(self, request: httpx.Request) -> httpx.Response:
        if self.s3_script:
            return self.s3_script.pop(0)(request)
        wanted = request.headers.get("range")
        if wanted is None:
            return _body(200, self.data)
        start = int(wanted.removeprefix("bytes=").removesuffix("-"))
        if start >= len(self.data):
            return httpx.Response(416)
        return _body(
            206,
            self.data[start:],
            {"content-range": f"bytes {start}-{len(self.data) - 1}/{len(self.data)}"},
        )

    def s3_requests(self) -> list[httpx.Request]:
        """Requests that reached S3."""
        return [request for request in self.requests if request.url.host == S3_HOST]


class Rig:
    """A client wired to a fake, with a frozen clock and a recording sleep."""

    def __init__(self, tmp_path: Path, clock: FrozenClock) -> None:
        self.fake = FakeWME()
        self.sleeps: list[float] = []
        self.clock = clock
        self.env = tmp_path / "enterprise.env"
        self.env.write_text(f"WME_USERNAME=Jane.Doe\nWME_PASSWORD={PASSWORD}\n", encoding="utf-8")
        self.client = EnterpriseClient(
            self.env,
            transport=httpx.MockTransport(self.fake.handler),
            clock=clock.monotonic,
            sleep=self.sleeps.append,
        )
        self.dest = tmp_path / "chunks" / f"{CHUNK}.tar.gz"
        self.dest.parent.mkdir()
        self.part = self.dest.with_name(self.dest.name + ".part")
        self.sidecar = self.dest.with_name(self.dest.name + ".etag")


@pytest.fixture
def rig(tmp_path: Path, frozen_clock: FrozenClock) -> Rig:
    """A client against a fresh fake."""
    return Rig(tmp_path, frozen_clock)


# -- credentials and tokens --------------------------------------------------------------------


@pytest.mark.contract
def test_login_sends_lowercase_username_and_the_raw_password(rig: Rig) -> None:
    rig.client.snapshot(SNAPSHOT)
    login = next(r for r in rig.fake.requests if r.url.path.endswith("/login"))
    assert json.loads(login.content) == {"username": "jane.doe", "password": PASSWORD}


@pytest.mark.contract
def test_token_is_cached_across_calls(rig: Rig) -> None:
    rig.client.snapshot(SNAPSHOT)
    rig.client.snapshot(SNAPSHOT)
    assert rig.fake.logins == 1


@pytest.mark.contract
def test_token_near_expiry_is_refreshed_not_relogged(rig: Rig) -> None:
    rig.client.snapshot(SNAPSHOT)
    rig.clock.advance(86_400 - 299)
    rig.client.snapshot(SNAPSHOT)
    assert (rig.fake.logins, rig.fake.refreshes) == (1, 1)
    assert rig.fake.requests[-1].headers["authorization"] == "Bearer access-token-2"


@pytest.mark.contract
def test_token_well_before_expiry_is_not_refreshed(rig: Rig) -> None:
    rig.client.snapshot(SNAPSHOT)
    rig.clock.advance(86_400 - 301)
    rig.client.snapshot(SNAPSHOT)
    assert rig.fake.refreshes == 0


@pytest.mark.contract
def test_failed_refresh_falls_back_to_a_fresh_login(rig: Rig) -> None:
    rig.client.snapshot(SNAPSHOT)
    rig.fake.refresh_ok = False
    rig.clock.advance(90_000)
    rig.client.snapshot(SNAPSHOT)
    assert (rig.fake.logins, rig.fake.refreshes) == (2, 1)


@pytest.mark.contract
def test_api_401_triggers_one_relogin_and_a_retry(rig: Rig) -> None:
    rig.client.snapshot(SNAPSHOT)
    rig.fake.reject_api = 1
    assert rig.client.snapshot(SNAPSHOT).identifier == SNAPSHOT
    assert rig.fake.logins == 2


@pytest.mark.contract
def test_api_second_401_raises_instead_of_looping(rig: Rig) -> None:
    rig.fake.reject_api = 2
    with pytest.raises(EnterpriseError, match="401"):
        rig.client.snapshot(SNAPSHOT)
    assert rig.fake.logins == 2


@pytest.mark.contract
def test_wrong_password_raises_without_echoing_it(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    rig = Rig(tmp_path, frozen_clock)
    rig.env.write_text("WME_USERNAME=x\nWME_PASSWORD=wrong-password\n", encoding="utf-8")
    client = EnterpriseClient(
        rig.env, transport=httpx.MockTransport(rig.fake.handler), sleep=rig.sleeps.append
    )
    with pytest.raises(EnterpriseError) as raised:
        client.snapshot(SNAPSHOT)
    assert "wrong-password" not in str(raised.value)
    assert "401" in str(raised.value)


@pytest.mark.unit
def test_env_file_missing_raises_naming_the_file(tmp_path: Path) -> None:
    with pytest.raises(EnterpriseError, match=r"enterprise\.env"):
        EnterpriseClient(tmp_path / "enterprise.env")


@pytest.mark.unit
def test_env_file_without_password_raises_naming_the_key(tmp_path: Path) -> None:
    path = tmp_path / "enterprise.env"
    path.write_text("WME_USERNAME=someone\n", encoding="utf-8")
    with pytest.raises(EnterpriseError, match="WME_PASSWORD"):
        EnterpriseClient(path)


@pytest.mark.unit
def test_env_file_comments_and_blank_lines_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "enterprise.env"
    path.write_text("# sign-in\n\nWME_USERNAME=u\nWME_PASSWORD=pw=1\n", encoding="utf-8")
    client = EnterpriseClient(path)
    client.close()


@pytest.mark.unit
def test_client_repr_does_not_show_credentials(rig: Rig) -> None:
    assert "jane" not in repr(rig.client).lower()
    assert "ss=w" not in repr(rig.client)


# -- snapshot description ----------------------------------------------------------------------


@pytest.mark.contract
def test_snapshot_reads_date_count_and_chunk_ids(rig: Rig) -> None:
    meta = rig.client.snapshot(SNAPSHOT)
    assert (meta.date, meta.record_count, meta.chunk_ids) == (
        "2026-10-01",
        7338688,
        (CHUNK, "other_2"),
    )


@pytest.mark.contract
def test_snapshot_chunk_objects_are_accepted_as_well_as_strings(rig: Rig) -> None:
    original = rig.fake._api

    def api(request: httpx.Request, path: str) -> httpx.Response:
        response = original(request, path)
        if path.endswith(SNAPSHOT):
            body = json.loads(response.content)
            body["chunks"] = [{"identifier": CHUNK}]
            return httpx.Response(200, json=body)
        return response

    rig.fake._api = api  # type: ignore[method-assign]
    assert rig.client.snapshot(SNAPSHOT).chunk_ids == (CHUNK,)


@pytest.mark.contract
def test_snapshot_without_chunks_falls_back_to_the_listing(rig: Rig) -> None:
    original = rig.fake._api

    def api(request: httpx.Request, path: str) -> httpx.Response:
        response = original(request, path)
        if path.endswith(SNAPSHOT):
            body = json.loads(response.content)
            del body["chunks"]
            return httpx.Response(200, json=body)
        return response

    rig.fake._api = api  # type: ignore[method-assign]
    assert rig.client.snapshot(SNAPSHOT).chunk_ids == (CHUNK, "other_2")


@pytest.mark.unit
def test_snapshot_identifier_with_a_slash_is_refused(rig: Rig) -> None:
    with pytest.raises(EnterpriseError, match="valid snapshot id"):
        rig.client.snapshot("../x")


# -- downloading -------------------------------------------------------------------------------


@pytest.mark.contract
def test_download_fetches_the_whole_chunk_and_leaves_no_part_file(rig: Rig) -> None:
    result = rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
    assert result == rig.dest
    assert rig.dest.read_bytes() == DATA
    assert not rig.part.exists()
    assert not rig.sidecar.exists()


@pytest.mark.contract
def test_download_presigned_request_carries_no_authorization_header(rig: Rig) -> None:
    rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
    s3 = rig.fake.s3_requests()
    assert len(s3) == 1
    assert "authorization" not in s3[0].headers


@pytest.mark.contract
def test_download_api_requests_carry_the_bearer_token(rig: Rig) -> None:
    rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
    api = [r for r in rig.fake.requests if r.url.host == "api.enterprise.wikimedia.com"]
    assert api
    assert all(r.headers["authorization"].startswith("Bearer ") for r in api)


@pytest.mark.contract
def test_download_resumes_a_part_file_with_a_range_request(rig: Rig) -> None:
    rig.part.write_bytes(DATA[:4000])
    rig.sidecar.write_text('"v1"', encoding="utf-8")
    rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
    assert rig.fake.s3_requests()[0].headers["range"] == "bytes=4000-"
    assert rig.dest.read_bytes() == DATA


@pytest.mark.contract
def test_download_changed_etag_discards_the_part_file(rig: Rig) -> None:
    rig.part.write_bytes(b"stale bytes of an older object")
    rig.sidecar.write_text('"v0"', encoding="utf-8")
    rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
    assert "range" not in rig.fake.s3_requests()[0].headers
    assert rig.dest.read_bytes() == DATA


@pytest.mark.contract
def test_download_part_file_without_a_sidecar_is_not_trusted(rig: Rig) -> None:
    rig.part.write_bytes(DATA[:100])
    rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
    assert "range" not in rig.fake.s3_requests()[0].headers


@pytest.mark.contract
def test_download_oversized_part_file_is_discarded(rig: Rig) -> None:
    rig.part.write_bytes(DATA + b"extra")
    rig.sidecar.write_text('"v1"', encoding="utf-8")
    rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
    assert rig.dest.read_bytes() == DATA


@pytest.mark.contract
def test_download_server_that_ignores_range_restarts_the_file(rig: Rig) -> None:
    rig.part.write_bytes(DATA[:3000])
    rig.sidecar.write_text('"v1"', encoding="utf-8")
    rig.fake.s3_script.append(lambda _request: _body(200, DATA))
    rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
    assert rig.dest.read_bytes() == DATA


@pytest.mark.contract
def test_download_416_discards_the_part_and_starts_over(rig: Rig) -> None:
    rig.part.write_bytes(DATA[:3000])
    rig.sidecar.write_text('"v1"', encoding="utf-8")
    rig.fake.s3_script.append(lambda _request: httpx.Response(416))
    rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
    assert rig.dest.read_bytes() == DATA
    assert len(rig.fake.s3_requests()) == 2


@pytest.mark.contract
def test_download_part_already_complete_is_renamed_without_a_transfer(rig: Rig) -> None:
    rig.part.write_bytes(DATA)
    rig.sidecar.write_text('"v1"', encoding="utf-8")
    rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
    assert rig.fake.s3_requests() == []
    assert rig.dest.read_bytes() == DATA


@pytest.mark.contract
def test_download_existing_file_of_the_right_size_is_skipped(rig: Rig) -> None:
    rig.dest.write_bytes(DATA)
    rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
    assert rig.fake.presigns == 0


@pytest.mark.contract
def test_download_connection_cut_mid_body_resumes_with_a_new_url(rig: Rig) -> None:
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield DATA[:2048]
            raise httpx.ReadError("connection reset")

    rig.fake.s3_script.append(lambda _request: httpx.Response(200, stream=Broken()))
    rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
    assert rig.fake.presigns == 2
    assert rig.fake.s3_requests()[1].headers["range"] == "bytes=2048-"
    assert rig.dest.read_bytes() == DATA


@pytest.mark.contract
def test_download_expired_url_403_is_retried_with_a_fresh_presign(rig: Rig) -> None:
    rig.fake.s3_script.append(lambda _request: httpx.Response(403))
    rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
    assert rig.fake.presigns == 2
    assert rig.sleeps == [2.0]


@pytest.mark.contract
def test_download_short_body_every_time_fails_and_keeps_the_part(rig: Rig) -> None:
    # The server delivers 5,000 bytes, then closes every resumed transfer with nothing.
    rig.fake.s3_script.append(lambda _request: _body(200, DATA[:5000]))
    rig.fake.s3_script.extend(lambda _request: _body(206, b"") for _ in range(10))
    with pytest.raises(EnterpriseError, match="incomplete after 4 attempts"):
        rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
    assert not rig.dest.exists()
    assert rig.part.stat().st_size == 5000


@pytest.mark.contract
def test_download_oversized_body_fails_and_removes_the_part(rig: Rig) -> None:
    rig.fake.s3_script.append(lambda _request: _body(200, DATA + b"junk"))
    with pytest.raises(EnterpriseError, match="downloaded 10244 bytes where the API reported"):
        rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
    assert not rig.dest.exists()
    assert not rig.part.exists()


@pytest.mark.contract
def test_download_api_answering_200_instead_of_a_redirect_raises(rig: Rig) -> None:
    original = rig.fake._api

    def api(request: httpx.Request, path: str) -> httpx.Response:
        if path.endswith("/download") and request.method == "GET":
            return httpx.Response(200, content=b"surprise")
        return original(request, path)

    rig.fake._api = api  # type: ignore[method-assign]
    with pytest.raises(EnterpriseError, match="expected a redirect"):
        rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)


@pytest.mark.contract
def test_download_head_without_content_length_raises(rig: Rig) -> None:
    original = rig.fake._api

    def api(request: httpx.Request, path: str) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200)
        return original(request, path)

    rig.fake._api = api  # type: ignore[method-assign]
    with pytest.raises(EnterpriseError, match="content-length"):
        rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)


@pytest.mark.unit
def test_download_chunk_id_with_a_path_separator_is_refused(rig: Rig) -> None:
    with pytest.raises(EnterpriseError, match="valid chunk id"):
        rig.client.download_chunk(SNAPSHOT, "../../etc/passwd", rig.dest)


# -- retries -----------------------------------------------------------------------------------


@pytest.mark.contract
def test_api_5xx_is_retried_with_doubling_backoff(rig: Rig) -> None:
    original = rig.fake._api
    failures = [503, 502]

    def api(request: httpx.Request, path: str) -> httpx.Response:
        if failures:
            return httpx.Response(failures.pop(0))
        return original(request, path)

    rig.fake._api = api  # type: ignore[method-assign]
    assert rig.client.snapshot(SNAPSHOT).identifier == SNAPSHOT
    assert rig.sleeps == [2.0, 4.0]


@pytest.mark.contract
def test_api_429_honours_retry_after(rig: Rig) -> None:
    original = rig.fake._api
    limited = [True]

    def api(request: httpx.Request, path: str) -> httpx.Response:
        if limited:
            limited.pop()
            return httpx.Response(429, headers={"retry-after": "30"})
        return original(request, path)

    rig.fake._api = api  # type: ignore[method-assign]
    rig.client.snapshot(SNAPSHOT)
    assert rig.sleeps == [30.0]


@pytest.mark.contract
def test_api_persistent_5xx_gives_up_with_a_clear_error(rig: Rig) -> None:
    rig.fake._api = lambda _request, _path: httpx.Response(503)  # type: ignore[method-assign]
    with pytest.raises(EnterpriseError, match="HTTP 503 after 4 attempts"):
        rig.client.snapshot(SNAPSHOT)
    assert len(rig.sleeps) == 3


@pytest.mark.contract
def test_transport_error_is_retried_then_succeeds(rig: Rig) -> None:
    rig.client.snapshot(SNAPSHOT)
    rig.fake.api_failure = httpx.ConnectError("no route")
    assert rig.client.snapshot(SNAPSHOT).identifier == SNAPSHOT
    assert rig.sleeps == [2.0]


# -- budget ------------------------------------------------------------------------------------


@pytest.mark.contract
def test_download_over_the_monthly_budget_raises_naming_the_budget(
    rig: Rig, tmp_path: Path
) -> None:
    budget = ChunkBudget(tmp_path / "state" / "budget.json", 1)
    rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest, budget=budget)
    rig.dest.unlink()
    with pytest.raises(EnterpriseError, match="budget of 1"):
        rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest, budget=budget)


@pytest.mark.contract
def test_download_retry_after_expired_url_costs_a_second_unit_of_budget(
    rig: Rig, tmp_path: Path
) -> None:
    path = tmp_path / "budget.json"
    rig.fake.s3_script.append(lambda _request: httpx.Response(403))
    rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest, budget=ChunkBudget(path, 10))
    assert sum(json.loads(path.read_text(encoding="utf-8")).values()) == 2


@pytest.mark.unit
def test_budget_counts_per_utc_month(tmp_path: Path) -> None:
    now = [datetime(2026, 10, 31, 23, 59, tzinfo=UTC)]
    budget = ChunkBudget(tmp_path / "b.json", 2, utc_now=lambda: now[0])
    assert budget.charge() == 1
    assert budget.charge() == 2
    with pytest.raises(EnterpriseError, match="2026-10"):
        budget.charge()
    now[0] = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    assert budget.charge() == 1


@pytest.mark.unit
def test_budget_counter_survives_a_new_instance(tmp_path: Path) -> None:
    ChunkBudget(tmp_path / "b.json", 5).charge()
    assert ChunkBudget(tmp_path / "b.json", 5).charge() == 2


@pytest.mark.unit
def test_budget_unreadable_file_raises_instead_of_resetting(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(EnterpriseError, match="unreadable"):
        ChunkBudget(path, 5).charge()


# -- secrets -----------------------------------------------------------------------------------


@pytest.mark.contract
def test_no_secret_reaches_the_logs_or_error_messages(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    messages: list[str] = []
    rig.fake.s3_script.extend(lambda _request: httpx.Response(503) for _ in range(30))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(EnterpriseError) as raised:
            rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
        messages.append(str(raised.value))
        rig.fake.s3_script.clear()
        rig.fake.s3_script.append(lambda _request: httpx.Response(403))
        rig.client.download_chunk(SNAPSHOT, CHUNK, rig.dest)
        rig.fake.api_failure = httpx.ConnectError(f"cannot reach https://x/?sig={SIGNATURE}")
        rig.client.snapshot(SNAPSHOT)
    everything = caplog.text + " ".join(messages)
    for secret in (PASSWORD, "jane.doe", "access-token", "refresh-token", SIGNATURE):
        assert secret not in everything


@pytest.mark.contract
def test_transport_failure_message_does_not_carry_the_exception_text(rig: Rig) -> None:
    rig.fake.api_failure = httpx.ConnectError(f"boom {SIGNATURE}")
    original = rig.fake.handler

    def always_fail(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.enterprise.wikimedia.com":
            raise httpx.ConnectError(f"boom {SIGNATURE}")
        return original(request)

    client = EnterpriseClient(
        rig.env, transport=httpx.MockTransport(always_fail), sleep=rig.sleeps.append
    )
    with pytest.raises(EnterpriseError) as raised:
        client.snapshot(SNAPSHOT)
    assert SIGNATURE not in str(raised.value)
    assert "ConnectError" in str(raised.value)
    assert raised.value.__cause__ is None
