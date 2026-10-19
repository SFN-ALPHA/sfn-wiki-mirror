"""Root pytest configuration: fixture plugin registration.

Every fixture package under tests/fixtures/ that provides pytest fixtures is registered here as
a plugin rather than being imported ad hoc, so any test can request any fixture. Test logic never
lives in this file. Only the clock plugin is
needed here, and there is no collection guard for missing optional dependencies, because
`selectolax` and `zstandard` are base dependencies of this package and always installed.

The mirror-specific fixtures (the real Enterprise records, a snapshot) live in
tests/wiki_mirror/conftest.py, scoped to the tests that use them.
"""

from __future__ import annotations

# Fixture modules are plugins, not imports. Adding a fixture module here makes it available
# suite-wide; forgetting to is the usual cause of "fixture not found".
pytest_plugins = [
    "tests.fixtures.clock",
]
