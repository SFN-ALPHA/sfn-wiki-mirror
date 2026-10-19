"""The mirror's settings: an optional YAML file plus one secret from the environment.

Read by every entry point in `wiki_mirror.__main__`. Paths and knobs live in
`/etc/wiki-mirror/mirror.yaml` (every field has a default, so the file may be absent);
the one secret the service itself holds, the primary bearer key, comes from the
environment (`WIKI_MIRROR_API_KEY`, set by systemd from `/etc/wiki-mirror/mirror.env`,
0600). The Wikimedia Enterprise sign-in is a separate file read only by the download stage
(`enterprise_env_file`), so the API process never holds it. Like every configuration in this
repository it is validated at start and an invalid one is a hard failure.

Non-obvious constraints:

* **Qdrant must be loopback.** The collection holds nothing secret, but an exposed Qdrant accepts
  writes from anyone who can reach it; only the API port is on the LAN, behind the key.
* **The article store and Qdrant belong on local SSD, never on the NAS.** SQLite's locking and
  Qdrant's memory-mapped files both misbehave on network filesystems (docs/DESIGN.md section 4); the
  NAS holds only the downloaded snapshots, which are read sequentially once per build.
* **Secrets are `SecretStr`**, and `load_mirror_config` builds its error text from field names
  and fixed messages, never from the input that failed.
* **Only one key is a setting.** The primary key (`WIKI_MIRROR_API_KEY`) comes from the
  environment; every other agent's key lives, hashed, in `clients_file` so that adding or
  revoking one needs no restart and no secret in a config file.

See docs/DESIGN.md sections 4 and 6.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

__all__ = [
    "API_KEY_ENV",
    "DEFAULT_CONFIG_PATH",
    "MirrorConfig",
    "MirrorConfigError",
    "load_mirror_config",
]

#: The environment variable of the primary bearer key (a client's env file may use the same name).
API_KEY_ENV: Final = "WIKI_MIRROR_API_KEY"

#: Where the installer puts the settings file.
DEFAULT_CONFIG_PATH: Final = Path("/etc/wiki-mirror/mirror.yaml")

#: Shortest API key accepted: long enough that it was generated, not typed.
_MIN_API_KEY_CHARS: Final = 32


class MirrorConfigError(ValueError):
    """The settings file or the environment holds something that cannot be right."""


class MirrorConfig(BaseModel):
    """Everything the mirror needs, and nothing it may decide for itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Enterprise snapshot to mirror: English Wikipedia, articles only (namespace 0).
    snapshot_identifier: str = Field(default="enwiki_namespace_0", pattern=r"^[a-z]+_namespace_0$")
    #: Downloaded snapshot chunks, one directory per snapshot date. On the NAS (NFS dataset
    #: `wikipedia-mirror`, bind-mounted into the LXC); large, read once per build, re-downloadable.
    snapshot_dir: Path = Path("/mnt/wikipedia/snapshots")
    #: Article stores, build checkpoints and sync state. Local SSD.
    data_dir: Path = Path("/var/lib/wiki-mirror")
    #: Wikimedia Enterprise sign-in: `WME_USERNAME=` and `WME_PASSWORD=` lines, 0600. Read only by
    #: the download stage.
    enterprise_env_file: Path = Path("/etc/wiki-mirror/enterprise.env")
    #: Qdrant on the same LXC. Loopback only (see the module docstring).
    qdrant_url: str = "http://127.0.0.1:6333"
    #: The API's listen address. All interfaces: nftables admits only the site's LAN on this port.
    listen_host: str = "0.0.0.0"  # noqa: S104 - deliberate; the firewall is the boundary
    #: The API's port.
    listen_port: int = Field(default=8300, ge=1, le=65535)
    #: Chunk downloads in flight. Each is 240-370 MB from S3; four keeps a build to about an
    #: hour on the LAN uplink without saturating it for other traffic.
    download_concurrency: int = Field(default=4, ge=1, le=8)
    #: Chunk downloads allowed per calendar month. The free tier allows 1,500; a full snapshot
    #: is 435 chunks, so this admits two full pulls plus retries and refuses a runaway loop
    #: before Enterprise does.
    monthly_chunk_budget: int = Field(default=1200, ge=1, le=1500)
    #: Parse processes. The LXC has 6 cores; one is left for Qdrant and the API.
    parse_workers: int = Field(default=5, ge=1, le=32)
    #: Hash space for the lexical vectors. Larger than a small-corpus 2^18 on purpose: Wikipedia's
    #: vocabulary runs to millions of distinct terms, and at 2^18 rare names and identifiers
    #: (exactly what lexical search is for) would collide. Query and document vectors are both
    #: encoded on this box, so the two never disagree.
    lexical_dimensions: int = Field(default=2**24, ge=2**18, le=2**31)
    #: Longest window `GET /v1/wiki/article` returns, in characters. A client's tool replies should
    #: stay under 12K characters (a model reads a compact reply better, and may ignore paging args).
    max_read_chars: int = Field(default=12_000, ge=1_000, le=50_000)
    #: Snapshot directories kept on the NAS: the serving one and the one before it.
    retain_snapshots: int = Field(default=2, ge=1, le=6)
    #: Free space, in GB, that `data_dir`'s disk must still have before a build creates a new
    #: article store or a new Qdrant collection (the two big local writers). A build that would
    #: fill the disk fails here with a clear message instead of half-way, with the serving
    #: snapshot's files unable to grow and the API's own state unwritable. 0 turns the check off.
    min_free_disk_gb: int = Field(default=20, ge=0, le=10_000)
    #: Keys of the other agents on the LAN (Claude Code, ...): `{"<name>": "<sha256 hex of
    #: the key>"}`, root:wikimirror 0640, managed with `wiki-mirror key add|list|revoke`. Hashes
    #: only, so the API user can read it. Absent means no extra clients; see `keys.py`.
    clients_file: Path = Path("/etc/wiki-mirror/clients.json")
    #: The primary bearer key. Required by `serve`; the build stages never need it.
    api_key: SecretStr | None = None

    @field_validator("qdrant_url")
    @classmethod
    def _qdrant_is_loopback_http(cls, value: str) -> str:
        """Refuse a Qdrant URL that is not plain HTTP to a loopback address."""
        try:
            parts = urlsplit(value)
            host = parts.hostname or ""
            _ = parts.port  # raises ValueError on a malformed port
        except ValueError as exc:
            msg = "is not a URL"
            raise ValueError(msg) from exc
        loopback = host == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
        if parts.scheme != "http" or not loopback:
            msg = "must be http:// to a loopback address (127.0.0.1, ::1 or localhost)"
            raise ValueError(msg)
        return value.rstrip("/")

    @field_validator("api_key")
    @classmethod
    def _api_key_is_long_enough(cls, value: SecretStr | None) -> SecretStr | None:
        """Refuse a key short enough to have been typed by a person."""
        if value is not None and len(value.get_secret_value()) < _MIN_API_KEY_CHARS:
            msg = f"must be at least {_MIN_API_KEY_CHARS} characters"
            raise ValueError(msg)
        return value


def load_mirror_config(
    path: Path | None = DEFAULT_CONFIG_PATH, environ: Mapping[str, str] | None = None
) -> MirrorConfig:
    """Read and validate the settings.

    Args:
        path: The YAML settings file. Absent or None means every default. A file that exists but
            is not a YAML mapping is an error, not an empty config.
        environ: The process environment (a mapping, so tests pass their own). Only
            `WIKI_MIRROR_API_KEY` is read from it.

    Raises:
        MirrorConfigError: The file cannot be read or parsed, or a value is invalid. The
            message names the fields and what is wrong, never their values.
    """
    raw: dict[str, object] = {}
    if path is not None and path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            msg = f"cannot read {path}: {type(exc).__name__}"
            raise MirrorConfigError(msg) from None
        if loaded is not None and not isinstance(loaded, dict):
            msg = f"{path} must hold a YAML mapping"
            raise MirrorConfigError(msg)
        raw.update(loaded or {})
    if "api_key" in raw:
        msg = f"api_key belongs in the environment ({API_KEY_ENV}), never in {path}"
        raise MirrorConfigError(msg)
    key = (environ or {}).get(API_KEY_ENV, "").strip()
    if key:
        raw["api_key"] = key
    try:
        return MirrorConfig.model_validate(raw)
    except ValidationError as exc:
        problems = [
            f"{_field_name(error['loc'])}: {error['msg']}" for error in exc.errors()
        ]
        msg = "invalid mirror configuration: " + "; ".join(problems)
        raise MirrorConfigError(msg) from None


def _field_name(location: tuple[int | str, ...]) -> str:
    """Name a failing field as the operator sets it: the variable for the key, else its YAML key."""
    if location[:1] == ("api_key",):
        return API_KEY_ENV
    return ".".join(map(str, location))
