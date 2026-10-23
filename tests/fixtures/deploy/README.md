# Deploy fixtures

| File | What it is |
|---|---|
| `nft_list_counters.txt` | `nft list counters` as root on the maintainer's box, 2026-10-23, with the installed `wiki_mirror` table. The counters' comments contain the word "packets", which is what once made `wiki-mirror status` print a comment fragment (`in_invalid "Inbound packets`) as if it were a counter; `tests/deploy/test_cli.py` feeds this output to the status command. |
