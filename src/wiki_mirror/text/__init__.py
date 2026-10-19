"""Text handling for the mirror: the chunker, the sanitizer and the keyword encoder.

Three modules: `sanitize` (normalization and the heading-aware chunker), `sparse` (the hashed
BM25 keyword encoder) and `indexed_text` (what a passage is indexed under).

The built indexes depend on their exact behaviour. The passage numbers the index stores come
from the chunker and the keyword vectors from the encoder, so a change in either silently
misaligns every built passage unless `CHUNKER_VERSION` is bumped or a keyword re-index is run.
`tests/wiki_mirror/text/` pins the outputs against a recorded fixture. See
docs/DESIGN.md section 12.7.
"""
