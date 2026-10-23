"""Qualifies the command line: exit codes, the serve guard, status output, stage wiring, keys.

Runs `main` in-process with a temporary settings file and an explicit environment. The download
and parse stages are replaced by recording fakes patched into the command module, Qdrant by its
in-memory mode and uvicorn by a recorder, so nothing touches the network, a port or the real
data directory. Failure paths named here: invalid settings and an unknown command (2), `serve`
without the key (2), with a malformed clients file (2) or without Qdrant (1), a store that cannot
be opened (served `not_ready`, `degraded`), a failing stage (1), a parse with nothing downloaded
(1), the Enterprise client being closed when a download fails, and the `key` command's refusals:
`primary`, a bad name, a name already taken, a non-root caller, an unknown name, a malformed file.
`maintain` runs against a fake Qdrant and a fake unit reader: it exits 0 whenever its record was
written (Qdrant down included), takes no build lock and leaves the build state alone, and exits 1
when the record cannot be written or the run raises.

See docs/DESIGN.md section 6.
"""

from __future__ import annotations

import functools
import io
import json
import sqlite3
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException

from tests.fixtures.qdrant_fakes import LEXICAL, SEMANTIC, FakeQdrant, lexical_info, semantic_info
from tests.fixtures.wiki_mirror_fakes import (
    API_KEY,
    FakeState,
    FakeStateStore,
    build_snapshot,
    index_articles,
    make_article,
    make_config,
    make_snapshot,
    memory_client,
)
from wiki_mirror import __main__ as cli
from wiki_mirror.build import (
    BuildError,
    BuildLockedError,
    BuildState,
    build_lock,
    read_build_state,
    write_build_state,
)
from wiki_mirror.embed import EmbedError
from wiki_mirror.keys import hash_key
from wiki_mirror.maintain import QDRANT_TIMEOUT_SECONDS, UnitReading, run_maintenance
from wiki_mirror.maintenance_record import QdrantUnitReport, read_maintenance_record
from wiki_mirror.records import SnapshotMeta
from wiki_mirror.serving import ServedSnapshot

pytestmark = pytest.mark.unit

_AUTH = {"Authorization": f"Bearer {API_KEY}"}


def _settings(tmp_path: Path, extra: str = "") -> Path:
    """Write a settings file whose paths are under `tmp_path`."""
    path = tmp_path / "mirror.yaml"
    path.write_text(
        f"data_dir: {tmp_path / 'data'}\nclients_file: {tmp_path / 'clients.json'}\n{extra}",
        encoding="utf-8",
    )
    return path


def _run(
    argv: list[str], env: dict[str, str] | None = None, *, euid: int = 0
) -> tuple[int, str, str]:
    """Run `main` (as root unless told otherwise) and return its exit code, stdout and stderr."""
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, environ=env or {}, out=out, err=err, euid=lambda: euid)
    return code, out.getvalue(), err.getvalue()


class _UvicornRecorder:
    """Stands in for `uvicorn.run`, remembering how it was called.

    `while_serving` runs inside the call, while the app's snapshot holder and Qdrant client are
    still open, as they are for the life of the real server.
    """

    def __init__(self, while_serving: Callable[[TestClient], None] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._while_serving = while_serving

    def __call__(self, app: Any, **kwargs: Any) -> None:
        self.calls.append({"app": app, **kwargs})
        if self._while_serving is not None:
            self._while_serving(TestClient(app))


# -- configuration and usage --------------------------------------------------------------------


def test_main_invalid_settings_exit_2_with_the_problem_on_stderr(tmp_path: Path) -> None:
    config = _settings(tmp_path, "listen_port: 0\n")
    code, _, err = _run(["--config", str(config), "status"])
    assert code == 2
    assert "listen_port" in err


def test_main_unknown_settings_key_exits_2(tmp_path: Path) -> None:
    config = _settings(tmp_path, "colour: blue\n")
    assert _run(["--config", str(config), "status"])[0] == 2


def test_main_api_key_in_the_settings_file_exits_2_and_is_not_echoed(tmp_path: Path) -> None:
    config = _settings(tmp_path, f"api_key: {API_KEY}\n")
    code, _, err = _run(["--config", str(config), "status"])
    assert code == 2
    assert API_KEY not in err


def test_main_unknown_command_exits_2() -> None:
    with pytest.raises(SystemExit) as raised:
        _run(["frobnicate"])
    assert raised.value.code == 2


def test_main_no_command_exits_2() -> None:
    with pytest.raises(SystemExit) as raised:
        _run([])
    assert raised.value.code == 2


# -- serve ------------------------------------------------------------------------------------


def test_serve_without_the_api_key_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _UvicornRecorder()
    monkeypatch.setattr(cli.uvicorn, "run", recorder)
    code, _, err = _run(["--config", str(_settings(tmp_path)), "serve"])
    assert code == 2
    assert "WIKI_MIRROR_API_KEY" in err
    assert recorder.calls == []


def test_serve_with_a_too_short_key_exits_2(tmp_path: Path) -> None:
    code, _, err = _run(
        ["--config", str(_settings(tmp_path)), "serve"], {"WIKI_MIRROR_API_KEY": "short"}
    )
    assert code == 2
    assert "WIKI_MIRROR_API_KEY" in err


def test_serve_with_a_malformed_clients_file_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _UvicornRecorder()
    monkeypatch.setattr(cli.uvicorn, "run", recorder)
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: memory_client())
    (tmp_path / "clients.json").write_text("{not json", encoding="utf-8")
    code, _, err = _run(
        ["--config", str(_settings(tmp_path)), "serve"], {"WIKI_MIRROR_API_KEY": API_KEY}
    )
    assert code == 2
    assert "clients.json" in err
    assert recorder.calls == []


def test_serve_runs_uvicorn_on_the_configured_address_without_an_access_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _UvicornRecorder()
    monkeypatch.setattr(cli.uvicorn, "run", recorder)
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: memory_client())
    config = _settings(tmp_path, "listen_host: 127.0.0.1\nlisten_port: 8311\n")
    code, _, _ = _run(["--config", str(config), "serve"], {"WIKI_MIRROR_API_KEY": API_KEY})
    assert code == 0
    assert (recorder.calls[0]["host"], recorder.calls[0]["port"]) == ("127.0.0.1", 8311)
    assert recorder.calls[0]["access_log"] is False


def test_serve_with_qdrant_unreachable_exits_1_so_systemd_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(client: QdrantClient) -> str:
        raise ConnectionError

    recorder = _UvicornRecorder()
    monkeypatch.setattr(cli.uvicorn, "run", recorder)
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: memory_client())
    monkeypatch.setattr(cli, "aliased_collection", down)
    code, _, err = _run(
        ["--config", str(_settings(tmp_path)), "serve"], {"WIKI_MIRROR_API_KEY": API_KEY}
    )
    assert code == 1
    assert "Qdrant" in err
    assert recorder.calls == []


def _serve_and_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, built: QdrantClient
) -> dict[str, Any]:
    """Run `serve` over `built` and return the search status and the health body it answered."""
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: built)
    seen: dict[str, Any] = {}

    def probe(app: TestClient) -> None:
        search = app.post("/v1/wiki/search", json={"query": "one"}, headers=_AUTH)
        seen["search"] = search.status_code
        seen["health"] = app.get("/v1/wiki/health", headers=_AUTH).json()

    monkeypatch.setattr(cli.uvicorn, "run", _UvicornRecorder(probe))
    code, _, _ = _run(
        ["--config", str(_settings(tmp_path)), "serve"], {"WIKI_MIRROR_API_KEY": API_KEY}
    )
    assert code == 0
    return seen


def test_serve_alias_whose_store_is_missing_starts_not_ready_and_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Indexed under the data directory `serve` will use, so the index state is there and only
    # the article store is not.
    _, built, _ = index_articles(tmp_path, [make_article(1, "One")])
    seen = _serve_and_probe(tmp_path, monkeypatch, built)
    assert seen["search"] == 503
    assert (seen["health"]["status"], seen["health"]["collection"]) == (
        "degraded",
        "wikipedia_lexical_20261001",
    )


def test_serve_alias_whose_index_state_is_missing_starts_not_ready_and_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Indexed under another data directory: the collection exists, `serve` finds no state for it.
    _, built, _ = index_articles(tmp_path / "x", [make_article(1, "One")])
    seen = _serve_and_probe(tmp_path, monkeypatch, built)
    assert seen["search"] == 503
    assert (seen["health"]["status"], seen["health"]["applied_snapshot"]) == ("degraded", None)


def _health_clock_near_the_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `serve`'s health route read a date a few days after the test snapshot (2026-10-01).

    `serve` builds its own health report on the system clock, and a test that expects `ok` must
    not start failing forty days after the snapshot date it was written with.
    """
    real = cli.build_service

    def with_fixed_clock(*args: Any, **kwargs: Any) -> Any:
        return real(*args, clock=lambda: datetime(2026, 10, 5, tzinfo=UTC), **kwargs)

    monkeypatch.setattr(cli, "build_service", with_fixed_clock)


def test_serve_opens_the_store_the_alias_points_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    build_snapshot(make_config(tmp_path), client, [make_article(1, "One")])
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: client)
    _health_clock_near_the_snapshot(monkeypatch)
    seen: dict[str, Any] = {}

    def probe(app: TestClient) -> None:
        seen["health"] = app.get("/v1/wiki/health", headers=_AUTH).json()
        seen["search"] = app.post("/v1/wiki/search", json={"query": "one"}, headers=_AUTH).json()

    monkeypatch.setattr(cli.uvicorn, "run", _UvicornRecorder(probe))
    _run(["--config", str(_settings(tmp_path)), "serve"], {"WIKI_MIRROR_API_KEY": API_KEY})
    assert (seen["health"]["status"], seen["health"]["snapshot"]) == ("ok", "2026-10-01")
    assert seen["search"]["results"][0]["title"] == "One"


def test_serve_closes_the_open_store_and_qdrant_when_the_server_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    build_snapshot(make_config(tmp_path), client, [make_article(1, "One")])
    closed: list[str] = []
    real_close = client.close
    monkeypatch.setattr(client, "close", lambda: (closed.append("qdrant"), real_close())[1])
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: client)
    holders: list[ServedSnapshot] = []

    class Recording(ServedSnapshot):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            holders.append(self)

    stores: list[Any] = []

    def probe(_: TestClient) -> None:
        with holders[0].lease() as serving:
            assert serving is not None
            stores.append(serving.store)

    monkeypatch.setattr(cli, "ServedSnapshot", Recording)
    monkeypatch.setattr(cli.uvicorn, "run", _UvicornRecorder(probe))
    code, _, _ = _run(
        ["--config", str(_settings(tmp_path)), "serve"], {"WIKI_MIRROR_API_KEY": API_KEY}
    )
    with pytest.raises(sqlite3.ProgrammingError):
        stores[0].count()
    assert (code, closed) == (0, ["qdrant"])


# -- status -----------------------------------------------------------------------------------


def test_status_prints_the_build_state_as_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = FakeStateStore(FakeState(phase="parsing", chunks_total=435, chunks_parsed=12))
    monkeypatch.setattr(cli, "read_build_state", state.read)
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: memory_client())
    code, out, _ = _run(["--config", str(_settings(tmp_path)), "status"])
    report = json.loads(out)
    assert code == 0
    assert (report["build"]["phase"], report["build"]["chunks_parsed"]) == ("parsing", 12)
    assert "serving" not in report


def test_status_with_an_index_reports_the_collection_and_passage_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client, _ = index_articles(tmp_path, [make_article(1, "One"), make_article(2, "Two")])
    monkeypatch.setattr(cli, "read_build_state", FakeStateStore(FakeState(phase="serving")).read)
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: client)
    code, out, _ = _run(["--config", str(_settings(tmp_path)), "status"])
    assert code == 0
    assert json.loads(out)["serving"]["collection"] == "wikipedia_lexical_20261001"
    assert json.loads(out)["serving"]["passages"] >= 2


def test_status_counts_the_articles_of_the_store_the_alias_points_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    articles = [make_article(1, "One"), make_article(2, "Two")]
    build_snapshot(make_config(tmp_path), client, articles)
    monkeypatch.setattr(cli, "read_build_state", FakeStateStore().read)
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: client)
    _, out, _ = _run(["--config", str(_settings(tmp_path)), "status"])
    assert json.loads(out)["serving"]["articles"] == 2


def test_status_reports_the_snapshot_the_collection_reflects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = memory_client()
    build_snapshot(make_config(tmp_path), client, [make_article(1, "One")])
    monkeypatch.setattr(cli, "read_build_state", FakeStateStore().read)
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: client)
    _, out, _ = _run(["--config", str(_settings(tmp_path)), "status"])
    assert json.loads(out)["serving"]["applied_snapshot"] == "20261001"


def test_status_alias_without_a_store_names_the_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client, _ = index_articles(tmp_path, [make_article(1, "One")])
    monkeypatch.setattr(cli, "read_build_state", FakeStateStore().read)
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: client)
    _, out, _ = _run(["--config", str(_settings(tmp_path)), "status"])
    serving = json.loads(out)["serving"]
    assert "no complete article store" in serving["problem"]
    assert serving["applied_snapshot"] == "20261001"


def test_status_alias_without_an_index_state_names_the_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client, _ = index_articles(tmp_path / "x", [make_article(1, "One")])
    monkeypatch.setattr(cli, "read_build_state", FakeStateStore().read)
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: client)
    _, out, _ = _run(["--config", str(_settings(tmp_path)), "status"])
    serving = json.loads(out)["serving"]
    assert "no usable index state" in serving["problem"]
    assert serving["applied_snapshot"] is None


def test_status_with_qdrant_down_still_prints_the_build_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(client: QdrantClient) -> str:
        raise ConnectionError

    state = FakeStateStore(FakeState(phase="failed", error="index: OSError"))
    monkeypatch.setattr(cli, "read_build_state", state.read)
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: memory_client())
    monkeypatch.setattr(cli, "aliased_collection", down)
    code, out, _ = _run(["--config", str(_settings(tmp_path)), "status"])
    report = json.loads(out)
    assert code == 0
    assert report["build"]["error"] == "index: OSError"
    assert "not answering" in report["serving"]["error"]


def test_status_shows_the_last_update_from_the_updaters_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = {
        "at": "2026-10-18T02:21:07-05:00",
        "from": "d127090abcde",
        "to": None,
        "outcome": "deferred",
        "detail": "update to e238101fghij waits: a build is running",
        "unattended": True,
    }
    path = tmp_path / "data" / "state" / "update.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(cli, "read_build_state", FakeStateStore().read)
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: memory_client())
    code, out, _ = _run(["--config", str(_settings(tmp_path)), "status"])
    assert code == 0
    assert json.loads(out)["update"] == record


def test_status_without_an_update_record_says_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "read_build_state", FakeStateStore().read)
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: memory_client())
    _, out, _ = _run(["--config", str(_settings(tmp_path)), "status"])
    assert json.loads(out)["update"] is None


def test_status_output_holds_no_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "read_build_state", FakeStateStore().read)
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: memory_client())
    _, out, err = _run(
        ["--config", str(_settings(tmp_path)), "status"], {"WIKI_MIRROR_API_KEY": API_KEY}
    )
    assert API_KEY not in out + err


# -- stages -----------------------------------------------------------------------------------


def test_index_without_any_article_store_exits_1_and_says_to_parse_first(tmp_path: Path) -> None:
    code, _, err = _run(["--config", str(_settings(tmp_path)), "index"])
    assert code == 1
    assert "run parse first" in err


def test_index_stage_gives_the_qdrant_client_a_bulk_load_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    class _Closeable:
        def close(self) -> None:
            pass

    def client(**kwargs: Any) -> _Closeable:
        seen.update(kwargs)
        return _Closeable()

    monkeypatch.setattr(cli, "QdrantClient", client)
    monkeypatch.setattr(cli.ArticleStore, "open", lambda *_a, **_k: _Closeable())
    monkeypatch.setattr(cli, "run_index", lambda *_a, **_k: 3)
    assert cli._index(make_config(tmp_path), tmp_path / "store.db") == 3
    # Above the client's 5 s default, which a 7 s upsert under memory pressure outlasted.
    assert seen["timeout"] >= 60


def test_build_runs_download_then_parse_then_index_with_the_same_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, object]] = []
    snapshot = make_snapshot()
    store_file = tmp_path / "store.db"

    def download(config: object) -> SnapshotMeta:
        calls.append(("download", None))
        return snapshot

    def parse(config: object, snap: SnapshotMeta | None = None) -> Path:
        calls.append(("parse", snap))
        return store_file

    def index(config: object, path: Path | None = None) -> int:
        calls.append(("index", path))
        return 0

    monkeypatch.setattr(cli, "_download", download)
    monkeypatch.setattr(cli, "_parse", parse)
    monkeypatch.setattr(cli, "_index", index)
    code, _, _ = _run(["--config", str(_settings(tmp_path)), "build"])
    assert code == 0
    assert calls == [("download", None), ("parse", snapshot), ("index", store_file)]


def test_build_stops_at_the_first_failing_stage_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def failing(config: object) -> SnapshotMeta:
        raise OSError("disk full")

    monkeypatch.setattr(cli, "_download", failing)
    monkeypatch.setattr(cli, "_parse", lambda config, snap=None: calls.append("parse"))
    code, _, err = _run(["--config", str(_settings(tmp_path)), "build"])
    assert code == 1
    assert "OSError" in err
    assert calls == []


class _FakeEnterprise:
    """Stands in for `EnterpriseClient`; records whether it was built and closed."""

    built = False
    closed = False

    def __init__(self, env_file: Path) -> None:
        del env_file
        _FakeEnterprise.built = True

    def close(self) -> None:
        _FakeEnterprise.closed = True


def _failing_download(config: object, client: object) -> SnapshotMeta:
    raise OSError("network down")


def test_download_failure_still_closes_the_enterprise_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeEnterprise.built = _FakeEnterprise.closed = False
    monkeypatch.setattr(cli, "EnterpriseClient", _FakeEnterprise)
    monkeypatch.setattr(cli, "run_download", _failing_download)
    code, _, _ = _run(["--config", str(_settings(tmp_path)), "download"])
    assert code == 1
    assert _FakeEnterprise.closed is True


def test_parse_uses_the_snapshot_already_downloaded_and_never_asks_enterprise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeEnterprise.built = False
    parsed: list[SnapshotMeta] = []
    snapshot = make_snapshot("2026-09-01")
    store_file = tmp_path / "store.db"

    def run_parse(config: object, snap: SnapshotMeta) -> Path:
        parsed.append(snap)
        return store_file

    monkeypatch.setattr(cli, "EnterpriseClient", _FakeEnterprise)
    monkeypatch.setattr(cli, "latest_downloaded_snapshot", lambda config: snapshot)
    monkeypatch.setattr(cli, "run_parse", run_parse)
    code, _, _ = _run(["--config", str(_settings(tmp_path)), "parse"])
    assert code == 0
    assert parsed == [snapshot]
    assert _FakeEnterprise.built is False


def test_parse_with_nothing_downloaded_exits_1_and_says_to_download_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeEnterprise.built = False
    monkeypatch.setattr(cli, "EnterpriseClient", _FakeEnterprise)
    code, _, err = _run(["--config", str(_settings(tmp_path)), "parse"])
    assert code == 1
    assert "run download first" in err
    assert _FakeEnterprise.built is False


# -- a failure always shows, and only one build runs at a time ---------------------------------


def _state(tmp_path: Path) -> BuildState:
    """The build state the test's settings point at."""
    return read_build_state(tmp_path / "data")


def test_a_failure_before_any_stage_bookkeeping_is_recorded_in_the_build_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(config: object) -> SnapshotMeta:
        raise OSError("cannot open https://example.test/chunk?token=SECRET")

    monkeypatch.setattr(cli, "_download", failing)
    code, _, _ = _run(["--config", str(_settings(tmp_path)), "build"])
    state = _state(tmp_path)
    assert code == 1
    assert state.phase == "failed"
    assert state.error == "build: OSError (see the build log)"
    assert "SECRET" not in state.error


def test_a_build_error_keeps_its_message_in_the_recorded_failure(tmp_path: Path) -> None:
    code, _, _ = _run(["--config", str(_settings(tmp_path)), "parse"])
    state = _state(tmp_path)
    assert code == 1
    assert state.phase == "failed"
    assert "no complete snapshot download" in state.error
    assert state.error.startswith("parse: BuildError")


def test_a_stage_that_found_its_own_reason_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(config: Any) -> SnapshotMeta:
        write_build_state(
            config.data_dir,
            BuildState(phase="failed", error="2 of 435 chunk downloads failed; first: chunk_7"),
        )
        raise BuildError("2 of 435 chunk downloads failed")

    monkeypatch.setattr(cli, "_download", failing)
    _run(["--config", str(_settings(tmp_path)), "build"])
    assert _state(tmp_path).error == "2 of 435 chunk downloads failed; first: chunk_7"


def test_an_old_failure_does_not_hide_a_new_one_that_no_stage_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_build_state(tmp_path / "data", BuildState(phase="failed", error="last month's reason"))

    def failing(config: object) -> SnapshotMeta:
        raise PermissionError("denied")

    monkeypatch.setattr(cli, "_download", failing)
    _run(["--config", str(_settings(tmp_path)), "build"])
    assert _state(tmp_path).error == "build: PermissionError (see the build log)"


def test_a_failure_that_cannot_be_recorded_still_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def failing(config: object) -> SnapshotMeta:
        raise OSError("boom")

    def unwritable(*_args: object, **_kwargs: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(cli, "_download", failing)
    monkeypatch.setattr(cli, "write_build_state", unwritable)
    code, _, err = _run(["--config", str(_settings(tmp_path)), "build"])
    assert code == 1
    assert "boom" in err  # the original failure is what the operator reads
    assert "could not record the failure" in caplog.text


def test_a_second_build_while_one_is_running_exits_1_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    monkeypatch.setattr(cli, "_download", lambda config: called.append("download"))
    running = BuildState(phase="parsing", chunks_total=435, chunks_parsed=120)
    write_build_state(tmp_path / "data", running)
    before = _state(tmp_path)
    with build_lock(tmp_path / "data"):
        code, _, err = _run(["--config", str(_settings(tmp_path)), "build"])
    assert code == 1
    assert "a build is already running" in err
    assert called == []
    # Not recorded as a failure: that would overwrite the running build's progress.
    assert _state(tmp_path) == before


# -- maintain: heals Qdrant beside a running build, and never touches the build state ----------


def _maintain_against(monkeypatch: pytest.MonkeyPatch, qdrant: FakeQdrant) -> None:
    """Point `maintain` at a fake Qdrant and a running unit, so no socket and no systemd is used."""
    running = UnitReading(QdrantUnitReport(found=True, active=True, restarts=0))
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: qdrant)
    monkeypatch.setattr(
        cli, "run_maintenance", functools.partial(run_maintenance, unit_reader=lambda: running)
    )


def _maintain(tmp_path: Path) -> tuple[int, str, str]:
    """Run `maintain` with the test's settings."""
    return _run(["--config", str(_settings(tmp_path)), "maintain"])


def test_maintain_resumes_a_paused_collection_writes_the_record_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qdrant = FakeQdrant({SEMANTIC: semantic_info(status="grey"), LEXICAL: lexical_info()})
    _maintain_against(monkeypatch, qdrant)
    code, out, err = _maintain(tmp_path)
    record = read_maintenance_record(tmp_path / "data")
    assert (code, out, err) == (0, "", "")
    assert [update["collection_name"] for update in qdrant.updates] == [SEMANTIC]
    assert record is not None
    assert [action.action for action in record.actions] == ["resumed"]


def test_maintain_with_qdrant_down_exits_0_because_the_record_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    down = ResponseHandlingException(OSError("connection refused"))
    _maintain_against(monkeypatch, FakeQdrant({}, errors={"get_aliases": down}))
    code, _, _ = _maintain(tmp_path)
    record = read_maintenance_record(tmp_path / "data")
    assert code == 0
    assert record is not None
    assert record.problems == ("Qdrant did not answer: ResponseHandlingException",)


def test_maintain_while_a_build_holds_the_lock_still_runs_and_leaves_the_build_state_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qdrant = FakeQdrant({SEMANTIC: semantic_info(status="grey"), LEXICAL: lexical_info()})
    _maintain_against(monkeypatch, qdrant)
    write_build_state(tmp_path / "data", BuildState(phase="parsing", chunks_parsed=120))
    before = _state(tmp_path)
    with build_lock(tmp_path / "data"):
        code, _, err = _maintain(tmp_path)
    assert code == 0
    assert "already running" not in err
    assert len(qdrant.updates) == 1
    assert _state(tmp_path) == before


def test_maintain_does_not_create_a_build_state_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _maintain_against(monkeypatch, FakeQdrant({}))
    assert _maintain(tmp_path)[0] == 0
    assert not (tmp_path / "data" / "state" / "build.json").exists()


def test_maintain_with_a_record_that_cannot_be_written_exits_1_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _maintain_against(monkeypatch, FakeQdrant({}))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state").write_text("a file where the state directory should be")
    code, _, err = _maintain(tmp_path)
    assert code == 1
    assert "cannot write the record" in err
    assert "the record could not be written" in caplog.text


def test_maintain_an_unexpected_exception_exits_1_with_its_type_and_a_logged_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "QdrantClient", lambda **_: FakeQdrant({}))
    monkeypatch.setattr(cli, "run_maintenance", broken)
    code, _, err = _maintain(tmp_path)
    assert code == 1
    assert "RuntimeError: boom" in err
    assert "maintain failed" in caplog.text


def test_maintain_gives_the_qdrant_client_a_short_timeout_and_closes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    qdrant = FakeQdrant({})

    def client(**kwargs: Any) -> FakeQdrant:
        seen.update(kwargs)
        return qdrant

    _maintain_against(monkeypatch, qdrant)
    monkeypatch.setattr(cli, "QdrantClient", client)
    assert _maintain(tmp_path)[0] == 0
    # A 15-minute job: Qdrant not answering within a minute is the finding, not a wait.
    assert seen == {"url": "http://127.0.0.1:6333", "timeout": QDRANT_TIMEOUT_SECONDS}
    assert qdrant.closed is True


@pytest.mark.parametrize("command", ["download", "parse", "index", "build"])
def test_every_stage_command_runs_under_the_build_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    held: list[bool] = []

    def probe(*_args: object, **_kwargs: object) -> Any:
        try:
            with build_lock(tmp_path / "data"):
                held.append(False)
        except BuildLockedError:
            held.append(True)
        return make_snapshot()

    for stage in ("_download", "_parse", "_index"):
        monkeypatch.setattr(cli, stage, probe)
    code, _, _ = _run(["--config", str(_settings(tmp_path)), command])
    assert code == 0
    assert held, "the stage never ran"
    assert all(held)


def test_the_build_lock_is_released_after_a_failed_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(config: object) -> SnapshotMeta:
        raise OSError("boom")

    monkeypatch.setattr(cli, "_download", failing)
    _run(["--config", str(_settings(tmp_path)), "build"])
    with build_lock(tmp_path / "data"):
        pass


_EMBED_URL = "embedding_url: http://100.64.0.9:8080\n"


def test_embed_without_an_embedding_url_exits_2_and_says_which_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    monkeypatch.setattr(cli, "_embed", lambda config: called.append("embed"))
    code, _, err = _run(["--config", str(_settings(tmp_path)), "embed"])
    assert code == 2
    assert "embedding_url" in err
    assert called == []


def test_embed_runs_under_the_build_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    held: list[bool] = []

    def probe(*_args: object, **_kwargs: object) -> int:
        try:
            with build_lock(tmp_path / "data"):
                held.append(False)
        except BuildLockedError:
            held.append(True)
        return 0

    monkeypatch.setattr(cli, "_embed", probe)
    code, _, _ = _run(["--config", str(_settings(tmp_path, _EMBED_URL)), "embed"])
    assert code == 0
    assert held == [True]


def test_a_second_embed_while_a_build_runs_exits_1_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    monkeypatch.setattr(cli, "_embed", lambda config: called.append("embed"))
    with build_lock(tmp_path / "data"):
        code, _, err = _run(["--config", str(_settings(tmp_path, _EMBED_URL)), "embed"])
    assert code == 1
    assert "a build is already running" in err
    assert called == []


def test_a_failing_embed_exits_1_and_leaves_the_build_state_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(config: object) -> int:
        raise EmbedError("the embedding server went away")

    monkeypatch.setattr(cli, "_embed", failing)
    write_build_state(tmp_path / "data", BuildState(phase="serving"))
    before = _state(tmp_path)
    code, _, err = _run(["--config", str(_settings(tmp_path, _EMBED_URL)), "embed"])
    assert code == 1
    assert "went away" in err
    # A failing embedding run says nothing about the index being served: health must not turn
    # `degraded` because of it.
    assert _state(tmp_path) == before


def test_embed_stage_wires_the_store_the_bulk_timeout_and_the_environment_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    closed: list[str] = []

    class _Closeable:
        def close(self) -> None:
            closed.append("closed")

    def client(**kwargs: Any) -> _Closeable:
        seen.update(kwargs)
        return _Closeable()

    async def run(config: object, store: object, qdrant: object, embedder: Any) -> int:
        seen["key"] = embedder._api_key
        return 7

    monkeypatch.setattr(cli, "QdrantClient", client)
    monkeypatch.setattr(cli, "open_source_store", lambda config, qdrant: _Closeable())
    monkeypatch.setattr(cli, "run_embed", run)
    monkeypatch.setenv("EMBEDDING_API_KEY", "sentinel-key")
    config = make_config(tmp_path, embedding_url="http://100.64.0.9:8080")
    assert cli._embed(config) == 7
    assert seen["timeout"] >= 60
    assert seen["key"] == "sentinel-key"
    assert closed == ["closed", "closed"]  # the store and the Qdrant client


def test_status_runs_while_a_build_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "QdrantClient", lambda **_: memory_client())
    with build_lock(tmp_path / "data"):
        code, out, _ = _run(["--config", str(_settings(tmp_path)), "status"])
    assert code == 0
    assert json.loads(out)["build"]["phase"] == "idle"


# -- key --------------------------------------------------------------------------------------


def _clients(tmp_path: Path) -> dict[str, str]:
    """The clients file of the test, parsed (empty when absent)."""
    path = tmp_path / "clients.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_key_add_prints_the_key_once_and_stores_only_its_hash(tmp_path: Path) -> None:
    code, out, err = _run(["--config", str(_settings(tmp_path)), "key", "add", "claude-code"])
    key = out.strip().splitlines()[-1]
    assert code == 0
    assert len(key) >= 64
    assert "will not be shown again" in out
    assert out.count(key) == 1
    assert key not in err
    assert key not in (tmp_path / "clients.json").read_text(encoding="utf-8")
    assert _clients(tmp_path) == {"claude-code": hash_key(key)}


def test_key_add_new_file_is_group_readable_not_world_readable(tmp_path: Path) -> None:
    _run(["--config", str(_settings(tmp_path)), "key", "add", "worker"])
    mode = stat.S_IMODE((tmp_path / "clients.json").stat().st_mode)
    assert mode == 0o640
    assert (tmp_path / "clients.json").stat().st_gid == tmp_path.stat().st_gid


def test_key_add_primary_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    code, out, err = _run(["--config", str(_settings(tmp_path)), "key", "add", "primary"])
    assert code == 2
    assert "WIKI_MIRROR_API_KEY" in err
    assert out == ""
    assert not (tmp_path / "clients.json").exists()


@pytest.mark.parametrize("name", ["Claude", "1abc", "has space", "under_score", "x" * 33])
def test_key_add_invalid_name_is_refused(tmp_path: Path, name: str) -> None:
    code, out, _ = _run(["--config", str(_settings(tmp_path)), "key", "add", name])
    assert (code, out) == (2, "")
    assert not (tmp_path / "clients.json").exists()


def test_key_add_existing_name_is_refused_and_keeps_the_first_key(tmp_path: Path) -> None:
    args = ["--config", str(_settings(tmp_path)), "key", "add", "worker"]
    _run(args)
    before = _clients(tmp_path)
    code, out, err = _run(args)
    assert (code, out) == (1, "")
    assert "already exists" in err
    assert _clients(tmp_path) == before


def test_key_add_as_a_non_root_user_is_refused(tmp_path: Path) -> None:
    code, out, err = _run(
        ["--config", str(_settings(tmp_path)), "key", "add", "worker"], euid=1000
    )
    assert (code, out) == (2, "")
    assert "root" in err
    assert not (tmp_path / "clients.json").exists()


def test_key_add_never_overwrites_a_malformed_clients_file(tmp_path: Path) -> None:
    (tmp_path / "clients.json").write_text("{not json", encoding="utf-8")
    code, out, _ = _run(["--config", str(_settings(tmp_path)), "key", "add", "worker"])
    assert (code, out) == (1, "")
    assert (tmp_path / "clients.json").read_text(encoding="utf-8") == "{not json"


def test_key_list_prints_names_only(tmp_path: Path) -> None:
    config = str(_settings(tmp_path))
    for name in ("worker", "claude-code"):
        _run(["--config", config, "key", "add", name])
    code, out, _ = _run(["--config", config, "key", "list"], euid=1000)
    assert code == 0
    assert out.splitlines() == ["claude-code", "worker"]
    assert all(digest not in out for digest in _clients(tmp_path).values())


def test_key_list_with_no_clients_says_so_on_stderr(tmp_path: Path) -> None:
    code, out, err = _run(["--config", str(_settings(tmp_path)), "key", "list"])
    assert (code, out) == (0, "")
    assert "only the primary key" in err


def test_key_revoke_removes_the_client(tmp_path: Path) -> None:
    config = str(_settings(tmp_path))
    for name in ("worker", "claude-code"):
        _run(["--config", config, "key", "add", name])
    code, _, _ = _run(["--config", config, "key", "revoke", "worker"])
    assert code == 0
    assert list(_clients(tmp_path)) == ["claude-code"]


def test_key_revoke_unknown_name_exits_1(tmp_path: Path) -> None:
    code, _, err = _run(["--config", str(_settings(tmp_path)), "key", "revoke", "nobody"])
    assert code == 1
    assert "no client named" in err


def test_key_revoke_primary_is_refused(tmp_path: Path) -> None:
    code, _, err = _run(["--config", str(_settings(tmp_path)), "key", "revoke", "primary"])
    assert code == 2
    assert "mirror.env" in err


def test_key_commands_leave_no_temporary_files_behind(tmp_path: Path) -> None:
    config = str(_settings(tmp_path))
    _run(["--config", config, "key", "add", "worker"])
    _run(["--config", config, "key", "revoke", "worker"])
    assert sorted(path.name for path in tmp_path.iterdir() if "clients" in path.name) == [
        "clients.json"
    ]

