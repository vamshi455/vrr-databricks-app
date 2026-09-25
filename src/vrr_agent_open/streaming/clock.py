"""The simulated clock — N simulated days per real second.

Two clocks run in this system and conflating them is the classic mistake:

* **Simulated time** is the production date being generated. It starts the day after the
  seeded history ends and moves fast — at the default rate, a month of field history goes
  by in about eight seconds.
* **Wall time** is when a message was actually published, consumed and stored. Latency is
  measured only in wall time. `now() - event_date` would be meaningless, because
  `event_date` deliberately runs ahead into the future.

`due()` returns the dates that *should* have been emitted by now and advances the cursor,
so a slow tick — a Postgres stall, a GC pause — catches up rather than silently losing
days. Nothing sleeps in here: the class is pure arithmetic over a monotonic timestamp,
which is what makes it testable without waiting for real seconds to pass.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field


@dataclass
class SimClock:
    """Maps elapsed wall seconds onto simulated production dates."""

    start_date: dt.date
    days_per_second: float = 4.0
    max_days: int = 0                    # 0 = run until stopped
    t0: float = field(default_factory=time.monotonic)
    emitted: int = 0

    @property
    def frontier(self) -> dt.date:
        """The next date that has NOT been emitted yet."""
        return self.start_date + dt.timedelta(days=self.emitted)

    @property
    def finished(self) -> bool:
        return bool(self.max_days) and self.emitted >= self.max_days

    def due(self, now: float | None = None) -> list[dt.date]:
        """Dates owed by `now`, advancing the cursor past them.

        Returns a list rather than one date because a stalled tick owes several: falling
        behind must show up as a burst of work, not as skipped history.
        """
        now = time.monotonic() if now is None else now
        owed = int((now - self.t0) * self.days_per_second) - self.emitted
        if owed <= 0:
            return []
        if self.max_days:
            owed = min(owed, self.max_days - self.emitted)
        out = [self.start_date + dt.timedelta(days=self.emitted + i) for i in range(owed)]
        self.emitted += owed
        return out

    def event_time(self, date: dt.date, ordinal: int, n: int) -> dt.datetime:
        """Spread one simulated day's completions across that simulated day.

        Without this every row in a day shares a timestamp, and a watermark has nothing
        to advance through inside a day. Ordinal `k` of `n` lands at `k/n` through it.
        """
        base = dt.datetime.combine(date, dt.time.min, tzinfo=dt.timezone.utc)
        return base + dt.timedelta(seconds=86400.0 * ordinal / max(n, 1))
