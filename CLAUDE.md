# CLAUDE.md - Engineering conventions for sfn-wiki-mirror

Standing guidance for all development in this repository. Read alongside [docs/DESIGN.md](docs/DESIGN.md), the design
with its measurements, and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), the map of what is built. Where this file and
the design disagree, the design wins on *what* is built; this file governs *how* it is built. Where the design and the
code disagree, the code wins, and the design gets fixed.

## Project

A self-hosted, monthly-synced English Wikipedia for AI agents and applications on a local network: an article store,
keyword and (next) semantic search over 41M passages, an MCP server and an HTTP API. It runs on its own LXC,
downloads Wikimedia Enterprise snapshots, and never touches live Wikipedia at query time. Python 3.13 + FastAPI +
Qdrant + SQLite, systemd-managed.

Reading order: [README](README.md) -> [ARCHITECTURE](docs/ARCHITECTURE.md) -> [MCP](docs/MCP.md) ->
[API](docs/API.md) -> [OPERATIONS](docs/OPERATIONS.md) -> [DESIGN](docs/DESIGN.md) -> [ROADMAP](docs/ROADMAP.md) (where the build is now) ->
[EVALUATION](docs/EVALUATION.md) -> [DECISIONS](docs/DECISIONS.md).

---

## Constraints specific to this repository

Read these before any change. They are the ways this repository can lose data or break a running box.

1. **The chunker and the keyword encoder must stay byte-identical** unless a rebuild is planned. They live in the
   vendored `src/wiki_mirror/text/` package (a vendored copy of an earlier library). Every passage in the
   41.6M-passage index was cut and encoded by them; a snippet is made by re-cutting the stored article and taking
   chunk `n`; a query is encoded with the same encoder as the documents. Change either and the index silently
   mismatches: wrong snippets, wrong citations, lost recall. Any change there also changes the chunker fingerprint
   or the index format, which means a `deploy/FORMAT` change, which holds the nightly update and needs a
   rebuild of many hours ([docs/DESIGN.md §12.7](docs/DESIGN.md#127-what-forces-a-full-rebuild)). Do not reformat,
   "clean up" or lint-fix those files without checking that the tests that pin the cut still pass unchanged.
2. **`main` ships to production.** The box updates itself from `main` every night at about 02:20 local time
   (`wiki-mirror-update.timer`, [D4](docs/DECISIONS.md#d4)). **Never push untested work to `main`.** Tests,
   ruff and mypy must pass first; work in progress goes on a branch. A broken `main` rolls the box back, then fails
   again every night, and a bad format change is held until a person decides.
3. **Format changes move together.** `deploy/FORMAT` (`store=<schema> chunker=<fingerprint>
   index=<format>`) must change in the same commit as `store._SCHEMA_VERSION`, `index.CHUNKER_VERSION` or the chunk
   sizes, or `index.INDEX_FORMAT`. A test keeps it equal to the code. The parser version is deliberately not in it.
4. **Live installs, deploys and `wiki-mirror build-now` are run by the operator**, not by an agent or CI. Do not run
   `install.sh`, `wiki-mirror update`, `wiki-mirror build-now` or anything that starts a build or restarts a service on the box.
   Prepare the change, state the command, and hand it over. Never ssh to the live box to change it unless told to.
5. **The Enterprise credentials live only on the box** (`/etc/wiki-mirror/enterprise.env`, 0600, readable
   by the build user alone). They are never in the repository, a fixture, a log, an error message or model context.
   The real snapshot is not a test fixture either; fixtures are small hand-made or sampled records
   (`tests/fixtures/wme/`).
6. **The API user has no egress and no write access to the data.** Do not add a code path that makes the serving
   process open an outbound connection or write to the store or the index. Network egress is per Unix user in
   nftables; a change to who may open what is a security change, reviewed as one. The ruleset is a template: the
   site's own addresses (LAN, embedding server) live in `/etc/wiki-mirror/site.env` and are validated by
   `deploy/install.sh` before they reach it. Never write a real address into the repository, in code, a doc or a test;
   use the documentation ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) or the guide's example values. The
   installer accepts only private networks as `LAN_CIDR`, so a test that needs one uses the shared address space
   (100.64.0.0/10), which is nobody's own network.
7. **Query text is never logged**, and neither is a key, a hash, a presigned URL or article text. Log lengths and
   counts.
8. **Qdrant is loopback only** and the article store and Qdrant are on local disk, never the NAS. Config validation
   enforces the first; do not weaken it.
9. **Bump nothing silently in `deploy/constraints.txt`.** Dependencies are pinned exactly and installed
   from wheels only; a change is its own commit with the wheel check from
   [docs/OPERATIONS.md](docs/OPERATIONS.md#refreshing-constraintstxt).

---

## The four themes

Every change is reviewed against these four before it is reported done. When two collide, usually Robust against
Elegant, state the tradeoff explicitly rather than silently picking one.

### Professional

Production discipline, not demo discipline. Typed contracts at every boundary. Config Pydantic-validated at startup,
where invalid config is a hard failure and never a warning. Structured logs carrying `request_id` end to end. Systemd
units and operations docs ship *with* the service, not after it. No secrets in the repo, the logs, or model context.
No orphan TODOs in merged code. "Done" means tests green and skipped work named.

*Violated by:* a feature merged with its error paths unwritten; a milestone reported complete with one exit criterion
quietly unmet.

### Robust

Fails closed and degrades loudly. The failure path is designed before the happy path is celebrated: checkpoints and
deterministic point ids at every effect boundary so any stage can be killed and resumed; bounded budgets on every loop
and every download; one build at a time under a lock; a format guard on the update path; a rollback when the new code
does not come up. A missing answer is an empty result or an explicit error, never a confident guess. Health says
`degraded` and the log says why.

*Violated by:* a bare `except`; a blind retry on a non-idempotent call; an infinite wait; a silent fallback that hides
a broken dependency; a hit served from text the citation does not point at.

### Intelligent

Decisions backed by measurement. The passage cut was chosen by an evaluation that was itself corrected by review
before any number was read ([docs/EVALUATION.md](docs/EVALUATION.md)); keep to that standard. Choose the boring
sufficient solution. Anticipate the second-order consequence: the half-updated index, the resume path, the nightly
update restarting a service mid-build.

*Violated by:* cleverness that costs readability; a benchmark asserted instead of run; an abstraction built for a
requirement that does not exist yet.

### Elegant

The smallest honest design that satisfies the requirement, with the seams in the right places. One search path behind
both doors (REST and MCP). One checkpoint mechanism per stage. Uniform shapes across components so operations
semantics are learned once. Deleting is a legitimate feature.

*Violated by:* a second, weaker code path for a special case; a config knob added instead of a decision made; an
abstraction layer with exactly one implementation.

---

## Engineering baseline

- Python 3.13, FastAPI, Pydantic. Libraries used narrowly; no framework owns our control flow.
- YAML config with defaults for every field, validated by Pydantic at startup (`config.py`); unknown keys and a key
  in the YAML where only the environment may hold it are hard errors.
- Every client call carries `Authorization: Bearer <key>` and may carry an `X-Request-ID` (1 to 64 characters of
  `A-Za-z0-9._:-`), echoed on the response and present in every log line of the request.
- Errors are `{"error": "<code>", "detail": "<short>"}` and never carry the offending input.
- Secrets live in 0600 env files on the box. The primary key is `WIKI_MIRROR_API_KEY` (client name `primary`); every other client's key is a
  hash in `clients.json`, written only by `wiki-mirror key`.
- RFC 2119 MUST/SHOULD/MAY are used with their standard meaning in docs and docstrings.
- `ruff` clean, `mypy` clean. Logging through the standard `logging` module with `component` / `event` /
  `request_id` in `extra`.

---

## Tooling discipline

Use the harness's purpose-built tools. A shell command in their place bypasses the review surface they exist to
provide.

| Operation | Use | Not |
|---|---|---|
| Read a file, or part of one | **Read** (with `offset`/`limit`) | `cat`, `head`, `tail`, `sed -n` |
| Search file contents | **Grep**, where the harness offers it | an ad-hoc pipeline that changes every call |
| Find files by name or pattern | **Glob**, where the harness offers it | `ls -R` dumps |
| Change a file | **Edit** / **Write** | `sed -i`, `awk`, inline Python rewrite scripts |

**Never mutate a file through the shell.** `sed -i`, an `awk` rewrite, a heredoc Python script that patches source,
and `cat >> file << 'EOF'` all land with no visible diff. Appending counts: Read the last few lines and Edit against
them. The one exception is bulk generation where the generator is the reviewable artifact (a scripted rename across
fifty files); keep the script out of the repository and say so in the report. One file is never bulk.

**Bash is for running things, not inspecting them:** test runners, linters, type checkers, `git`, package managers.

### The verification vocabulary - use these exact strings

Every distinct command string is a fresh approval decision, so copy these character for character.

```bash
.venv/bin/ruff check .
.venv/bin/ruff check --fix .
.venv/bin/mypy src tools
.venv/bin/pytest -q
.venv/bin/pytest -q -m unit
.venv/bin/pytest -q -m contract
```

The `.venv` is built with `python3 -m venv .venv` then `.venv/bin/pip install -e ".[dev]"`. If `.venv/bin/pytest` is
missing, rebuild it there; never build one in a scratch directory. Narrowing a run to one file path is normal;
rebuilding the prefix is not.

- **Never prefix a command with `cd`.** The working directory is already the repository root.
- **One command per call.** Chaining varies the string every time; three short calls beat one chain.
- **Multi-line input goes in a file**, never in the command: a commit message in a scratch file passed with
  `git commit -q -F <path>`, a probe script as a file run with `.venv/bin/python <path>`. Reuse the same scratch
  filename so the command string is familiar.

---

## Documentation and comments

Code here is read by a future engineer with no memory of the conversation that produced it, often at 23:00 with a
service down. Documentation is the handoff, not decoration.

### File header block - required on every file

Every file opens with a header block: a module docstring in Python, a `#` comment block in YAML, shell, systemd units
and nftables rules. It states, in this order:

1. **What this file is**: one or two sentences naming its single responsibility.
2. **How it fits**: who calls it, what it calls, where it sits in the pipeline.
3. **Design pointer**: the section of `docs/DESIGN.md` (or another doc) that explains it.
4. **Non-obvious constraints**: accepted tradeoffs, security posture, ordering requirements, anything that would look
   like a bug to someone who does not know why.

A header that merely restates the filename is a defect.

### Docstrings - required on every module, class and function

Google style, wrapped at the line limit in `pyproject.toml`.

- **Summary line**: what it does, so a caller can decide whether to use it without reading the body.
- **Args / Returns / Raises** whenever a caller could plausibly get it wrong. Unit choices, index bases and ownership
  of mutable arguments are stated.
- **The why**: when the code encodes a design rule, a security control or a deliberate tradeoff, say so and cite the
  section.
- **Private helpers** (`_name`) are not exempt. Pydantic models and dataclasses carry a class docstring.

### Inline comments - explain why, never what

- Every design-derived constant or threshold cites its source or measurement.
- Every deviation from the obvious approach states its reason on the spot.
- Every deliberate silence (a swallowed exception, a no-op fallback) says why it is safe.
- Commented-out code is deleted; git remembers it.
- `TODO` only with an owner and a phase: `# TODO(w2): ...`. Anything else is an orphan and blocks the commit.

### Enforcement

`ruff` runs `pydocstyle` (Google convention) as configured in `pyproject.toml`, so a missing docstring fails the
build. Tests carry a module docstring stating what the file qualifies; per-test docstrings are not required, because
the test name is the specification.

---

## Testing standard

Unit-level tests qualify low-level behaviour on every change. Nothing is claimed working on a manual walkthrough.

### Tiers and markers

Every test carries exactly one tier marker.

| Marker | Scope | May touch | Budget |
|---|---|---|---|
| `unit` | One function or class in isolation | Nothing external: no network, no Qdrant, no disk beyond `tmp_path` | < 100 ms each |
| `contract` | One component against a mock or an in-process fake of a real dependency's published contract (Qdrant, the Enterprise API, the MCP SDK, the embedding server) | In-process fakes only | < 2 s each |

The suite needs no network, no GPU and no live service, ever. The embedding server, Qdrant and Enterprise are faked
(`tests/fixtures/`). Tests that use the real article parser run on the sampled records in `tests/fixtures/wme/`.

### Test architecture (pytest)

- `tests/` mirrors the package tree: `src/wiki_mirror/search.py` -> `tests/wiki_mirror/test_search.py`;
  `tools/wiki_golden.py` -> `tests/tools/test_wiki_golden.py`; `deploy/` scripts -> `tests/deploy/`. A source
  file with no mirrored test file is an incomplete change.
- Name tests `test_<subject>_<condition>_<expected>`. The name is the specification.
- Fixtures over setup and teardown; `conftest.py` scoped to the narrowest directory that needs it; shared fixtures in
  `tests/fixtures/`.
- **No `time.sleep`, no wall-clock dependence, no unseeded randomness.** Time comes from the injected clock; anything
  that waits uses it.
- One behaviour per test. Multiple asserts are fine when they describe one behaviour.
- **Failure-path coverage is the target**, not a coverage percentage. Every error code in
  [docs/API.md](docs/API.md) has a named test. Every build stage has a test for being killed and resumed. Every
  rule that guards the update path (deferred, held, rolled back) has a test.
- **Mocks must be read, not imagined.** Write a fake from the real service's actual reply (a recorded Enterprise
  record, the embedding server's real error codes), not from what you expect it to say. Fixtures written to
  expectation have caused live failures before.

### Gates for a change

A change is mergeable when: mirrored tests pass, new failure paths have named tests, `FORMAT` matches the code,
`ruff` and `mypy` are clean, docs are updated in the same commit, the four themes were reviewed, and anything skipped
is stated plainly.

---

## Security rules

- No secrets in the repository, the logs, an error message, a fixture or model context. This includes the Enterprise
  username and password, any API key, a clients-file hash and a presigned S3 URL.
- **`.agent/` is never committed.** It may hold a local agent's credentials or notes; it is gitignored and stays so.
  Check `git status` before every commit.
- The Enterprise credentials live only on the box, in `enterprise.env`, readable by the build user. The API process
  never reads that file.
- Keys are generated with `secrets.token_urlsafe(48)`, stored only as SHA-256 hashes, and compared in constant time
  against every stored hash. Do not shorten them, store them in the clear, or add a per-key early exit.
- Wikipedia text is untrusted. It is labelled so in replies and tool descriptions; never add a path that treats it as
  instructions.
- A change to the firewall, to who may open an outbound connection, to the guard in `api.py`, or to `keys.py` is a
  security change: say so in the commit and get it reviewed.

---

## Commits

Conventional-commit prefixes (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`). The body explains *why*, not
what. A decision that changes the stored data's format, the chunker, the embedding model, the network posture or the
update path gets a dated row in [docs/DECISIONS.md](docs/DECISIONS.md) in the same commit, numbered with the next `D` number. Remember that a push to `main` is a deploy.
