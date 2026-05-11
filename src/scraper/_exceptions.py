"""
Centralized exception hierarchy for the scraper subsystem.

Rationale (Commit 1 cleanup, pre-PR-3):
    Defining exceptions inline in `airbnb_scraper.py` mixed concerns: the
    orchestrator imports both its own raised exceptions and its callers'
    expected exceptions from the same module. Centralizing the hierarchy in
    a dedicated module follows the convention used by SQLAlchemy, requests,
    Flask, and FastAPI, and decouples the exception names from any single
    consumer.

Backwards compatibility:
    `airbnb_scraper.py` re-exports the four classes via top-level import, so
    existing call-sites (tests, scripts, downstream modules) that import
    `BatchAbortError` and friends from `src.scraper.airbnb_scraper` keep
    working unchanged. The `issubclass(X, ScraperError)` assertions in
    `tests/test_airbnb_scraper_helpers.py` evaluate against the same class
    object regardless of import path.

Hierarchy:
    ScraperError                      (base — no instances raised directly)
      ├── BatchAbortError             (D29 circuit breaker)
      ├── CloudflareDetectedError     (CF challenge detected on PDP)
      └── BrowserSessionError         (unrecoverable browser-level failure)

Future additions (PR-3 and beyond):
    - ShapeDriftError will be added in Commit 2 alongside the ForensicManager
      implementation that uses it. Not added preemptively here to keep this
      commit a pure refactor with no behavior change.
"""

from __future__ import annotations


class ScraperError(Exception):
    """Base exception for all scraper-level failures.

    Never raised directly — only as a base class for the typed exceptions
    below. Callers wishing to catch any scraper error should `except
    ScraperError`, but never `raise ScraperError(...)`.
    """


class BatchAbortError(ScraperError):
    """Raised when MAX_CONSECUTIVE_SHAPE_DRIFT is reached (D29 circuit breaker).

    Signals that the SSR/runtime payload has likely undergone a structural
    mutation in production (Airbnb deployed a new schema), and continuing
    the batch without manual review would archive payloads under a parser
    that no longer matches their shape. The batch driver
    (`scripts/run_scraper_batch.py`) catches this exception, persists the
    current progress, and exits with a non-zero status for operator review.
    """


class CloudflareDetectedError(ScraperError):
    """Raised when a Cloudflare challenge is detected on a PDP navigation.

    Detection combines HTML body sniffing (`_detect_cloudflare_in_html`) and
    HTTP status code matching (`_is_cloudflare_status`). Currently not
    raised by the orchestrator (CF detection produces a `ScrapeOutcome.CLOUDFLARE`
    via the result type, not an exception), but the class is kept available
    for callers (e.g., future MCP integrations or batch wrappers) that
    prefer exception-based flow control.
    """


class BrowserSessionError(ScraperError):
    """Raised on unrecoverable browser-level failures.

    Covers: navigation timeouts, lifecycle violations (e.g. calling
    `navigate()` before `__enter__`), and Playwright internal errors
    propagated from `goto()`. The orchestrator catches this in
    `scrape_listing` (step 4: Navigate) and downgrades the outcome to
    `ScrapeOutcome.ERROR` without aborting the batch.
    """