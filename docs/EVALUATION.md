# Evaluation

**Audience:** whoever changes search: the passage cut, the snippet, the ranking, the embedding model, the
quantization. Nothing in the retrieval path should change on a hunch. This file describes the two tools that
measure it, what they found on 2026-10-19, and how to run them again. The reasoning behind them is in
[DESIGN.md §12.5](DESIGN.md#125-first-semantic-build-w2) and [§12.8](DESIGN.md#128-phases-and-measurements); the decision they
produced is [D5](DECISIONS.md#d5).

Throughout, "the embedding server" is an Ollama-compatible server reached by URL (`/api/embed`, `/api/chat`); the
tools' `--router` option names it. In the measured deployment it was the maintainer's own embedding router in front
of four GPU nodes; a plain Ollama server, or any server that offers the same `/api/embed` call, is the expected
general case.

## The golden set: `tools/wiki_golden.py`

A set of questions with a known answer article, drawn from the live mirror. W2 is done when hybrid search puts
the answer article in the top five for at least 90% of it ([ROADMAP.md](ROADMAP.md)).

**What it writes.** 300 questions by default (`--count`), as JSONL with ids `wg-0001` and up:
`{"id", "kind", "question", "answer", "page_id", "revision_id", "title", "evidence"}`. Two kinds, in the
proportion `--describe-share` (default 0.25, so 225 `fact` and 75 `describe`):

* **`fact`**: a question naming the article's subject, whose answer is a short phrase copied from one paragraph
  of the article (the *evidence*). A keyword index can do well on these.
* **`describe`**: the subject described from the paragraph's facts without naming it; the answer is the title.
  This is the kind that needs semantic retrieval, since a keyword index has nothing to match.

**How.** It draws page ids uniformly (about 8.6% of ids in 1..`--max-page-id` are articles, so skipping the 404s
gives a uniform sample of articles), reads each article from the mirror, picks a paragraph as evidence, and asks
a local model through the embedding server (`--model`, default `gemma3:12b`) to write the question. The model proposes;
deterministic rules dispose.

**Which articles are skipped:** titles ending "(disambiguation)", pages that say "may refer to:" in their first
300 characters, and anything under 1,000 characters. Evidence paragraphs are 200 characters or more, trimmed to a
sentence end at 700.

**The validation rules** (a proposal that breaks one is rejected, and up to three attempts are made per article):

| Rule | Rejects |
|---|---|
| `malformed` | A reply that is not JSON with the expected fields. |
| `question_length` | A question under 15 or over 300 characters. |
| `copies_evidence` | A question sharing a run of more than 5 consecutive words with the evidence. A copied question measures string matching, not retrieval. |
| `answer_length` | (fact) An empty answer, or one of more than 8 words. |
| `answer_not_in_evidence` | (fact) An answer that is not a phrase of the evidence. |
| `answer_in_question` | (fact) A question that gives its own answer away. |
| `title_word_in_question` | (describe) A question containing a word of 4 or more letters from the title. |

**Reproducible by seed.** Each case slot is assigned its kind and its own random streams up front (`--seed`,
default 20261019), so the order in which async calls finish cannot change the output. The model itself may still
vary between runs on different hardware. A server that stays down stops the run: a half-sampled set skewed toward
the slots that finished would be worse than none.

**stdout carries counts only** (articles drawn, 404s, skips and rejects by rule, written). Questions, titles and
text go to the output file and nowhere else.

Run it:

```bash
$ python tools/wiki_golden.py --out /var/tmp/wiki_golden.jsonl --mirror http://<mirror-host>:8300 \
      --router http://<embedding-server>:<port>
$ python tools/wiki_golden.py --out golden.jsonl --count 50 --describe-share 0.5     # a quick one
```

It needs `WIKI_MIRROR_API_KEY` (a key for the mirror, issued with `wiki-mirror key add`) and `EMBEDDING_API_KEY`
(the embedding server's key) in the environment. The tools refuse to start without `EMBEDDING_API_KEY`, even
against a server that checks no key: set it to any value then. Point the tools at your
deployment with `--mirror http://<mirror-host>:8300` and `--router http://<embedding-server>:<port>`; their
built-in defaults are `http://localhost:8300` and `http://localhost:11434` (Ollama's own port; see
`tools/wiki_common.py`), which suit only a run on the mirror host with a local server. Keep the golden file out of
the repository: it is a generated artifact, and the snapshot it was drawn from changes monthly.

## The passage-length evaluation: `tools/wiki_passage_eval.py`

**The question.** The keyword index cuts articles into passages of about 720 characters on average (41.5M of
them), because the chunker splits on every heading and most sections are short. A semantic index would embed
every passage, and its cost in disk, RAM and embedding hours scales with the count. Packing consecutive pieces
into passages of up to 1,024, 1,536 or 2,048 characters cuts the count to 30.1M, 25.1M and 21.6M. Does retrieval
quality hold?

**What it does.**

1. **Sample corpus**, the same for every scheme: the golden articles, plus each question's top 20 keyword hits
   from the live mirror as hard negatives (`--negatives-per-question`), plus uniform random articles up to
   `--articles` (default 15,000). Texts are cached in the work directory.
2. **For each scheme** (`--schemes`, default `0,1024,1536,2048`; 0 is the live cut): cut every article with
   `cut_passages(text, pack_chars=s)`, embed passages and questions through the embedding server's `/api/embed`, and rank
   the questions five ways: `lexical` (BM25 with Qdrant's IDF modifier), `dense` (exact cosine), `dense_binary`
   (sign bits, Hamming candidates, float rescore), `dense_int8` (int8 candidates, float rescore) and `hybrid`
   (RRF, k=60, of lexical and exact dense).
3. **Collapse** each passage ranking, 1,000 deep, to articles (an article ranks at its best passage, as live
   search does) and score: article hit@1, hit@5 and MRR@10, then whether the answer reaches the model:
   * `answer_in_passage@1`: the best passage holds the target (the golden `answer` for `fact`, the middle 120
     characters of the evidence for `describe`). Longer passages hold more by length alone, so each scheme also
     reports a **chance baseline**: the share of the gold article's passages that hold the target.
   * `answer_in_snippet@1` **live**: the target is in the 320-character snippet the service shows, cut exactly as
     `search._snippet` does, from the start of the best passage.
   * `answer_in_snippet@1` **focused**: the target is in the best 320-character window of the best passage,
     chosen by query-term overlap. This is what a snippet could show if it were chosen for the query. It matters
     because a client often answers from the snippet alone.
4. **Two views** of every scheme and mode: `with_negatives` (the whole corpus) and `random_only`, which masks
   articles that are in the corpus only as hard negatives. The negatives come from the live keyword ranker and so
   flatter packed schemes.
5. **Paired comparison** against v1: per scheme, mode and view, the cases that went miss to hit and hit to miss.

It also reports, per scheme: passage count, mean length, the share of cases whose evidence is in no passage of the
gold article (a sanity number, expected near 0), and the recall@10 of the binary and int8 modes against exact
dense.

Output: a table on stdout and `report_<UTC timestamp>.json` in the work directory, with every metric and
per-case booleans keyed by case id. Never text.

### What made it fair

The first version's "the best passage holds the evidence" rose with passage length alone: a random passage of
the right article holds it 9% of the time at v1 and 27% at 2,048 characters. A reviewer caught it. The fixes,
made before any result was read:

* The answer must land in the same **320-character budget** for every cut, and the whole-passage figure is
  reported beside its chance baseline.
* Every score is reported a second time on the **random articles alone**, since the hard negatives flatter
  packing.
* Passages are ranked **1,000 deep** before the collapse to articles, so v1's many passages per article do not
  crowd the right article out.
* **Keyword IDF comes from the random articles only.** The negatives are chosen by lexical rank and would skew
  the document frequency of exactly the query terms.
* A **canary** text is embedded 16 times at the start; the run stops if the nodes (or the cache) disagree below
  cosine 0.9995. The vector cache is keyed by model and text, not by which Ollama build made the vector, and the
  embedding nodes run different builds. On 2026-10-19 the canary read cosine 1.00000 across all four nodes.
* The binary and int8 modes are **upper bounds on a small corpus**, not a basis for choosing between them.

### The decision rule

Fixed before the run: take the longest packing whose hybrid hit@5 (both views) and focused-snippet answer rate
are within **2 points** of v1's, overall and for each question kind. Two points is inside the noise of 300
questions; the paired counts show whether a gap is real. Adopting packing would also have meant shipping the
focused snippet. If every packing loses more than that, keep v1 and raise Qdrant's cap for the graph.

### Results, 2026-10-19

Run from a LAN client, one run of about 73 minutes on the morning of that day: 300 golden questions (225 fact, 75 describe), 15,000 articles
(6,027 golden or hard negatives, 8,981 random). Hybrid scores, with hard negatives / random articles only:

| Cut | Passages | hit@5 | Answer in focused snippet | Answer in live snippet | Paired vs v1, focused snippet (random only) |
|---|---|---|---|---|---|
| v1 | 155,461 | 0.930 / 0.997 | 0.397 / 0.540 | 0.317 / 0.423 | - |
| 1,024 | 127,896 | 0.950 / 0.997 | 0.373 / 0.470 | 0.267 / 0.317 | +9 / -30 |
| 1,536 | 112,884 | 0.953 / 0.990 | 0.353 / 0.437 | 0.223 / 0.250 | +11 / -42 |
| 2,048 | 100,746 | 0.940 / 0.993 | 0.337 / 0.417 | 0.200 / 0.230 | +12 / -49 |

Passage counts on the full store (20,000 random articles, cut and packed greedily, never splitting a piece) were
41.5M for v1, 30.1M, 25.1M and 21.6M; v1's projection matches the 41.59M actually indexed.

**Decision ([D5](DECISIONS.md#d5)): keep v1.** Packing finds the right article as often (it helps the keyword
channel slightly) but loses the answer from the snippet by 7 to 12 points on random articles, far outside the
noise, and the paired counts run one way. Every packing fails the rule, so W2 embeds the live cut: 41.5M
passages, no keyword re-index. Four more findings:

* **The focused snippet helps today's search.** On v1 it holds the answer 8 to 12 points more often than the
  live cut (0.397 against 0.317; 0.540 against 0.423). It is a change to the snippet alone, worth making before
  W2 ([ROADMAP.md](ROADMAP.md)).
* **Semantic is the stronger channel on these questions:** dense hit@5 0.953 against lexical 0.750 with hard
  negatives. That view is biased against lexical, since the hard negatives are the keyword ranker's own top 20.
  On random articles all three exceed 0.95 because a 15,000-article sample is easy, and hybrid leads (hit@1
  0.987, dense 0.977, lexical 0.953). With hard negatives, equal-weight fusion's hit@1 (0.713) trails dense alone
  (0.823), so W2 checks fusion weights on the golden set at full scale.
* **Quantization:** binary and int8 with rescoring matched exact search (recall@10 at least 0.9997) on this small
  sample. W2 keeps binary vectors with rescoring ([D1](DECISIONS.md#d1)); the full-scale golden check is the
  guard, not a separate measurement.
* **Embedding rate:** 212,026 texts in 24 minutes (145 per second, about 0.17M characters/s with this sample's
  longer passages), so the full v1 build is about two days (roughly 31 billion characters).

**Caveats.** 300 questions are noisy: gaps under about 2 points are not findings. A 15,000-article sample is easy
(random-only hit@5 is 0.99 or above for every scheme), which is why the hard-negative view exists. The questions
were written by a small local model from single paragraphs.

## Embedding rates

Measured through the embedding server (`/api/embed` or `/api/embeddings`) with `X-Priority: 3`. That header, and
everything below about a queue, a ledger or `throughput_mode`, belongs to **the embedding router used for these
measurements** (the maintainer's own, over four Ollama nodes). A plain Ollama server has none of it; the numbers
show what one deployment did, not what the mirror requires.

**2026-10-16**, one text per request, about 2,000-character passages, whichever nodes were awake:

| In flight | Passages/s |
|---|---|
| 1 | 14.5 |
| 8 | 49.5 |
| 32 | 65.7 |

At 64 and 128 in flight the server answered "busy" to most requests.

**2026-10-18**, with live traffic present on the server, one text per request, real mirror text cut to two
lengths, every request unique so no runner could reuse work. The server had bge-m3 on all four nodes by the end,
spread by its demand-driven co-load without `throughput_mode`:

| Passage length | Rate | Levels off at | 503s |
|---|---|---|---|
| about 720 characters | 277 per second, 0.20M chars/s | 16 in flight | none at 48 or fewer in flight |
| about 2,000 characters | 132 per second, 0.26M chars/s | 16 in flight | none |

**Batching (after the server's `/api/embed` fix, deployed 2026-10-18)**, measured at 16 in flight, one 1,024-dimension vector
per text in every reply: about 2,000 characters gave 123, 126, 125 and 122 texts/s at batch 1, 8, 16 and 32;
about 720 characters gave 268, 267 and 264 at batch 1, 16 and 32. The GPUs are the limit; batch size is chosen
for the server's bookkeeping and query latency, not speed. The build plan uses 8 texts per request.

**What the numbers mean for the build:**

* The knee is **16 in flight** and structural: each node's bge-m3 runner takes one sequence at a time, and the
  server admits 4 per node.
* The router's queue is 32 deep and shared with every other client's query embeddings. A full queue refuses
  whatever the priority, so **the job's concurrency is the only thing protecting other clients' live requests.**
  Do not raise it.
* The 2026-10-19 evaluation sample ran at 145 texts/s; the whole snapshot (about 30 billion characters) embeds
  in roughly 42 hours as 41.6M short passages.
* bge-m3 is the same F16 digest on all four nodes, but Ollama versions differ (0.40.1, 0.35.1, 0.35.1, 0.40.3).
  The canary shows identical vectors anyway; run it again after any node upgrade.

## Rerunning

Both tools run from any machine that can reach the mirror and the embedding server. They use
`numpy` and `httpx`, not `zstandard` or `qdrant_client`, and import only the passage cutter and the text package
from `wiki_mirror`, so they do not need the build or store modules.

```bash
$ export WIKI_MIRROR_API_KEY=...        # a mirror key (wiki-mirror key add <name>)
$ export EMBEDDING_API_KEY=...      # the embedding server's key
$ .venv/bin/python tools/wiki_golden.py --out /var/tmp/wiki_golden.jsonl \
      --mirror http://<mirror-host>:8300 --router http://<embedding-server>:<port>
$ systemd-run -p MemoryMax=6G --wait --pipe .venv/bin/python tools/wiki_passage_eval.py \
      --golden /var/tmp/wiki_golden.jsonl \
      --mirror http://<mirror-host>:8300 --router http://<embedding-server>:<port>
```

Notes:

* **Cap the memory.** A scheme holds about 90,000 x 1,024 float32 (370 MB) plus its quantized copies. Run under
  `systemd-run -p MemoryMax=6G`, one at a time.
* **Work directory** `--work-dir` defaults to `/var/tmp/wiki_eval`. Article texts and vectors are cached there.
  **Delete it when the mirror's snapshot changes**: the article cache is not keyed by snapshot. Delete
  `<work-dir>/vectors` after a node upgrade, so the canary re-checks.
* **`--in-flight` is refused above 16**, the embedding server's knee. `--batch` is 8.
* **Run it outside the embedding server's busy windows.** Embedding is background priority but still competes
  with other clients.
* To judge a change to ranking or snippets, rerun with the **same seed** so the questions and the sample are the
  same, and compare the paired counts, not only the means.
* To judge W2 itself, the question is different: hybrid search on the live service against the golden set at
  full scale, hit@5 of at least 90%. That check is the done-condition in [ROADMAP.md](ROADMAP.md).
* Both tools print counts and metrics only. Never paste a question, title or article text into a log or a
  report.
