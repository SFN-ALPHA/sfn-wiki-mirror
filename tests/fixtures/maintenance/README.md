# Maintenance record fixtures

`maintenance_after_crash.json` is a record in the shape `wiki_mirror.maintenance_record` writes and
reads (schema 2), describing the box as it was on 2026-10-23: Qdrant restarted by systemd after an
OOM kill (the start time and counters are the real ones from `tests/fixtures/systemd/`), both
collections as recorded in `tests/fixtures/qdrant/`, and the run that resumed the paused semantic
collection (one automatic restart since Qdrant was last started by hand, so nothing is `held`). It
is written by hand from those real values, because the job that writes such files was built with
it; `tests/wiki_mirror/test_maintenance_record.py` checks that the module reads it and writes it
back unchanged.

The health route's and `wiki-mirror status`'s tests read it as the "a maintenance run has happened"
case.
