# Sizing: data, memory and compute

**Audience:** someone deciding whether their hardware can run this. Every figure is labelled **measured** (with a
date) or **estimate**. Measured figures come from the maintainer's deployment on the 2026-10-01 snapshot; the
sources are [DESIGN.md](DESIGN.md) sections 3, 11, 12.1 and 12.8 and [EVALUATION.md](EVALUATION.md). Where a
figure there disagrees with another, the measured one is used.

## Summary

| Item | Size | Status |
|---|---|---|
| Enterprise snapshot, compressed (435 `.tar.gz` chunks) | 151 GB | measured 2026-10-16 |
| The same, uncompressed (HTML plus wikitext) | roughly 700 GB of HTML, about 900 GB with wikitext | estimate (see below) |
| Article store (SQLite, zstd text) | 25 GB (file 26.7 GB) | measured 2026-10-18 |
| Keyword index (Qdrant sparse vectors) | 37 GB on disk; 4.3 GB RAM at rest | measured 2026-10-18 |
| Semantic index, live cut (41.5M passages) | about 95 GB on disk, about 10 GB RAM | estimate |
| **Total local disk after the semantic build** | **about 160 GB** | estimate |
| Snapshot storage (current and previous) | about 300 GB | measured per snapshot |
| Embedding the whole corpus once | about 4.7 exaFLOPs, about 45 h on the maintainer's 4-GPU server | estimate and measured |

## Raw data

**The Enterprise snapshot** (`enwiki_namespace_0`, 2026-10-01; measured 2026-10-16):

- 7,338,688 records, **151 GB compressed**, in 435 `.tar.gz` chunks of 236-372 MB. One chunk downloaded in 6 s,
  so the whole download takes about **45 minutes** on the maintainer's link.
- A chunk unpacks to about 2.1 GB of NDJSON (rendered HTML and wikitext per article), so 435 chunks are roughly
  900 GB uncompressed. The HTML alone is about **700 GB** (the project's estimate when choosing a parser). It is
  never stored: each chunk is streamed through the parser. It is why the parser uses lexbor, a C HTML parser,
  where the standard library's would take most of a day on six cores.
- Rendered HTML per article: median 70K characters, 90th percentile 226K, largest 1.0M (measured 2026-10-16).
- The snapshot is replaced monthly. Keep the current and previous one (`retain_snapshots` = 2), so **about 300 GB**
  of bulk storage. Network-attached storage is fine for these: they are read sequentially once per build.

## Stores on local disk

Qdrant and the article store must be on local disk (SSD or NVMe), not on network storage.

**Article store** (measured 2026-10-18): 7,245,149 articles, 30.0 billion characters of plain text, **25 GB**
(the file is 26.7 GB). About 3x compression on prose.

**Keyword index** (measured 2026-10-18): 41,590,915 passages in 43 segments, **37 GB** on disk. Qdrant's anonymous
memory peaked at 5.1 GB during the last merges and rests at 4.3 GB. The rest is memory-mapped files that the
kernel can drop.

**Semantic index for the live passage cut** (estimates; the semantic build has not run yet). 41.5M passages,
1,024 dimensions:

| Part | Arithmetic | Size |
|---|---|---|
| Float16 originals, on disk | 41.5M x 1,024 x 2 B | about **85 GB** |
| Binary-quantized vectors, in RAM | 41.5M x 1,024 bits = 128 B each | about **5.3 GB** |
| HNSW graph (m=16) | layer 0 holds up to 2m = 32 links of 4 B per point: 41.5M x 128 B | about **5 GB** |
| Payload (ids, section, hash) | | about 1-2 GB |
| **Total on disk** | 85 + 5 + 1-2, plus the binary copy if stored | **about 95 GB** |

The binary vectors and the graph make about 10 GB that should be resident for fast search. `qdrant.yaml` currently
sets the HNSW index to `on_disk: true`; whether the graph is held in RAM or paged from NVMe is a configuration
choice to settle in the semantic build ([ROADMAP.md](ROADMAP.md)).

**Totals (estimates).**

- Local disk: 25 (articles) + 37 (keyword) + about 95 (semantic) = about **160 GB**. Add headroom: a monthly
  build holds two article stores side by side for a few hours (about 100 GB peak before the semantic index), and
  Qdrant's optimizer needs free space for segment rewrites. The build refuses to start below 20 GB free. **Plan on
  250 GB of local disk, plus about 300 GB of bulk storage for the snapshots.** This is the one disk figure the other
  docs quote.
- RAM for Qdrant: about 4-5 GB (keyword) + 5.3 GB (binary) + up to 5 GB (graph) = about 15 GB, which is why the
  cap rises from 12G to about 20G for the semantic build. A 32 GB host also runs the API (cap 1.5 GB) and a build
  (cap 5 GB), and the kernel needs page cache for the memory-mapped files.

**Packed alternatives** (estimates from the passage-length evaluation, not adopted; see [D5](DECISIONS.md#d5)):

| Cut | Passages | float16 on disk | Binary in RAM |
|---|---|---|---|
| Live cut (sections never merge) | 41.5M | 85 GB | 5.3 GB |
| Packed to 1,024 characters | 30.1M | 62 GB | 3.9 GB |
| Packed to 1,536 characters | 25.1M | 51 GB | 3.2 GB |
| Packed to 2,048 characters | 21.6M | 44 GB | 2.8 GB |

## Compute to embed the corpus (estimates)

The semantic build runs every passage through an embedding model once. The model is bge-m3 (the XLM-RoBERTa-large
architecture). The arithmetic below lets you redo the estimate for your own hardware.

**FLOPs per token.**

- bge-m3 has about 568M parameters. About 256M of them are the token-embedding table (250K vocabulary x 1,024),
  which is a lookup and costs no matrix multiplications. About **305M** are in the 24 transformer layers.
- A forward pass costs about 2 FLOPs per non-embedding parameter per token: 2 x 305M = **0.61 GFLOP per token**.
- Attention adds about 4 x layers x sequence length x model width = 4 x 24 x 250 x 1,024 = 0.025 GFLOP per token
  at an average passage of about 250 tokens.
- Total: about **0.63 GFLOP per token**.

**Tokens to embed.**

- Passage text including title and heading prefixes comes to about 31 billion characters (the corpus is 30.0
  billion characters of article text).
- XLM-RoBERTa's tokenizer produces about 4.2 characters per token on English.
- 31e9 / 4.2 = about **7.4 billion tokens**.

**Total work.** 7.4e9 tokens x 0.63e9 FLOPs = about **4.7e18 FLOPs (4.7 exaFLOPs)**.

**Calibration against the measured run.** On the maintainer's embedding server (four nodes with consumer GPUs
behind an Ollama-compatible front end), the evaluation run embedded 212,026 texts in 24 minutes, about 0.17M
characters per second, and the earlier measurement at 720-character passages reached 0.20M characters per second
(measured 2026-10-18 and 2026-10-19). At about 0.17-0.19M characters per second, 31 billion characters take about
**45 hours**, in line with the "about two days" planned for the build. Dividing the work by the time:

> 4.7e18 FLOPs / (45 h x 3,600 s/h = 162,000 s) = about **29 TFLOPS effective**, or about 7 TFLOPS per node.

"Effective" means useful model FLOPs per second of wall time. It includes padding to the longest text in a batch,
tokenization, HTTP and scheduling overhead, and idle gaps, so it is well below the GPUs' peak FP16 numbers. Real
utilisation for small-batch embedding serving is often **20-40% of peak**; do not size from a data sheet.

**Hours for the whole corpus, by effective throughput** (hours = 4.7e18 / (effective TFLOPS x 1e12 x 3,600)):

| Effective TFLOPS | Hours |
|---|---|
| 10 | about 130 |
| **29 (measured, four nodes)** | **about 45** |
| 30 | about 44 |
| 100 | about 13 |
| 300 | about 4.4 |
| 1,000 | about 1.3 |

**Rule of thumb:** hours = about 1,300 / effective TFLOPS, for this corpus and this model. To use it, measure your
own effective rate on a sample (embed 10,000 real passages, divide 0.63 GFLOP x their token count by the wall
time), not the peak rating. The server's concurrency also matters: on the maintainer's server the rate levels off
at 16 requests in flight and larger batches add nothing ([DESIGN.md section 12.5](DESIGN.md#125-first-semantic-build-w2)).

**The monthly update re-embeds only changed passages** ([DESIGN.md section 12.6](DESIGN.md#126-the-monthly-update-w3)).

- Upper bound, estimate: if 10-20% of the work were redone, that is one tenth to one fifth of the full figure,
  about 4.5-9 hours at 29 TFLOPS.
- The design expects less. A passage whose text hash is unchanged keeps its stored vector, and many revisions
  change no plain text. The design's estimate is 0.3-1M new passages a month, 0.7-2.4% of the corpus: about 0.3-1.1
  hours at 29 TFLOPS. Churn is not measured until two snapshots exist.

**Other models.** Time scales with a model's non-embedding parameter count (and with how many tokens its
tokenizer makes from the same text). A model with half bge-m3's 305M non-embedding parameters would take about
half the time; one with 3 billion would take about ten times as long (3,000 / 305), and its vectors would change
the disk and RAM figures above. This is arithmetic for planning, not a recommendation; the model choice is
recorded in [D5](DECISIONS.md#d5).

## Query-time load

Search needs no GPU. A query is one keyword encoding plus one Qdrant query, answered in about 27 ms when the index
is cached and about 240 ms on first touch (measured 2026-10-18). Once the semantic index is live, each search also
embeds the query text once (one short request to the embedding server). See
[API.md](API.md#performance-and-capacity) for what limits concurrency.

## Minimum hardware, by stage

These combine the figures above; they are estimates, not tested minimums.

| Stage | What it needs |
|---|---|
| Serve the keyword index | About 8 GB RAM for Qdrant and the API (the first deployment ran on 8 GB until the first build), about 70 GB of fast local disk for the store and index (250 GB once monthly builds need their headroom), 6 cores (the parse stage runs 5 workers by default) |
| First keyword build | The above plus about 300 GB of bulk storage for snapshots, and free local disk for two stores; the first build's run, 2026-10-17 15:45Z to 2026-10-18 05:12Z, took about 13.5 hours on six cores (measured; download and parse timings were not recorded separately) |
| Semantic build | About 160 GB of fast local disk, 32 GB RAM with Qdrant capped near 20G, and an embedding server with enough effective TFLOPS: about 45 hours at 29 |
| Monthly refresh | About 45 minutes of download, 1-2 hours of parse and the embedding of changed passages (estimates for the update; the full build figures are measured) |
