# Contributing

Thanks for looking at sfn-wiki-mirror. This page is for people. [CLAUDE.md](CLAUDE.md) holds the same rules in the
form a coding agent follows, with more detail on style; where the two disagree, CLAUDE.md is the fuller statement.
Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first for a map of the code and
[docs/DESIGN.md](docs/DESIGN.md) for the reasoning and measurements behind it.

Open an issue before a large change, so we can agree on the direction. For a vulnerability, do not open an issue:
see [SECURITY.md](SECURITY.md).

## Rules that bind a change

These are the ways this repository can lose data or break a running box. They are summarised from the numbered
constraints in CLAUDE.md.

1. **The chunker and the keyword encoder stay byte-identical** (`src/wiki_mirror/text/`) unless a rebuild is
   planned. Every stored passage was cut and encoded by them; a snippet is made by re-cutting the stored article. Do
   not reformat, "clean up" or lint-fix those files. Any behaviour change there means a format change (rule 3) and
   a rebuild of many hours ([docs/DESIGN.md §12.7](docs/DESIGN.md#127-what-forces-a-full-rebuild)).
2. **`main` ships to production.** A box with the opt-in nightly update enabled pulls `main` and applies it as root.
   Never push untested work to `main`: tests, `ruff` and `mypy` must pass first, and work in progress goes on a
   branch or a fork.
3. **Format changes move together.** `deploy/FORMAT` must change in the same commit as `store._SCHEMA_VERSION`,
   `passages.CHUNKER_VERSION` or the chunk sizes, or `index.INDEX_FORMAT`. A test keeps it equal to the code.
4. **Live installs, deploys and `wiki-mirror build-now` are the operator's**, not a contributor's or CI's. A pull
   request prepares the change; it does not run `install.sh` or start a build on anyone's box.
5. **No credentials in the repository.** The Enterprise username and password live only on the box
   (`/etc/wiki-mirror/enterprise.env`, 0600, readable by the build user). Never put them, a key, a key hash or a
   presigned URL in code, a fixture, a log line, an error message or an issue. Fixtures are small hand-made or
   sampled records (`tests/fixtures/wme/`); the real snapshot is never a fixture.
6. **The API user's only egress is the embedding server's address and port, and it has no write access to the
   data.** That one connection exists so the API can embed a semantic question
   ([D10](docs/DECISIONS.md#d10)); the firewall admits it only when `site.env` names a server. Do not add a path that
   makes the serving process open any other outbound connection or write to the store or the index. A change to who
   may open what in the firewall, to the guard in `api.py`, or to `keys.py` is a security change: say so in the pull
   request.
7. **No real addresses.** Use the documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) or the
   guide's `192.168.1.x` examples in docs. Tests that need a private LAN use the shared address space (`100.64.x`),
   which the installer accepts and is nobody's own network. Never write your own LAN into the repository.
8. **Query text is never logged**, nor is a key, a hash, a presigned URL or article text. Log lengths and counts.
9. **Qdrant is loopback only**, and the article store and Qdrant are on local disk, never network storage.
10. **Dependencies are pinned exactly** in `deploy/constraints.txt` and installed from wheels only. A change to a
    pin is its own commit, with the wheel check in
    [docs/OPERATIONS.md](docs/OPERATIONS.md#refreshing-constraintstxt).

## Set up

Python 3.13 is what the host runs; the tests also run on newer Pythons (the floor is 3.12).

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Test and lint

Run these before you open a pull request. All three must be clean:

```bash
.venv/bin/ruff check .
.venv/bin/mypy src tools
.venv/bin/pytest -q
```

The suite needs no network, no GPU, no Qdrant and no Enterprise account; all of those are faked
(`tests/fixtures/`). `.venv/bin/pytest -q -m unit` and `-m contract` run one tier.

## Tests

- `tests/` mirrors the package tree: `src/wiki_mirror/search.py` is tested by `tests/wiki_mirror/test_search.py`,
  `tools/wiki_golden.py` by `tests/tools/test_wiki_golden.py`, and `deploy/` scripts by `tests/deploy/`. A source
  file with no mirrored test file is an incomplete change.
- Name tests `test_<subject>_<condition>_<expected>`. The name is the specification.
- Every test carries one tier marker: `unit` (one function in isolation, nothing external, under 100 ms) or
  `contract` (one component against an in-process fake of a real dependency, under 2 s).
- No `time.sleep`, no wall-clock dependence, no unseeded randomness. Time comes from the injected clock.
- Failure paths matter more than coverage: every error code in [docs/API.md](docs/API.md) has a named test, and so
  does every rule that guards the update path.
- **Write a fake from the real service's actual reply** (a recorded Enterprise record), not from what you expect it
  to say.

## Code and docs

Every file opens with a header block saying what it is, how it fits and where the design is explained, and every
module, class and function has a Google-style docstring (`ruff` enforces this). Comments explain why, not what.
Docs are updated in the same commit as the change. Commit messages use conventional prefixes (`feat:`, `fix:`,
`docs:`, `test:`, `refactor:`, `chore:`) and explain why. A decision that changes the stored format, the chunker, the
embedding model, the network posture or the update path gets a dated row in
[docs/DECISIONS.md](docs/DECISIONS.md).

## Licence

By contributing you agree that your contribution is released under the MIT licence ([LICENSE](LICENSE)). The two
test fixtures that hold Wikipedia text are CC BY-SA 4.0 ([NOTICE](NOTICE)); do not add more Wikipedia text to the
repository without extending that notice.
