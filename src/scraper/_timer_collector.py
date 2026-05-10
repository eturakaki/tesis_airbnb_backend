"""
TimerCollector — granular per-step wall-time tracking for scraper observability.

Used by AirbnbScraper.scrape_listing to measure duration of each phase
(dedup_check, navigate, extract_ssr, parse_ssr, parse_runtime, write_metadata,
fetch_mep, archive) plus a wall-clock total. Output goes into the JSONL
event under `timings_ms`.

Determinism: monotonic_fn is injectable for testing. Production uses
time.perf_counter (monotonic, unaffected by NTP sync or timezone changes).
Times are recorded even when the wrapped block raises — diagnostic value
of "step X took 30s and then timed out" is high.

NOT thread-safe. Each scrape_listing call gets its own instance.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable, Iterator


class TimerCollector:
    def __init__(
        self,
        monotonic_fn: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._monotonic_fn = monotonic_fn
        self._timings_ms: dict[str, int] = {}

    @contextmanager
    def step(self, key: str) -> Iterator[None]:
        """Context manager that records elapsed wall-time for `key`.

        If `key` was already recorded, the new elapsed time is ADDED to the
        existing value (defensive accumulation; relevant if a step is retried
        within a single scrape).

        The timer fires in `finally` — exceptions raised inside the block
        propagate, but the partial elapsed time is still recorded.
        """
        start = self._monotonic_fn()
        try:
            yield
        finally:
            elapsed_ms = int((self._monotonic_fn() - start) * 1000)
            self._timings_ms[key] = self._timings_ms.get(key, 0) + elapsed_ms

    def to_dict(self) -> dict[str, int]:
        """Return a defensive copy of the timings dict."""
        return dict(self._timings_ms)

    def has(self, key: str) -> bool:
        return key in self._timings_ms