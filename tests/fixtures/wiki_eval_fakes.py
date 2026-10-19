"""An in-process mirror service for the Wikipedia evaluation tools' tests.

Imported by ``tests/tools/test_wiki_common.py``, ``test_wiki_golden.py`` and
``test_wiki_passage_eval.py`` (a plain module, not a pytest plugin). ``FakeMirror`` answers
``GET /v1/wiki/article`` and ``POST /v1/wiki/search`` over an ``httpx.MockTransport`` in the
shapes ``wiki_mirror.api`` sends (read from ``search.ReadReply`` and ``_error``), so the
tools' clients are exercised against the contract and not against an invented one.

Non-obvious constraints:

* Windows are cut at a fixed character count, not at the real service's paragraph breaks; the
  tools only rely on ``next_offset`` chaining and on the windows concatenating to the text.
* ``fail_first`` makes the first N requests answer 500, which ``MirrorClient`` turns into
  ``MirrorUnavailableError``, the failure the tools' mirror retry exists for.

See docs/DESIGN.md sections 5 and 12.5.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

from tools.wiki_common import MirrorClient

__all__ = ["MIRROR_KEY", "FakeMirror"]

#: The key the fake accepts; tests export it as ``WIKI_MIRROR_API_KEY``.
MIRROR_KEY = "test-reference-key"


@dataclass
class FakeMirror:
    """A mirror holding a few articles: ``page_id -> (title, revision_id, text)``."""

    articles: dict[int, tuple[str, int, str]]
    window: int = 12_000
    #: Search results by exact query text; a query not listed returns ``default_search``.
    search_results: dict[str, list[int]] = field(default_factory=dict)
    default_search: list[int] = field(default_factory=list)
    fail_first: int = 0
    #: ``(method, path)`` of every request, in order.
    requests: list[tuple[str, str]] = field(default_factory=list)
    #: ``(page_id, offset)`` of every article window requested.
    reads: list[tuple[int, int]] = field(default_factory=list)

    def client(self) -> MirrorClient:
        """A ``MirrorClient`` wired to this fake."""
        return MirrorClient(
            base_url="http://mirror.test:8300",
            timeout_seconds=5.0,
            transport=httpx.MockTransport(self._handle),
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        """Route one request."""
        self.requests.append((request.method, request.url.path))
        if self.fail_first > 0:
            self.fail_first -= 1
            return httpx.Response(500, json={"error": "boom", "detail": "injected"})
        if request.headers.get("authorization") != f"Bearer {MIRROR_KEY}":
            return httpx.Response(401, json={"error": "unauthorized", "detail": "bad key"})
        if request.url.path == "/v1/wiki/article":
            params = request.url.params
            return self._article(int(params["page_id"]), int(params["offset"]))
        if request.url.path == "/v1/wiki/search":
            body = json.loads(request.content)
            ids = self.search_results.get(body["query"], self.default_search)[: body["limit"]]
            results = [
                {
                    "page_id": pid,
                    "revision_id": self.articles[pid][1],
                    "title": self.articles[pid][0],
                    "section": "",
                    "snippet": "",
                    "score": 1.0,
                    "url": f"https://en.wikipedia.org/?oldid={self.articles[pid][1]}",
                    "disambiguation": False,
                }
                for pid in ids
                if pid in self.articles
            ]
            return httpx.Response(
                200, json={"snapshot": "2026-10-01", "mode": "lexical", "results": results}
            )
        return httpx.Response(404, json={"error": "not_found", "detail": "no such route"})

    def _article(self, page_id: int, offset: int) -> httpx.Response:
        """One window of one article, or the service's 404."""
        self.reads.append((page_id, offset))
        if page_id not in self.articles:
            return httpx.Response(404, json={"error": "not_found", "detail": "no article matches"})
        title, revision, text = self.articles[page_id]
        end = min(len(text), offset + self.window)
        return httpx.Response(
            200,
            json={
                "page_id": page_id,
                "revision_id": revision,
                "title": title,
                "snapshot": "2026-10-01",
                "revision_time": "2026-09-30T00:00:00Z",
                "url": f"https://en.wikipedia.org/?oldid={revision}",
                "license": "CC BY-SA 4.0",
                "offset": offset,
                "next_offset": end if end < len(text) else None,
                "total_chars": len(text),
                "text": text[offset:end],
            },
        )
