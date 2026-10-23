"""The mirror's command line: `python -m wiki_mirror <command>`.

The one entry point on the mirror's container. The monthly job runs `build` (download, parse, index)
as the `wikimirror-sync` user, which has internet but never serves; the systemd unit runs `serve` as
`wikimirror`, which serves but has no internet. `download`, `parse` and `index` run one stage by
hand, for resuming a stage that failed, `embed` (W2) embeds every passage into the semantic
collection, run by `wiki-mirror-embed.service` once a build has finished, `maintain` heals Qdrant
(resumes paused collections, keeps the semantic collection's settings) and records what it saw,
run every 15 minutes by `wiki-mirror-maintain.timer`, `status` shows where a build stands, and
`key` manages the API keys of the other agents (reached as `wiki-mirror key add|list|revoke`).

Non-obvious constraints:

* **Exit codes.** 0 success, 1 a stage failed (the log has the cause), 2 the configuration or
  the command line is wrong. systemd treats 2 as "do not retry": an unfixable start. `embed`
  without `embedding_url` is such a case. `maintain` exits 0 whenever it wrote its record,
  whatever problems the record holds (they are for health to show), and 1 only when the record
  could not be written or the run raised.
* **`maintain` takes no build lock and never touches the build state.** It must run while a build
  or an embedding run does (that is when Qdrant most needs it), and a failed maintenance run says
  nothing about the index being served. It only probes the lock before it changes a collection's
  settings (`maintain`).
* **One build at a time, and a failure always shows.** `download`, `parse`, `index`, `build` and
  `embed` hold `build.build_lock` while they run; a second one exits 1 with "a build is already
  running" and touches nothing. Any exception a stage raises ends with `phase="failed"` and a
  short, secret-free error in the build state (`_record_failure`), so `/v1/wiki/health` and
  `status` say so even when the failure came before the stage's own bookkeeping began. `embed` is
  the exception: it never touches the build state, because a failing embedding run says nothing
  about the index being served.
* **`serve` refuses to start without `WIKI_MIRROR_API_KEY`**, with a malformed clients file
  (exit 2), and without a reachable Qdrant (it exits 1 so systemd retries once Qdrant is up,
  rather than serving "not ready" forever). It does *not* need a snapshot: it serves whatever
  the alias `wikipedia_lexical` points at, with the article store of the snapshot that
  collection's index state says it reflects, and follows both afterwards
  (`serving.ServedSnapshot`), so neither the first build nor a monthly rebuild needs a restart.
* **`parse` on its own parses the newest completely downloaded snapshot** on the NAS
  (`build.latest_downloaded_snapshot`), not whatever Enterprise currently publishes: the stages
  share the snapshot description through the files they leave behind, and a parse never makes a
  network call.
* **`key add` and `key revoke` must run as root.** The clients file is root-owned and the API
  user only reads it. `key add` prints the new key once, on stdout, and stores only its hash.
* **No secrets are printed** except that one key. `status` prints the build state and counts;
  the API key is read from the environment by `load_mirror_config` and never echoed.

See docs/DESIGN.md sections 4, 6 and 12.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

import uvicorn
from qdrant_client import QdrantClient

from wiki_mirror.api import build_service
from wiki_mirror.build import (
    BuildError,
    BuildLockedError,
    BuildState,
    build_lock,
    describe_failure,
    latest_downloaded_snapshot,
    read_build_state,
    run_download,
    run_parse,
    write_build_state,
)
from wiki_mirror.config import (
    DEFAULT_CONFIG_PATH,
    MirrorConfig,
    MirrorConfigError,
    load_mirror_config,
)
from wiki_mirror.embed import EmbeddingClient, open_source_store, run_embed
from wiki_mirror.enterprise import EnterpriseClient
from wiki_mirror.index import aliased_collection, applied_snapshot, run_index
from wiki_mirror.keys import (
    ClientKeyError,
    ClientKeys,
    ClientNameError,
    add_client,
    list_clients,
    revoke_client,
)
from wiki_mirror.maintain import QDRANT_TIMEOUT_SECONDS, run_maintenance
from wiki_mirror.records import SnapshotMeta
from wiki_mirror.serving import ServedSnapshot, open_store_for_snapshot
from wiki_mirror.store import ArticleStore, StoreError
from wiki_mirror.update_record import read_update_record

__all__ = ["main"]

logger = logging.getLogger("wiki_mirror.main")

#: Longest uvicorn waits for open requests at shutdown, seconds; under the unit's 30 s stop timeout.
_GRACEFUL_SHUTDOWN_SECONDS = 10

#: How long the index stage waits for one Qdrant call, seconds. The client's default (5 s) suits
#: the API, which should fail fast, but not a bulk load: on the mirror LXC (2026-10-17)
#: Qdrant's cgroup sat at its MemoryHigh mark, so upserts slowed to 2-3 s and one took 7.0 s,
#: which ended the first build at 6.2M passages although the write succeeded. The retry at
#: 120 s then met a 144 s stall while Qdrant's optimizer held the segment locks, so 600 s, with
#: `index._upsert` retrying on top. An upsert is idempotent (point ids are deterministic), so
#: waiting longer costs nothing; a server that is truly gone still fails within this bound and
#: the build resumes from its checkpoint.
_INDEX_QDRANT_TIMEOUT_SECONDS = 600

_COMMANDS = {
    "download": "fetch the current snapshot's chunks from Wikimedia Enterprise",
    "parse": "turn the downloaded chunks into the article store",
    "index": "load the article store's passages into Qdrant and swap the alias",
    "build": "download, parse and index: the monthly job",
    "embed": "embed every passage into the semantic collection (W2); resumable",
    "maintain": "heal Qdrant (resume paused collections, keep settings) and record its state",
    "serve": "run the HTTP API",
    "status": "show where the build stands and what is being served",
}


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    euid: Callable[[], int] = os.geteuid,
) -> int:
    """Run one command.

    Args:
        argv: Arguments after the program name; `sys.argv[1:]` when None.
        environ: The process environment, for the API key; `os.environ` when None.
        out: Where results are printed; stdout when None.
        err: Where problems are printed; stderr when None.
        euid: The effective user id, for the root check of `key add` and `key revoke`; tests
            inject theirs.

    Returns:
        The process exit code: 0, 1 (a stage failed) or 2 (bad configuration or usage). An
        unknown command makes argparse exit 2 itself.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    parser = argparse.ArgumentParser(
        prog="python -m wiki_mirror", description="The Wikipedia mirror service."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"settings file (default {DEFAULT_CONFIG_PATH}); absent means all defaults",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, summary in _COMMANDS.items():
        commands.add_parser(name, help=summary)
    key = commands.add_parser("key", help="manage the API keys of the other agents (as root)")
    key_commands = key.add_subparsers(dest="key_command", required=True)
    key_commands.add_parser("add", help="issue a key for a new agent").add_argument("name")
    key_commands.add_parser("list", help="the agents that have a key (names only)")
    key_commands.add_parser("revoke", help="withdraw an agent's key").add_argument("name")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        config = load_mirror_config(args.config, os.environ if environ is None else environ)
    except MirrorConfigError as exc:
        err.write(f"wiki-mirror: {exc}\n")
        return 2

    if args.command == "status":
        return _status(config, out)
    if args.command == "serve":
        return _serve(config, err)
    if args.command == "maintain":
        # Before the build lock, like `status` and `serve`: see the module docstring.
        return _maintain(config, err)
    if args.command == "key":
        return _key(config, args, out, err, euid)
    if args.command == "embed" and config.embedding_url is None:
        err.write("wiki-mirror: embed needs embedding_url in mirror.yaml\n")
        return 2
    stages: dict[str, Callable[[MirrorConfig], object]] = {
        "download": _download,
        "parse": _parse,
        "index": _index,
        "build": _build,
        "embed": _embed,
    }
    before: BuildState | None = None
    try:
        with build_lock(config.data_dir):
            before = read_build_state(config.data_dir)
            stages[args.command](config)
    except BuildLockedError as exc:
        # Not a failure of the build: the one that holds the lock is still going, and recording
        # `failed` here would overwrite its progress on the health route.
        err.write(f"wiki-mirror: {args.command}: {exc}\n")
        return 1
    except Exception as exc:
        # The command line is the boundary: whatever a stage raises becomes an exit code and a
        # logged traceback, so systemd records a failed unit instead of a Python crash.
        logger.exception("%s failed", args.command)
        if args.command != "embed":
            # Not the embed stage: it does not touch the build state, and a failing embedding run
            # must not turn `/v1/wiki/health` to `degraded` while the served index is fine.
            _record_failure(config, args.command, exc, before)
        err.write(f"wiki-mirror: {args.command} failed: {type(exc).__name__}: {exc}\n")
        return 1
    return 0


def _record_failure(
    config: MirrorConfig, command: str, exc: Exception, before: BuildState | None
) -> None:
    """Make the build state say `failed` after a stage raised, so health and `status` show it.

    The stages record most of their own failures, with more detail than the command line has
    (which chunk, how many). What they cannot record is anything raised before they get going:
    no credentials, no complete download, a store that will not open. If the state already
    changed during this run and says `failed`, a stage got there first and its text is kept;
    otherwise this writes the failure.

    Never raises: a state file that cannot be written is logged, and the exit code still says 1.

    Args:
        config: Service settings (`data_dir`).
        command: The command that failed, named in the state's error.
        exc: What it raised; only `build.describe_failure` of it is recorded, never raw text.
        before: The state when the run began (None if the lock could not even be taken).
    """
    try:
        current = read_build_state(config.data_dir)
        if current.phase == "failed" and current != before:
            return
        error = f"{command}: {describe_failure(exc)} (see the build log)"
        write_build_state(
            config.data_dir, dataclasses.replace(current, phase="failed", error=error)
        )
    except Exception:
        # Deliberately broad: this runs while handling another failure, and must not mask it.
        logger.exception("could not record the failure in the build state")


def _download(config: MirrorConfig) -> SnapshotMeta:
    """Download the current snapshot's chunks; return its description."""
    client = EnterpriseClient(config.enterprise_env_file)
    try:
        return run_download(config, client)
    finally:
        client.close()


def _parse(config: MirrorConfig, snapshot: SnapshotMeta | None = None) -> Path:
    """Parse the downloaded chunks into the article store; return the store's path.

    Args:
        config: Service settings.
        snapshot: The snapshot to parse (`build` passes the one it just downloaded). None means
            the newest completely downloaded one on the NAS.

    Raises:
        BuildError: No snapshot is completely downloaded, or the parse itself failed.
    """
    if snapshot is None:
        snapshot = latest_downloaded_snapshot(config)
        if snapshot is None:
            msg = f"no complete snapshot download under {config.snapshot_dir}; run download first"
            raise BuildError(msg)
    return run_parse(config, snapshot)


def _index(config: MirrorConfig, store_file: Path | None = None) -> int:
    """Index an article store (the newest one if none is named); return the passage count."""
    path = store_file or _latest_store_path(config)
    if path is None:
        msg = f"no article store under {config.data_dir / 'articles'}; run parse first"
        raise FileNotFoundError(msg)
    store = ArticleStore.open(path, readonly=True)
    client = QdrantClient(url=config.qdrant_url, timeout=_INDEX_QDRANT_TIMEOUT_SECONDS)
    try:
        return run_index(config, store, client)
    finally:
        client.close()
        store.close()


def _embed(config: MirrorConfig) -> int:
    """Embed the passages of the store the live lexical collection serves; return the count.

    The embedding key is read from the process environment here (`EMBEDDING_API_KEY`), not from
    the settings.

    Raises:
        EmbedError: `embedding_url` is unset, there is no lexical collection to embed from, or
            the embedding run itself failed (see `embed.run_embed`).
    """
    client = QdrantClient(url=config.qdrant_url, timeout=_INDEX_QDRANT_TIMEOUT_SECONDS)
    try:
        store = open_source_store(config, client)
        try:
            return asyncio.run(_embed_async(config, store, client))
        finally:
            store.close()
    finally:
        client.close()


async def _embed_async(config: MirrorConfig, store: ArticleStore, client: QdrantClient) -> int:
    """Run the embed stage with an embedding client that is opened and closed on one event loop."""
    async with EmbeddingClient.from_config(config, os.environ) as embedder:
        return await run_embed(config, store, client, embedder)


def _build(config: MirrorConfig) -> None:
    """The monthly job: download, parse, index, each stage resuming from its checkpoint."""
    snapshot = _download(config)
    _index(config, _parse(config, snapshot))


def _serve(config: MirrorConfig, err: TextIO) -> int:
    """Run the API on the configured address until stopped."""
    if config.api_key is None:
        err.write("wiki-mirror: serve needs WIKI_MIRROR_API_KEY in the environment\n")
        return 2
    try:
        keys = ClientKeys(config.clients_file, config.api_key.get_secret_value())
    except ClientKeyError as exc:
        # Like any invalid configuration: an unfixable start, so systemd does not retry.
        err.write(f"wiki-mirror: {exc}\n")
        return 2
    client = QdrantClient(url=config.qdrant_url)
    try:
        aliased_collection(client)
    except Exception as exc:
        # Qdrant is not answering: exit so systemd restarts us when it is, instead of serving
        # "not ready" until someone notices (the module docstring).
        err.write(
            f"wiki-mirror: cannot reach Qdrant at {config.qdrant_url}: {type(exc).__name__}\n"
        )
        client.close()
        return 1
    served = ServedSnapshot(config, client)
    try:
        # Open what the alias points at now, so the first request is answered from a warm
        # store. A store that cannot be opened is not fatal here: the service starts, answers
        # `not_ready` and `degraded` (the log says why), and picks the store up when it appears.
        served.refresh()
        app = build_service(config, served, client, keys=keys, state_reader=read_build_state)
        logger.info(
            "serving the mirror's API on %s:%d (alias: %s)",
            config.listen_host,
            config.listen_port,
            served.alias or "none yet",
        )
        # No access log: it records the query string of article reads, and questions can be
        # personal.
        # A restart must not wait on a client that holds a connection open: systemd gives the
        # unit 30 s (TimeoutStopSec) before it kills it, so uvicorn gets less than that.
        uvicorn.run(
            app,
            host=config.listen_host,
            port=config.listen_port,
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_SECONDS,
        )
    finally:
        served.close()
        client.close()
    return 0


def _maintain(config: MirrorConfig, err: TextIO) -> int:
    """Run one maintenance pass (`maintain.run_maintenance`); exit 1 only if it did not finish.

    Qdrant not answering is not a failure of the run: it is a problem in the record, which was
    written, and the exit code is 0. A record that cannot be written (the state directory is not
    writable) or an unexpected exception exits 1 so systemd shows the failed run.

    Args:
        config: Service settings (`qdrant_url`, `data_dir`, `min_free_disk_gb`).
        err: Where the failure is printed.

    Returns:
        0 when the record was written, 1 otherwise.
    """
    client = QdrantClient(url=config.qdrant_url, timeout=QDRANT_TIMEOUT_SECONDS)
    try:
        run_maintenance(config, client)
    except OSError as exc:
        logger.exception("maintain: the record could not be written")
        err.write(f"wiki-mirror: maintain: cannot write the record: {exc}\n")
        return 1
    except Exception as exc:
        # The command line is the boundary, as for the stages: a logged traceback and an exit
        # code, not a Python crash.
        logger.exception("maintain failed")
        err.write(f"wiki-mirror: maintain failed: {type(exc).__name__}: {exc}\n")
        return 1
    finally:
        client.close()
    return 0


def _status(config: MirrorConfig, out: TextIO) -> int:
    """Print the build state, the last code update and what is being served, if anything."""
    try:
        report: dict[str, Any] = {"build": dataclasses.asdict(read_build_state(config.data_dir))}
    except (OSError, ValueError) as exc:
        report = {"build": {"phase": "unknown", "error": f"unreadable: {type(exc).__name__}"}}
    # The last code update (update.sh), or None; `wiki-mirror status` prints it as a line.
    report["update"] = read_update_record(config.data_dir)
    client = QdrantClient(url=config.qdrant_url)
    try:
        collection = aliased_collection(client)
        if collection is not None:
            passages = client.count(collection_name=collection, exact=False).count
            report["serving"] = {
                "collection": collection,
                "passages": passages,
                **_store_report(config, collection),
            }
    except Exception as exc:
        # `status` is a diagnostic: Qdrant being down is itself the finding to show.
        report["serving"] = {"error": f"Qdrant not answering: {type(exc).__name__}"}
    finally:
        client.close()
    out.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


def _store_report(config: MirrorConfig, collection: str) -> dict[str, Any]:
    """What `status` says about the article store behind the aliased collection.

    Args:
        config: Service settings (`data_dir`, `snapshot_identifier`).
        collection: What the alias points at.

    Returns:
        `applied_snapshot` (the tag the collection's index state names, or None) plus either
        `articles` (the open store's count) or `problem` (no usable index state, or no complete
        store for that snapshot), so the operator sees which of the two to fix.
    """
    applied = applied_snapshot(config.data_dir, collection)
    if applied is None:
        return {
            "applied_snapshot": None,
            "problem": f"{collection} has no usable index state (missing, unreadable, unfinished)",
        }
    try:
        store = open_store_for_snapshot(config, applied)
    except StoreError as exc:
        return {"applied_snapshot": applied, "problem": str(exc)}
    try:
        return {"applied_snapshot": applied, "articles": store.count()}
    finally:
        store.close()


def _key(
    config: MirrorConfig,
    args: argparse.Namespace,
    out: TextIO,
    err: TextIO,
    euid: Callable[[], int],
) -> int:
    """Add, list or revoke an agent's key (the `key` command).

    `add` prints the new key as the last line of stdout and never again: only its hash is
    stored. The running service picks the change up within `keys.RECHECK_SECONDS`.
    """
    path = config.clients_file
    if args.key_command != "list" and euid() != 0:
        err.write(f"wiki-mirror: key {args.key_command} must run as root ({path} is root's)\n")
        return 2
    try:
        if args.key_command == "list":
            names = list_clients(path)
            if not names:
                err.write(
                    "wiki-mirror: no agent has a key; only the primary key is accepted\n"
                )
            out.writelines(f"{name}\n" for name in names)
        elif args.key_command == "add":
            key = add_client(path, args.name)
            out.write(
                f"Key for '{args.name}'. It will not be shown again; only its hash is kept:\n"
                f"{key}\n"
            )
            err.write("wiki-mirror: the service accepts it within a few seconds; no restart\n")
        else:
            revoke_client(path, args.name)
            err.write(
                f"wiki-mirror: revoked '{args.name}'; the service stops accepting it shortly\n"
            )
    except ClientNameError as exc:
        err.write(f"wiki-mirror: {exc}\n")
        return 2
    except ClientKeyError as exc:
        err.write(f"wiki-mirror: {exc}\n")
        return 1
    except OSError as exc:
        err.write(f"wiki-mirror: cannot use {path}: {type(exc).__name__}\n")
        return 1
    return 0


def _latest_store_path(config: MirrorConfig) -> Path | None:
    """The newest article store on disk (`<identifier>_<yyyymmdd>.db` sorts by date), or None."""
    stores = sorted((config.data_dir / "articles").glob(f"{config.snapshot_identifier}_*.db"))
    return stores[-1] if stores else None


if __name__ == "__main__":
    sys.exit(main())
