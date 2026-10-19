# Changelog

All notable changes are listed here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- The semantic index's embedding run (W2, [D7](docs/DECISIONS.md#d7)): `wiki-mirror embed-now` embeds every
  passage into `wikipedia_semantic_<born>` through an Ollama-compatible server, resumably. Search does not use it
  yet.
- A resumed embedding run now checks its checkpoint against Qdrant: if a power loss cost Qdrant the tail of its
  write-ahead log, the checkpoint steps back and the lost windows are embedded again instead of skipped.

### Changed

- Qdrant's memory cap is 20G (the semantic index needs a 32 GB box).

## [0.1.0] - 2026-10-19

Initial public release.

### What works today

- **Keyword passage search** over 41.6M passages of 7.2M English Wikipedia articles (snapshot 2026-10-01), one result
  per article with a snippet, the section and an exact-revision citation.
- **Article read:** the text of any article by title or page id, in pageable windows.
- **MCP server** at `/mcp` (Streamable HTTP) with two read-only tools, `wiki_search` and `wiki_read`, and a **REST
  API** (`POST /v1/wiki/search`, `GET /v1/wiki/article`, `GET /v1/wiki/health`).
- **Keys:** one bearer key per client, stored as a hash, issued and revoked with `wiki-mirror key`.
- **Installer** (`deploy/install.sh`) for a dedicated Debian 12 or 13 container or VM: users, pinned Python and
  Qdrant, systemd units, a default-deny nftables firewall built from a site file, and a `wiki-mirror` operator
  command. It clones over anonymous HTTPS by default.
- **Monthly build timer** that downloads the Wikimedia Enterprise snapshot, parses it and builds a new index beside
  the serving one. The first build was started by hand; the timer's first unattended run is due 2026-11-02, so
  unattended operation is scheduled and not yet proven.
- **Opt-in nightly update** (`install.sh --auto-update`, or `systemctl enable --now wiki-mirror-update.timer`) with a
  format guard and rollback. Without it, update with `wiki-mirror update`.

### Planned

- **W2:** a semantic (embedding) index and hybrid search. The optional `EMBEDDING_HOST` and `EMBEDDING_PORT` in
  `site.env` open one egress rule for it; nothing uses an embedding server yet.
- **W3:** in-place monthly updates that reuse unchanged work, instead of a full rebuild each month.

See [docs/ROADMAP.md](docs/ROADMAP.md).

[Unreleased]: https://github.com/SFN-ALPHA/sfn-wiki-mirror/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/SFN-ALPHA/sfn-wiki-mirror/releases/tag/v0.1.0
