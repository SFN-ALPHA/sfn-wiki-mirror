"""The mirror service: a local, searchable mirror of English Wikipedia on its own LXC.

Clients never download from Wikimedia. This package runs on the mirror's own LXC
(`wiki-mirror`), the only box with an egress rule for Wikimedia Enterprise and
the S3 bucket its downloads redirect to, and it serves the mirror over the LAN with bearer keys:
the primary client over REST, and other agents and applications (Claude Code, ...) over
MCP, each with a key of its own.

What lives here:

* `config` - the service's settings (YAML plus one secret from the environment), validated at
  start, invalid meaning no start;
* `records` - the article and snapshot shapes every other module shares, and the citation URL;
* `enterprise` - the Wikimedia Enterprise client: login, token refresh, chunked resumable
  downloads to the NAS;
* `parse` - Enterprise's rendered (Parsoid) HTML to clean, sectioned plain text;
* `store` - the per-snapshot article store, SQLite with zstd-compressed text;
* `build` - the download and parse stages of a build, checkpointed per chunk;
* `index` - passages into Qdrant: the lexical collection in W1 (W2 adds a semantic one beside it);
* `search` - ranked results with snippets, and windowed article reads;
* `serving` - the snapshot being served, following the Qdrant alias so a rebuild needs no
  restart;
* `keys` - the primary key plus each other agent's hashed key, re-read live; add, list,
  revoke;
* `mcp_server` - the MCP front door, `wiki_search` and `wiki_read`, over the same search code;
* `api` - the FastAPI app (`/v1/wiki/search`, `/v1/wiki/article`, `/v1/wiki/health`, `/mcp`) and
  the authentication in front of all of it;
* `text` - the chunker, the sanitizer and the keyword encoder, owned here; the
  built indexes depend on their exact behaviour;
* `__main__` - the command line: `download`, `parse`, `index`, `build`, `serve`, `status`, `key`.

Wikipedia text is untrusted content: anyone can edit it. It is sanitized with
`text.sanitize.sanitize(untrusted=True)` at parse time and labelled untrusted by the client's tools.

See docs/DESIGN.md.
"""

from __future__ import annotations

__all__ = ["__doc__"]
