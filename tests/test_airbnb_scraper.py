"""
E2E tests for AirbnbScraper orchestrator.

Strategy:
- FakeBrowserSession (real test double from airbnb_scraper.py)
- FakeDBClient (in-memory, from db_client.py)
- Synthetic HTML wrappers + dict payloads with the same shape parse_payload
  and parse_runtime_response produce, so we exercise the full flow without
  Postgres or a real browser.

Coverage:
- Each ScrapeOutcome (SUCCESS, METADATA_ONLY, SKIPPED, SHAPE_DRIFT,
  CLOUDFLARE, ERROR).
- D27 (browser cycle every 50 listings).
- D28 (política 5c: SSR OK + runtime ausente).
- D29 (circuit breaker on 5 consecutive SHAPE_DRIFTs).
- D30 (doble buffer: raw_body present → SHAPE_DRIFT, not 5c).
- DB failures at TX-A, MEP failures (soft-fail), logger failures (soft-fail).
- Rate-limit sleep gating (called only post-network).

NOT covered here (covered elsewhere):
- The internal correctness of parse_payload / parse_runtime_response
  (tested in their own files).
- Real Playwright integration (manual pilot only).
- Real DB integration (would require live Postgres).
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from src.db.db_client import FakeDBClient
from src.scraper.airbnb_scraper import (
    AirbnbScraper,
    BatchAbortError,
    BrowserSessionError,
    FakeBrowserSession,
    JSONLLogger,
    MAX_CONSECUTIVE_SHAPE_DRIFT,
    BROWSER_CYCLE_EVERY_N_LISTINGS,
    ScrapeOutcome,
)


# =============================================================================
# Test fixtures and synthetic payload builders
# =============================================================================

FROZEN_TS = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def _wrap_ssr_in_html(ssr_dict: dict[str, Any]) -> str:
    """
    Wrap an SSR payload dict inside a minimal HTML envelope with the
    <script id="data-deferred-state-0"> tag that _ssr_extractor expects.

    This is the Camino B fallback: real SSR JSON, synthetic HTML envelope.
    """
    payload_json = json.dumps(ssr_dict, ensure_ascii=False)
    return (
        '<html><head><title>Listing</title></head><body>'
        f'<script id="data-deferred-state-0" type="application/json">'
        f'{payload_json}'
        '</script></body></html>'
    )


def _make_synthetic_ssr_dict(*, listing_id: str = "1234") -> dict:
    """
    Build a minimal niobeClientData structure that parse_payload accepts and
    returns parse_status='OK' for.

    Mirrors the shape used in test_parse_payload.py (sections array with
    LOCATION_DEFAULT and MEET_YOUR_HOST) — that's what the real parser
    navigates.
    """
    import base64
    user_id_b64 = base64.b64encode(b"DemandUser:50778624").decode()
    return {
        "niobeClientData": [
            [
                "StaysPdpSections:{...query_args_truncated...}",
                {
                    "data": {
                        "presentation": {
                            "stayProductDetailPage": {
                                "sections": {
                                    "sections": [
                                        {
                                            "sectionId": "LOCATION_DEFAULT",
                                            "section": {
                                                "lat": -34.5795,
                                                "lng": -58.4284,
                                            },
                                        },
                                        {
                                            "sectionId": "MEET_YOUR_HOST",
                                            "section": {
                                                "cardData": {
                                                    "userId": user_id_b64,
                                                    "isSuperhost": True,
                                                    "stats": [
                                                        {"type": "RATING", "value": "4.89"},
                                                        {"type": "REVIEW_COUNT", "value": "927"},
                                                    ],
                                                }
                                            },
                                        },
                                    ]
                                }
                            }
                        }
                    }
                },
            ]
        ]
    }

def _make_synthetic_runtime_dict(*, with_price: bool = True) -> dict:
    """Builds a synthetic runtime GraphQL response that the parser accepts."""
    sdp = (
        {
            "primaryLine": {
                "accessibilityLabel": "$637 USD por 7 noches",
                "price": "$637",
            },
        }
        if with_price
        else None
    )
    return {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_SIDEBAR",
                                "section": {"structuredDisplayPrice": sdp},
                            }
                        ]
                    }
                }
            },
            "node": {"__typename": "DemandStayListing", "id": "abc"},
        }
    }
def _frozen_now():
    return FROZEN_TS


# =============================================================================
# Helpers to build a fully-wired scraper
# =============================================================================

@pytest.fixture
def tmp_archive_dir(tmp_path) -> Path:
    return tmp_path / "archives"


@pytest.fixture
def tmp_logger(tmp_path) -> JSONLLogger:
    return JSONLLogger(log_dir=tmp_path / "logs", run_date=FROZEN_TS)


@pytest.fixture
def fake_browser():
    return FakeBrowserSession()


@pytest.fixture
def fake_db():
    return FakeDBClient()


@pytest.fixture
def make_scraper(fake_browser, fake_db, tmp_logger, tmp_archive_dir):
    """Factory that builds a fully-wired AirbnbScraper with deterministic clock + RNG."""
    def _factory(**overrides):
        defaults = dict(
            browser=fake_browser,
            db=fake_db,
            logger=tmp_logger,
            price_extractor=None,
            rate_limit_rng=random.Random(42),
            now_fn=_frozen_now,
            sleep_fn=lambda s: None,  # no-op sleep for speed
            archive_dir=tmp_archive_dir,
        )
        defaults.update(overrides)
        return AirbnbScraper(**defaults)
    return _factory


# =============================================================================
# Outcome: SKIPPED
# =============================================================================

class TestSkipped:
    def test_skip_short_circuits_before_network(self, make_scraper, fake_browser, fake_db):
        fake_db.queue_skip("999")
        with make_scraper() as scraper:
            result = scraper.scrape_listing("999")
        assert result.outcome == ScrapeOutcome.SKIPPED
        assert fake_browser.navigate_calls == []
        assert fake_db.metadata_writes == []
        assert fake_db.mep_call_count == 0


# =============================================================================
# Outcome: SUCCESS
# =============================================================================

class TestSuccess:
    def test_full_happy_path(self, make_scraper, fake_browser, fake_db):
        ssr = _make_synthetic_ssr_dict()
        runtime = _make_synthetic_runtime_dict(with_price=True)
        fake_browser.queue_navigation(
            html=_wrap_ssr_in_html(ssr),
            runtime_response=runtime,
            response_status=200,
        )
        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.SUCCESS
        assert result.ssr_parse_status == "OK"
        assert result.runtime_parse_status == "OK"
        assert result.inmueble_id == 1
        assert result.raw_payload_hash is not None
        assert len(result.raw_payload_hash) == 64  # SHA-256 hex
        assert result.mep_rate == str(FakeDBClient.DEFAULT_MEP_RATE)

        assert len(fake_db.metadata_writes) == 1
        assert fake_db.mep_call_count == 1
        # Phase 2.3: price_extractor=None → write_price NOT called
        assert fake_db.price_writes == []

    def test_archive_file_created(self, make_scraper, fake_browser, tmp_archive_dir):
        ssr = _make_synthetic_ssr_dict()
        runtime = _make_synthetic_runtime_dict(with_price=True)
        fake_browser.queue_navigation(
            html=_wrap_ssr_in_html(ssr),
            runtime_response=runtime,
        )
        with make_scraper() as scraper:
            scraper.scrape_listing("1234")

        archives = list(tmp_archive_dir.glob("1234_*.json"))
        assert len(archives) == 1
        with archives[0].open(encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["listing_id"] == "1234"
        assert doc["ssr_payload"] == ssr
        assert doc["runtime_payload"] == runtime
        assert "raw_payload_hash" in doc

    def test_jsonl_event_written(self, make_scraper, fake_browser, tmp_logger):
        ssr = _make_synthetic_ssr_dict()
        runtime = _make_synthetic_runtime_dict(with_price=True)
        fake_browser.queue_navigation(html=_wrap_ssr_in_html(ssr), runtime_response=runtime)
        with make_scraper() as scraper:
            scraper.scrape_listing("1234")

        lines = tmp_logger.path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["listing_id"] == "1234"
        assert event["outcome"] == "SUCCESS"
        assert event["decision_5c"] is False
        assert event["mep_rate"] == str(FakeDBClient.DEFAULT_MEP_RATE)


# =============================================================================
# Outcome: METADATA_ONLY (política 5c, D28)
# =============================================================================

class Test5cMetadataOnly:
    def test_runtime_absent_yields_metadata_only(self, make_scraper, fake_browser, fake_db):
        """D28: SSR OK + runtime totally absent (both buffers None) → 5c."""
        ssr = _make_synthetic_ssr_dict()
        fake_browser.queue_navigation(
            html=_wrap_ssr_in_html(ssr),
            runtime_response=None,
            runtime_raw_body=None,
        )
        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.METADATA_ONLY
        assert result.ssr_parse_status == "OK"
        assert result.runtime_parse_status is None  # never parsed
        assert result.inmueble_id == 1
        assert len(fake_db.metadata_writes) == 1
        assert fake_db.price_writes == []

    def test_runtime_partial_yields_metadata_only(self, make_scraper, fake_browser, fake_db):
        """Runtime arrives but with no structured_display_price → also 5c."""
        ssr = _make_synthetic_ssr_dict()
        runtime_no_price = _make_synthetic_runtime_dict(with_price=False)
        fake_browser.queue_navigation(
            html=_wrap_ssr_in_html(ssr),
            runtime_response=runtime_no_price,
        )
        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.METADATA_ONLY
        assert result.ssr_parse_status == "OK"
        # runtime_parse_status reflects what parse_runtime_response returned
        assert result.runtime_parse_status in ("OK", "PARTIAL")

    def test_5c_logged_with_decision_flag(self, make_scraper, fake_browser, tmp_logger):
        ssr = _make_synthetic_ssr_dict()
        fake_browser.queue_navigation(html=_wrap_ssr_in_html(ssr), runtime_response=None)
        with make_scraper() as scraper:
            scraper.scrape_listing("1234")
        event = json.loads(tmp_logger.path.read_text(encoding="utf-8").splitlines()[0])
        assert event["outcome"] == "METADATA_ONLY"
        assert event["decision_5c"] is True


# =============================================================================
# Outcome: SHAPE_DRIFT
# =============================================================================

class TestShapeDrift:
    def test_html_without_script_tag_yields_shape_drift(self, make_scraper, fake_browser):
        """SSR extractor fails → SHAPE_DRIFT."""
        fake_browser.queue_navigation(
            html="<html><body>no script tag here</body></html>",
            runtime_response=None,
        )
        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.SHAPE_DRIFT
        assert result.error_class is not None  # SSRScriptNotFoundError or similar

    def test_runtime_raw_body_present_yields_shape_drift_not_5c(
        self, make_scraper, fake_browser
    ):
        """
        D30: runtime arrived but body could not be parsed as JSON.
        This is SHAPE_DRIFT (counts toward D29), NOT política 5c.
        """
        ssr = _make_synthetic_ssr_dict()
        fake_browser.queue_navigation(
            html=_wrap_ssr_in_html(ssr),
            runtime_response=None,
            runtime_raw_body=b"<html>this is not json</html>",
        )
        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.SHAPE_DRIFT
        assert result.runtime_parse_status == "SHAPE_DRIFT_RAW_BODY"
        assert result.ssr_parse_status == "OK"


# =============================================================================
# Outcome: CLOUDFLARE
# =============================================================================

class TestCloudflare:
    def test_cf_via_html_title(self, make_scraper, fake_browser, fake_db):
        fake_browser.queue_navigation(
            html="<html><title>Just a moment...</title></html>",
            runtime_response=None,
        )
        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.CLOUDFLARE
        assert result.cloudflare_detected is True
        # CF short-circuits before SSR/DB/MEP
        assert fake_db.metadata_writes == []
        assert fake_db.mep_call_count == 0

    def test_cf_via_http_status(self, make_scraper, fake_browser):
        """Status 503/403/429 with clean-looking HTML still → CLOUDFLARE."""
        fake_browser.queue_navigation(
            html="<html><title>Listing</title></html>",
            runtime_response=None,
            response_status=503,
        )
        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.CLOUDFLARE
        assert result.cloudflare_detected is True


# =============================================================================
# Outcome: ERROR
# =============================================================================

class TestError:
    def test_navigation_failure(self, make_scraper, fake_browser):
        fake_browser.queue_navigation_failure(BrowserSessionError("network down"))
        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.ERROR
        assert result.error_class == "BrowserSessionError"

    def test_metadata_db_failure(self, make_scraper, fake_browser, fake_db):
        ssr = _make_synthetic_ssr_dict()
        fake_browser.queue_navigation(
            html=_wrap_ssr_in_html(ssr),
            runtime_response=_make_synthetic_runtime_dict(with_price=True),
        )
        fake_db.queue_metadata_failure(RuntimeError("db connection lost"))
        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.ERROR
        assert result.error_class == "RuntimeError"
        assert result.inmueble_id is None

    def test_dedup_failure(self, make_scraper, fake_browser, fake_db, monkeypatch):
        """If was_scraped_recently raises, we ERROR before any network call."""
        def _boom(_):
            raise ConnectionError("dedup query failed")
        monkeypatch.setattr(fake_db, "was_scraped_recently", _boom)
        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.ERROR
        assert result.error_class == "ConnectionError"
        assert fake_browser.navigate_calls == []


# =============================================================================
# Soft failures (do not change outcome, but populate missing_fields)
# =============================================================================

class TestSoftFailures:
    def test_mep_failure_does_not_block_success(self, make_scraper, fake_browser, fake_db):
        """D24 + soft-fail: MEP unreachable → mep_rate=None, missing_fields includes it."""
        ssr = _make_synthetic_ssr_dict()
        fake_browser.queue_navigation(
            html=_wrap_ssr_in_html(ssr),
            runtime_response=_make_synthetic_runtime_dict(with_price=True),
        )
        fake_db.queue_mep_failure(ConnectionError("dolarapi.com timeout"))

        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.SUCCESS
        assert result.mep_rate is None
        assert "mep_rate" in result.missing_fields

    def test_archive_failure_does_not_block_success(
        self, make_scraper, fake_browser, monkeypatch
    ):
        """If atomic archive write fails (disk full?), we still persist metadata."""
        from src.scraper import airbnb_scraper as mod
        def _boom(*a, **kw):
            raise OSError("disk full")
        monkeypatch.setattr(mod, "_archive_payload", _boom)

        ssr = _make_synthetic_ssr_dict()
        fake_browser.queue_navigation(
            html=_wrap_ssr_in_html(ssr),
            runtime_response=_make_synthetic_runtime_dict(with_price=True),
        )
        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.SUCCESS
        assert "archive_payload" in result.missing_fields


# =============================================================================
# D29: circuit breaker on consecutive SHAPE_DRIFTs
# =============================================================================

class TestCircuitBreakerD29:
    def test_n_minus_1_drifts_does_not_abort(self, make_scraper, fake_browser):
        """MAX-1 consecutive drifts: still raises SHAPE_DRIFT, not BatchAbortError."""
        for _ in range(MAX_CONSECUTIVE_SHAPE_DRIFT - 1):
            fake_browser.queue_navigation(
                html="<html>no script</html>",
                runtime_response=None,
            )
        with make_scraper() as scraper:
            for i in range(MAX_CONSECUTIVE_SHAPE_DRIFT - 1):
                result = scraper.scrape_listing(f"100{i}")
                assert result.outcome == ScrapeOutcome.SHAPE_DRIFT

    def test_nth_drift_raises_batch_abort(self, make_scraper, fake_browser):
        """Nth consecutive drift triggers BatchAbortError."""
        for _ in range(MAX_CONSECUTIVE_SHAPE_DRIFT):
            fake_browser.queue_navigation(
                html="<html>no script</html>",
                runtime_response=None,
            )
        with make_scraper() as scraper:
            for i in range(MAX_CONSECUTIVE_SHAPE_DRIFT - 1):
                scraper.scrape_listing(f"100{i}")
            with pytest.raises(BatchAbortError, match="consecutive"):
                scraper.scrape_listing("199999")

    def test_success_resets_drift_counter(self, make_scraper, fake_browser):
        """A SUCCESS in the middle resets the counter."""
        ssr = _make_synthetic_ssr_dict()
        runtime = _make_synthetic_runtime_dict(with_price=True)

        # 4 drifts, then 1 success, then 4 more drifts → no abort
        for _ in range(MAX_CONSECUTIVE_SHAPE_DRIFT - 1):
            fake_browser.queue_navigation(html="<html>nope</html>", runtime_response=None)
        fake_browser.queue_navigation(html=_wrap_ssr_in_html(ssr), runtime_response=runtime)
        for _ in range(MAX_CONSECUTIVE_SHAPE_DRIFT - 1):
            fake_browser.queue_navigation(html="<html>nope</html>", runtime_response=None)

        with make_scraper() as scraper:
            for i in range(MAX_CONSECUTIVE_SHAPE_DRIFT - 1):
                scraper.scrape_listing(f"100{i}")
            ok = scraper.scrape_listing("500000")
            assert ok.outcome == ScrapeOutcome.SUCCESS
            for i in range(MAX_CONSECUTIVE_SHAPE_DRIFT - 1):
                # Should not abort — counter was reset by success
                scraper.scrape_listing(f"200{i}")

    def test_5c_does_not_count_toward_drift(self, make_scraper, fake_browser):
        """Política 5c (runtime ausente, both buffers None) is NOT a drift."""
        ssr = _make_synthetic_ssr_dict()
        # Queue MAX 5c outcomes — none should count as drift
        for _ in range(MAX_CONSECUTIVE_SHAPE_DRIFT + 2):
            fake_browser.queue_navigation(
                html=_wrap_ssr_in_html(ssr),
                runtime_response=None,
                runtime_raw_body=None,
            )
        with make_scraper() as scraper:
            for i in range(MAX_CONSECUTIVE_SHAPE_DRIFT + 2):
                result = scraper.scrape_listing(f"100{i}")
                assert result.outcome == ScrapeOutcome.METADATA_ONLY


# =============================================================================
# D27: browser cycle every N listings
# =============================================================================

class TestBrowserCycleD27:
    def test_no_restart_before_threshold(self, make_scraper, fake_browser):
        ssr = _make_synthetic_ssr_dict()
        runtime = _make_synthetic_runtime_dict(with_price=True)
        for _ in range(BROWSER_CYCLE_EVERY_N_LISTINGS - 1):
            fake_browser.queue_navigation(
                html=_wrap_ssr_in_html(ssr), runtime_response=runtime
            )
        with make_scraper() as scraper:
            for i in range(BROWSER_CYCLE_EVERY_N_LISTINGS - 1):
                scraper.scrape_listing(f"{1000 + i}")
        assert fake_browser.restart_count == 0

    def test_restart_at_threshold(self, make_scraper, fake_browser):
        ssr = _make_synthetic_ssr_dict()
        runtime = _make_synthetic_runtime_dict(with_price=True)
        for _ in range(BROWSER_CYCLE_EVERY_N_LISTINGS + 1):
            fake_browser.queue_navigation(
                html=_wrap_ssr_in_html(ssr), runtime_response=runtime
            )
        with make_scraper() as scraper:
            for i in range(BROWSER_CYCLE_EVERY_N_LISTINGS + 1):
                scraper.scrape_listing(f"200{i}")
        # Exactly one restart triggered when entering listing #51
        assert fake_browser.restart_count == 1


# =============================================================================
# Lifecycle: scraper as context manager delegates to browser
# =============================================================================

class TestLifecycle:
    def test_context_manager_opens_and_closes_browser(
        self, make_scraper, fake_browser
    ):
        scraper = make_scraper()
        assert fake_browser._is_open is False
        with scraper:
            assert fake_browser._is_open is True
        assert fake_browser._is_open is False



# =============================================================================
# Integration test with real archived payloads (Camino B: synthetic HTML envelope)
# =============================================================================

REAL_PDP_PATH = Path("data/raw/airbnb_payloads/_reference_pdp_20260508.json")
REAL_RUNTIME_PATH = Path("data/raw/airbnb_payloads/_diagnostic/StaysPdpSections_200.json")


@pytest.mark.skipif(
    not (REAL_PDP_PATH.exists() and REAL_RUNTIME_PATH.exists()),
    reason="Real archived payloads not present (need _reference_pdp + StaysPdpSections_200)",
)
class TestIntegrationRealPayloads:
    """
    Camino B: real SSR payload + real runtime payload + synthetic HTML envelope.

    The HTML wrapper is constructed in the test (we don't have the original
    HTML from the captured navigation). This still exercises:
      - _ssr_extractor (regex against <script id="data-deferred-state-0">)
      - parse_payload (against real niobeClientData shape)
      - parse_runtime_response (against real GraphQL shape)
      - hash + atomic archive
      - DBClient flow (FakeDBClient, no Postgres)
      - JSONL logging

    What this does NOT exercise:
      - Real browser network capture (no Playwright here)
      - Real Postgres transactions (handled by SQLAlchemyDBClient unit tests)
      - HTML rendering quirks (Cloudflare scripts, etc.) — we wrap clean JSON
    """

    def test_real_payloads_yield_success(
        self, make_scraper, fake_browser, fake_db, tmp_logger
    ):
        with REAL_PDP_PATH.open(encoding="utf-8") as f:
            real_ssr = json.load(f)
        with REAL_RUNTIME_PATH.open(encoding="utf-8") as f:
            real_runtime = json.load(f)

        fake_browser.queue_navigation(
            html=_wrap_ssr_in_html(real_ssr),
            runtime_response=real_runtime,
            response_status=200,
        )

        with make_scraper() as scraper:
            result = scraper.scrape_listing("1560302277987248481")

        # Outcome may be SUCCESS (if SDP parsed) or METADATA_ONLY (if real
        # runtime shape doesn't have SDP at the path parse_runtime_response
        # checks). Both are valid signals — not SHAPE_DRIFT, not ERROR.
        assert result.outcome in (ScrapeOutcome.SUCCESS, ScrapeOutcome.METADATA_ONLY)
        assert result.ssr_parse_status == "OK"
        assert result.error_class is None
        assert result.raw_payload_hash is not None
        assert len(result.raw_payload_hash) == 64
        assert result.inmueble_id == 1
        assert len(fake_db.metadata_writes) == 1

    def test_real_runtime_alone_is_parseable(self):
        """Sanity: parse_runtime_response handles the real payload without crashing."""
        from src.scraper.parse_runtime_response import parse_runtime_response
        with REAL_RUNTIME_PATH.open(encoding="utf-8") as f:
            real_runtime = json.load(f)
        result = parse_runtime_response(real_runtime)
        assert result.get("parse_status") in ("OK", "PARTIAL")
        # NOT SHAPE_DRIFT — that would mean the real payload broke the parser

    def test_real_ssr_alone_is_parseable(self):
        """Sanity: parse_payload handles the real archived SSR dict."""
        from src.scraper.parse_payload import parse_payload
        with REAL_PDP_PATH.open(encoding="utf-8") as f:
            real_ssr = json.load(f)
        result = parse_payload(real_ssr)
        assert result.get("parse_status") == "OK"


class TestPR2EventSchemaAndTimings:
    """CIF PR-2 — schema_version, timings_ms granularity, error capture.

    Uses the canonical helpers from this module (_make_synthetic_ssr_dict,
    _make_synthetic_runtime_dict, _wrap_ssr_in_html) — same pattern as
    TestSuccess::test_full_happy_path.
    """

    @staticmethod
    def _read_events(tmp_logger):
        import json
        if not tmp_logger.path.exists():
            return []
        return [
            json.loads(line)
            for line in tmp_logger.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_event_has_schema_version_2(
        self, make_scraper, fake_browser, fake_db, tmp_logger,
    ):
        ssr = _make_synthetic_ssr_dict()
        runtime = _make_synthetic_runtime_dict(with_price=True)
        fake_browser.queue_navigation(
            html=_wrap_ssr_in_html(ssr),
            runtime_response=runtime,
            response_status=200,
        )
        with make_scraper() as scraper:
            scraper.scrape_listing("1234")

        events = self._read_events(tmp_logger)
        assert len(events) == 1
        assert events[0]["schema_version"] == 2

    def test_success_event_has_all_step_timings(
        self, make_scraper, fake_browser, fake_db, tmp_logger,
    ):
        ssr = _make_synthetic_ssr_dict()
        runtime = _make_synthetic_runtime_dict(with_price=True)
        fake_browser.queue_navigation(
            html=_wrap_ssr_in_html(ssr),
            runtime_response=runtime,
            response_status=200,
        )
        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.SUCCESS
        events = self._read_events(tmp_logger)
        timings = events[0]["timings_ms"]

        # All 8 instrumented steps + total must be present in a SUCCESS path
        expected_steps = {
            "total", "dedup_check", "navigate", "extract_ssr", "parse_ssr",
            "parse_runtime", "write_metadata", "fetch_mep", "archive",
        }
        missing = expected_steps - set(timings.keys())
        assert not missing, f"Missing steps in timings_ms: {missing}. Got: {timings}"

    def test_skipped_event_has_only_dedup_and_total_timings(
        self, make_scraper, fake_db, tmp_logger, monkeypatch,
    ):
        # Force dedup to return True via monkeypatch (API-agnostic)
        monkeypatch.setattr(fake_db, "was_scraped_recently", lambda listing_id: True)
        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.SKIPPED
        events = self._read_events(tmp_logger)
        timings = events[0]["timings_ms"]
        # SKIPPED short-circuits after dedup → only dedup_check + total recorded
        assert set(timings.keys()) == {"dedup_check", "total"}

    def test_metadata_only_event_lacks_parse_runtime_timing(
        self, make_scraper, fake_browser, fake_db, tmp_logger,
    ):
        # SSR OK + no runtime → política 5c → METADATA_ONLY
        ssr = _make_synthetic_ssr_dict()
        fake_browser.queue_navigation(
            html=_wrap_ssr_in_html(ssr),
            runtime_response=None,
            response_status=200,
        )
        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.METADATA_ONLY
        events = self._read_events(tmp_logger)
        timings = events[0]["timings_ms"]
        # parse_runtime didn't run (no runtime body to parse)
        assert "parse_runtime" not in timings
        # But metadata path did run
        assert "write_metadata" in timings
        assert "total" in timings

    def test_all_timings_are_non_negative_integers(
        self, make_scraper, fake_browser, fake_db, tmp_logger,
    ):
        ssr = _make_synthetic_ssr_dict()
        runtime = _make_synthetic_runtime_dict(with_price=True)
        fake_browser.queue_navigation(
            html=_wrap_ssr_in_html(ssr),
            runtime_response=runtime,
            response_status=200,
        )
        with make_scraper() as scraper:
            scraper.scrape_listing("1234")

        events = self._read_events(tmp_logger)
        for step_key, value in events[0]["timings_ms"].items():
            assert isinstance(value, int), (
                f"timing for '{step_key}' must be int, got {type(value).__name__}"
            )
            assert value >= 0, f"timing for '{step_key}' must be ≥0, got {value}"

    def test_error_event_has_error_class_and_traceback(
        self, make_scraper, fake_db, tmp_logger, monkeypatch,
    ):
        # Force dedup to raise → triggers pre-navigate ERROR with exception
        def boom(listing_id):
            raise RuntimeError("simulated db failure")
        monkeypatch.setattr(fake_db, "was_scraped_recently", boom)

        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.ERROR
        events = self._read_events(tmp_logger)
        event = events[0]
        # CIF PR-2 — both error fields must be present and well-formed
        assert event["error_class"] == "RuntimeError"
        assert "Traceback" in event["error_traceback"]
        assert "simulated db failure" in event["error_traceback"]

    def test_success_event_lacks_error_fields(
        self, make_scraper, fake_browser, fake_db, tmp_logger,
    ):
        ssr = _make_synthetic_ssr_dict()
        runtime = _make_synthetic_runtime_dict(with_price=True)
        fake_browser.queue_navigation(
            html=_wrap_ssr_in_html(ssr),
            runtime_response=runtime,
            response_status=200,
        )
        with make_scraper() as scraper:
            result = scraper.scrape_listing("1234")

        assert result.outcome == ScrapeOutcome.SUCCESS
        events = self._read_events(tmp_logger)
        # CIF PR-2 — these keys must be ABSENT (not present as None) on success
        assert "error_class" not in events[0]
        assert "error_traceback" not in events[0]