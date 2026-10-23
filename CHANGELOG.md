# Changelog

All notable changes are listed here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

The first full embedding run finished on 2026-10-22, and Qdrant was killed for memory the next night while building
the semantic index's graphs. The index now lives on disk, and a maintenance job keeps it healthy. Search answers from
the keyword index by default; semantic and hybrid search are now available as opt-in modes.

### Added

- **Search modes** ([D10](docs/DECISIONS.md#d10), [docs/API.md](docs/API.md#post-v1wikisearch)):
  `POST /v1/wiki/search` and the MCP `wiki_search` tool take `mode`: `lexical` (the default, unchanged), `semantic` or
  `hybrid`. Every mode returns the same reply and result shape; the reply's `mode` names the one that answered, and
  `score` is the mode's own (BM25, cosine similarity or the RRF sum). Hybrid fuses the two rankings with reciprocal
  rank fusion (k=60, equal untuned weights) on (point id, `text_hash`). A semantic or hybrid request that cannot be
  served is `503 semantic_unavailable` with a plain reason, never keyword results under another mode's name: this is
  also what it answers after each monthly build until the semantic index is caught up to the new snapshot. The
  MCP tool gives a fixed sentence the model can act on. The default stays `lexical` until the golden set and the
  fusion-weight check pass.
- **The API embeds each semantic question itself**, with a new synchronous client (`query_embed.py`): the model and
  vector length come from the semantic index's state (not from `embedding_model`), no retries, a 15 s timeout, and the
  API's own key, `EMBEDDING_API_KEY` in its environment (`mirror.env`), a different key from the embed run's. A new
  setting, `query_embedding_headers`, is separate from `embedding_headers` so a person waiting on a search does not get
  the bulk run's background priority. **This gives the API user its first outbound connection**, to the embedding
  server's host and port only.
- `/v1/wiki/health` gains `search_modes` (the modes a search can use now) and `semantic.searchable_reason` (why
  semantic and hybrid are off, or `null`). An unpaired semantic index does not make the status `degraded`.
- **Security: the firewall admits the API user to the embedding server, and to nothing else**
  ([D10](docs/DECISIONS.md#d10), [SECURITY.md](SECURITY.md)). One new rule, `meta skuid "wikimirror" ip daddr
  $EMBEDDING_V4 tcp dport $EMBEDDING_PORT accept`, sits in the output chain above the refusals that still cover the
  API user and Qdrant, between the `embedding-server` markers, so a site with no `EMBEDDING_HOST` and `EMBEDDING_PORT`
  in `site.env` gets no such rule and the API user stays refused everything, as before. Qdrant gets no exception.
  Anything else the API user tries, DNS included, is still counted in `service_refused` and logged. **A box applies
  the rule only when `install.sh` is run again** (the update path reports a changed firewall but never applies it).
  The API's `embedding_url` must therefore name the server by its IPv4 address and port, as the site file does.
- Turning semantic and hybrid search on is one documented procedure
  ([docs/OPERATIONS.md](docs/OPERATIONS.md#turning-on-semantic-and-hybrid-search)): a key of its own on the embedding
  server, `EMBEDDING_API_KEY=` in `mirror.env`, `install.sh`, a restart, then
  `wiki-mirror search --mode semantic "<question>"` and `wiki-mirror status`. `install.sh` says once, and fails
  nothing, when `site.env` names an embedding server and `mirror.yaml` an `embedding_url` but `mirror.env` has no
  `EMBEDDING_API_KEY` line (a keyless local Ollama is valid); it never writes, rewrites or prints that line.
  `query_embedding_headers` is in the example `mirror.yaml`, commented out, beside the `embedding_*` lines.
- `wiki-mirror search "<query>" [limit] [--mode lexical|semantic|hybrid]`: the mode is checked before anything is
  sent, a `503 semantic_unavailable` prints its reason and exits 1, and the score is printed with `%.4g` (a hybrid
  RRF score of about 0.03 read as `0.03` for every hit at two decimals). `wiki-mirror status` shows `search_modes`
  and, when a semantic index exists but cannot be searched, the reason.

- A maintenance job ([D8](docs/DECISIONS.md#d8), [docs/OPERATIONS.md](docs/OPERATIONS.md#the-maintenance-job)):
  `python -m wiki_mirror maintain`, run every 15 minutes by `wiki-mirror-maintain.timer`. It resumes a mirror
  collection that Qdrant left paused after a restart, keeps the semantic collection's live settings equal to the
  constants in `embed.py`, and writes `state/maintenance.json`. A box that updates itself gets the units but must
  enable the timer once: `systemctl enable --now wiki-mirror-maintain.timer`.
- `/v1/wiki/health` gains `reasons` (why the status is `degraded`), `semantic` (the collection behind the
  `wikipedia_semantic` alias, asked live) and `maintenance` (the job's last record). It turns `degraded` when the
  record is more than 60 minutes old, when Qdrant was restarted after a failure within 24 hours, and when Qdrant's
  anonymous memory is above 90% of its cap. Nothing is renamed.
- A crash-loop guard in the maintenance job ([D8](docs/DECISIONS.md#d8),
  [docs/OPERATIONS.md](docs/OPERATIONS.md#the-crash-loop-guard)). Qdrant leaves a collection paused after a restart on
  purpose, so the job no longer resumes it blindly. It reads systemd's `NRestarts` and resumes a paused collection by
  itself only while Qdrant has restarted itself after a failure at most once since it was last started by hand. Above
  that, or when the count cannot be read, or when Qdrant holds over 90% of its memory cap, or when the disk is under
  its floor, the collection is held (no resume, no settings change). At most two crashes follow a start by hand. A
  hold turns health `degraded`, and `wiki-mirror status` shows the command to resume by hand. The record is now
  schema 2. Health is also `degraded` when Qdrant's unit is not installed.
- A settings change that rebuilds also waits while Qdrant holds over 90% of its memory cap or its restart count is
  2 or more or unreadable, and a change of the
  optimizer thread count alone waits until the collection is `green`, because any collection update stops the
  optimizations in flight. A changed layout constant in `embed.py` is a rebuild on every box that runs the code.
- `wiki-mirror status` shows each reason, a Semantic index section and a Maintenance section, and warns when the
  maintenance timer is off. `wiki-mirror logs maintain` follows the job.

### Changed

- The operator command's API calls use `curl --fail-with-body` (curl 7.76 or newer; Debian 12 has 7.88) in place of
  `-f`, so a command can show the service's own error reason (`semantic_unavailable`) instead of curl's bare status
  line. `wiki-mirror status` still exits 1 when the API does not answer.
- The installer's check of the API user says "refused outbound connections other than to the embedding server" on a
  site that names one. It still probes a public host only and never connects to the embedding server as that user.
- Fewer, larger semantic segments ([D9](docs/DECISIONS.md#d9)): the semantic collection's segment size cap goes from
  1 GB to 10 GB of vector data (`SEMANTIC_MAX_SEGMENT_KB` in `embed.py`, new), measured on 2026-10-23 as the cause of
  most of a first-time search's page faults (about 1,370 a query, about 1,230 of them from rescoring each of 85
  segments). The maintenance job applies it to the live collection as a rebuilding change, and Qdrant then merges the
  85 segments into about 10 or 11 in the background: one rewrite of about 100 GB, likely hours, with search answering
  throughout and the collection `yellow` while it runs. Expected to cut the reads per query about eight times; that
  is measured after the merge, not asserted. Every box that runs the code applies it on its next maintenance run.
- A rebuilding settings change now waits for free disk of `min_free_disk_gb` plus the room the merges need (25 GB for
  two merges of 10 GB segments at 1.27 times the cap on disk, in Qdrant's 1024-byte KB), not the floor alone; the problem sentence names both.
  `build.ensure_free_space` takes an optional `room_gb` for it; its other callers are unchanged. A resume and a
  threads-only change still ask for the floor or nothing, as before.
- The semantic index lives on disk ([D8](docs/DECISIONS.md#d8)): the HNSW graph (`on_disk`) and the binary-quantized
  copy (`always_ram: false`) join the float16 vectors there, and the semantic collection's optimizer threads go
  from 1 to 2. The goal is under 10 GB of pinned memory; measured after the rebuild (2026-10-23), Qdrant rests at
  7.4-7.5 GB. Qdrant rebuilds every segment's graph in the background when the placement changes (about 5 h 40 min
  here, limited by disk; the keyword index keeps serving). Qdrant's memory cap is now 24G (was 20G), because under
  cgroup v2 the page cache of its memory-mapped files counts against it; a box that set a cap at runtime keeps it until
  reboot, and the unit's value applies after the next `install.sh` run or reboot. Semantic search latency on this
  layout, measured: about 45 ms cached, about half a second the first time, seconds to tens of seconds after a
  restart. The pinned peak during a monthly build is still an estimate (12 GB).
- The code is the truth for the semantic collection's settings: a setting changed by hand on the box is put back by
  the next maintenance run. To change the layout, change `SEMANTIC_HNSW`, `SEMANTIC_QUANTIZATION`,
  `SEMANTIC_OPTIMIZATION_THREADS` and `SEMANTIC_MAX_SEGMENT_KB` in `embed.py`.

### Fixed

- A semantic collection left paused (`grey`) when Qdrant restarted after an out-of-memory kill is resumed by the
  maintenance job within 15 minutes; before, it stayed paused and nothing showed it.
- `wiki-mirror status` no longer prints a comment fragment as a firewall counter (`in_invalid "Inbound packets`).
- `wiki-mirror status` shows when each timer fires next, `wiki-mirror-maintain.timer` included. The line read
  `NextElapseUSecRealtime`, which systemd leaves empty for a timer on the monotonic clock, so the maintenance
  timer's "next" was blank. It now reads `systemctl list-timers`, which answers for both kinds.
- The Qdrant client is pinned to the server's minor version (`qdrant-client` 1.12.2 for the 1.12.4 server;
  was 1.19.1). Qdrant supports a client at most one minor version from its server, and the 1.19 client warned
  "incompatible with server version 1.12.4" every time it was constructed, which is every 15 minutes from the
  maintenance job, and could send request fields the server does not know. `portalocker` moves back to 2.10.1 and
  `grpcio-tools` 1.84.0 and `setuptools` join the pins, because that client needs them. A test keeps the pin's
  minor version equal to `QDRANT_VERSION` in `deploy/install.sh`, so upgrading the server and the client is one
  change.

### Documented

- The measured memory breakdown of the in-RAM layout, the 2026-10-23 incident, the alternatives and the order of
  fallbacks if cold semantic search is too slow ([D8](docs/DECISIONS.md#d8)).
- The measurement behind the 10 GB segment cap, Qdrant 1.12.4's merge rule, the transient disk and heap of the merge,
  the plan for measuring its effect, and what an operator sees while it runs ([D9](docs/DECISIONS.md#d9),
  [OPERATIONS.md](docs/OPERATIONS.md#the-merge-of-the-semantic-segments), [SIZING.md](docs/SIZING.md)).

## [0.2.0] - 2026-10-20

The embedding run for the semantic index. Search still answers from the keyword index alone; hybrid search comes
when the first full run has finished and been checked.

### Added

- The semantic index's embedding run (W2, [D7](docs/DECISIONS.md#d7)): `wiki-mirror embed-now` embeds every
  passage into `wikipedia_semantic_<born>` through an Ollama-compatible server, resumably. Search does not use it
  yet.
- A resumed embedding run now checks its checkpoint against Qdrant: if a power loss cost Qdrant the tail of its
  write-ahead log, the checkpoint steps back and the lost windows are embedded again instead of skipped.

### Changed

- Qdrant's memory cap is 20G (the semantic index needs a 32 GB box). Superseded by D8: it is 24G now.
- The README leads with what the project is for, and compares it with live-API MCP servers and Kiwix.

### Documented

- How to set up the embedding server: one Ollama node, Ollama on the mirror box itself, or an Ollama-compatible
  cluster. OpenAI-style `/v1/embeddings` servers and hosted embedding APIs are not supported.
- What each interruption of an embedding run does, and how a run resumes.
- The plan for the semantic catch-up: after a monthly build, embed only the passages whose text changed
  ([docs/ROADMAP.md](docs/ROADMAP.md#the-semantic-catch-up)).

## [0.1.0] - 2026-10-19

Initial public release.

### What works today

- **Keyword passage search** over 41.6M passages of 7.2M English Wikipedia articles (snapshot 2026-10-01), one result
  per article with a snippet, the section and an exact-revision citation.
- **Article read:** the text of any article by title or page id, in pageable windows.
- **MCP server** at `/mcp` (Streamable HTTP) with two read-only tools, `wiki_search` and `wiki_read`, and a **REST
  API** (`POST /v1/wiki/search`, `GET /v1/wiki/article`, `GET /v1/wiki/health`).
- **Keys:** one bearer key per client, stored as a hash, issued and revoked with `wiki-mirror key`.
- **Installer** (`deploy/install.sh`) for a dedicated Debian 12 or 13 container or VM: users, pinned Python and
  Qdrant, systemd units, a default-deny nftables firewall built from a site file, and a `wiki-mirror` operator
  command. It clones over anonymous HTTPS by default.
- **Monthly build timer** that downloads the Wikimedia Enterprise snapshot, parses it and builds a new index beside
  the serving one. The first build was started by hand; the timer's first unattended run is due 2026-11-02, so
  unattended operation is scheduled and not yet proven.
- **Opt-in nightly update** (`install.sh --auto-update`, or `systemctl enable --now wiki-mirror-update.timer`) with a
  format guard and rollback. Without it, update with `wiki-mirror update`.

### Planned

- **W2:** a semantic (embedding) index and hybrid search. The optional `EMBEDDING_HOST` and `EMBEDDING_PORT` in
  `site.env` open one egress rule for it; nothing uses an embedding server yet.
- **W3:** in-place monthly updates that reuse unchanged work, instead of a full rebuild each month.

See [docs/ROADMAP.md](docs/ROADMAP.md).

[Unreleased]: https://github.com/SFN-ALPHA/sfn-wiki-mirror/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/SFN-ALPHA/sfn-wiki-mirror/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/SFN-ALPHA/sfn-wiki-mirror/releases/tag/v0.1.0
