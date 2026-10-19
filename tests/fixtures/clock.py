"""Controllable clock for tests.

Nothing in the suite may depend on wall-clock time or call ``time.sleep``: key caches, snapshot
re-checks and download backoff are all time-driven, and a suite that actually waits is both slow
and flaky. Tests advance time explicitly instead.

Registered as a plugin in tests/conftest.py, which is what provides the ``frozen_clock`` fixture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

#: Fixed origin for every test that needs a timestamp. Chosen to be unambiguous in
#: logs and far from DST boundaries; all timestamps are timezone-aware UTC.
EPOCH = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@dataclass
class FrozenClock:
    """A clock that only moves when a test moves it."""

    now_value: datetime = EPOCH
    #: Every advance, in order - lets a test assert *how long* code waited.
    advances: list[timedelta] = field(default_factory=list)

    def now(self) -> datetime:
        """Current time. Substitute for ``datetime.now(UTC)`` in production code."""
        return self.now_value

    def monotonic(self) -> float:
        """Seconds since the epoch origin. Substitute for ``time.monotonic()``."""
        return (self.now_value - EPOCH).total_seconds()

    def advance(self, seconds: float) -> datetime:
        """Move time forward and return the new now.

        Args:
            seconds: How far to advance. Must not be negative - time going backwards
                is never a legitimate test scenario here and hides real bugs.

        Returns:
            The new current time.
        """
        if seconds < 0:
            msg = f"cannot advance the clock backwards ({seconds}s)"
            raise ValueError(msg)
        delta = timedelta(seconds=seconds)
        self.advances.append(delta)
        self.now_value += delta
        return self.now_value

    def sleep(self, seconds: float) -> None:
        """Drop-in for ``time.sleep`` that advances instead of blocking."""
        self.advance(seconds)


@pytest.fixture
def frozen_clock() -> FrozenClock:
    """A clock frozen at :data:`EPOCH`, advanced only by the test."""
    return FrozenClock()
