# HTTP API

**Audience:** whoever writes a client for the mirror's HTTP API. For MCP clients (Claude Code and others) see
[MCP.md](MCP.md); both doors run the same search code, so a query ranks identically through either.

Everything here is read from the code (`src/wiki_mirror/api.py`, `search.py`, `keys.py`, `config.py`) and
checked by running the real application against a test index. Example content is made up. **The service publishes
no OpenAPI document** (`/openapi.json`, `/docs` and `/redoc` are disabled and answer 401/404 like any unknown
path), so this page is the complete contract.

The service listens on `http://<mirror-host>:8300` (`listen_port`, default 8300), plain HTTP, on the LAN. Every
request needs a key.

| Method and path | Purpose |
|---|---|
| `POST /v1/wiki/search` | Keyword search, one result per article |
| `GET /v1/wiki/article` | One window of one article's text, by page id or title |
| `GET /v1/wiki/health` | Snapshot, counts, build state, last update, and whether to trust the service |
| `POST /mcp` | The MCP server ([MCP.md](MCP.md)) |

## Authentication

```
Authorization: Bearer <key>
```

- **Every route needs a key**, including `/v1/wiki/health`, `/mcp` and paths that do not exist, so an
  unauthenticated caller cannot learn which paths exist. Authentication runs before request validation.
- A missing, wrong or revoked key gets `401`, the header `WWW-Authenticate: Bearer`, and
  `{"error": "unauthorized", "detail": "invalid service token"}`.
- The scheme word is case-insensitive; any scheme other than `Bearer` is refused.
- Only HTTP is served. A websocket upgrade is refused.
- There is no TLS. The key travels in the clear, so keep the service on a trusted network or terminate TLS in a
  proxy in front of it.

## Request IDs

Send `X-Request-ID: <id>` to correlate your logs with the service's. It is accepted if it matches
`^[A-Za-z0-9._:-]{1,64}$` (letters, digits, `.`, `_`, `:` and `-`; 1 to 64 characters). Anything else, including
an empty value, is replaced by a generated 32-character hex id. Either way the id is echoed in the `X-Request-ID`
response header (on errors too) and appears in every log line for the request, together with the client's name.

## `POST /v1/wiki/search`

Keyword search over the passage index, one result per article, best first.

Request body (JSON; `Content-Type: application/json`; unknown fields are rejected with 422):

| Field | Type | Bounds | Default | Meaning |
|---|---|---|---|---|
| `query` | string | 1 to 500 characters | required | Keywords or a question. Stop-words are dropped, so distinctive names and phrases work better than whole sentences. |
| `limit` | integer | 1 to 20 | 8 | Most articles to return. |

Ranking:

- An article whose **title or redirect alias equals the whole query** comes first regardless of score
  (`Einstein` finds *Albert Einstein*).
- Then articles by their best passage's score, then **disambiguation pages** by score.
- A query with no searchable term (`the`, `???`) returns an empty `results` array with status 200.
- The array may hold fewer than `limit` entries: only the best `limit x 4` passages are fetched, and several
  passages of one long article can fill them.
- A hit whose passage was cut from different text than the stored article is dropped (this only matters during an
  index update; it is logged with the page id).

Reply `200`:

```json
{
  "snapshot": "2026-10-01",
  "mode": "lexical",
  "results": [
    {
      "page_id": 98765,
      "revision_id": 1234567001,
      "title": "Zeppelin",
      "section": "History",
      "snippet": "A Zeppelin is a type of rigid airship named after the German inventor ...",
      "score": 18.42,
      "url": "https://en.wikipedia.org/wiki/Zeppelin?oldid=1234567001",
      "disambiguation": false
    }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `snapshot` | string | Date of the snapshot served, `YYYY-MM-DD`: how old the answer can be (up to about a month, longer if monthly builds are failing). |
| `mode` | string | The ranking mode that answered: `lexical` (keyword) today. The semantic build ([ROADMAP.md](ROADMAP.md)) will add `hybrid`. Read it rather than assume. |
| `results[].page_id` | integer | Wikipedia page id. Pass it to the article route. |
| `results[].revision_id` | integer | The revision the mirror holds. |
| `results[].title` | string | Display title. |
| `results[].section` | string | Section path of the matching passage, empty before the first heading. |
| `results[].snippet` | string | Up to 320 characters of the matching passage, whitespace collapsed, cut at a word boundary with `…`. It is the start of the passage, so it does not always contain the matched words. |
| `results[].score` | number | The passage's keyword (BM25-style) score; `0.0` for a title match that no passage hit. Comparable within one reply only. |
| `results[].url` | string | Permanent link to the exact revision. What an answer cites. |
| `results[].disambiguation` | boolean | True for a "may refer to" page. |

```bash
curl -s -X POST http://<mirror-host>:8300/v1/wiki/search \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"query": "zeppelin rigid airship", "limit": 3}'
```

```python
import os
import httpx

BASE = "http://<mirror-host>:8300"
HEADERS = {"Authorization": f"Bearer {os.environ['WIKI_MIRROR_KEY']}", "X-Request-ID": "demo-1"}

with httpx.Client(base_url=BASE, headers=HEADERS, timeout=30) as http:
    reply = http.post("/v1/wiki/search", json={"query": "zeppelin rigid airship", "limit": 3})
    reply.raise_for_status()
    for hit in reply.json()["results"]:
        print(hit["title"], hit["url"])
```

## `GET /v1/wiki/article`

One window of one article's text.

Query parameters:

| Parameter | Type | Bounds | Default | Meaning |
|---|---|---|---|---|
| `page_id` | integer | 0 to 2^63-1 | none | The article's page id. |
| `title` | string | 1 to 300 characters | none | The article's title. Matched loosely (underscores for spaces, any case after the first letter); a redirect resolves to its target. |
| `offset` | integer | at least 0 | 0 | Characters into the text where the window starts. |

Give **exactly one** of `page_id` and `title`; neither or both is `422 invalid_request`
(`give exactly one of page_id or title`).

Windows are at most `max_read_chars` characters (12,000 by default; the operator can set 1,000 to 50,000). A window
ends at a blank line, or else at a line break, if one falls in its last 15%; otherwise at the limit. Windows do not
overlap. An `offset` at or beyond the end returns an empty `text` and `next_offset: null`, not an error.

Reply `200`:

```json
{
  "page_id": 98765,
  "revision_id": 1234567001,
  "title": "Zeppelin",
  "snapshot": "2026-10-01",
  "revision_time": "2026-09-30T12:00:00Z",
  "url": "https://en.wikipedia.org/wiki/Zeppelin?oldid=1234567001",
  "license": "CC BY-SA 4.0",
  "offset": 0,
  "next_offset": 11874,
  "total_chars": 48210,
  "text": "A Zeppelin is a type of rigid airship ..."
}
```

| Field | Type | Meaning |
|---|---|---|
| `page_id`, `revision_id`, `title` | integer, integer, string | Identity of the article and the revision held. |
| `snapshot` | string | Snapshot date, `YYYY-MM-DD`. |
| `revision_time` | string | When that revision was made, ISO 8601 UTC. |
| `url` | string | Permanent link to the exact revision. |
| `license` | string | Licence line, normally `CC BY-SA 4.0`. |
| `offset` | integer | Where this window starts. |
| `next_offset` | integer or null | Where the next window starts; `null` when this window reaches the end. |
| `total_chars` | integer | Length of the full text in characters. |
| `text` | string | The window: plain text with section headings; infobox fields as short key-value lines; reference lists, navigation boxes and "See also" link farms are not included. |

To read a whole article, repeat the call with `offset` set to `next_offset` until it is `null`:

```bash
curl -s -H "Authorization: Bearer $KEY" \
  'http://<mirror-host>:8300/v1/wiki/article?page_id=98765&offset=0'
curl -s -G -H "Authorization: Bearer $KEY" \
  --data-urlencode 'title=Albert Einstein' http://<mirror-host>:8300/v1/wiki/article
```

```python
def read_article(http: httpx.Client, page_id: int) -> str:
    """Return the full text of an article by following next_offset."""
    parts, offset = [], 0
    while offset is not None:
        window = http.get("/v1/wiki/article", params={"page_id": page_id, "offset": offset})
        window.raise_for_status()
        body = window.json()
        parts.append(body["text"])
        offset = body["next_offset"]
    return "".join(parts)
```

`404 not_found` when no article matches.

## `GET /v1/wiki/health`

No parameters. Whether the service can be trusted, and what it is serving.

```json
{
  "status": "ok",
  "reasons": [],
  "snapshot": "2026-10-01",
  "applied_snapshot": "20261001",
  "articles": 7245149,
  "passages": 41593444,
  "collection": "wikipedia_lexical_20261001",
  "build": {
    "snapshot_date": "2026-10-01", "phase": "idle", "chunks_total": 435, "chunks_downloaded": 435,
    "chunks_parsed": 435, "passages_indexed": 41590915, "articles": 7245149, "error": "",
    "updated_at": "2026-10-18T05:12:00+00:00"
  },
  "update": {
    "at": "2026-10-19T07:21:03Z", "from": "abc1234", "to": "def5678",
    "outcome": "updated", "detail": "", "unattended": true
  },
  "semantic": {
    "collection": "wikipedia_semantic_20261001", "status": "green", "optimizer_status": "ok",
    "points": 41590915, "indexed_vectors": 41590915, "segments": 85, "complete": true
  },
  "maintenance": {
    "ran_at": "2026-10-23T18:30:04Z",
    "qdrant": {
      "found": true, "active": true, "restarts": 0, "invocation_id": "0b1c2d3e4f5a47688f9a0b1c2d3e4f5a",
      "started_at": "2026-10-23T07:12:24Z", "last_crash_at": null,
      "anon_bytes": 7000000000, "memory_max_bytes": 14000000000
    },
    "collections": [
      {
        "name": "wikipedia_semantic_20261001", "alias": "wikipedia_semantic", "status": "green",
        "optimizer_status": "ok", "points": 41590915, "indexed_vectors": 41590915, "segments": 85,
        "held": null
      }
    ],
    "actions": [],
    "last_action": {
      "at": "2026-10-23T07:30:05Z", "collection": "wikipedia_semantic_20261001", "action": "resumed",
      "detail": "status was grey (optimizations pending, not started); resumed them"
    },
    "problems": [],
    "schema": 2
  }
}
```

The numbers in the `semantic` and `maintenance` blocks are an illustration of the shape, not a measurement.

| Field | Type | Meaning |
|---|---|---|
| `status` | string | `ok`, `degraded`, `building` or `empty`; see below. |
| `reasons` | array of strings | Why `status` is `degraded`, one short plain sentence each; empty for every other status. The log names each cause too. |
| `snapshot` | string or null | Date of the snapshot served; `null` when none is. |
| `applied_snapshot` | string or null | Tag (`YYYYMMDD`) of the snapshot the index reflects. |
| `articles` | integer | Articles in the open store (0 when none). |
| `passages` | integer | Passages in the served collection: Qdrant's *approximate* count (41,593,444 against an exact 41,590,915). 0 when none. |
| `collection` | string or null | The Qdrant collection served. |
| `build` | object | The last or current build: `snapshot_date`, `phase` (`idle`, `downloading`, `parsing`, `indexing`, `serving`, `failed`), chunk counts, `passages_indexed`, `articles`, `error` (short, no secrets; empty unless failed), `updated_at`. If the state file is unreadable: `{"phase": "unknown", "error": "..."}`. |
| `update` | object or null | The last code update: `at`, `from`, `to` (commit ids; `to` may be null), `outcome` (`updated`, `current`, `deferred`, `held`, `failed`, `rolled_back`), `detail`, `unattended`. `null` if none recorded. Reporting only; it never changes `status`. |
| `semantic` | object or null | The collection behind the `wikipedia_semantic` alias, asked from Qdrant on every call: `collection`, `status` (Qdrant's: `green`, `yellow` while it builds the search graph, `grey` when its optimizations are paused, `red` when the optimizer failed), `optimizer_status` (`ok`, or `error: <Qdrant's text>`, cut to 300 characters), `points`, `indexed_vectors`, `segments`, and `complete` (whether its embedding run finished, from the run's state file). `null` when there is no such alias or Qdrant could not be asked (when the passage count failed, the lookup is skipped and `semantic` is `null` for that poll). Reporting only; it never changes `status`, because nothing searches this collection yet. |
| `maintenance` | object or null | The maintenance job's last record, as written ([OPERATIONS.md](OPERATIONS.md#the-maintenance-job)): `ran_at` (UTC), `qdrant` (the unit: `found`, `active`, `restarts` after a failure, `invocation_id`, `started_at`, `last_crash_at`, `anon_bytes` and `memory_max_bytes` of its cgroup; `found` is `true` when the unit exists, `false` when it is not installed and `null` when it could not be read), `collections` (each mirror collection as the job saw it: its status, counts and `held`, the reason the job is not touching it, or `null`; the list is empty when Qdrant did not answer, and a collection Qdrant would not describe appears only in `problems`; nothing is carried over from the previous record), `actions` (what this run did), `last_action` (the latest of any run), `problems` (what it saw and could not fix, one sentence each) and `schema`. `null` when there is no record that can be trusted, which is the normal state before the timer is first enabled. Its age, a recent Qdrant crash and Qdrant's memory can make `status` `degraded`, see below. |

**`status` values.**

| Value | Meaning | Search and reads |
|---|---|---|
| `ok` | Serving a snapshot, Qdrant answering, nothing wrong | Work |
| `building` | Nothing served yet and a first build is running (`phase` is downloading, parsing or indexing) | `503 not_ready` |
| `empty` | Nothing served and no build running (a fresh install) | `503 not_ready` |
| `degraded` | Something is wrong; the old snapshot usually keeps serving. Causes: Qdrant did not answer; the served snapshot is more than **40 days old** (the monthly build is failing); the last build `failed`; the index was cut by a different chunker than this code's; the collection has no usable index state; the article store behind it cannot be opened; or one of the six causes the maintenance record adds, below | Usually work, possibly stale |

The causes of `degraded` are in `reasons`, and in the service log (once when each starts, once when it clears).

**The six causes the maintenance record adds.** They turn a serving mirror (`ok`) `degraded`; they do not
override `building` or `empty`, which say more. Having no record at all is not a cause.

| Cause | Threshold | `reasons` sentence |
|---|---|---|
| The job has stopped | The record's `ran_at` is more than **60 minutes** old (four missed runs of the 15-minute timer: the timer is off or the job is failing). A `ran_at` that cannot be read counts as stale. | `the maintenance job last ran at <time>, N minutes ago; its timer or the job has stopped` (or `the maintenance job's last run time is unreadable`) |
| Qdrant was killed or crashed | systemd restarted Qdrant after a failure within the last **24 hours** (`qdrant.last_crash_at`). A start by hand is not a failure. Two blind spots: `systemctl reset-failed`, and a restart by hand during systemd's wait before an automatic restart. The crash-loop guard does not depend on this field. | `Qdrant was restarted after a failure at <time>` |
| Qdrant is near its cap | Its anonymous memory is above **90%** of its cap (`anon_bytes` against `memory_max_bytes`). | `Qdrant holds N% of its memory cap and is close to being killed` |
| A collection is held | The job found a `grey` collection it will not resume (the crash-loop guard, [OPERATIONS.md](OPERATIONS.md#the-crash-loop-guard)): systemd restarted Qdrant after a failure 2 or more times since it was last started by hand, the count could not be read, memory is above 90% of the cap, or the disk is under its floor. The collection's `held` is set. | `<name> is held by the maintenance job: <held>` |
| Qdrant's unit could not be read | `qdrant.found` is `null`: `systemctl` is missing, slow or failing, so crashes and memory are not watched. | `the maintenance job could not read Qdrant's unit, so crashes and memory are not watched` |
| Qdrant's unit is not installed | `qdrant.found` is `false`. | `Qdrant's unit is not installed, so the maintenance job does not watch crashes and memory` |

The other `reasons` sentences are `Qdrant could not be asked`, `serving has a problem: <summary>`, `the alias moved
and serving has not caught up with it yet`, `the last build failed`, `the snapshot served is N days old, past the
40-day limit` and `the date of the snapshot served is unreadable`.

**What to alert on.** Poll `/v1/wiki/health` every minute or so with a key. Alert when:

- the request fails or times out, or returns 5xx (service down);
- `status` is `degraded` for more than a few minutes, or `empty`/`building` when it should be serving;
- `build.phase` is `failed` (read `build.error`);
- `update.outcome` is `failed` or `rolled_back`; `held` and `deferred` are not faults, but a `held` that persists
  means a person has a decision to make ([OPERATIONS.md](OPERATIONS.md#the-nightly-update));
- `snapshot` is older than about 35 days (the service itself turns `degraded` at 40);
- `semantic.status` is `grey` or `red` (paused or failed; the maintenance job resumes a paused one within 15
  minutes), or `maintenance.problems` is not empty.

## Errors

Every error is JSON, `{"error": "<code>", "detail": "<short sentence>"}`, and never carries the offending input
(a query can be personal).

| Status | `error` | When |
|---|---|---|
| 401 | `unauthorized` | Missing, wrong or revoked key. Also sets `WWW-Authenticate: Bearer`. |
| 404 | `not_found` | No such route (`detail`: `no such route`), or the article route found no article (`no article matches`). |
| 405 | `method_not_allowed` | Wrong method for the route. |
| 422 | `invalid_request` | A parameter or body field broke a bound, an unknown body field was sent, or both or neither of `page_id` and `title` were given. `detail` names the fields and rules, never the values, e.g. `limit: Input should be less than or equal to 20`. |
| 500 | `internal` | An unexpected error; the log has the traceback under your request id. |
| 503 | `not_ready` | No snapshot is served yet. The service notices a finished first build by itself within 30 seconds. |
| 503 | `index_unavailable` | Qdrant refused the query or could not be reached. Search only. |

Every route is read-only, so retrying a `503` after a few seconds is safe.

## Client keys

There are two kinds of key.

- **The primary key** is the environment variable `WIKI_MIRROR_API_KEY`, set from
  `/etc/wiki-mirror/mirror.env` (root, 0600) and read by the service at start. At least 32 characters;
  the installer generates one. It authenticates as the client named **`primary`**, which is
  reserved. Rotating it means editing that file and restarting the API.
- **Every other client has its own key**, so the log names the caller and one key can be withdrawn alone. On the
  host, as root:

  ```bash
  wiki-mirror key add my-agent      # prints the key once; only its SHA-256 hash is stored
  wiki-mirror key list              # names only, never keys or hashes
  wiki-mirror key revoke my-agent   # stops working within about five seconds
  ```

  A client name is 1 to 32 characters: a lowercase letter, then lowercase letters, digits or `-`. A lost key
  cannot be shown again; revoke the name and add it again. A name can only be added once at a time.

**How keys are stored.** Only SHA-256 hashes, in `/etc/wiki-mirror/clients.json`
(`{"<name>": "<hex>"}`, root:wikimirror 0640), so the API user can check a key but cannot learn one. A plain unsalted
hash is adequate because a key is 48 random bytes (`secrets.token_urlsafe(48)`, 288 bits): there is nothing to
guess. On each request the presented key's hash is compared in constant time against every stored hash without
stopping at the first match, so timing does not reveal which client matched. The file is re-read when it changes
(checked every 5 seconds), so no restart is needed; a malformed edit while running is ignored (the last good list
is kept and an error is logged), and at start it is a hard failure. Only `wiki-mirror key` should write the file.

Keys are credentials even though the data is public: keep them out of repositories, logs and model context. In a
client, load them from the environment or a 0600 file.

## Limits and logging

**Rate limiting: none.** The code applies no per-client or global rate limit, quota or throttle. The only limits
are the request bounds above. A client that wants to be polite should limit itself; a misbehaving client can be
cut off with `wiki-mirror key revoke`.

**What is logged per request** (standard `logging`, to the journal; the uvicorn access log is **off** because it
would print query strings):

| Event | Fields |
|---|---|
| Search | request id, client name, `query_chars` (length only), result count |
| Article read | request id, client name, page id, offset |
| MCP tool call | tool name, client name, request id, `query_chars` or page id, result count |
| Unauthorized | request id only |
| Unhandled error | request id, client name, traceback |

**Query text, titles asked for, article text, keys and key hashes are never logged.** Errors name fields and
rules, not values.

## Performance and capacity

Measured from a client on the LAN over the API on 2026-10-18, right after the container moved to NVMe (20 searches
and 10 article reads, 41.6M-passage keyword index):

| Operation | p50 | p90 |
|---|---|---|
| Search, first touch (segments read from disk) | 241 ms | 483 ms |
| Search, once cached | 27 ms | 35 ms |
| Article read | 1-2 ms | |

Test searches through `wiki-mirror search` on the first day answered in 0.2 to 0.6 s.

What limits concurrency, from the code and configuration:

- **One API process.** `uvicorn.run` starts a single worker process. The REST routes are plain (synchronous)
  functions, which FastAPI runs in its thread pool (AnyIO's default of 40 threads), so up to that many requests
  proceed at once. There is no tuning knob for this in the service's settings.
- **Qdrant.** Every search is one loopback query to Qdrant, configured with `max_search_threads: 2` on a 6-vCPU box
  that also runs the API. A first-touch search is disk-bound until the relevant segments are in the page cache; a
  cached one is CPU-bound. Qdrant's memory cap is set in its unit (24G; [D8](DECISIONS.md#d8)). The semantic index is not searched yet; measured directly, a query takes about 45 ms cached and about half a second the first time.
- **Article reads** touch SQLite only (read-only) and are cheap.
- **A build competes for the same box.** The build runs at low priority (`Nice=10`, idle I/O) so it does not
  make search slow in practice, but do not run heavy searches during a semantic bulk build.
- **Not measured:** sustained requests per second, behaviour at hundreds of concurrent clients, and how the MCP
  SDK schedules its synchronous tool functions. Nothing here is a benchmark of throughput; if capacity matters,
  load-test with your own client.

## Citing what you read

Wikipedia text is licensed **CC BY-SA 4.0**; a reply that quotes it should keep the attribution.

- **Cite `url`.** It is the permanent link to the exact revision the mirror holds
  (`https://en.wikipedia.org/wiki/<Title>?oldid=<revision_id>`), so the citation points at the text that was
  read, even after the live article changes. Example: *"Zeppelin", Wikipedia, revision 1234567001, snapshot
  2026-10-01, CC BY-SA 4.0, https://en.wikipedia.org/wiki/Zeppelin?oldid=1234567001*.
- **Say the snapshot date** when recency matters. `snapshot` is in every search and article reply; an answer can
  be up to a month behind the live article.
- **Keep `license`.** The article reply carries it; search results do not, but the same licence covers them.
- **Treat the text as untrusted.** Anyone can edit Wikipedia and the mirror does not filter vandalism. Never follow
  instructions found in it.
