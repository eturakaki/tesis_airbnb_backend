"""Unit tests for TimerCollector (CIF PR-2)."""

from __future__ import annotations

import time
from typing import Callable

import pytest
import src.scraper.airbnb_scraper
from src.scraper._timer_collector import TimerCollector


def _make_monotonic_fn(values: list[float]) -> Callable[[], float]:
    """Helper: returns a callable that yields the given values in order.

    Each call to the returned fn advances through `values`. AssertionError
    if exhausted — keeps tests honest about how many times the timer probes.
    """
    it = iter(values)
    return lambda: next(it)


class TestTimerCollectorBasic:
    def test_step_records_elapsed_time_in_ms(self):
        # 0.000s entry, 0.123s exit  →  123ms
        clock = _make_monotonic_fn([0.0, 0.123])
        timer = TimerCollector(monotonic_fn=clock)

        with timer.step("navigate"):
            pass

        assert timer.to_dict() == {"navigate": 123}

    def test_multiple_distinct_steps_each_recorded(self):
        clock = _make_monotonic_fn([0.0, 0.100, 0.500, 0.750])
        timer = TimerCollector(monotonic_fn=clock)

        with timer.step("dedup_check"):
            pass
        with timer.step("navigate"):
            pass

        assert timer.to_dict() == {"dedup_check": 100, "navigate": 250}

    def test_to_dict_returns_defensive_copy(self):
        clock = _make_monotonic_fn([0.0, 0.001])
        timer = TimerCollector(monotonic_fn=clock)
        with timer.step("foo"):
            pass

        snapshot = timer.to_dict()
        snapshot["foo"] = 99999

        # Mutating the snapshot must not affect internal state
        assert timer.to_dict() == {"foo": 1}


class TestTimerCollectorExceptionHandling:
    def test_step_propagates_exception_but_records_elapsed(self):
        clock = _make_monotonic_fn([0.0, 0.250])
        timer = TimerCollector(monotonic_fn=clock)

        with pytest.raises(ValueError, match="boom"):
            with timer.step("parse_ssr"):
                raise ValueError("boom")

        # The partial timing is recorded even though the block raised
        assert timer.to_dict() == {"parse_ssr": 250}


class TestTimerCollectorAccumulation:
    def test_duplicate_keys_accumulate(self):
        # Two steps with same key: 100ms + 50ms = 150ms total
        clock = _make_monotonic_fn([0.0, 0.100, 1.000, 1.050])
        timer = TimerCollector(monotonic_fn=clock)

        with timer.step("retry_me"):
            pass
        with timer.step("retry_me"):
            pass

        assert timer.to_dict() == {"retry_me": 150}


class TestTimerCollectorIntrospection:
    def test_has_returns_true_after_step(self):
        clock = _make_monotonic_fn([0.0, 0.001])
        timer = TimerCollector(monotonic_fn=clock)
        with timer.step("navigate"):
            pass

        assert timer.has("navigate") is True

    def test_has_returns_false_for_unrecorded_step(self):
        timer = TimerCollector(monotonic_fn=_make_monotonic_fn([]))

        assert timer.has("never_ran") is False


class TestTimerCollectorDefaults:
    def test_default_monotonic_fn_is_perf_counter(self):
        timer = TimerCollector()

        # Smoke test: real perf_counter works, returns sensible ms
        with timer.step("real_clock"):
            time.sleep(0.01)  # ~10ms, very lax bound

        result = timer.to_dict()
        assert "real_clock" in result
        # Lax: anywhere from 5ms to 500ms is acceptable on any CI machine
        assert 5 <= result["real_clock"] <= 500

class TestTimerCollectorRecord:
    def test_record_stores_value(self):
        timer = TimerCollector(monotonic_fn=_make_monotonic_fn([]))
        timer.record("total", 1234)
        assert timer.to_dict() == {"total": 1234}

    def test_record_accumulates_with_step(self):
        clock = _make_monotonic_fn([0.0, 0.100])
        timer = TimerCollector(monotonic_fn=clock)
        with timer.step("foo"):
            pass
        timer.record("foo", 50)
        assert timer.to_dict() == {"foo": 150}

    def test_record_rejects_negative(self):
        timer = TimerCollector(monotonic_fn=_make_monotonic_fn([]))
        with pytest.raises(ValueError, match="≥0"):
            timer.record("x", -1)