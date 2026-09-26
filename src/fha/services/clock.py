"""The injectable clock (SPEC §3), so cache-TTL logic is testable."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Seconds since the epoch."""
        ...


class SystemClock:
    def now(self) -> float:
        return time.time()
