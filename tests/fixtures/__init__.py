"""Shared test fixtures: the frozen clock plugin, plain fixture modules and the real WME records.

`clock` is registered as a pytest plugin in tests/conftest.py. `wiki_mirror_fakes` and
`wiki_eval_fakes` are plain modules the tests import (in-memory Qdrant, fake article sources, the
fake mirror for the tools' tests); `wme/` holds the real Wikimedia Enterprise records and
`vendored_golden.json` the recorded outputs of the chunker and encoder that pin the
`wiki_mirror.text` package. No test logic lives here.
"""
