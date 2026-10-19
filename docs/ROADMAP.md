# Roadmap

**Audience:** whoever plans the next piece of work. Status as of 2026-10-19. The phase names (W0 to W3) come from
[DESIGN.md §7](DESIGN.md#7-phases) and [§12.8](DESIGN.md#128-phases); the decisions are in
[DECISIONS.md](DECISIONS.md).

| Phase | What | Status |
|---|---|---|
| W0 | The LXC, storage, Enterprise account | Done 2026-10-16 |
| W1 | Download, parse, article store, keyword search, REST, MCP | Done; installed 2026-10-17 |
| W1.5 | Store schema 2, `text_hash`, collections not named after a snapshot, segment cap | Done 2026-10-17 |
| First keyword build | 7,245,149 articles, 41,590,915 passages (snapshot 2026-10-01) | Done 2026-10-18; moved to NVMe the same day |
| Nightly self-update | `update.sh` with deferral, format guard, rollback ([D4](DECISIONS.md#d4)) | Live since 2026-10-17 |
| **W2** | Semantic index, hybrid search, query-focused snippet | **Next.** Cut decided 2026-10-19 ([D5](DECISIONS.md#d5)); not started |
| **W3** | Monthly in-place updates | Designed ([DESIGN.md §12.6](DESIGN.md#126-the-monthly-update-w3)); not started |

Until W3 lands the monthly timer runs a full build beside the live collection and swaps the alias, which works
but redoes everything each month.

## W2: the semantic build

**Goal.** Hybrid search (keyword and bge-m3 meaning, fused) live over all articles. **Done when** the golden set
([EVALUATION.md](EVALUATION.md)) shows the answer article in the top five for at least 90% of its questions,
with hybrid on.

**Decided** ([D5](DECISIONS.md#d5), [DESIGN.md §12.8](DESIGN.md#128-phases)):

* Embed the **live passage cut**: 41.5M passages, sections never merge. No keyword re-index.
* Model **bge-m3** (1,024 dimensions, cosine), so a query is embedded once for both indexes. Newer open
  models scored higher on public benchmarks but were not worth a different model now; a model change stays
  possible at any later full rebuild.
* **Float16 originals on disk** (about 85 GB) with **binary quantization in RAM** (about 5.3 GB) and rescoring.
  Binary against int8 is not measured separately at full scale; the golden check is the guard.
* A second Qdrant collection, `wikipedia_semantic_<born>` behind the alias `wikipedia_semantic`, with the same
  point ids and payload as the keyword collection ([ARCHITECTURE.md](ARCHITECTURE.md#the-index-two-collections-shared-point-ids)).

**What it needs.**

1. **Egress from the container to an Ollama-compatible embedding server** (configured by URL). The firewall
   ruleset already admits `wikimirror-sync` to the embedding server set in `/etc/wiki-mirror/site.env`
   (`EMBEDDING_HOST` and `EMBEDDING_PORT`, rendered into `deploy/nftables/` by the installer; check the installed
   ruleset has it with `nft list ruleset`, and apply a change with `install.sh`). The embedding job runs as
   `wikimirror-sync`, like the other build stages.
2. **An embedding-server key** for the mirror, with no other use (if the server requires one). It must live in a 0600 file readable by `wikimirror-sync` alone
   (like `enterprise.env`), never in `mirror.yaml`, a log or model context. The key's file, its setting name
   and its place in `config.py` are not designed yet; record the choice as the next decision in [DECISIONS.md](DECISIONS.md).
3. **Qdrant's memory cap raised from 12G to about 20G** (`MemoryMax` in
   `systemd/wiki-mirror-qdrant.service`; the box has 32 GB). The keyword index rests at 4.3 GB; binary
   vectors plus the HNSW graph add about 10 to 11 GB. Also check `vm.max_map_count` on the Proxmox host
   (`sysctl -w vm.max_map_count=1048576`). The API (1.5 GB) and a build (5 GB) must still fit beside it.
4. **The indexer.** A build stage after `index`, or a mode of it, that makes one pass over the article store in
   page-id order:
   * cut each article with the same chunker, and embed through the embedding server's `/api/embed`;
   * **16 requests in flight** (the embedding server's measured knee), **8 texts per request**, `X-Priority: 3`;
   * retry a 503 honouring `Retry-After`, and retry a timeout;
   * upsert into the new collection with HNSW indexing off during the load and switched on at the end;
   * checkpoint the last page id fully embedded, so a resume repeats at most one batch; write it into an index
     state file like the keyword collection's;
   * show progress and an ETA on `/v1/wiki/health`;
   * stay within the existing rules: one build at a time (the build lock), the disk-floor check before creating
     a collection, and no secrets in logs.
5. **Hybrid search.** Query both collections and fuse with RRF (k=60). Fusion must key on (point id,
   `text_hash`), so a passage already updated in one index is never merged with the other index's older version
   of it. Switch hybrid on only when the semantic collection covers every page in `applied_snapshot` and the
   golden set passes; until then keyword search answers alone and `mode` stays `lexical`. The reply's `mode`
   becomes `hybrid` ([API.md](API.md#post-v1wikisearch)).
6. **A fusion-weight check.** On the evaluation sample, equal-weight fusion's hit@1 (0.713) trailed dense alone
   (0.823) with hard negatives. Before switching hybrid on, try weights on the golden set at full scale and keep
   equal weights only if they hold up.
7. **A query-focused snippet.** Show the best 320-character window of the matching passage, chosen by overlap
   with the query terms, instead of the start of the passage. It held the answer 8 to 12 points more often on
   the live cut ([EVALUATION.md](EVALUATION.md)). It is a change to the snippet alone and can ship before the
   semantic index; it changes no stored data.

**Cost.** About two days of embedding for the live cut (measured 145 passages/s on the evaluation sample, about
0.17M characters/s, roughly 31 billion characters), at background priority against the embedding server. It runs best in a
bulk window the operator picks, with the server's nodes awake and the server otherwise quiet. A pause longer than
five minutes can unload bge-m3 and let small GPUs sleep, so a resume may wait a minute or two.

**Before the run starts.**

* Check that the embedding server's `/api/embed` returns one vector per text of a batched request (the evaluation's
  canary fails otherwise). A server that answered only the first vector needed a fix, and applying one restarts it.
* Check any per-request logging or quota limits on the embedding server. Batching cuts the request count sixteen-fold
  or more.
* If the server fronts several nodes, align their Ollama versions if practicable. The 2026-10-19 canary showed
  identical vectors across versions 0.35.1 to 0.40.3 on one such server, so this is hygiene, not a blocker. Rerun
  the canary after any node upgrade.
* The nightly update defers while a build holds the lock, so a multi-day embed delays code updates; that is by
  design ([D4](DECISIONS.md#d4)).
* If a monthly snapshot arrives mid-build, the build lock holds the monthly job until the embed is done.

**Disk.** 206 GB free on NVMe (2026-10-18) against about 85 GB of new float16 vectors: it fits.

## W3: monthly in-place updates

**Goal.** Two consecutive monthly updates applied unattended; the audit passes; a changed article serves its new
revision; measured churn recorded in [DESIGN.md §12.8](DESIGN.md#128-phases).

Full design: [DESIGN.md §12.6](DESIGN.md#126-the-monthly-update-w3), summarized here.

1. **Download** the new snapshot to the NAS (about 45 minutes, counted against the monthly download budget).
2. **Parse with carry-forward.** A record whose page id and revision id match the previous store, with an
   unchanged parser version, has its row copied across without parsing the HTML. Expected 5 to 10 times faster
   than a full parse.
3. **Validate before touching either index:** record count within 3% of last month's, parse failures under 0.1%,
   a fixed list of 100 sentinel titles present with text. A failure stops here, the previous month keeps serving,
   and health says why.
4. **Diff** the two stores by streaming in page-id order: added, removed, text changed, unchanged (a new revision
   with the same text counts as unchanged). Write the change list to a file; show counts on health.
5. **Apply to the keyword index** (CPU only, under an hour expected): upsert changed pages' passages, delete
   their chunks beyond the new count, delete removed pages by a `page_id` filter. Checkpointed by page id.
6. **Apply to the semantic index:** a new passage whose text hash matches an old passage's takes that point's
   stored vector; only new text is embedded (0.3 to 1M passages expected, 1.5 to 4 hours).
7. **Switch over:** `applied_snapshot` moves to the new month, serving swaps in the new store, an audit samples
   1,000 random pages and checks their points' `text_hash` against the store, the old store is deleted once its
   readers' leases end. The NAS keeps the previous snapshot.

**Known gaps to close in W3** (from [DESIGN.md §12.8](DESIGN.md#128-phases)):

* **Exact-title hits during a mixed-month update:** a title match with a stale passage should fall back to the
  article's lead rather than be dropped. During the update, serving must resolve a hit's `text_hash` against the
  new store, then the old one.
* **Rebuilding a snapshot that is serving:** a full rebuild of the snapshot whose collection is serving now
  refuses with "delete it by hand". The build-beside path needs a distinct collection name.
* **The index state directory is not backed up.** Losing an index state file takes search down until it is
  restored. Back up `/var/lib/wiki-mirror/state/`.

**The way back** is the same operation: an in-place update has no instant rollback, so a bad month found later is
undone by applying the previous snapshot as if it were the new one, which re-embeds only the passages that
differ.

## Later, and not planned

* Images, other languages, Wiktionary, Wikidata (a strong candidate for dates, populations and constants),
  talk and user pages ([DESIGN.md §8](DESIGN.md#8-not-in-scope-now)).
* A category filter on search. [DESIGN.md §5](DESIGN.md#5-the-design) proposed one; the store keeps categories,
  but the API does not offer it yet.
* Alerts. Nothing pushes a notification on a `held`, `deferred` or `failed` update or build; they show on
  `wiki-mirror status` and health only.
* A different embedding model, at a later full rebuild, if a measurement says it is worth it
  ([DESIGN.md §12.7](DESIGN.md#127-what-forces-a-full-rebuild)).
* If the Enterprise free tier changes or ends, the wikitext dump is the fallback (a parser cost, lower text
  quality).
