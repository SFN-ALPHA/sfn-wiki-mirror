"""Test suite root.

Makes `tests.fixtures` importable as a plugin package from tests/conftest.py. Directories under
this root mirror the source tree (`tests/wiki_mirror/` for `src/wiki_mirror/`,
`tests/tools/` for `tools/`); `tests/deploy/` qualifies the units and scripts under
`deploy/`, which belong to no module.

The tier markers (unit, contract, ...) are defined in pyproject.toml.
"""
