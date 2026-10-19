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
at this scale yet.
