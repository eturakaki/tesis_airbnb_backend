"""Tests for pure helpers and data classes in airbnb_scraper.py (Entrega 1)."""
from __future__ import annotations

import hashlib
import json
import random
import statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
from src.scraper.airbnb_scraper import _check_abort_threshold
from src.scraper._exceptions import (
    BatchAbortError,
    CloudflareDetectedError,
    BrowserSessionError,
    ScraperError,
)
from src.scraper.airbnb_scraper import (
    # Constants
    BROWSER_CYCLE_EVERY_N_LISTINGS,
    CLOUDFLARE_HTTP_STATUS_CODES,
    MAX_CONSECUTIVE_SHAPE_DRIFT,
    RATE_LIMIT_MEAN,
    RATE_LIMIT_MU_LOG,
    RATE_LIMIT_SIGMA_LOG,
    RATE_LIMIT_STD,
    RATE_LIMIT_TRUNCATE_HI,
    RATE_LIMIT_TRUNCATE_LO,
    # Exceptions
    BatchAbortError,
    CloudflareDetectedError,
    BrowserSessionError,
    ScraperError,
    # Result types
    ScrapeOutcome,
    ScrapeResult,
    # Logger
    JSONLLogger,
    # Pure helpers
    _archive_payload,
    _detect_cloudflare_in_html,
    _hash_payloads,
    _is_cloudflare_status,
    _sample_rate_limit_sleep,
)


# =============================================================================
# Constants & metodological invariants
# =============================================================================

class TestConstants:
    def test_d27_browser_cycle_is_50(self):
        """D27 — proactive browser cycle every 50 PDPs."""
        assert BROWSER_CYCLE_EVERY_N_LISTINGS == 50

    def test_d29_shape_drift_threshold_is_5(self):
        """D29 — abort batch on 5 consecutive SHAPE_DRIFTs."""
        assert MAX_CONSECUTIVE_SHAPE_DRIFT == 5

    def test_rate_limit_distribution_params_are_consistent(self):
        """RATE_LIMIT_MU_LOG and RATE_LIMIT_SIGMA_LOG must yield the declared mean & std."""
        import math
        # E[X] = exp(mu + sigma^2/2)
        mean = math.exp(RATE_LIMIT_MU_LOG + RATE_LIMIT_SIGMA_LOG ** 2 / 2)
        # Var[X] = (exp(sigma^2) - 1) * exp(2*mu + sigma^2)
        var = (math.exp(RATE_LIMIT_SIGMA_LOG ** 2) - 1) * math.exp(
            2 * RATE_LIMIT_MU_LOG + RATE_LIMIT_SIGMA_LOG ** 2
        )
        std = math.sqrt(var)
        assert abs(mean - RATE_LIMIT_MEAN) < 1e-9, f"mean mismatch: {mean} vs {RATE_LIMIT_MEAN}"
        assert abs(std - RATE_LIMIT_STD) < 1e-9, f"std mismatch: {std} vs {RATE_LIMIT_STD}"

    def test_rate_limit_support_is_6_to_25(self):
        assert RATE_LIMIT_TRUNCATE_LO == 6.0
        assert RATE_LIMIT_TRUNCATE_HI == 25.0

    def test_cloudflare_status_codes(self):
        assert 503 in CLOUDFLARE_HTTP_STATUS_CODES
        assert 403 in CLOUDFLARE_HTTP_STATUS_CODES
        assert 429 in CLOUDFLARE_HTTP_STATUS_CODES
        assert 200 not in CLOUDFLARE_HTTP_STATUS_CODES


# =============================================================================
# Exception hierarchy
# =============================================================================

class TestExceptionHierarchy:
    def test_batch_abort_subclass_of_scraper_error(self):
        assert issubclass(BatchAbortError, ScraperError)

    def test_cloudflare_subclass_of_scraper_error(self):
        assert issubclass(CloudflareDetectedError, ScraperError)

    def test_browser_session_subclass_of_scraper_error(self):
        assert issubclass(BrowserSessionError, ScraperError)


# =============================================================================
# ScrapeResult / ScrapeOutcome
# =============================================================================

class TestScrapeOutcome:
    def test_all_outcomes_present(self):
        names = {o.value for o in ScrapeOutcome}
        assert names == {
            "SUCCESS", "METADATA_ONLY", "SKIPPED",
            "SHAPE_DRIFT", "CLOUDFLARE", "ERROR",
        }

    def test_outcome_is_str_enum_for_json_serialization(self):
        # ScrapeOutcome es str subclass → JSON serializable directamente.
        assert json.dumps(ScrapeOutcome.SUCCESS) == '"SUCCESS"'


class TestScrapeResult:
    def test_minimal_construction(self):
        r = ScrapeResult(
            listing_id="123",
            outcome=ScrapeOutcome.SKIPPED,
            duration_ms=42,
        )
        assert r.listing_id == "123"
        assert r.outcome is ScrapeOutcome.SKIPPED
        assert r.url is None
        assert r.missing_fields == []

    def test_is_immutable(self):
        r = ScrapeResult(listing_id="1", outcome=ScrapeOutcome.SUCCESS, duration_ms=0)
        with pytest.raises(Exception):  # FrozenInstanceError subclass of AttributeError
            r.listing_id = "2"  # type: ignore[misc]

    def test_missing_fields_default_independent(self):
        """default_factory: dos instancias no comparten lista."""
        a = ScrapeResult(listing_id="1", outcome=ScrapeOutcome.SUCCESS, duration_ms=0)
        b = ScrapeResult(listing_id="2", outcome=ScrapeOutcome.SUCCESS, duration_ms=0)
        assert a.missing_fields is not b.missing_fields


# =============================================================================
# _hash_payloads
# =============================================================================

class TestHashPayloads:
    def test_deterministic(self):
        ssr = {"a": 1, "b": [1, 2]}
        runtime = {"x": "y"}
        h1 = _hash_payloads(ssr, runtime)
        h2 = _hash_payloads(ssr, runtime)
        assert h1 == h2

    def test_returns_64_char_hex(self):
        h = _hash_payloads({}, None)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_key_order_invariant(self):
        """sort_keys=True → orden de inserción no afecta hash."""
        a = {"alpha": 1, "beta": 2}
        b = {"beta": 2, "alpha": 1}
        assert _hash_payloads(a, None) == _hash_payloads(b, None)

    def test_runtime_none_vs_empty_dict_differ(self):
        assert _hash_payloads({}, None) != _hash_payloads({}, {})

    def test_value_change_changes_hash(self):
        h1 = _hash_payloads({"a": 1}, None)
        h2 = _hash_payloads({"a": 2}, None)
        assert h1 != h2

    def test_unicode_preserved(self):
        """ensure_ascii=False — caracteres argentinos no se escapan a \\uXXXX."""
        h_acentos = _hash_payloads({"barrio": "Núñez"}, None)
        # Reproducimos el canonical manualmente para verificar.
        canonical = json.dumps(
            {"ssr": {"barrio": "Núñez"}, "runtime": None},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert h_acentos == expected

    def test_nested_structures(self):
        nested = {"a": {"b": {"c": [1, {"d": [2, 3]}]}}}
        # No raise.
        h = _hash_payloads(nested, nested)
        assert len(h) == 64


# =============================================================================
# _sample_rate_limit_sleep
# =============================================================================

class TestSampleRateLimit:
    def test_returns_within_truncation_bounds(self):
        rng = random.Random(42)
        for _ in range(1000):
            s = _sample_rate_limit_sleep(rng)
            assert RATE_LIMIT_TRUNCATE_LO <= s <= RATE_LIMIT_TRUNCATE_HI

    def test_deterministic_with_seed(self):
        rng1 = random.Random(123)
        rng2 = random.Random(123)
        seq1 = [_sample_rate_limit_sleep(rng1) for _ in range(20)]
        seq2 = [_sample_rate_limit_sleep(rng2) for _ in range(20)]
        assert seq1 == seq2

    def test_empirical_mean_within_truncated_expectation(self):
        """
        El truncamiento sesga la media (corta colas). La media empírica truncada
        debe estar cercana a la teórica untruncated (12s) pero ligeramente
        menor (porque la cola derecha está más cortada que la izquierda).
        Tolerancia: ±1.0s con n=5000.
        """
        rng = random.Random(7)
        samples = [_sample_rate_limit_sleep(rng) for _ in range(5000)]
        empirical_mean = statistics.mean(samples)
        # Mean truncado debe estar dentro de [10, 13] aproximadamente.
        assert 10.0 < empirical_mean < 13.0, f"empirical mean = {empirical_mean}"

    def test_distribution_skewed_right(self):
        """LogNormal es skewed-right → mediana < media."""
        rng = random.Random(99)
        samples = [_sample_rate_limit_sleep(rng) for _ in range(2000)]
        median = statistics.median(samples)
        mean = statistics.mean(samples)
        assert median < mean


# =============================================================================
# _archive_payload
# =============================================================================

class TestArchivePayload:
    def test_creates_file_with_correct_name(self, tmp_path: Path):
        scraped_at = datetime(2026, 5, 9, 18, 30, 45, tzinfo=timezone.utc)
        path = _archive_payload(
            archive_dir=tmp_path,
            listing_id="12345",
            scraped_at=scraped_at,
            url="https://airbnb.com.ar/rooms/12345",
            ssr_dict={"a": 1},
            runtime_dict={"b": 2},
            raw_hash="abc",
        )
        assert path.exists()
        assert path.name == "12345_20260509T183045Z.json"

    def test_filename_no_colons(self, tmp_path: Path):
        """Windows-friendly: filename no contiene ':'."""
        path = _archive_payload(
            archive_dir=tmp_path,
            listing_id="1",
            scraped_at=datetime(2026, 5, 9, tzinfo=timezone.utc),
            url="x",
            ssr_dict={},
            runtime_dict=None,
            raw_hash="h",
        )
        assert ":" not in path.name

    def test_naive_datetime_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError, match="tz-aware"):
            _archive_payload(
                archive_dir=tmp_path,
                listing_id="1",
                scraped_at=datetime(2026, 5, 9, 0, 0, 0),  # naive
                url="x",
                ssr_dict={},
                runtime_dict=None,
                raw_hash="h",
            )

    def test_invalid_listing_id_rejected(self, tmp_path: Path):
        ts = datetime.now(timezone.utc)
        for bad in ["", "../etc/passwd", "a/b", "a\\b"]:
            with pytest.raises(ValueError):
                _archive_payload(tmp_path, bad, ts, "x", {}, None, "h")

    def test_content_is_well_formed_json(self, tmp_path: Path):
        ts = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
        path = _archive_payload(
            archive_dir=tmp_path,
            listing_id="42",
            scraped_at=ts,
            url="https://airbnb.com.ar/rooms/42",
            ssr_dict={"foo": "bar"},
            runtime_dict={"price": None},
            raw_hash="deadbeef",
        )
        content = json.loads(path.read_text(encoding="utf-8"))
        assert content["listing_id"] == "42"
        assert content["url"] == "https://airbnb.com.ar/rooms/42"
        assert content["ssr_payload"] == {"foo": "bar"}
        assert content["runtime_payload"] == {"price": None}
        assert content["raw_payload_hash"] == "deadbeef"
        assert content["fuente_scraper"] == "airbnb_scraper_v1"
        # ISO-8601 timestamp parseable
        parsed_ts = datetime.fromisoformat(content["scraped_at"])
        assert parsed_ts == ts

    def test_unicode_preserved_no_escaping(self, tmp_path: Path):
        path = _archive_payload(
            archive_dir=tmp_path,
            listing_id="1",
            scraped_at=datetime.now(timezone.utc),
            url="x",
            ssr_dict={"barrio": "Núñez", "title": "🏠"},
            runtime_dict=None,
            raw_hash="h",
        )
        raw = path.read_text(encoding="utf-8")
        assert "Núñez" in raw  # not "N\\u00fa\\u00f1ez"
        assert "🏠" in raw

    def test_runtime_none_serializes_as_null(self, tmp_path: Path):
        path = _archive_payload(
            archive_dir=tmp_path,
            listing_id="1",
            scraped_at=datetime.now(timezone.utc),
            url="x",
            ssr_dict={},
            runtime_dict=None,
            raw_hash="h",
        )
        content = json.loads(path.read_text(encoding="utf-8"))
        assert content["runtime_payload"] is None

    def test_atomic_no_tmp_file_left(self, tmp_path: Path):
        _archive_payload(
            archive_dir=tmp_path,
            listing_id="1",
            scraped_at=datetime.now(timezone.utc),
            url="x",
            ssr_dict={},
            runtime_dict=None,
            raw_hash="h",
        )
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"orphan tmp files: {tmp_files}"

    def test_creates_archive_dir_if_missing(self, tmp_path: Path):
        nested = tmp_path / "deeply" / "nested" / "dir"
        assert not nested.exists()
        _archive_payload(
            archive_dir=nested,
            listing_id="1",
            scraped_at=datetime.now(timezone.utc),
            url="x",
            ssr_dict={},
            runtime_dict=None,
            raw_hash="h",
        )
        assert nested.exists()

    def test_two_scrapes_at_different_times_dont_collide(self, tmp_path: Path):
        ts1 = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 5, 9, 12, 0, 1, tzinfo=timezone.utc)
        p1 = _archive_payload(tmp_path, "42", ts1, "u", {}, None, "h1")
        p2 = _archive_payload(tmp_path, "42", ts2, "u", {}, None, "h2")
        assert p1 != p2
        assert p1.exists() and p2.exists()


# =============================================================================
# Cloudflare detection
# =============================================================================

class TestDetectCloudflareInHtml:
    def test_clean_html_returns_false(self):
        html = "<html><head><title>Apartment in Palermo</title></head><body>...</body></html>"
        assert not _detect_cloudflare_in_html(html)

    def test_just_a_moment_title(self):
        html = "<html><head><title>Just a moment...</title></head><body></body></html>"
        assert _detect_cloudflare_in_html(html)

    def test_attention_required_title(self):
        html = "<html><head><title>Attention Required! | Cloudflare</title></head></html>"
        assert _detect_cloudflare_in_html(html)

    def test_cf_challenge_in_body(self):
        html = "<html><head><title>X</title></head><body><div class='cf-challenge'></div></body></html>"
        assert _detect_cloudflare_in_html(html)

    def test_challenge_platform_in_body(self):
        html = "<html><body><script src='/cdn-cgi/challenge-platform/h/x.js'></script></body></html>"
        assert _detect_cloudflare_in_html(html)

    def test_checking_browser_in_body(self):
        html = "<html><body>Checking your browser before accessing...</body></html>"
        assert _detect_cloudflare_in_html(html)

    def test_marker_after_5kb_not_detected(self):
        """Body scan limited to first N bytes — performance bound."""
        padding = "x" * 6000
        html = f"<html><body>{padding}cf-challenge</body></html>"
        assert not _detect_cloudflare_in_html(html)

    def test_empty_html_returns_false(self):
        assert not _detect_cloudflare_in_html("")

    def test_case_insensitive_title(self):
        html = "<html><head><title>JUST A MOMENT...</title></head></html>"
        assert _detect_cloudflare_in_html(html)


class TestIsCloudflareStatus:
    @pytest.mark.parametrize("status", [503, 403, 429])
    def test_cf_status_codes(self, status: int):
        assert _is_cloudflare_status(status)

    @pytest.mark.parametrize("status", [200, 201, 301, 404, 500, 502])
    def test_non_cf_status_codes(self, status: int):
        assert not _is_cloudflare_status(status)

    def test_none_returns_false(self):
        assert not _is_cloudflare_status(None)


# =============================================================================
# JSONLLogger
# =============================================================================

class TestJSONLLogger:
    def test_creates_log_dir(self, tmp_path: Path):
        log_dir = tmp_path / "logs"
        assert not log_dir.exists()
        JSONLLogger(log_dir=log_dir)
        assert log_dir.exists()

    def test_filename_uses_run_date(self, tmp_path: Path):
        run_date = datetime(2026, 5, 9, 14, 30, 0, tzinfo=timezone.utc)
        logger = JSONLLogger(log_dir=tmp_path, run_date=run_date)
        assert logger.path.name == "scraper_20260509.jsonl"

    def test_naive_run_date_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError, match="tz-aware"):
            JSONLLogger(log_dir=tmp_path, run_date=datetime(2026, 5, 9))

    def test_log_appends_one_line(self, tmp_path: Path):
        run_date = datetime(2026, 5, 9, tzinfo=timezone.utc)
        logger = JSONLLogger(log_dir=tmp_path, run_date=run_date)
        logger.log({"listing_id": "1", "outcome": "SUCCESS"})
        lines = logger.path.read_text(encoding="utf-8").splitlines()

        # CIF PR-2: events are auto-stamped with schema_version, so test
        # by parsing + key-by-key inspection (not literal-string equality).
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["listing_id"] == "1"
        assert parsed["outcome"] == "SUCCESS"
        assert parsed["schema_version"] == 2

    def test_multiple_logs_appended(self, tmp_path: Path):
        run_date = datetime(2026, 5, 9, tzinfo=timezone.utc)
        logger = JSONLLogger(log_dir=tmp_path, run_date=run_date)
        for i in range(5):
            logger.log({"i": i})
        lines = logger.path.read_text(encoding="utf-8").splitlines()

        assert len(lines) == 5
        for i, line in enumerate(lines):
            parsed = json.loads(line)
            assert parsed["i"] == i
            assert parsed["schema_version"] == 2  # CIF PR-2

    def test_unicode_preserved(self, tmp_path: Path):
        run_date = datetime(2026, 5, 9, tzinfo=timezone.utc)
        logger = JSONLLogger(log_dir=tmp_path, run_date=run_date)
        logger.log({"barrio": "Núñez"})
        raw = logger.path.read_text(encoding="utf-8")
        assert "Núñez" in raw

    def test_logger_does_not_overwrite_existing_lines(self, tmp_path: Path):
        run_date = datetime(2026, 5, 9, tzinfo=timezone.utc)
        # First logger writes
        logger1 = JSONLLogger(log_dir=tmp_path, run_date=run_date)
        logger1.log({"first": True})
        # Second logger same date appends
        logger2 = JSONLLogger(log_dir=tmp_path, run_date=run_date)
        logger2.log({"second": True})
        lines = logger1.path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2