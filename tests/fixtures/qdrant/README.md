# Qdrant fixtures

Real replies from the mirror's Qdrant (1.12.4, REST on loopback), recorded on the maintainer's box on
2026-10-23. The maintenance job's and the health route's tests build their fakes from these, so the
fakes say what the server says (see "Mocks must be read, not imagined" in `CLAUDE.md`). Qdrant has
no API key on the box, and nothing here is secret.

| File | What it is |
|---|---|
| `collection_semantic_yellow.json` | `GET /collections/wikipedia_semantic_20261001` at 07:28, six minutes into a graph rebuild: status `yellow`, graph and binary copy moved to disk (`hnsw_config.on_disk: true`, `quantization_config.binary.always_ram: false`) |
| `collection_semantic_grey.json` | The same collection at 06:49, after Qdrant was OOM-killed mid-build and restarted: status `grey` (optimizations pending, not started), graph in RAM (`on_disk: false`), binary copy pinned (`always_ram: true`). **Reconstructed**, not recorded whole: the 07:28 reply with the values read at 06:49 (`status`, `segments_count` 86, `on_disk`, `always_ram`) put back. Every other field is as recorded. |
| `collection_lexical_green.json` | `GET /collections/wikipedia_lexical_20261001` at 07:28: the keyword collection, status `green` |
| `aliases.json` | `GET /aliases` at 07:28: both aliases |

`indexed_vectors_count` above `points_count` in the lexical reply is real: Qdrant counts it
approximately.
