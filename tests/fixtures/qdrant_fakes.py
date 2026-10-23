"""Fakes of the Qdrant calls the maintenance job makes, built from the replies recorded on the box.

`FakeQdrant` stands in for `qdrant_client.QdrantClient` in the job's tests: it answers
`get_aliases`, `get_collections` and `get_collection` from `models.CollectionInfo` objects parsed
from the recorded replies in `tests/fixtures/qdrant/` (the client's own types, so the job sees
exactly what it sees from a server), records every `update_collection` it receives, raises what a
test tells it to and, when asked, applies the updates it receives to the collections it serves.
`recorded_transport` serves the recorded files over `httpx.MockTransport`, byte for byte, so a
test can put the real client in front of them and see both what it parses and the request it sends.

Non-obvious constraints:

* **The recorded replies are the source.** `semantic_info` starts from the recorded `yellow`
  reply and changes only the fields a test names. The one reply nobody recorded is a `red`
  collection's: its `optimizer_status` takes the shape of the OpenAPI schema
  (`{"error": "<text>"}`), and the text is made up.
* **The recorded semantic reply predates the thread change.** It has `max_optimization_threads: 1`
  (the layout of 2026-10-23 07:28, before D8 made it 2), so `semantic_info` defaults the thread
  count to the code's value; a test about the thread difference says `threads=1`. It also
  predates D9: it has `max_segment_size: 1000000` (the old 1 GB cap), so `semantic_info` defaults
  the cap to the code's 10,000,000 KB; a test about the cap difference says `segment_cap=` with
  the old value, or `None` for the unset cap Qdrant reports as its default.
* **`GET /collections` was not recorded.** `recorded_transport` builds it from the names it is
  given, in Qdrant's documented shape (`{"result": {"collections": [{"name": ...}]}}`).
* **Applying an update is a model, not Qdrant.** With `applies=True` the fake merges the diffs
  of an `update_collection` into the stored configuration the way Qdrant's PATCH does (only the
  fields sent change) and moves a `grey` collection to `yellow`, as Qdrant starts its
  optimizations. It is enough to show that the job's change settles; it does not imitate the
  rebuild. A test that needs another status sets it with `set_status`.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from qdrant_client import models

__all__ = [
    "LEXICAL",
    "SEMANTIC",
    "FakeQdrant",
    "lexical_info",
    "recorded_bytes",
    "recorded_info",
    "recorded_transport",
    "semantic_info",
]

_DIRECTORY = Path(__file__).resolve().parent / "qdrant"

#: The collections the recorded replies describe.
SEMANTIC = "wikipedia_semantic_20261001"
LEXICAL = "wikipedia_lexical_20261001"


def recorded_bytes(file_name: str) -> bytes:
    """A recorded reply from `tests/fixtures/qdrant/`, exactly as the file holds it."""
    return (_DIRECTORY / file_name).read_bytes()


def _recorded(file_name: str) -> dict[str, Any]:
    """The whole recorded reply (`result`, `status`, `time`), parsed."""
    return json.loads(recorded_bytes(file_name))  # type: ignore[no-any-return]


def recorded_info(file_name: str) -> models.CollectionInfo:
    """A recorded `GET /collections/<name>` reply parsed by the client's own model.

    Args:
        file_name: One of `collection_semantic_yellow.json`, `collection_semantic_grey.json`,
            `collection_lexical_green.json`.
    """
    return models.CollectionInfo.model_validate(_recorded(file_name)["result"])


def semantic_info(
    *,
    status: str = "yellow",
    on_disk: bool = True,
    always_ram: bool = False,
    threads: int = 2,
    segment_cap: int | None = 10_000_000,
    optimizer_error: str | None = None,
) -> models.CollectionInfo:
    """The recorded semantic collection with the named settings (defaults: the code's layout).

    Args:
        status: The collection's status word.
        on_disk: `hnsw_config.on_disk`.
        always_ram: `quantization_config.binary.always_ram`.
        threads: `optimizer_config.max_optimization_threads`.
        segment_cap: `optimizer_config.max_segment_size`, in KB; None leaves it unset, which
            Qdrant reports as `null` when the collection runs on its default.
        optimizer_error: When set, `optimizer_status` is `{"error": <this text>}` (a `red`
            collection's shape); otherwise `ok`.
    """
    raw = copy.deepcopy(_recorded("collection_semantic_yellow.json")["result"])
    raw["status"] = status
    raw["config"]["hnsw_config"]["on_disk"] = on_disk
    raw["config"]["quantization_config"]["binary"]["always_ram"] = always_ram
    raw["config"]["optimizer_config"]["max_optimization_threads"] = threads
    raw["config"]["optimizer_config"]["max_segment_size"] = segment_cap
    if optimizer_error is not None:
        raw["optimizer_status"] = {"error": optimizer_error}
    return models.CollectionInfo.model_validate(raw)


def lexical_info() -> models.CollectionInfo:
    """The recorded lexical collection (`green`)."""
    return recorded_info("collection_lexical_green.json")


def _word(status: object) -> str:
    """A status as its word, whether the client's enum or a plain string."""
    return str(getattr(status, "value", status))


class FakeQdrant:
    """The slice of `QdrantClient` the maintenance job uses, with a log of what it was sent."""

    def __init__(
        self,
        collections: Mapping[str, models.CollectionInfo],
        aliases: Mapping[str, str] | None = None,
        *,
        errors: Mapping[str, Exception] | None = None,
        applies: bool = False,
    ) -> None:
        """Serve `collections` under `aliases`.

        Args:
            collections: Collection name to what `get_collection` answers.
            aliases: Alias to collection name; the recorded pair (`wikipedia_semantic`,
                `wikipedia_lexical`) when None.
            errors: What to raise instead of answering, keyed by `get_aliases`,
                `get_collections`, `get_collection:<name>` or `update_collection` (every update
                is refused). `update_collection:once` refuses only the next update, then goes
                away: the first of two updates in one run fails and the second is taken.
            applies: Merge each update into the collection it names (module docstring).
        """
        self.collections = dict(collections)
        self.aliases = (
            dict(aliases)
            if aliases is not None
            else {"wikipedia_semantic": SEMANTIC, "wikipedia_lexical": LEXICAL}
        )
        self.errors = dict(errors or {})
        self.applies = applies
        #: Every `update_collection` call, as its keyword arguments.
        self.updates: list[dict[str, Any]] = []
        #: Names `get_collection` was asked for.
        self.described: list[str] = []
        self.closed = False

    def set_status(self, name: str, status: str) -> None:
        """Change what `get_collection` says a collection's status is, as Qdrant would over time."""
        info = self.collections[name]
        self.collections[name] = info.model_copy(update={"status": models.CollectionStatus(status)})

    def _fail_if_told(self, key: str) -> None:
        """Raise the error the test set for `key`, if any."""
        if key in self.errors:
            raise self.errors[key]

    def get_aliases(self) -> models.CollectionsAliasesResponse:
        """The alias map, as the client's response model."""
        self._fail_if_told("get_aliases")
        return models.CollectionsAliasesResponse(
            aliases=[
                models.AliasDescription(alias_name=alias, collection_name=collection)
                for alias, collection in self.aliases.items()
            ]
        )

    def get_collections(self) -> models.CollectionsResponse:
        """The collection names, as the client's response model."""
        self._fail_if_told("get_collections")
        return models.CollectionsResponse(
            collections=[models.CollectionDescription(name=name) for name in self.collections]
        )

    def get_collection(self, collection_name: str, **_kwargs: Any) -> models.CollectionInfo:
        """One collection's description."""
        self.described.append(collection_name)
        self._fail_if_told(f"get_collection:{collection_name}")
        return self.collections[collection_name]

    def update_collection(self, collection_name: str, **kwargs: Any) -> bool:
        """Remember the update, and merge it into the collection if the fake applies updates."""
        self._fail_if_told("update_collection")
        if "update_collection:once" in self.errors:
            raise self.errors.pop("update_collection:once")
        self.updates.append({"collection_name": collection_name, **kwargs})
        if self.applies:
            self._apply(collection_name, kwargs)
        return True

    def _apply(self, name: str, diffs: Mapping[str, Any]) -> None:
        """Merge the diffs of one update into the stored description (only fields sent change)."""
        info = self.collections[name]
        config = info.config
        changes: dict[str, Any] = {}
        if diffs.get("hnsw_config") is not None:
            changes["hnsw_config"] = config.hnsw_config.model_copy(
                update=diffs["hnsw_config"].model_dump(exclude_none=True)
            )
        if diffs.get("quantization_config") is not None:
            changes["quantization_config"] = diffs["quantization_config"]
        if diffs.get("optimizers_config") is not None:
            changes["optimizer_config"] = config.optimizer_config.model_copy(
                update=diffs["optimizers_config"].model_dump(exclude_none=True)
            )
        status = models.CollectionStatus.YELLOW if _word(info.status) == "grey" else info.status
        self.collections[name] = info.model_copy(
            update={"config": config.model_copy(update=changes), "status": status}
        )

    def close(self) -> None:
        """Note that the client was closed."""
        self.closed = True


def recorded_transport(
    bodies: Mapping[str, bytes], requests: list[httpx.Request]
) -> httpx.MockTransport:
    """An HTTP transport that answers like the box's Qdrant, for the real `QdrantClient`.

    Serves `GET /aliases` (the recorded file), `GET /collections` (the names in `bodies`),
    `GET /collections/<name>` (the bytes given for it, untouched) and answers any `PATCH
    /collections/<name>` with `{"result": true}`, as Qdrant does. Every request is appended to
    `requests` first.

    Args:
        bodies: Collection name to the exact bytes `GET /collections/<name>` answers: a recorded
            file read with `recorded_bytes`.
        requests: Receives each request as it arrives.
    """
    headers = {"content-type": "application/json"}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if request.method == "PATCH":
            return httpx.Response(200, json={"result": True, "status": "ok", "time": 0.001})
        if path == "/aliases":
            return httpx.Response(200, content=recorded_bytes("aliases.json"), headers=headers)
        if path == "/collections":
            listing = {
                "result": {"collections": [{"name": name} for name in bodies]},
                "status": "ok",
                "time": 0.001,
            }
            return httpx.Response(200, json=listing)
        name = path.removeprefix("/collections/")
        if name in bodies:
            return httpx.Response(200, content=bodies[name], headers=headers)
        return httpx.Response(404, json={"status": {"error": "Not found"}, "time": 0.001})

    return httpx.MockTransport(handler)
