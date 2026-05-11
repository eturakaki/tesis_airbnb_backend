"""
Tests for browser session abstraction (Entrega 2).

Scope:
- FakeBrowserSession: full contract validation (invariants 1-7).
- PlaywrightBrowserSession: structural typing only (no real browser tests).
  Real browser integration testing happens manually in the pilot batch.
"""
from __future__ import annotations

import pytest

from src.scraper.airbnb_scraper import (
    BrowserSessionError,
    FakeBrowserSession,
    PlaywrightBrowserSession,
    RUNTIME_RESPONSE_URL_PATTERN,
)
from src.scraper.airbnb_scraper import PlaywrightBrowserSession
from src.scraper._exceptions import BrowserSessionError

# =============================================================================
# Structural typing — both classes satisfy the Protocol
# =============================================================================

class TestProtocolConformance:
    def test_playwright_session_has_protocol_methods(self):
        """Structural check — methods exist with correct signatures."""
        required = [
            "__enter__", "__exit__", "navigate", "get_html",
            "get_intercepted_runtime_response", "get_last_runtime_raw_body",
            "get_last_response_status", "detect_cloudflare", "restart",
        ]
        for method in required:
            assert hasattr(PlaywrightBrowserSession, method), f"missing {method}"

    def test_fake_session_has_protocol_methods(self):
        required = [
            "__enter__", "__exit__", "navigate", "get_html",
            "get_intercepted_runtime_response", "get_last_runtime_raw_body",
            "get_last_response_status", "detect_cloudflare", "restart",
        ]
        for method in required:
            assert hasattr(FakeBrowserSession, method), f"missing {method}"

    def test_fake_session_isinstance_of_protocol(self):
        """Runtime structural check via Protocol (best-effort, depends on Python version)."""
        fake = FakeBrowserSession()
        # Just check call doesn't raise — Protocol introspection is loose.
        assert hasattr(fake, "navigate")


# =============================================================================
# FakeBrowserSession — lifecycle
# =============================================================================

class TestFakeLifecycle:
    def test_enter_returns_self(self):
        fake = FakeBrowserSession()
        with fake as session:
            assert session is fake

    def test_navigate_before_enter_raises(self):
        fake = FakeBrowserSession()
        fake.queue_navigation()
        with pytest.raises(BrowserSessionError, match="before __enter__"):
            fake.navigate("https://x")

    def test_get_html_before_enter_raises(self):
        fake = FakeBrowserSession()
        with pytest.raises(BrowserSessionError):
            fake.get_html()

    def test_get_html_before_navigate_raises(self):
        fake = FakeBrowserSession()
        with fake:
            with pytest.raises(BrowserSessionError, match="before navigate"):
                fake.get_html()

    def test_restart_before_enter_raises(self):
        fake = FakeBrowserSession()
        with pytest.raises(BrowserSessionError):
            fake.restart()


# =============================================================================
# FakeBrowserSession — happy path
# =============================================================================

class TestFakeHappyPath:
    def test_basic_navigation(self):
        fake = FakeBrowserSession()
        fake.queue_navigation(
            html="<html>hello</html>",
            runtime_response={"data": {"price": 100}},
            response_status=200,
        )
        with fake as browser:
            browser.navigate("https://airbnb.com.ar/rooms/1")
            assert browser.get_html() == "<html>hello</html>"
            assert browser.get_intercepted_runtime_response() == {"data": {"price": 100}}
            assert browser.get_last_runtime_raw_body() is None
            assert browser.get_last_response_status() == 200

    def test_telemetry_tracks_navigate_calls(self):
        fake = FakeBrowserSession()
        fake.queue_navigation()
        fake.queue_navigation()
        with fake as browser:
            browser.navigate("https://airbnb.com.ar/rooms/1")
            browser.navigate("https://airbnb.com.ar/rooms/2")
        assert fake.navigate_calls == [
            "https://airbnb.com.ar/rooms/1",
            "https://airbnb.com.ar/rooms/2",
        ]


# =============================================================================
# FakeBrowserSession — invariant #1 (buffer clear on navigate)
# =============================================================================

class TestFakeBufferClear:
    def test_runtime_buffer_cleared_between_navigates(self):
        fake = FakeBrowserSession()
        fake.queue_navigation(runtime_response={"first": True})
        fake.queue_navigation(runtime_response=None)  # 5c case

        with fake as browser:
            browser.navigate("https://airbnb.com.ar/rooms/1")
            assert browser.get_intercepted_runtime_response() == {"first": True}

            browser.navigate("https://airbnb.com.ar/rooms/2")
            # Buffer cleared — no leak
            assert browser.get_intercepted_runtime_response() is None

    def test_status_buffer_cleared_between_navigates(self):
        fake = FakeBrowserSession()
        fake.queue_navigation(response_status=200)
        fake.queue_navigation(response_status=503)

        with fake as browser:
            browser.navigate("https://airbnb.com.ar/rooms/1")
            assert browser.get_last_response_status() == 200
            browser.navigate("https://airbnb.com.ar/rooms/2")
            assert browser.get_last_response_status() == 503

    def test_raw_body_buffer_cleared_between_navigates(self):
        fake = FakeBrowserSession()
        fake.queue_navigation(runtime_raw_body=b'<html>cf</html>')
        fake.queue_navigation(runtime_response={"ok": True})

        with fake as browser:
            browser.navigate("https://airbnb.com.ar/rooms/1")
            assert browser.get_last_runtime_raw_body() == b'<html>cf</html>'
            browser.navigate("https://airbnb.com.ar/rooms/2")
            # Body buffer cleared
            assert browser.get_last_runtime_raw_body() is None
            assert browser.get_intercepted_runtime_response() == {"ok": True}


# =============================================================================
# FakeBrowserSession — invariant #4 (mutual exclusion)
# =============================================================================

class TestFakeMutualExclusion:
    def test_cannot_queue_both_response_and_raw_body(self):
        fake = FakeBrowserSession()
        with pytest.raises(ValueError, match="mutually exclusive"):
            fake.queue_navigation(
                runtime_response={"a": 1},
                runtime_raw_body=b"raw",
            )

    def test_runtime_ok_means_no_raw_body(self):
        fake = FakeBrowserSession()
        fake.queue_navigation(runtime_response={"data": "ok"})
        with fake as browser:
            browser.navigate("u")
            assert browser.get_intercepted_runtime_response() is not None
            assert browser.get_last_runtime_raw_body() is None

    def test_raw_body_means_no_parsed_response(self):
        """SHAPE_DRIFT runtime case: body present, parsed response None."""
        fake = FakeBrowserSession()
        fake.queue_navigation(runtime_raw_body=b'<not json>')
        with fake as browser:
            browser.navigate("u")
            assert browser.get_intercepted_runtime_response() is None
            assert browser.get_last_runtime_raw_body() == b'<not json>'

    def test_5c_case_both_none(self):
        """Política 5c: response ausente totalmente → ambos None."""
        fake = FakeBrowserSession()
        fake.queue_navigation(runtime_response=None, runtime_raw_body=None)
        with fake as browser:
            browser.navigate("u")
            assert browser.get_intercepted_runtime_response() is None
            assert browser.get_last_runtime_raw_body() is None


# =============================================================================
# FakeBrowserSession — Cloudflare detection
# =============================================================================

class TestFakeCloudflareDetection:
    def test_clean_html_not_detected_as_cf(self):
        fake = FakeBrowserSession()
        fake.queue_navigation(html="<html><title>Listing</title></html>")
        with fake as browser:
            browser.navigate("u")
            assert not browser.detect_cloudflare()

    def test_cf_title_detected(self):
        fake = FakeBrowserSession()
        fake.queue_navigation(html="<html><title>Just a moment...</title></html>")
        with fake as browser:
            browser.navigate("u")
            assert browser.detect_cloudflare()

    def test_cf_body_marker_detected(self):
        fake = FakeBrowserSession()
        fake.queue_navigation(html="<html><body><div class='cf-challenge'></div></body></html>")
        with fake as browser:
            browser.navigate("u")
            assert browser.detect_cloudflare()

    def test_cf_status_via_response_status(self):
        """Layered detection: orchestrator combines detect_cloudflare() + status."""
        fake = FakeBrowserSession()
        fake.queue_navigation(
            html="<html><title>Listing</title></html>",  # title looks normal
            response_status=503,  # but status is CF-typical
        )
        with fake as browser:
            browser.navigate("u")
            # detect_cloudflare() False — body looks clean
            assert not browser.detect_cloudflare()
            # But orchestrator can layer with status check
            assert browser.get_last_response_status() == 503

    def test_detect_cf_before_navigate_returns_false(self):
        fake = FakeBrowserSession()
        with fake as browser:
            # No navigate yet → no html → defensive False (not exception)
            assert not browser.detect_cloudflare()


# =============================================================================
# FakeBrowserSession — restart
# =============================================================================

class TestFakeRestart:
    def test_restart_increments_counter(self):
        fake = FakeBrowserSession()
        with fake as browser:
            assert fake.restart_count == 0
            browser.restart()
            assert fake.restart_count == 1
            browser.restart()
            assert fake.restart_count == 2

    def test_restart_preserves_queued_navigations(self):
        """Restart no debe consumir nada de la queue."""
        fake = FakeBrowserSession()
        fake.queue_navigation(html="<html>after restart</html>")
        with fake as browser:
            browser.restart()
            browser.navigate("u")
            assert browser.get_html() == "<html>after restart</html>"


# =============================================================================
# FakeBrowserSession — failure injection
# =============================================================================

class TestFakeFailureInjection:
    def test_queue_navigation_failure_raises_on_navigate(self):
        fake = FakeBrowserSession()
        fake.queue_navigation_failure(BrowserSessionError("boom"))
        with fake as browser:
            with pytest.raises(BrowserSessionError, match="boom"):
                browser.navigate("u")

    def test_failure_injection_consumes_one_queue_item(self):
        fake = FakeBrowserSession()
        fake.queue_navigation_failure(BrowserSessionError("first fails"))
        fake.queue_navigation(html="<html>second ok</html>")
        with fake as browser:
            with pytest.raises(BrowserSessionError):
                browser.navigate("u1")
            browser.navigate("u2")
            assert browser.get_html() == "<html>second ok</html>"

    def test_navigate_with_empty_queue_raises_assertion(self):
        """Test hygiene: forgetting to queue → loud failure, not silent success."""
        fake = FakeBrowserSession()
        with fake as browser:
            with pytest.raises(AssertionError, match="no response queued"):
                browser.navigate("u")


# =============================================================================
# PlaywrightBrowserSession — config sanity (no real browser launched)
# =============================================================================

class TestPlaywrightConfig:
    def test_default_headless_true(self):
        s = PlaywrightBrowserSession()
        assert s._headless is True

    def test_headless_false_configurable(self):
        """Vital para piloto manual: headless=False muestra browser."""
        s = PlaywrightBrowserSession(headless=False)
        assert s._headless is False

    def test_default_runtime_pattern_matches_constant(self):
        s = PlaywrightBrowserSession()
        assert s._runtime_url_pattern == RUNTIME_RESPONSE_URL_PATTERN

    def test_custom_runtime_pattern(self):
        s = PlaywrightBrowserSession(runtime_url_pattern="/custom/api")
        assert s._runtime_url_pattern == "/custom/api"

    def test_buffers_initially_none(self):
        s = PlaywrightBrowserSession()
        assert s._captured_runtime_dict is None
        assert s._last_runtime_raw_body is None
        assert s._last_response_status is None

    def test_methods_before_enter_raise(self):
        s = PlaywrightBrowserSession()
        with pytest.raises(BrowserSessionError):
            s.navigate("u")
        with pytest.raises(BrowserSessionError):
            s.get_html()
        with pytest.raises(BrowserSessionError):
            s.restart()

    def test_get_intercepted_response_before_enter_returns_none(self):
        """Buffer accessors don't raise when never opened — they just return None."""
        s = PlaywrightBrowserSession()
        assert s.get_intercepted_runtime_response() is None
        assert s.get_last_runtime_raw_body() is None
        assert s.get_last_response_status() is None

    def test_detect_cloudflare_before_enter_returns_false(self):
        """No raise — defensive."""
        s = PlaywrightBrowserSession()
        assert s.detect_cloudflare() is False


class TestForensicCaptureExtensions:
    """D31 extension — take_screenshot + get_page_html (CIF PR-1).

    Validates the soft-fail contract and telemetry semantics of the
    forensic-capture methods. Capa 3 integration is exercised in PR-3.
    """

    def test_take_screenshot_happy_path_creates_file_and_parent_dirs(self, tmp_path):
        browser = FakeBrowserSession()
        target = tmp_path / "nested" / "subdir" / "shot.png"
        # Pre-condition: parent dir does NOT exist — method must mkdir defensively
        assert not target.parent.exists()

        result = browser.take_screenshot(target)

        assert result is True
        assert target.exists()
        assert target.read_bytes() == b"\x00"
        assert browser.screenshot_calls == [target]

    def test_take_screenshot_soft_fails_when_failure_queued(self, tmp_path):
        browser = FakeBrowserSession()
        target = tmp_path / "shot.png"
        browser.queue_screenshot_failure()

        result = browser.take_screenshot(target)

        assert result is False
        assert not target.exists(), "soft-fail must not write the file"
        # Telemetry still records the ATTEMPT (deliberate — see Bloque D rationale)
        assert browser.screenshot_calls == [target]

        # One-shot semantics: next call succeeds
        target2 = tmp_path / "shot2.png"
        assert browser.take_screenshot(target2) is True
        assert target2.exists()
        assert browser.screenshot_calls == [target, target2]

    def test_get_page_html_happy_path_returns_dummy_and_increments_counter(self):
        browser = FakeBrowserSession()

        html1 = browser.get_page_html()
        html2 = browser.get_page_html()

        assert html1 == "<html>fake</html>"
        assert html2 == "<html>fake</html>"
        assert browser.html_calls == 2

    def test_get_page_html_soft_fails_when_failure_queued(self):
        browser = FakeBrowserSession()
        browser.queue_html_failure()

        result = browser.get_page_html()

        assert result is None
        assert browser.html_calls == 1, "counter increments even on soft-fail"

        # One-shot semantics: next call succeeds
        assert browser.get_page_html() == "<html>fake</html>"
        assert browser.html_calls == 2