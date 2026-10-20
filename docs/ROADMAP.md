# Roadmap

**Audience:** whoever plans the next piece of work. Status as of 2026-10-19. The phase names (W0 to W3) come from
[DESIGN.md §7](DESIGN.md#7-phases) and [§12.8](DESIGN.md#128-phases-and-measurements); the decisions are in
[DECISIONS.md](DECISIONS.md).

| Phase | What | Status |
|---|---|---|
| W0 | The LXC, storage, Enterprise account | Done 2026-10-16 |
| W1 | Download, parse, article store, keyword search, REST, MCP | Done; installed 2026-10-17 |
| W1.5 | Store schema 2, `text_hash`, collections not named after a snapshot, segment cap | Done 2026-10-17 |
| First keyword build | 7,245,149 articles, 41,590,915 passages (snapshot 2026-10-01) | Done 2026-10-18; moved to NVMe the same day |
| Nightly self-update | `update.sh` with deferral, format guard, rollback ([D4](DECISIONS.md#d4)) | Built 2026-10-17; opt-in for an installation (`--auto-update`), on for the maintainer's |
| **Annotations** | What the parser drops today, kept beside the text: links, images, references, infobox fields, tables, math, quality signals and more; article reads return them | **Planned 2026-10-19**, [below](#article-annotations-and-the-reader); must be on the box before the 2026-11-02 build |
| **Reader** | A thin web page on the LAN: search, results, article text with clickable links | **Planned 2026-10-19**, [below](#article-annotations-and-the-reader) |
| **W2** | Semantic index, hybrid search, query-focused snippet | **In progress.** Cut decided 2026-10-19 ([D5](DECISIONS.md#d5)); the embedding run is built ([D7](DECISIONS.md#d7)); hybrid search, the fusion-weight check, the focused snippet and the post-rebuild catch-up remain |
| **W3** | Monthly in-place updates | Designed ([DESIGN.md §12.6](DESIGN.md#126-the-monthly-update-w3)); not started |

Until W3 lands the monthly timer runs a full build beside the live collection and swaps the alias, which should work
but redoes everything each month. The first build was started by hand; the timer's first unattended run is due
2026-11-02, so the unattended monthly rebuild is scheduled and not yet proven.

## Article annotations and the reader

**Goal.** An article read gives the full picture, not just the prose: an agent can follow its links and its
editors' "main article" pointers without a new search, check which source backs a sentence, read infobox facts
and table data as structure, see which images the article shows, and judge how far to trust what it read. A
person on the LAN can search and read the mirror in a browser with the same links. **Done when** a `wiki_read`
reply carries the window's annotations (bounded), following a link is a plain `wiki_read` of its target, and the
reader shows a searched article with working local links.

**Why now.** The parser keeps clean text only (`src/wiki_mirror/parse.py`): it drops link targets, references,
hatnotes, "See also", cleanup banners and `[citation needed]`, flattens infoboxes and tables, and ignores record
fields such as the Wikidata id. All of it can only be captured when an article is parsed, which happens in a
build. The monthly timer runs a full build on **2026-11-02 at 05:00 UTC** (until W3, every monthly job is a full
build), so whatever capture is installed on the box before then arrives with that build at no extra cost. Missing
the date means a hand-started rebuild (about 14 hours) or waiting for 2026-12-02. **Target: merged and installed
by 2026-10-30.**

### Part 1: capture (parser and store)

**One mechanism.** The parser emits **typed annotations** beside the text: a kind, a position in the final clean
text (a start and end offset for a span, or a single offset for something that sits at a point, such as a figure
or a footnote marker), and a small structured payload. The store keeps them per article in one place, and reads
return them through one path. A kind added later is a parser change, not a new table, a new read path or a new
reply field. Every kind has a typed model (a Pydantic model or dataclass) that the parser produces, the store
encodes and the API serializes, so the agent-facing shape and the stored shape cannot drift apart.

**The text stays byte-identical.** The chunker, the keyword encoder and every stored text must not change
(constraint 1 in [CLAUDE.md](../CLAUDE.md)): annotations sit beside the text, never inside it. Offsets must
survive `sanitize(untrusted=True)`, which runs after `html_to_text`; compute them against the sanitized text and
prove it on the golden fixtures (the text is identical to today's; every span's slice equals the text it came
from).

**Kinds**, all approved 2026-10-19. Fixture counts are from the eight records in `tests/fixtures/wme/`.

| Kind | Source | Payload | Fixtures |
|---|---|---|---|
| `link` | `a[rel="mw:WikiLink"]` in kept content | target title (percent-decoded, underscores to spaces), fragment | 651 |
| `related` | hatnotes ("Main article: X", "Further information", "For other uses, see Y") and the "See also" section, both dropped from the text today | relation (`main`, `further`, `see_also`, `other_uses`), target titles, the section it belongs to | 4 hatnotes |
| `image` | `typeof="mw:File/..."` figures and inline images, including the infobox image | file title, caption, alt text; icons, flags and decorative images under a size threshold are skipped (threshold set on the fixtures) | 22 |
| `ref` | footnote markers (`mw:Extension/ref`) and the reference list (`mw:Extension/references`, with its `cite` elements and COinS `Z3988` metadata) | per marker: its position and footnote id; per footnote: citation type, title, authors, date, publisher or journal, URL, DOI, ISBN and the plain citation text | 44 markers, 23 citations |
| `infobox` | the infobox table | its type (from the class or template) and its rows as label and value fields, in order, values with their links; not capped at 40 rows like the flattened text | 18 |
| `table` | `wikitable`s | caption, header rows, cells as rows of strings (with row and column spans resolved), and position | 5 |
| `math` | `mw:Extension/math` | the LaTeX source (the annotation's `alttext`), display or inline, and position | 0 (check what the parser emits for formulas today) |
| `coordinates` | the `geo` microformat | latitude, longitude, and whether it is the article's own location (title coordinates) or one in the text | 0 |
| `quality` | cleanup banners (ambox and similar), `[citation needed]` and similar inline tags, `templates[]` (Featured or Good article status), and `version.scores.revertrisk` | banner kinds with their section, inline-tag positions and counts, article status, revert-risk probability | present |
| `section` | the headings | anchor id per section (as Wikipedia builds it), so `#Section` links land in the right place | all |

**Article-level fields**, from the Enterprise record and the HTML, stored with the article row rather than as
annotations: the **Wikidata id** (`main_entity.identifier`, in every record) and the **short description**
(in the HTML; 5 of 8 fixtures). Not kept: navboxes (41 in the eight fixtures: large, repetitive, and their links
duplicate the article's), editor names, interlanguage links and page furniture.

**Images are references only.** The mirror never downloads one. The file page URL
(`https://commons.wikimedia.org/wiki/File:<Name>`, or the English Wikipedia file page for a local, non-Commons
file) is derived at read time; it carries the image's own licence and author, which differ from the article's.

**Store: the engines stay; the layout is new** (reviewed 2026-10-19). The data is written once a month by one
build, in page-id order, into a new file per snapshot, and read by point lookups (an article by id, title or
redirect, then a window of it) from many concurrent readers while nothing writes. **SQLite** fits that exactly:
embedded, no server to run, concurrent readers, terabyte limits, and a file per snapshot that is built beside the
live one and swapped. **Qdrant** stays for vectors only. Considered and rejected: PostgreSQL (solves concurrent
writers and network clients, which this does not have, and adds a server to run and back up; a whole-snapshot
swap gets harder), RocksDB or LMDB (marginally faster point reads, at the cost of the title and redirect
indexes), DuckDB or Parquet as the store (strong for corpus-wide analysis, weak for fetching one article; if
analysis is wanted, export Parquet beside the store), and Qdrant payloads (per passage; annotations there would
bloat the index). Recommended layout:

1. **A separate annotations file per snapshot** (`annotations-<snapshot>.sqlite` beside the article store),
   built in the same pass and sharing the store's lifecycle: built beside, swapped together, deleted together,
   carried forward together in W3. The text store stays at schema 2, so `deploy/FORMAT` does not change, the
   nightly update is not held, and today's snapshot keeps serving untouched. A snapshot without the file has no
   annotations, and reads say so rather than pretend an article has none. The file carries its own schema
   version and the parser version that wrote it.
2. **One compressed blob per article for the bulk** (references, infobox, tables, images, math, quality, links):
   a compact binary encoding (decide between MessagePack and compact JSON on the chunk), in text order, so a read
   is one row fetch, matching how reads work. Fields that a reply filters on (kind, offsets) sit at the front of
   each record so a window can be cut without decoding every payload.
3. **Small indexed tables only for lookups across articles:**
   * **Wikidata id to page id** (and the short description) on every article: cheap, and the join key for
     Wikidata later.
   * **"What links here"** (reverse links: target to source pages), if the measurement allows. An agent
     browsing related topics, and the reader's related panel, both want it; it could be hundreds of millions of
     rows, so store it compactly (target page id and source page id as integers, `WITHOUT ROWID`) and decide on
     its measured size.
4. **zstd with trained dictionaries.** Small, repetitive records compress far better with a dictionary trained
   on a sample of the corpus: one for annotation blobs, and measure whether one for article text shrinks the
   existing 25 GB enough to justify it (that one would change the text store, so it would wait for a planned
   format change). Each dictionary is stored in its file's `meta` table with its id, so a file always decodes on
   its own.
5. **Read-side settings for files that never change while served:** opened read-only and immutable, a page size
   chosen on the measurement, memory-mapped I/O sized to the box, and `ANALYZE` run once at the end of the build
   so the query planner has statistics.

**Measure before committing**, on one snapshot chunk (about 100,000 articles): bytes per article per kind, blob
encoding and dictionary gains, the reverse-link table's size, read latency for a window with annotations, and
the parse stage's slowdown so the monthly build still fits its window. References are a large share of each
page's HTML, so the total could be several GB to tens of GB for about 7.2M articles; 206 GB was free on
2026-10-18. The measurement and the chosen layout go into [DECISIONS.md](DECISIONS.md) as the next decision.

**No outage on upgrade.** Because the text store is untouched, the API serves today's snapshot exactly as now
until the 2026-11-02 build produces a snapshot with an annotations file. Code that reads annotations must treat
a missing file as "none for this snapshot", tested.

**If time runs short.** Capture is in this priority order: `link`, `section`, the article-level fields,
`related`, `quality`, `image`, `ref`, `infobox`, `table`, `math`, `coordinates`. A kind not ready by 2026-10-30
waits for the 2026-12-02 build. That cut is decided and written down, not left to chance.

**Tests.** The golden fixtures pin every kind. Named tests for at least: a redirect target, a section link, a red
link, a link inside a dropped reference (not recorded), a link inside a table cell, each hatnote relation, a
captioned figure, an infobox image, a skipped icon, a footnote marker resolved to its structured citation, an
infobox field with a link in its value, a table with merged cells, a math formula, title coordinates, a cleanup
banner and a `[citation needed]` position.

### Part 2: annotations in the API and MCP replies

* `GET /v1/wiki/article` and the `wiki_read` MCP tool return the annotations that fall inside the returned
  window, grouped by kind, plus the article-level fields (Wikidata id, short description, quality summary) on
  every read. Bounded: a cap per kind per reply, links deduplicated by target, so a dense window stays small;
  a parameter selects kinds. Following a link or a `related` pointer is an ordinary `wiki_read` of its title; no
  new tool.
* A footnote marker in the window comes with its citation, so an agent can see which source backs the sentence.
* Images: caption, alt text, file title and the file page URL, so an agent that needs the diagram can open it
  with its own web access. The reply says plainly that the image is not in the mirror and has its own licence.
* If the reverse-link table is built, a read can ask for the pages that link here (bounded and paged), so an
  agent can explore outward and inward from an article.
* Search replies gain the short description only.
* A schema-2 store returns no annotations and says they are unavailable for this snapshot, rather than pretending
  the article has none.
* [API.md](API.md) and [MCP.md](MCP.md) document every kind; the MCP tool description tells the agent the links
  are there to follow and the quality signals are there to weigh.

### Part 3: the reader

A few server-rendered pages served by the existing API process on the same port; no JavaScript, no new
service, no firewall change.

* **Pages:** `/reader/` (a search box), `/reader/search?q=` (results: title, section, snippet, snapshot date),
  `/reader/wiki/<Title>` (the article: headings from the stored `##` lines, paragraphs, lists, the flattened
  infobox, links from Part 1 as local `/reader/wiki/` links, unresolved ones as plain text; each image as a
  captioned placeholder with an "open on Wikimedia Commons" link, never loaded automatically, so a page view
  sends nothing to Wikimedia and `img-src 'self'` holds; loading thumbnails inline could be a later opt-in
  setting). With Part 1's data the article page also shows: the short description under the title, cleanup
  banners and the article's status at the top, "Main article" pointers under their headings, the infobox as a
  table of its fields, wikitables as tables, footnote markers linking to a reference list at the end with each
  citation's DOI or URL, math as its LaTeX source, and the coordinates. The page footer
  carries the snapshot date, the `?oldid=` link to the exact revision on Wikipedia and the CC BY-SA 4.0 licence
  line. Long articles page by section or by window, using the existing windowed read.
* **Clean and easy to navigate.** A readable single column (comfortable line length, system fonts, light and
  dark following the device), usable on a phone. A persistent search box on every page; results with title,
  short description, section and snippet. On the article page: a table of contents from the section anchors
  (a sidebar on wide screens, a collapsible list on narrow ones), the infobox beside the lead on wide screens,
  "Main article" and "See also" pointers where the editors put them, and a related panel at the end (See also,
  then the pages that link here when that table exists). A redirect lands on the target with "redirected from"
  shown; a disambiguation page renders as a list of choices with their short descriptions. Every page is a
  plain link-addressable URL, so the browser's back button, bookmarks and new tabs just work. Keyboard focus is
  visible and the markup is semantic (headings, lists, `<table>`, `<nav>`) so screen readers work too.
* **Untrusted text.** Everything from Wikipedia is HTML-escaped on output; a strict `Content-Security-Policy`
  (`default-src 'self'`, no scripts) and `X-Content-Type-Options: nosniff` on every reader page. One small
  stylesheet, light and dark. Pages are rendered from templates with autoescaping on by default (Jinja2, a new
  dependency to pin, or the standard library's `html.escape` behind one small rendering helper; decide in
  review), never by string concatenation of untrusted text.
* **Login.** A browser cannot send a bearer header on a click, so the reader gets a sign-in page: the person
  pastes a client key (best: one issued for the reader with `wiki-mirror key add reader`), and the response sets
  an `HttpOnly`, `SameSite=Strict` cookie limited to `/reader`. The guard (`_Guard` in `api.py`) accepts that
  cookie on `/reader/*` routes only; the sign-in page and its form are the only unauthenticated paths, they leak
  nothing about other paths, and every other route keeps requiring the bearer header. Revoking the key signs
  the browser out at once. Sign-out clears the cookie. Decide in review whether the cookie holds the key itself
  or a signed session token (the trade is a server secret to manage against a key at rest in the browser).
* **Privacy, as for the API.** Query text and titles are never logged; a reader request logs its route, client
  name and result count only.
* **Out of scope:** image files, rendered math, full Wikipedia styling (Kiwix does that well; see the README),
  editing, and any route that writes.

### Order and ownership

Part 1 first, because of the build date, and it starts with the measurement on one snapshot chunk (annotation
size, parse slowdown) that settles the store design. Part 1 is large enough to split between coders by kind once
the mechanism and the store exist: the mechanism, `link`, `section` and the article fields first, then the other
kinds in parallel. Part 3 can be built at the same time in separate files (a new `reader` module and its
templates, against today's article reads, with annotations switched on when Parts 1 and 2 land); Part 2 after
Part 1. Parts 1 and 2 touch the parser, the store and the API's read path, so they get a reviewer; Part 3 gets
one for the sign-in and the guard change.

## W2: the semantic build

**Goal.** Hybrid search (keyword and bge-m3 meaning, fused) live over all articles. **Done when** the golden set
([EVALUATION.md](EVALUATION.md)) shows the answer article in the top five for at least 90% of its questions,
with hybrid on.

**Decided** ([D5](DECISIONS.md#d5), [DESIGN.md §12.8](DESIGN.md#128-phases-and-measurements)):

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
   ruleset admits `wikimirror-sync` to the embedding server set in `/etc/wiki-mirror/site.env` when you give it
   (`EMBEDDING_HOST` and `EMBEDDING_PORT`, both or neither; optional, because only the embedding run uses them, and
   Ollama on the box itself needs neither; rendered into `deploy/nftables/` by the installer; check the installed ruleset has the rule with
   `nft list ruleset`, and apply a change with `install.sh`). The embedding job runs as
   `wikimirror-sync`, like the other build stages.
2. **An embedding-server key** for the mirror, with no other use (if the server requires one). It must live in a 0600 file readable by `wikimirror-sync` alone
   (like `enterprise.env`), never in `mirror.yaml`, a log or model context. The key's file, its setting name
   and its place in `config.py` are not designed yet; record the choice as the next decision in [DECISIONS.md](DECISIONS.md).
3. **Qdrant's memory cap raised from 12G to about 20G** (`MemoryMax` in
   `systemd/wiki-mirror-qdrant.service`; the box has 32 GB). The keyword index rests at 4.3 GB; binary
   vectors plus the HNSW graph add about 10 to 11 GB. Also check `vm.max_map_count` on the Proxmox host
   (`sysctl -w vm.max_map_count=1048576`). The API (1.5 GB) and a build (5 GB) must still fit beside it.
4. **The indexer.** **Built 2026-10-19** as the `embed` stage ([D7](DECISIONS.md#d7); how to run it, and how it
   resumes and verifies its checkpoint: [OPERATIONS.md](OPERATIONS.md)), except the health route's progress, which is
   a log line each minute instead. As planned: a build stage after `index`, or a mode of it, that makes one pass over
   the article store in page-id order:
   * cut each article with the same chunker, and embed through the embedding server's `/api/embed`;
   * **16 requests in flight** (the knee measured on the maintainer's embedding router; tune it to your server),
     **8 texts per request**, and a background-priority header where the server has one (`X-Priority: 3` on that
     router);
   * retry a 503 honouring `Retry-After`, and retry a timeout;
   * upsert into the new collection with HNSW indexing off during the load and switched on at the end;
   * checkpoint the last page id fully embedded, so a resume repeats only the windows in flight, and verify it
     against Qdrant on resume; write it into an index state file like the keyword collection's;
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
revision; measured churn recorded in [DESIGN.md §12.8](DESIGN.md#128-phases-and-measurements).

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

**Known gaps to close in W3** (from [DESIGN.md §12.8](DESIGN.md#128-phases-and-measurements)):

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

* Image files themselves (references to them are planned above), other languages, Wiktionary, Wikidata (a strong candidate for dates, populations and constants),
  talk and user pages ([DESIGN.md §8](DESIGN.md#8-not-in-scope-now)).
* A category filter on search. [DESIGN.md §5](DESIGN.md#5-the-design) proposed one; the store keeps categories,
  but the API does not offer it yet.
* Alerts. Nothing pushes a notification on a `held`, `deferred` or `failed` update or build; they show on
  `wiki-mirror status` and health only.
* A different embedding model, at a later full rebuild, if a measurement says it is worth it
  ([DESIGN.md §12.7](DESIGN.md#127-what-forces-a-full-rebuild)).
* If the Enterprise free tier changes or ends, the wikitext dump is the fallback (a parser cost, lower text
  quality).
