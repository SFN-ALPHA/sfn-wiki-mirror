# Wikimedia Enterprise fixtures

`records.ndjson` holds eight real article records, one JSON object per line, in the shape the
Enterprise snapshot API delivers them. The mirror's parser, store and build tests read
them (`tests/wiki_mirror/`).

**Source.** Wikimedia Enterprise snapshot `enwiki_namespace_0`, dated 2026-10-01, chunk 1
(`enwiki_namespace_0_chunk_1`), fetched 2026-10-16 through the snapshot download API.

**Licence.** The article text is from English Wikipedia and is available under the Creative
Commons Attribution-ShareAlike 4.0 licence (CC BY-SA 4.0), attributed to Wikipedia contributors.
Each record carries its own `license` and `url` fields, and its page history is at the `url`.

**How they were cut.** The first 40 records of the chunk were read, and these eight were chosen by
what they contain. The only change was removing `article_body.wikitext` from each (the parser
reads the rendered HTML). Nothing else was edited or truncated; every record is whole.

| Record | Why it is here |
|---|---|
| Acoustic Sessions | a disambiguation page (`Template:Disambiguation`, `Category:Disambiguation pages`), two redirects, a `dmbox` message box |
| Acontista cayennensis | a species infobox whose rows are plain `td`/`td` pairs, a reference list, a navbox, a stub box |
| Adaptive beamformer | figures with captions, a plain list, a reference list |
| 1963 NHRA Winternationals | two `ambox` maintenance banners, an infobox, a reference list |
| 2018-19 England Korfball League | an `ambox`, an infobox, `wikitable`s, a `thumb` figure |
| 2018 in Lithuania | four hatnotes, an infobox with navigation cells, navboxes, empty sections |
| 1947-48 Klass B season | three `wikitable`s, a navbox |
| 1765 in Great Britain | headings, nested lists, a captioned figure, a reference list with CS1 citations, a "See also" section |

**What is NOT here: mathematics.** None of the 40 sampled records contains a formula, so the
parser's math handling is tested with a small HTML snippet written inside `test_parse.py` in the
shape MediaWiki's Math extension documents (`span.mwe-math-element` holding a `math` element with
an `alttext` and a fallback `img`). It is the one test input that is not a real record; replace
it with a cut from a real article (Mass-energy equivalence, say) when a chunk containing one is
downloaded.
