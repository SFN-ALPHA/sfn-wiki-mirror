# Design: a local, searchable, regularly synced English Wikipedia

**Status (2026-10-19).** Built and running: the download, parse, article store, keyword index, HTTP API and MCP
server, the nightly self-update, and a first complete keyword build (7,245,149 articles, 41,590,915 passages,
snapshot 2026-10-01). Decided but not built: the semantic (embedding) index, W2, and in-place monthly updates, W3.
[ROADMAP.md](ROADMAP.md) tracks them; [DECISIONS.md](DECISIONS.md) logs the decisions with their dates.

This is the design document: why it is built the way it is, with the measurements behind each choice.
[ARCHITECTURE.md](ARCHITECTURE.md), [API.md](API.md), [MCP.md](MCP.md) and [OPERATIONS.md](OPERATIONS.md) describe
what the code does today. Where this text and the code disagree, the code wins.

---

## 1. Purpose

AI agents and applications that use Wikipedia live run into its API rate limits, its policies for automated
clients, and network latency, and a loop that fetches pages hammers a volunteer-run service. This project holds
the whole of English Wikipedia locally and answers searches and article reads from local disk.

What that demands:

| Goal | Requirement |
|---|---|
| Broad knowledge across many topics | All English articles, searchable by keyword and, later, by meaning |
| Reasonably current | A refresh every month (the free source's cadence), cheap enough to run unattended |
| Not hammering Wikipedia | One bulk download per month from Wikimedia's own data service; every lookup answered locally |
| As fast and as often as a client likes | The text is on local disk; no external rate limit applies |
| Usable by any agent | An MCP server and a plain HTTP API, with a key per client |
| Citable | Every reply names its snapshot date and a permanent link to the exact revision |
| Efficient on disk and RAM | Compressed text, compact vectors, rebuildable data kept out of backups |

## 2. What Wikimedia publishes (checked 2026-10-16)

| Source | What it is | Size | Cadence | Notes |
|---|---|---|---|---|
| Wikitext dump (`pages-articles-multistream.xml.bz2`) | Every article's source markup | about 26 GB compressed | Twice a month (1st, 20th) | No account. Templates (infoboxes, units, maths) arrive unexpanded; turning them into clean text is the hard part |
| Daily adds-changes (`other/incr/enwiki/`) | Revisions of new and changed pages | Small | Daily | Wikitext; could keep a mirror fresher between dumps |
| Wikimedia Enterprise Snapshot API | Every article as rendered HTML (plus wikitext), NDJSON in `.tar.gz` | **151 GB** (435 chunks, measured 2026-10-16) | **Monthly on the free tier** (the 1st, 00:00 UTC); weekly or daily paid | Needs a free Enterprise account. Free tier: 30 full-snapshot and 1,500 chunk downloads a month. The public HTML mirror on dumps.wikimedia.org stopped on 2025-03-24 |
| Kiwix ZIM | Packaged offline Wikipedia with its own search | nopic 49 GB, with images 119 GB | About quarterly | Easiest to stand up; too stale for "regularly synced" |

**Choice: the Enterprise HTML snapshot**, with the wikitext dump as the fallback. Rendered HTML has the templates
already expanded (infobox values, converted units, maths alt text, reference text), so its plain text is what a
reader sees. The wikitext route needs a template expander and still loses infobox and maths content. The cost is a
free account and one secret.

## 3. Scale

- **Articles:** about 7 million in namespace 0, of which about 0.3 million are disambiguation pages. Redirects are
  kept as title aliases, not documents. Measured on the 2026-10-01 snapshot: 7,245,149 articles, 30.0 billion
  characters of plain text.
- **Passages:** the chunker (512 tokens, 50-token overlap) splits on every heading, and most Wikipedia sections are
  short, so the average passage is about 720 characters and the index holds **41,590,915 passages**: three times
  the 13M first estimated from a 2,000-character assumption.
- **Embedding rate:** measured in section 12.5. The whole snapshot embeds in about two days.
- **Each monthly refresh:** an estimated 10-20% of articles get a new revision in a month, many of them bot edits
  that leave the plain text unchanged; churn is measured once two snapshots exist (W3).

## 4. Where it lives: a dedicated container

A separate LXC on a Proxmox host (hostname `wikipedia-mirror`) so that:

- **Egress is confined.** It is the only machine that downloads from Wikimedia. Its firewall allows the
  Enterprise endpoints and the embedding server, nothing else (section 6).
- **Memory is its own.** Its Qdrant holds tens of millions of points.
- **It is disposable.** A rebuild, a bad snapshot or a runaway embed job touches no other service. Clients reach it
  over the LAN.

**Storage:**

| Data | Size | Where | Backed up |
|---|---|---|---|
| Downloaded snapshots (current and previous) | 2 x 151 GB | NAS (NFS dataset bind-mounted at `/mnt/wikipedia`) | No (re-downloadable) |
| Article store: clean text per article, zstd, SQLite | 25 GB measured | Local disk (NVMe) | No (rebuildable from the snapshot) |
| Qdrant: sparse keyword vectors, and later dense vectors and the HNSW graph | 37 GB measured for keywords | Local disk | No (rebuildable) |
| Index state and build state | small | Local disk | Should be: losing an index state file takes search down until it is restored |

- Qdrant must not run on NFS: it memory-maps its files, and a network filesystem makes every search a network round
  trip. SQLite's locking misbehaves on NFS too. The NAS holds only the large, rarely read snapshots.
- **Memory.** The container started with 8 GB and has 32 GB since 2026-10-17. Qdrant is capped at 12G (the first
  keyword build peaked at 5.1 GB of anonymous memory and rests at 4.3 GB); the semantic index needs the cap raised
  to about 20G (section 12.5). Decision D1 chose binary quantization (about 2 GB per 13M vectors) with
  full-precision rescoring from disk, so the searchable vectors stay small in RAM.

## 5. The design

**The article store.** Each article becomes one record: `page_id`, `title`, `revision_id`, `revision_time`, `url`,
`categories`, redirect aliases, `license`, and its text as plain text with section headings. Reference lists,
navigation boxes and "See also" link farms are dropped. Infobox fields are kept as a short key-value block,
because they are often the answer.

**The index.** Qdrant collections whose points are passages:
- A sparse `lexical` vector per passage: hashed BM25-style term weights (2^24 hashed dimensions) with Qdrant's IDF
  modifier. The semantic collection (W2) adds a dense `bge-m3` vector (1,024 dimensions, cosine). Results from the
  two are fused with reciprocal rank fusion (RRF, k=60).
- Payload: `page_id`, `revision_id`, `chunk`, `section`, `disambiguation`, `text_hash`. No text: the article store
  serves it, which saves tens of GB.
- Point id = `page_id * 10000 + chunk`, in both collections.

**The service** (FastAPI on the container, bearer keys and request ids):
- `POST /v1/wiki/search {query, limit}` returns one result per article: title, section, a snippet, `page_id`,
  `revision_id`, score, the revision permalink, and the snapshot date on the reply.
- `GET /v1/wiki/article?page_id=|title=&offset=` returns the article in windows, so a long article reaches a
  client that cannot take 100K characters in one reply.
- `GET /v1/wiki/health` reports the snapshot date, article and passage counts, build state and the last update.
- **An MCP server** at `/mcp` (Streamable HTTP, stateless) offers the same two read-only tools, `wiki_search` and
  `wiki_read`, to any MCP client on the LAN. It calls the same search code as the REST routes, so results rank
  identically. Each client has its own key, so logs name the caller and one key can be revoked alone.

**Citations.** A reply carries `url`, a permanent link to the exact revision the mirror holds
(`https://en.wikipedia.org/wiki/<Title>?oldid=<revision_id>`), the snapshot date, and the licence (CC BY-SA 4.0).
Wikipedia is editable by anyone, so the text is labelled untrusted.

**Embedding: the bulk job (W2).** Runs on the container against an Ollama-compatible embedding server, configured
by URL, not a direct GPU path. A checkpointed queue of passages (resume after any crash), concurrency held at the
server's measured limit, background priority, and back-off on 503 (section 12.5).

**Sync.** Superseded by section 12.6: the original plan built a new collection beside the old one each month and
swapped an alias. Monthly updates will change the collections in place instead. Today the monthly job is still the
build-beside path, which remains the route for rare full rebuilds (section 12.7).

## 6. Security

- **Untrusted content.** Wikipedia text can be vandalised. Replies and MCP tool descriptions say so; the text is
  never treated as instructions.
- **Secrets.** The Enterprise sign-in lives in a 0600 env file on the container, readable only by the build user,
  never in the repository, configuration, logs or model context.
- **Network.** The container accepts its service port and SSH from the LAN only, and every caller needs its own
  key. The data is public Wikipedia, so a leaked key exposes nothing private to a LAN host; the key still limits
  who can use the service and tells the logs who asked.
- **Per-site settings, not per-site code.** Three values differ between networks: the LAN (`LAN_CIDR`), the
  embedding server's address (`EMBEDDING_HOST`) and its port (`EMBEDDING_PORT`). They live in
  `/etc/wiki-mirror/site.env`, not in the repository, and the firewall file in the repository is a template with a
  placeholder for each. The values reach a ruleset that loads as root, so the installer treats the file as data
  (it is never sourced), accepts only a dotted-quad network or address with every octet 0-255 and a port of 1-65535,
  refuses a LAN shorter than `/8` or outside the private blocks (10/8, 172.16/12, 192.168/16, 100.64/10) and an
  address with host bits set, and stops on anything else, rather than escaping it. It also refuses a file that
  is not root's or is writable by others, and a LAN that this box or the SSH session running it is not in. A first
  install copies the example and stops; a file still equal to the example is refused, because a firewall built from
  someone else's network would lock the real one out. The whole ruleset is then checked with `nft -c` before it is
  applied, and the install is transactional: the file it replaces is kept until the check passes and put back if it
  fails, so a ruleset that failed its check is never left for the next boot to load ([D6](DECISIONS.md#d6)).
- **Egress is per Unix user, not per address.** Downloads redirect (307) to presigned URLs on an S3 bucket whose
  addresses rotate, so an address allowlist cannot work. Only the build user (`wikimirror-sync`) may open HTTPS to the
  internet; the API user (`wikimirror`) has no egress at all, so a flaw in the code that parses and serves untrusted
  text cannot phone out. The access token is never sent to the presigned URL (S3 refuses it with a 400, observed
  2026-10-16), and the presigned URL is never logged: its query string is a credential.

## 7. Phases

| Phase | What | Done when |
|---|---|---|
| **W0** | The container and storage, the Enterprise account, the egress rule | The container answers, and the snapshot API returns its catalogue |
| **W1** | Download and parse into the article store; keyword search; the MCP server | An MCP client searches and reads Wikipedia with revision citations |
| **W2** | Measure the bulk embed rate; first full semantic build; hybrid search | Hybrid search live over all articles; a golden set of questions finds the right article in the top 5 for at least 90% |
| **W3** | Monthly in-place update with revision diff | Two consecutive updates complete unattended; a changed article serves its new revision |

W1 is useful without any embedding. W2 is the long one.

## 8. Not in scope (now)

- Images (Kiwix with images, or Commons): later, if wanted.
- Other languages, Wiktionary, Wikidata. Wikidata is a strong later candidate for facts (dates, populations,
  constants).
- Talk, user and project pages.

## 9. Risks

- **The Enterprise free tier changes or ends.** The wikitext dump is the fallback (parser cost, lower text
  quality).
- **The bulk embed competes with other users of the embedding server.** Mitigated by background priority, a
  concurrency cap and a bulk window.
- **Disk:** about 100 GB at peak during a monthly build (two stores side by side for a few hours).
- **A half-updated index.** Mitigated by the per-passage text hash (section 12.6).

## 10. Choices made at the start (2026-10-16, D1)

1. **Source:** the Enterprise HTML snapshot.
2. **Where:** a dedicated container, with less RAM than the 24-32 GB first proposed. So the default is **binary
   quantization**: about 2 GB of RAM per 13M vectors, with full-precision rescoring from local disk. W2 measures
   its recall against int8 on a sample and against the golden set before the full build. If binary misses the
   bar, a larger RAM allocation is the alternative.
3. **The bulk window:** a time the operator picks, with every embedding node awake.
4. **Scope:** the mirror serves Wikipedia itself; it does not copy articles into any other store.

## 11. W0: what the box and the account turned out to be (2026-10-16)

- **The container:** hostname `wikipedia-mirror`, an address reserved in the router's DHCP server, Debian 12, 6 vCPU,
  8 GB RAM at the time, 150 GB local disk. Debian 12 ships Python 3.11 only, so the installer builds 3.13 from
  source. Qdrant 1.12.4.
- **The snapshot:** `enwiki_namespace_0` dated 2026-10-01, 7,338,688 records, 151 GB compressed in 435 chunks of
  236-372 MB. One chunk downloaded in 6 s and unpacks to 2.1 GB of NDJSON (HTML and wikitext per article).
  Rendered HTML per article: median 70K characters, 90th percentile 226K, largest 1.0M.
- **The record:** `identifier` (page id), `name`, `version.identifier` (revision), `date_modified`, `abstract`,
  `categories[]`, `redirects[]`, `templates[]`, `license[]`, `article_body.html` (a full Parsoid document with
  `<section>` wrappers) and `article_body.wikitext`. Duplicates are possible; the highest revision wins.
- **Auth:** login returns a 24-hour access token and a refresh token (90 days or 90 refreshes). The container keeps
  the username and password in a 0600 file so a monthly job can always sign in again.
- **Chunks, not the single file:** a chunk is a small unit to retry and parse in parallel, and 435 of the 1,500
  monthly chunk downloads leaves room for retries. The build counts its own downloads against a budget (1,200 by
  default) and stops before Enterprise does.

## 12. Building both indexes and keeping them current

Decided 2026-10-17 (D3). This section replaces the original "build beside" sync and the original refresh
estimate.

### 12.1 What constrains it

- **Disk.** All sizes are measured or estimated as marked:

  | Data | Size |
  |---|---|
  | Article store | 25 GB measured, doubled for a few hours during a monthly update |
  | Keyword index | 37 GB measured |
  | Semantic index, float16 originals | about 85 GB (41.5M x 1,024 x 2 B) |
  | Semantic index, binary vectors in RAM | about 5.3 GB |
  | HNSW graph | a few GB |

  One copy of everything fits on the NVMe pool (206 GB free after the first build); two full copies of both indexes
  side by side do not, which is why monthly updates change the collections in place.
- **RAM.** Qdrant is capped at 12G today. Only the binary vectors stay resident; the rest is memory-mapped.
- **The free tier.** It gives one full snapshot a month and no change feed. Downloading the whole 151 GB takes about
  45 minutes (435 chunks at about 6 s each), so the download is not worth optimising. Working out what changed is
  our job.
- **Churn is unknown** until two snapshots exist.

### 12.2 The four decisions in this plan

1. **Two collections, one per index, sharing point ids and payload.**
   - `wikipedia_lexical` is the keyword index; `wikipedia_semantic` is W2's.
   - Point id stays `page_id x 10000 + chunk` in both.
   - Search queries both and fuses the results with RRF (k=60).
   - **Why not one collection holding both vectors:** W2 would have to rebuild the keyword index beside the live
     one, putting two keyword copies on a tight disk. And a future embedding model change would re-encode every
     keyword vector for nothing.
   - **The cost:** two loopback queries per search instead of one.
2. **Monthly updates change the collections in place; they are not rebuilt beside the old ones.**
   - A month changes a minority of articles. Rebuilding tens of millions of passages and an HNSW graph every month
     would waste days of compute and double the disk.
   - Building a new collection beside the old one and moving the alias stays for the rare full rebuild: a new
     embedding model, a new chunker, or a damaged index (section 12.7).
3. **Work is reused at three levels:**
   - an article with an unchanged revision is copied forward without parsing its HTML;
   - an article with unchanged plain text costs no index work;
   - a changed article re-embeds only the passages whose text is new.
4. **Semantic vectors:**
   - bge-m3, 1,024 dimensions. Re-examined 2026-10-19: newer open models (Qwen3-Embedding 0.6B, EmbeddingGemma)
     score higher on public benchmarks and would build in about the same time. The maintainer kept bge-m3, since it
     serves the need and the priority is the wider system rather than a perfect Wikipedia lookup. A model change
     stays possible at any later full rebuild (section 12.7);
   - float16 originals on disk, binary quantization in RAM, and rescoring with oversampling (D1);
   - W2 checks binary against int8 through the golden set.

### 12.3 W1.5: format changes before the first build

These changed the store and index formats and landed before the first build, because afterwards they cost a
rebuild:

- **Store schema 2:**
  - a `text_hash` column (the first 8 bytes of SHA-256 of the article's plain text), so the diff never decompresses
    unchanged text;
  - `parser_version` in `meta`, so a parser change forces a full reparse instead of copying stale text forward.
- **Payload gains `text_hash`.** It records which month's text a passage was cut from (section 12.6, "during an
  update").
- **Collections are not named after a snapshot.**
  - They are `wikipedia_lexical_<born>` and `wikipedia_semantic_<born>`, behind the aliases `wikipedia_lexical` and
    `wikipedia_semantic`.
  - The snapshot an index reflects is recorded as `applied_snapshot` in the index state. Serving and health read it
    instead of parsing the collection name.
- **A segment size cap** (`max_segment_size`). When Qdrant's optimizer rewrites a segment to clear deleted points,
  it then needs a few GB of headroom, not a third of the disk.

### 12.4 First keyword build (W1)

1. Download the snapshot to the NAS (about 45 minutes).
2. Parse it into the store with 5 workers.
3. Build the keyword index with 5 encoder processes.

Each stage resumes after a crash. The first run measured the rates and sizes in section 12.8.

### 12.5 First semantic build (W2)

1. **Measurements on a sample** (the remaining one is the HNSW build time and Qdrant's memory use at scale).
   - **Embedding rate:** below.
   - **Recall:** binary and int8, each rescored, against exact search: recall@10 at least 0.9997 on the evaluation
     sample ([EVALUATION.md](EVALUATION.md)); the full-scale golden check is the guard.
2. **Build.** One pass over the store in page-id order:
   - cut each article with the same chunker;
   - embed through the embedding server's `/api/embed` at **16 requests in flight** (the server's knee, below),
     **8 texts per request** (batching adds no speed, but it cuts the server's per-request bookkeeping eightfold
     while a search query waits at most one batch, about 0.5 s); retry a 503 honouring `Retry-After`, and retry a
     timeout;
   - upsert into `wikipedia_semantic_<born>` with HNSW indexing off during the load and switched on at the end
     (Qdrant's bulk-load pattern);
   - checkpoint the last page id fully embedded, so a resume repeats at most one batch;
   - show progress and the ETA on health.
3. **Lend RAM if the graph build is disk-bound:** give the container more memory for the build only
   (`pct set <ctid> -memory <MB>` takes effect live on an LXC), then hand it back.
4. **Switch hybrid on** when the semantic collection covers every page in `applied_snapshot` and the golden set
   passes (at least 90% in the top 5). Until then, keyword search answers alone.

**Embedding rate, measured 2026-10-16** (bge-m3 through the embedding server at background priority, about
2,000-character passages, whichever nodes were awake): 14.5 passages/s with one request in flight, 49.5/s with 8,
65.7/s with 32, no errors; at 64 and 128 in flight the server answered "busy" to most requests.

**Embedding rate, measured 2026-10-18** (with other traffic present on the server). The test drove the server
one text per request, because the server then returned only the first vector of a batch (since fixed). The texts
were real mirror text cut to two lengths and every request was unique, so no runner could reuse work. The server
had bge-m3 on all four nodes by the end of the run; its demand-driven co-load spread it unprompted.

| Passage length | Rate | Levels off at | 503s |
|---|---|---|---|
| about 720 chars (the index's average) | 277 per s, 0.20M chars/s | 16 in flight | none at 48 or fewer in flight |
| about 2,000 chars | 132 per s, 0.26M chars/s | 16 in flight | none |

So the whole snapshot (30.0 billion characters) embeds in **about 42 hours as 41.6M short passages, or about 32
hours as about 15M merged ones**. Passage length moves the time by about a quarter, but storage and RAM by about
2.7x: float16 originals of 85 GB against about 31 GB, and binary vectors of 5.3 GB against about 2 GB.

**What the embedding server's maintainer confirmed (2026-10-18):**
- **The knee is 16, and it is structural.** Each node's bge-m3 runner takes one sequence at a time, and the server
  admits 4 per node. Raising the parallelism setting is server-wide and would multiply the chat models' KV cache
  on the 8 GB cards, so it stays.
- **Batching was expected to be the remaining lever; measured, it is not.** After the server's `/api/embed` fix
  (returns every vector of a list, sizes a batch by its longest text), at 16 in flight: about 2,000 characters
  gave 123, 126, 125 and 122 texts/s at batch 1, 8, 16 and 32; about 720 characters gave 268, 267 and 264 at batch
  1, 16 and 32. The GPUs are the limit, so batch size is chosen for bookkeeping and query latency, not speed.
- **The queue is 32 deep and shared with every other client's query embeddings.** A full queue refuses whatever
  the priority, and priority 3 ages past 5 within a second. **The job's concurrency is the only thing protecting
  other clients' live requests**, so 16 in flight keeps the queue empty and a query waits for at most one batch.
- **One build everywhere.** bge-m3 is the same F16 digest on all four nodes. The Ollama versions differ between
  nodes (0.35.1 to 0.40.3), but the evaluation's canary showed identical vectors.
- **Housekeeping for a two-day run:** the server's usage ledger gains a row per request, so batching helps; a
  pause longer than 5 minutes unloads bge-m3 and lets small GPUs sleep, so a resume waits a minute or two; deploy
  any server fix before the run starts, because it restarts the server.

If a new monthly snapshot arrives mid-build, the build lock holds the monthly update until the semantic build is
done. The update then applies that month's diff to both collections; it is the same operation either way.

### 12.6 The monthly update (W3)

It runs from the existing timer, on the 2nd at 05:00 UTC.

1. **Download** the new snapshot to the NAS (about 45 minutes, counted against the download budget).
2. **Parse with carry-forward.**
   - When a record's page id and revision id match the previous store, and the parser version is unchanged, its row
     (compressed text and hash) is copied across without parsing the HTML.
   - Everything else is parsed.
   - Expected 5-10x faster than a full parse.
3. **Validate before touching either index.** The new store must meet three checks:
   - the record count is within 3% of last month's;
   - parse failures are under 0.1%;
   - a fixed list of 100 sentinel titles is present with text.

   A failure stops the update here. The previous month keeps serving, and health says why.
4. **Diff** by streaming both stores in page-id order. Every page is one of:
   - added;
   - removed;
   - text changed;
   - unchanged (a new revision with the same text counts as unchanged).

   The change list is written to a file on the local disk, and the counts appear on health.
5. **Apply to the keyword index.** This is fast, CPU only.
   - For each added or changed page: encode all its passages, upsert them (overwriting the same ids), then delete
     its chunks numbered at or beyond the new count.
   - Removed pages are deleted by a `page_id` filter.
   - Progress is checkpointed by page id, and re-running a step does no harm.
6. **Apply to the semantic index.**
   - For each changed page, cut both the old and the new text.
   - A new passage whose text hash matches an old passage's takes that point's stored vector, retrieved by id.
   - Only new text goes to the embedder.
   - Checkpointing and re-runs work as in step 5.
7. **Switch over.** When both applies are done:
   - `applied_snapshot` moves to the new month, and serving swaps in the new store;
   - an audit samples 1,000 random pages and checks that their points' `text_hash` matches the store;
   - the old store is deleted once its readers' leases end;
   - the NAS keeps the previous snapshot.

**During an update, search keeps working.** The index is a mix of old and new text while steps 5-7 run, and three
rules keep each answer consistent:
- every hit carries its `text_hash`;
- serving resolves the hash against the new store, then the old one, so a snippet and its `?oldid=` citation always
  come from the text the passage was cut from;
- fusion keys on (point id, `text_hash`), so a passage already updated in the keyword index is never merged with the
  semantic index's older version of it.

**The way back is the same operation.** An in-place update has no instant rollback. The protection is step 3's
checks before anything is touched and step 7's audit afterwards. A bad month found later is undone by applying the
previous snapshot, which the NAS keeps, as if it were the new one. That re-embeds only the passages that differ.

**Expected cost per month** (estimates that W3 replaces with measurements):

| Step | Time |
|---|---|
| Download | about 45 minutes |
| Parse | 1-2 hours |
| Diff | minutes |
| Keyword apply | under an hour |
| Semantic apply | 0.3-1M new passages: 1.5-4 hours at 66/s, under an hour at 200/s |

It runs at background priority on whichever nodes are awake, so a monthly update needs no bulk window.

### 12.7 What forces a full rebuild

| Change | What it rebuilds | How |
|---|---|---|
| Parser version | The store (full reparse) | The monthly path with carry-forward off. Passage reuse still saves the embeddings of text that comes out the same |
| Chunker fingerprint | Both indexes | Build beside, then move the alias. The semantic index needs about 85 GB of headroom for this |
| Embedding model | The semantic index | Build beside, then move the alias |
| The audit fails | The damaged index | Build beside, then move the alias |

### 12.8 Phases and measurements

| Phase | What | Done when |
|---|---|---|
| **W1.5** | Store schema 2, `text_hash` in the payload, collections not named after a snapshot, segment cap | Built 2026-10-17; the first keyword build ran after it and its sizes and rates are below |
| **W2** | Sample measurements, the semantic build, hybrid search with (point id, hash) fusion | Hybrid live; golden set at least 90% top 5 |
| **W3** | Carry-forward parse, validation, diff, both applies, switch-over and audit | Two monthly updates applied unattended; the audit passes; a changed article serves its new revision; measured churn recorded here |

**W1.5 as built.** The store is schema 2 (`text_hash` column, `parser_version` in `meta`). The keyword collection is
`wikipedia_lexical_<born>` behind the alias `wikipedia_lexical`. Its index state,
`state/index_lexical_<born>.json`, doubles as the build checkpoint and records `applied_snapshot`. Serving pairs
(collection, applied snapshot) with that snapshot's store. Search drops a hit whose `text_hash` disagrees with the
store. Carried into W3:
- **Exact-title hits:** during a mixed-month update, a title match with a stale passage should fall back to the
  article's lead rather than be dropped.
- **Rebuilding a snapshot that is serving:** a full rebuild of the snapshot whose `wikipedia_lexical_<born>` is
  serving now refuses with "delete it by hand"; section 12.7's build-beside needs a distinct name.
- **Losing the index state file takes search down** until it is restored, so the state directory should be backed
  up (nothing does it yet).

**First keyword build, as measured (2026-10-17 15:45Z to 2026-10-18 05:12Z).** Snapshot 2026-10-01: 7,245,149
articles, 30.0 billion characters, article store 25 GB. **41,590,915 passages** (exact count, matching the
build's log; Qdrant's approximate collection count read 41,593,444) in 43 segments, collection green, Qdrant
storage 37 GB; 170 GB of disk free afterwards. Indexing ran at 1,150-1,500 passages/s, CPU only. Qdrant's
anonymous memory peaked at 5.1 GB during the last merges and rests at 4.3 GB; the unit's cap is now 12G. Test
searches put the expected article first and answer in 0.2-0.6 s through `wiki-mirror search`. Five things broke on the
way, each fixed in the repo: the client's 5 s timeout, MemoryHigh throttling that stalled the optimizer, and three
OOM kills from concurrent merges and oversized sparse segments (now one optimization at a time, 400 MB segments).
Qdrant recovered from its WAL every time and the build resumed from its checkpoint.

**Moved to NVMe (2026-10-18).** The container's disk now lives on NVMe storage on the host (45 GB used with
compression, 206 GB free). After the move the index was green with the same exact count, 43 segments, and the 12G
cap in force. Measured by a client over the API, 20 searches and 10 article reads, right after the reboot: search
p50 241 ms / p90 483 ms on first touch (segments read from disk), 27 ms / 35 ms once cached; article reads 1-2 ms.

**What that meant for W2.** The passage count is three times the first estimate: the average passage is about
720 characters, not about 2,000. **Passage length was W2's first decision.** Packing short sections together
would need a re-index of the keyword collection too (about 9 hours, CPU only), since both indexes share point ids.

**Passage counts, measured 2026-10-19** on 20,000 random articles from the store, cut with the live chunker and then
packed greedily (consecutive pieces joined while they fit the target; a piece is never split). The v1 projection,
41.5M, matches the 41.59M actually indexed. Packing only inside one top-level section barely helps (2,048 gives
35.8M), because most sections have no subsections, so the options below pack across sections:

| Cut | Passages | Mean chars | Under 300 chars | Embed time | float16 on disk | Binary in RAM |
|---|---|---|---|---|---|---|
| v1, sections never merge | 41.5M | 717 | 31% | ~42 h | 85 GB | 5.3 GB |
| pack to 1,024 | 30.1M | 1,004 | 6% | ~41 h | 62 GB | 3.9 GB |
| pack to 1,536 | 25.1M | 1,205 | 4% | ~39 h | 51 GB | 3.2 GB |
| pack to 2,048 | 21.6M | 1,400 | 3% | ~37 h | 44 GB | 2.8 GB |

Packing buys RAM and time, not disk: v1's binary vectors plus HNSW graph come to about 10-11 GB beside the keyword
index's ~4.3 GB, above Qdrant's 12G cap, while 2,048 needs about half that. The cost is retrieval quality, which
the passage-length evaluation measured before anything was rebuilt.

**Passage-length evaluation and result (2026-10-19, D5): keep v1.** The method, the fairness corrections, the
decision rule and the full results table are in [EVALUATION.md](EVALUATION.md). In short: on 300 golden questions
and a 15,000-article sample, packing found the right article as often but lost the answer from a 320-character
query-focused snippet by 7 to 12 points on random articles (paired +9/-30 to +12/-49), so every packing failed
the rule set before the run. W2 embeds the live cut: 41.5M passages, no keyword re-index. Four more findings:
- **The focused snippet helps today's search:** on v1 it holds the answer 8 to 12 points more often than the live
  cut. It is a change to the snippet alone, worth making before W2.
- **Semantic is the stronger channel on these questions** (dense hit@5 0.953 against lexical 0.750 with hard
  negatives), and equal-weight fusion trails dense alone on hard negatives, so W2 checks fusion weights at full
  scale.
- **Quantization:** binary and int8 with rescoring matched exact search (recall@10 at least 0.9997) on the sample;
  W2 keeps binary vectors with rescoring, with the full-scale golden check as the guard.
- **Embedding rate:** 212,026 texts in 24 min (145 per second, about 0.17M chars/s with this sample's longer
  passages), so the full v1 build is about **two days** (about 31 billion characters).
