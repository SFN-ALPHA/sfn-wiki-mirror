"""The record of the last update run, as `deploy/update.sh` leaves it for health.

`update.sh` (root, by hand or from `wiki-mirror-update.timer`) writes
`<data_dir>/state/update.json` atomically at the end of every run, whatever the outcome. The API
and `python -m wiki_mirror status` read it through `read_update_record`, so the one place
that says "did last night's update happen" is the same place that says whether the mirror is up.

Non-obvious constraints:

* **Reading never fails and never guesses.** A missing, unreadable or malformed file is `None`,
  not an error and not a partial record: health must answer on a box where the updater has never
  run, and a half-trusted record would be worse than none. Every field is type-checked and the
  outcome must be one of `OUTCOMES`.
* **The writer is shell**, not this module (the updater runs before, between and after Python
  installs, so it cannot depend on the package it is replacing). `tests/deploy` runs the real
  script and `tests/wiki_mirror/test_update_record.py` reads what it writes; the field
  names below are the contract between them.
* **Reporting only.** The outcome does not change the health status: a held or rolled-back update
  leaves a working mirror on the previous code, which is `ok`; the operator learns of it from the
  `update` object, `wiki-mirror status` and the update unit's journal.

See docs/DESIGN.md section 12 and docs/OPERATIONS.md (R.9, R.10).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

__all__ = ["OUTCOMES", "UPDATE_RECORD_NAME", "read_update_record", "update_record_file"]

#: The file's name under `<data_dir>/state/`.
UPDATE_RECORD_NAME: Final = "update.json"

#: What an update run can end as (`update.sh` `record_outcome`). `updated`: new code is serving;
#: `current`: nothing to do; `deferred`: a build holds the lock, try again; `held`: a format
#: change needs a person; `failed`: stopped with the previous code still serving; `rolled_back`:
#: the new code did not come up and the previous commit was restored.
OUTCOMES: Final = frozenset({"updated", "current", "deferred", "held", "failed", "rolled_back"})

#: Longest `detail` passed on: the updater writes short text, so this only bounds a damaged file.
_MAX_DETAIL_CHARS: Final = 400


def update_record_file(data_dir: Path) -> Path:
    """Where the last update's record lives: `<data_dir>/state/update.json`."""
    return data_dir / "state" / UPDATE_RECORD_NAME


def read_update_record(data_dir: Path) -> dict[str, Any] | None:
    """The last update run's record, or None when there is none that can be trusted.

    Args:
        data_dir: The service's local data directory.

    Returns:
        A dict with `at`, `from`, `to` (None when the run stopped before it learned the target),
        `outcome` (one of `OUTCOMES`), `detail` and `unattended`; or None when the file is
        absent, unreadable, not JSON, or any field has the wrong type. Never raises.
    """
    try:
        raw = json.loads(update_record_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Absent is the normal state of a box that has never been updated by the new updater,
        # and a damaged file is not worth failing health for; both read as "no record".
        return None
    if not isinstance(raw, dict):
        return None
    at, source, target = raw.get("at"), raw.get("from"), raw.get("to")
    outcome, detail, unattended = raw.get("outcome"), raw.get("detail"), raw.get("unattended")
    valid = (
        "to" in raw  # null is allowed, absent is not: the writer always says which
        and isinstance(at, str)
        and isinstance(source, str)
        and (target is None or isinstance(target, str))
        and isinstance(outcome, str)  # a list would raise TypeError in the set lookup
        and outcome in OUTCOMES
        and isinstance(detail, str)
        and isinstance(unattended, bool)
    )
    if not valid:
        return None
    return {
        "at": at,
        "from": source,
        "to": target,
        "outcome": outcome,
        "detail": str(detail)[:_MAX_DETAIL_CHARS],
        "unattended": unattended,
    }
