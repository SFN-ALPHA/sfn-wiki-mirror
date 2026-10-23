# Decision log

**What this file is:** the dated decisions that shaped the project, with their reasoning, for whoever maintains
it later.

Decisions are numbered D1, D2, ... in the order they were made. A new decision gets the next number and a row in
the table, in the same commit as the change it justifies. A decision that changes the stored data's format, the
chunker, the embedding model, the network posture or the update path belongs here. Quote the measurement, not
only the conclusion, and say what was accepted as a cost.

| ID | Date | Decision |
|---|---|---|
| [D1](#d1) | 2026-10-16 | A local Wikipedia mirror from Enterprise HTML snapshots on a dedicated container; keyword and semantic search; binary quantization |
| [D2](#d2) | 2026-10-16 | The first service build: Enterprise download, parser, article store, keyword index, HTTP API and an MCP server with a key per client |
| [D3](#d3) | 2026-10-17 | Two collections updated in place each month; a per-article `text_hash`; a format change before the first build |
| [D4](#d4) | 2026-10-17 | The container updates itself nightly at 02:20 local time (opt-in for other installations, 2026-10-19) |
| [D5](#d5) | 2026-10-19 | The semantic build embeds the live passage cut with bge-m3; packing rejected on measurement |
| [D6](#d6) | 2026-10-19 | A site's network lives in `/etc/wiki-mirror/site.env`; the firewall in the repository is a template |
| [D7](#d7) | 2026-10-19 | The embedding run is its own stage, unit and key file |
| [D8](#d8) | 2026-10-23 | The semantic index lives on disk, and a maintenance job keeps it healthy |

<a id="d1"></a>
## D1 - 2026-10-16 - A local Wikipedia mirror

**Decision.** Build a local mirror of English Wikipedia ([DESIGN.md](DESIGN.md)): Enterprise HTML snapshots on a
dedicated container, search over all articles by keyword and by meaning, and no copying of articles into any other
store. Decided by the maintainer.

**Reasoning.** The aim is a local copy refreshed regularly, so that clients can process it as fast as they like
while Wikipedia itself is contacted only for the bulk refresh, with a semantic vector index as well, efficient on
disk (20 TB free on the NAS). **Checked that day:** the public HTML dump mirror stopped on 2025-03-24, so rendered
HTML now comes from the free Enterprise Snapshot API. The wikitext dump is about 26 GB compressed, twice a month;
Kiwix nopic is 49 GB, about quarterly. **Measured:** bge-m3 through the embedding server at background priority
ran 14.5 passages/s with 1 request in flight, 49.5 with 8 and 65.7 with 32; the server answered "busy" past that.
That put the first build of an estimated 13M passages at about 2.5 days with the nodes that were awake, and less
with every node up. **Decided:** Enterprise HTML; less RAM than the proposed 24-32 GB, so binary quantization with
full-precision rescoring from local disk, measured against int8 and a golden set first; a bulk window the operator
picks. Phases W0-W3 in [DESIGN.md section 7](DESIGN.md#7-phases).

*Later note.* The plan said Enterprise snapshots "twice a month". D2 measured that the free tier is monthly, and
the cadence has been monthly since.

<a id="d2"></a>
## D2 - 2026-10-16 - The first service build

**Decision.** Build the service ([DESIGN.md section 11](DESIGN.md#11-w0-what-the-box-and-the-account-turned-out-to-be-2026-10-16)):
Enterprise chunk downloads, a Parsoid-HTML parser, a SQLite and zstd article store, a keyword Qdrant index, REST
routes and an MCP server for other agents. Decided by the maintainer, who also asked for MCP access to make the
mirror easy to use from several agents, choosing **the whole LAN with a key per agent**.

**Reasoning.** **Measured (W0):** login works; the 2026-10-01 snapshot is 7,338,688 records, 151 GB in 435 chunks;
the **free tier is monthly**, not twice monthly as some documentation says; downloads redirect (307) to presigned
S3 URLs, and S3 answers 400 when the bearer token is forwarded, so egress is per Unix user (only `wikimirror-sync`
reaches HTTPS) rather than per address. A real 40-record sample shaped the parser and its fixtures. The container
came with public resolvers, which the LAN-only DNS rule would break, so the installer refuses them.

**Built:** `wiki_mirror` (config; records; `enterprise` with token refresh, resumable chunk downloads and
a monthly download budget; `parse`; `store` with highest-revision-wins and a finalized immutable read-only open;
`build` with per-chunk checkpoints and a bounded zstd spool between parse workers and the single writer; `index`
with resume and an atomic alias swap; `search`; `serving`, which follows the alias so a rebuild needs no restart;
hashed per-agent `keys` re-read live; `mcp_server` on the MCP SDK in stateless Streamable HTTP; and the API), and
`deploy/` (CPython 3.13 and Qdrant 1.12.4 checksum-pinned, Python dependencies pinned to the tested
versions, units, a monthly timer on the 2nd at 05:00 UTC with hourly retries, nftables, `wiki-mirror`).

**The fresh-eyes review caught** that apt fetches as `_apt` (the root-only web rule would have stopped every
update), that `python -m` imported from a directory the build user can write (now `-I`), that an early build
failure never reached health (now recorded, and a snapshot older than 40 days reads `degraded`), and that old
article stores were never deleted; plus a build lock, corrupt-chunk re-download, DHCP renewals, fast `reject` for
refused egress, and a chunker fingerprint so a changed chunker shows instead of misaligning snippets.

At the time of writing the service had not yet run on the box; the semantic vectors were still to come.

<a id="d3"></a>
## D3 - 2026-10-17 - Two collections, updated in place

**Decision.** Two Qdrant collections (`wikipedia_lexical`, `wikipedia_semantic`) sharing point ids, **updated in
place each month** instead of rebuilt beside the old ones; carry-forward parsing of unchanged revisions; a
per-article `text_hash` in the store and payload; and a format change (store schema 2, collections not named after
a snapshot, a segment cap) before the first build ([DESIGN.md section 12](DESIGN.md#12-building-both-indexes-and-keeping-them-current)).
Decided by the maintainer.

**Reasoning.** **Why in place:** the container's disk (139 GB free at the time) holds one copy of the store and
both indexes (about 100 GB at peak) but not two, so a build-beside sync cannot survive the semantic index; and a
month changes a minority of articles, so rebuilding tens of millions of passages and an HNSW graph monthly would
waste days. **Why two collections, not one holding both vectors:** the semantic index can be added without
rebuilding the keyword one beside itself, and an embedding-model change re-embeds only the semantic one; the cost
is two loopback queries per search. **Why `text_hash`:** during a half-applied update, a hit is resolved against
the store whose text it was cut from, so snippets and `?oldid=` citations stay honest, and fusion never merges two
months' versions of one passage. **Accepted:** an in-place update has no instant rollback; the protection is
validation before any write and an audit after, and the way back is applying the previous snapshot (kept on the
NAS). The first build waited for the format change, and the install itself went ahead the same day with a clean
dry run.

<a id="d4"></a>
## D4 - 2026-10-17 - Nightly self-update

**Decision.** The container updates itself nightly: `wiki-mirror-update.timer` at 02:20 local time (the
container's own time zone; first written with an explicit zone, changed on 2026-10-19 so the unit carries nobody's
location) runs `update.sh --unattended`, which pulls `main`. Decided by the maintainer, reversing the first
plan that code reaches the box only when the operator decides.

**Reasoning.** An unattended box that follows `main` stays current without a person remembering. **Guards,
because nobody is watching:** a running build (unit active or `state/build.lock` held) defers the update rather
than failing it, since the semantic build can run for days; a change to the on-disk format
(`deploy/FORMAT`: store schema, chunker fingerprint, `INDEX_FORMAT`, kept equal to the code by a test)
is **held** when built data exists, because restarting on it would leave the mirror unreadable until a rebuild;
any non-zero stop after the merge rolls back to the previous commit. Every run writes `state/update.json`, which
`/v1/wiki/health` and `wiki-mirror status` show. **Accepted:** nothing pushes an alert on `held` or `deferred` (there is
no alert unit on this box); the update that installs this feature runs the old script, so only later updates are
guarded; and anything merged to `main` reaches the box within a day.

**Amended 2026-10-19, before the public release.** For an installation other than the maintainer's, the timer is
opt-in: the installer enables it only with `--auto-update` (or the operator runs `systemctl enable --now
wiki-mirror-update.timer`), and re-running the installer never turns an enabled timer off. Reason: an enabled timer
means the box runs, as root, whatever lands on the upstream `main` (or the fork's), which is a trust decision for
each operator and not a default. The maintainer's own box keeps it on.

<a id="d5"></a>
## D5 - 2026-10-19 - The semantic build embeds the live passage cut

**Decision.** The semantic build embeds the index's live passage cut (sections never merge, 41.5M passages) with
bge-m3; packing is rejected on measurement; binary vectors with rescoring, as in D1. Decided by the maintainer.

**Reasoning.** **Measured** ([EVALUATION.md](EVALUATION.md)) with `tools/wiki_golden.py` (300 questions) and
`tools/wiki_passage_eval.py` on a 15,000-article sample. Packing to 1,024/1,536/2,048 characters found the right
article as often but lost the answer from a 320-character query-focused snippet by 7-12 points on random articles
(paired +9/-30 to +12/-49), so every packing failed the rule set before the run. A reviewer caught that the first
version's whole-passage metric favoured packing by length alone (chance 27% against 9%); the rule was fixed before
any number was read. **bge-m3 kept:** newer open models (Qwen3-Embedding 0.6B, EmbeddingGemma) were offered, and
the maintainer chose to stay with bge-m3 as long as it does well enough, since the priority is the work the mirror serves,
not a perfect Wikipedia lookup. **Also measured:** the canary showed identical vectors across all four embedding
nodes despite different Ollama versions; about 0.17M chars/s, so the build takes about two days; the focused
snippet beats today's snippet by 8-12 points on the live cut. **Accepted:** 85 GB of float16 originals on disk
(fits on the NVMe) and about 10-11 GB of binary vectors plus graph in RAM, so Qdrant's memory cap rises for the
semantic build (the box has 32 GB); binary-versus-int8 is not measured separately at full scale, and the
full-scale golden check (hybrid at least 90% top 5) is the guard.

<a id="d6"></a>
## D6 - 2026-10-19 - A site's network lives in `site.env`

**Decision.** The values that differ between networks - the LAN (`LAN_CIDR`, required) and, optionally, the
embedding server's address (`EMBEDDING_HOST`) and its port (`EMBEDDING_PORT`), both or neither - are read by `deploy/install.sh` from
`/etc/wiki-mirror/site.env` and rendered into `deploy/nftables/wiki-mirror.nft`, which is a template. Nothing of
any site's network is in the repository. Decided by the maintainer when the repository was prepared for
publication.

**Reasoning.** The firewall is the control that keeps the API off the internet and the build user off the LAN (except the
embedding server, when one is set), and
its three site values used to be literals in the ruleset, the installer's resolver check and the docs. A public
repository cannot carry one site's addresses, and a deployer should not have to edit a root-loaded file by hand.
**The values reach a ruleset that loads as root**, so the file is data, never sourced: each line must be one of
three keys and a value without whitespace, and each value must be a valid literal (an IPv4 network with no host
bits, a port of 1-65535, an IPv4 address) or the installer stops and lists every problem. The LAN is further
limited, because SSH and the API are admitted from everything in it: a prefix shorter than `/8` is refused (a `/0`
or a `128.0.0.0/1` would open them to the internet, or half of it), and so is any network outside RFC 1918
(10/8, 172.16/12, 192.168/16) and the shared address space 100.64/10, so a public block cannot pass as "the LAN".
Two connection-safety checks keep the firewall from locking the operator out: the installer stops when the SSH
client running it (`SSH_CONNECTION`, IPv4 only) is outside `LAN_CIDR`, and when the box has no global IPv4 address
inside it. The file itself decides what a root-applied firewall admits, so a file that is not owned by root, is
writable by group or others, or is a symbolic link is refused, and the copy of the example the installer makes is
root:root 0644. A first install copies `deploy/site.env.example` and stops, and a file still equal to the example
is refused, so a firewall is never built from example values. The rendered ruleset is checked whole with `nft -c` before it is
applied, and the install is transactional: the file it replaces is kept as `wiki-mirror.nft.previous` until that
check passes, and if the check fails the previous file is put back (or the new one removed, with the include line
and the configuration this run added), so the next boot never loads a ruleset that failed; the resolver preflight
now checks the resolver against `LAN_CIDR` rather than a fixed network. `update.sh` renders the same way (`install.sh --show-firewall`) to report a firewall change, and carries
on if it cannot. **Accepted:** an operator who leaves the example's values but edits a comment passes the
"unedited" check (it compares bytes, not meaning); the installer cannot know whether `LAN_CIDR` contains a
workstation other than the one running it, nor check an IPv6 client or a console session; a LAN outside the four
private blocks (a routed public allocation used internally, say) is refused with no override, because a mistake
there is silent and costly; and the box's existing ruleset is replaced only when the installer is run,
never by an update.

<a id="d7"></a>
## D7 - 2026-10-19 - The embedding run: its own stage, unit and key file

**Decision.** The first semantic build is a separate `embed` stage (`src/wiki_mirror/embed.py`), started by hand
with `wiki-mirror embed-now` through its own unit, `wiki-mirror-embed.service`, with no timer. The embedding
server is set in `mirror.yaml` (`embedding_url`, `embedding_model`, `embedding_concurrency`, `embedding_batch`,
`embedding_headers`); its key, if it needs one, is `EMBEDDING_API_KEY` in `/etc/wiki-mirror/embedding.env` (0600,
`wikimirror-sync`), loaded only by that unit and never a setting. Qdrant's memory cap rises to 20G. Decided by the
maintainer, who wanted the run started the same day.

**Reasoning.** The embed reads the store the live lexical collection serves, so it never touches the download or
parse and does not need the NAS. Each passage is embedded as its title, heading and text, the form the evaluation
measured (D5), and that form, the model and the chunker are recorded in the state, so a resume refuses to mix
vectors made differently. The run holds the build lock, commits windows of 64 articles in order and checkpoints
after each, so a failure repeats only the windows still in flight and the unit's retry resumes it. A server that
answers fewer vectors than texts stops the run rather than putting a vector on the wrong point. A failing run
leaves the build state alone, because the index being served is fine. **Deferred:** the health route's progress
(the journal has a line a minute), hybrid search and fusion weights, and the catch-up after a monthly rebuild,
which embeds only articles whose `text_hash` changed; until that lands, `embed` after a rebuild starts a new full
collection. **Accepted:** the HNSW graphs go in RAM, so the box needs 32 GB; the 1 GB segment cap is not measured
at this scale yet. **Superseded in part by [D8](#d8):** the graphs and the binary copy went in RAM for one day, and
the 20G cap did not hold them; both now live on disk and the cap is 24G.

<a id="d8"></a>
## D8 - 2026-10-23 - The semantic index lives on disk, and a maintenance job keeps it healthy

**Decision.** All three files of the semantic index live on disk: the float16 vectors (as before), the HNSW graph
(`hnsw_config.on_disk: true`) and the binary-quantized copy (`quantization_config.binary.always_ram: false`). The
optimizer threads for the semantic collection go from 1 to 2. The goal is a mirror whose pinned RAM stays under
10 GB. A new job, `wiki-mirror-maintain.timer`, runs `python -m wiki_mirror maintain` every 15 minutes: it resumes a
collection Qdrant left paused, keeps the semantic collection's live settings equal to the constants in `embed.py`,
and writes a record that `/v1/wiki/health` and `wiki-mirror status` show. Decided by the maintainer.

**Context.** The first full embedding run finished on 2026-10-22 at 21:34 CDT: 41,590,915 passages from 7,245,149
articles (exactly the keyword index's count), at 220 passages per second overall, with no over-stride or
empty-passage warning. As the code then said ([D7](#d7)), the run switched HNSW on with the graph in RAM
(`on_disk=False`) and the binary copy pinned (`always_ram=True`), and Qdrant built the graphs in the background, one
segment at a time. At 02:12:18 on 2026-10-23 Qdrant was killed for exceeding its 20G cap (`MemoryMax=20G`), 4 h
38 min into the build, with 25,855,559 of 41,590,915 passages indexed (62%, about 93,000 per minute). systemd
restarted it 6 s later (`Restart=on-failure`), so keyword search was down for those seconds only. After the restart
the semantic collection sat at status `grey`: optimizations pending and not started. Qdrant 1.12 does not restart
pending optimizations after it restarts until an update operation arrives, and nothing noticed; `wiki-mirror status`
did not show the semantic collection at all.

Qdrant's memory at rest after the restart was 19.2 GB of anonymous (pinned) memory, with a peak of 20.1 GB while
loading. Measured, the breakdown was:

| Part | Size |
|---|---|
| HNSW graph links, with 62% built | 6.87 GB (about 250 B per passage, so about 10.4 GB when complete) |
| Binary copy, pinned | 5.32 GB (128 B per passage) |
| Keyword index at rest (measured 2026-10-18) | 4.3 GB |
| Other per-point bookkeeping (ID maps, payload stores) | about 2.7 GB (the remainder; not measured directly) |

With the graph completed in RAM the collection would have rested at about 22.7 GB, so 20G could never have held it.

**Reasoning.** What "on disk" means here: Qdrant memory-maps the files, and the kernel's page cache keeps the
recently used parts in spare RAM and reclaims them under pressure. A Qdrant that is heavy on page cache is not
killed for it, because only anonymous memory can be. The container's memory graph shows the cache as used; it is
lent, not owned, with one exception found afterwards: under cgroup v2 the cache of Qdrant's memory-mapped files is
charged to Qdrant's cgroup, so `MemoryMax` is also its cache budget (below). With the graph and the binary copy on
disk, the pinned memory at rest was expected to be about 7.0 GB for Qdrant. **Measured after the rebuild finished
(2026-10-23, 13:10-13:20): 7.4-7.5 GB** (`RssAnon` 7.39 GB; 7.52 GB of anonymous memory in the cgroup), and 0.14 GB
for the API. The whole container used 7.9 GB of its 34.4 GB (a 32 GiB container, in decimal GB), with 18.7 GB of page
cache and 26.4 GB available. The goal of under 10 GB pinned is met.

The rebuild is not free to start: in every Qdrant version up to 1.20 (`HnswConfig::mismatch_requires_rebuild` and
the config-mismatch optimizer), changing where the graph or the quantized copy lives makes Qdrant rebuild every
segment's graph in the background, although the files are identical (its own source comment says so). It
costs background work, not embeddings: the keyword index keeps serving and nothing is embedded again. Changing
`max_optimization_threads` does not rebuild. The change was applied on the box by hand at 07:22 on 2026-10-23 (a
`PATCH` of the collection, with Qdrant's cap raised to 26G for the rebuild, temporarily, by `systemctl set-property
--runtime`). Measured on the rebuild: 16 segments in the first hour at one thread, and memory fell from 19.4 GB to
14.6 GB in the first 80 minutes. The threads were set to 2 by hand at about 08:45. The rebuild finished green at
13:04, about 5 h 40 min after it started: 41,590,915 points indexed in 85 segments, with no Qdrant restart. It was
limited by disk, not CPU: Qdrant used about 38% of one core.

The maintenance job exists because the incident had two halves, and only one was memory. The other was that nothing
resumed the paused collection and nothing showed it. The job resumes any `grey` collection with an empty optimizers
update, reconciles the semantic collection's settings to the code, and leaves a record that health turns into
`degraded` when the job stops, when Qdrant was restarted after a failure within 24 hours, or when its anonymous
memory is above 90% of its cap. Reconciling to the code, not to a hand `PATCH`, makes the layout a reviewed change.
A settings change that Qdrant would turn into a rebuild waits until the collection's embedding run is complete, the
collection is not `red`, the build lock is free, the disk has its free floor, Qdrant holds no more than 90% of its
memory cap and its restart count is readable and at most 1 ([OPERATIONS.md](OPERATIONS.md#the-maintenance-job)). A change that does not rebuild (the thread count)
waits until the collection is `green` instead: any collection update makes Qdrant stop the optimizations in flight,
which throws away the segment being rebuilt.

The resume is guarded, because Qdrant's `grey` after a restart is deliberate: it keeps an optimization that just ran
Qdrant out of memory from starting again by itself. A job that resumed every `grey` collection on every run would turn
one such optimization into a kill every 15 minutes, and each kill takes keyword search down. The guard is stateless
on purpose: it reads systemd's `NRestarts`, the count of Qdrant's automatic restarts after a failure since it was
last started by hand. A record the job keeps of its own work can be lost, damaged, or left stale by a run killed after
it sent its updates; the count cannot. A `grey` collection is resumed by itself only while the count is at most 1.
At 2 or more, or when the count cannot be read, it is held: neither resumed nor given a settings update. It is held
too when memory is above 90% of the cap or the disk is under its floor, and those two lift by themselves. The bound is
at most two crashes after a start by hand: a first crash the job did not cause is resumed once, and a resume that
kills Qdrant is held at the second crash. After a clean start the job gets one more retry.

The cost is accepted: a second crash holds the collection even when the job did not cause it, and a box whose unit
already shows `NRestarts` of 2 or more holds its `grey` collections until someone resumes them by hand or starts Qdrant
by hand. A start by hand, `systemctl reset-failed` and a reboot reset the count and give the job its allowance again,
on purpose: the operator has looked. The 90% is a judgment, the line health already uses, not a measurement
([OPERATIONS.md](OPERATIONS.md#the-crash-loop-guard)).

**Alternatives considered.**

* **Keep everything in RAM.** About 22.7 GB at rest; it crashed at 20G, and the cap would have had to rise past what
  a 32 GB box leaves for the API, a build and the page cache.
* **Graph on disk, binary copy pinned.** About 12.3 GB pinned. It is Qdrant's usual low-memory setup, and it misses
  the 10 GB goal. It remains the last resort if the latency below proves too slow (lever 6).
* **Upgrade Qdrant first, for inline storage.** Not needed to reach the goal. Still worth doing later, for two years
  of fixes, leaner ID maps and the inline option below.

**Consequences.**

* **Semantic search latency, measured 2026-10-23.** On the box, over loopback, with `limit` 10, `with_payload:
  ["page_id"]` and real stored passages as query vectors. Qdrant's data is on ZFS on the NVMe pool, not on NFS.

  | Case | Median | Worst |
  |---|---|---|
  | Right after the rebuild, 20 stored passages, first query each | 6.0 s | 37 s |
  | Same 20, repeated (cached) | 45 ms | 158 ms |
  | 5 random vectors, first time | 867 ms | 1.1 s |
  | Same 5, repeated | 42 ms | 45 ms |
  | Minutes later, 5 new stored passages, first time | 553 ms | 628 ms |
  | 5 more, first time, rescoring with the full vectors off | 231 ms | 258 ms |

  For the last 10 first-time queries Qdrant read 2,870 MB from disk, about **290 MB per query**: a search walks the
  graph of every one of the 85 segments and reads small pieces of the graph, the binary copy and, when rescoring, the
  float16 vectors. ZFS reads a whole record per access (128K by default; the dataset's `recordsize` was not checked),
  so most of those bytes are amplification. For comparison, keyword search on the box takes 241 ms cold and 27 ms
  cached. So cached semantic search is fast, a first-time search takes about half a second, and the first searches
  after a restart or with a cold cache can take seconds to tens of seconds. Nothing searches the semantic collection
  yet, so nothing is slower today; this decides what to do before hybrid search ships.
* **The levers if that is too slow for hybrid search**, in the order they are worth trying; each is still to be
  measured and none is done: (1) fewer, larger segments (about 85 down to 10-20), at the next rebuild: each search
  walks every segment's graph, so the work per query falls about in proportion, with no extra RAM; (2) a smaller ZFS
  `recordsize` for Qdrant's dataset (for example 16K), host-side, applying to files written after the change and so
  to the next rebuild; (3) rescoring and oversampling: rescoring with the full vectors costs about half the time
  here, and whether it can be lowered without losing recall is to be measured on the golden set; (4) pre-warm the
  graph and binary files after Qdrant starts, so the first searches after a restart are not the slow ones; (5) a
  newer Qdrant (1.16 or later) with `hnsw_config.inline_storage`, which stores each neighbour's quantized vector next
  to the link so a graph step is one read (an estimate from its file format: about 150 GB more disk; it needs a
  staged upgrade from 1.12); (6) the binary copy in RAM (`always_ram: true`), about 5.3 GB more pinned (about 12.8 GB),
  which misses the 10 GB goal: the last resort.
* **The code is the truth for the semantic settings.** `embed.SEMANTIC_HNSW`, `SEMANTIC_QUANTIZATION` and
  `SEMANTIC_OPTIMIZATION_THREADS` are the layout. A setting changed by hand on the box is put back within 15 minutes.
  To change the layout, change the constants; the job then applies the change, and for a graph or quantization
  placement change Qdrant rebuilds every segment's graph (hours of background work, limited by disk). A new collection from `embed` is created
  with the same constants, so a fresh embedding run starts on the same layout. This applies to **every box that runs
  the code**: a changed layout constant starts a rebuild of the semantic collection on each box's next maintenance run
  after the code update (hours of disk work, search unaffected). Unlike a change to `deploy/FORMAT`, nothing holds
  such an update back; a hold like that is a possible follow-up.
* **Qdrant's memory cap is 24G, and the page cache counts against it.** Under cgroup v2 the cache that Qdrant's
  memory-mapped files bring in is charged to its cgroup, so `MemoryMax` is also its cache budget. Measured under a 26G
  runtime cap: `memory.current` 27.91 GB, equal to the cap (anonymous 7.52 GB plus file 19.93 GB, of which 16.06 GB
  mapped); `memory.events` showed `max` 13693 (the cgroup reached its cap and reclaimed cache that often), `oom` 0 and
  `oom_kill` 0. Reclaiming cache is normal and never kills, but a smaller cap means a smaller cache and slower
  semantic search. The 14G first planned would have left about 6.5 GB of cache, less than the graph (about 10.4 GB)
  and the binary copy (about 5.3 GB) that a search walks. The cap's job is to stop a runaway Qdrant before the
  container runs out of memory, not to limit cache. On a 32 GB box 24G is what is left after the other units' caps:
  API 1.5G, build 5G, maintenance job 256M (the embedding run, 2G, never runs beside a build: they share the build
  lock) and about 1.25 GB for the system, so even if everything peaks at once the container does not run out. A box
  with less RAM lowers the cap by the difference; the cost is cache, so semantic search is slower. Qdrant's pinned
  memory during a monthly build, when two keyword indexes exist side by side, is an estimate of 12 GB and the one
  figure **still to be measured**. A box that set a cap at runtime keeps it until reboot; the unit's 24G applies after
  the next `install.sh` run or reboot.
* **Alerting stays pull-only.** Nothing pushes. A stopped job, a recent crash or a Qdrant near its cap shows as
  `degraded` in `/v1/wiki/health` and as red lines in `wiki-mirror status`.
* **Accepted:** the job's resume depends on a Qdrant behaviour that a newer version may fix, which makes the resume a
  harmless no-op; the job does not start or retry embedding runs, and does not touch the keyword collection's
  settings or any segment size.
