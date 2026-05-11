"""
Forensic Manager — CIF PR-3 (D36).

Encapsulates the forensic archival matrix decided in D36. Per-outcome policy
determines what to capture; soft-fail semantics inherited from the BrowserSession
Protocol (PR-1 contract + PR-3 extensions for url/title).

INVARIANT: ForensicManager.capture() must be invoked while the BrowserSession
is still active. The Protocol's soft-fail contract handles closed sessions
gracefully, but capture attempts on a closed browser will record
`capture_attempts[*] = False` — semantically distinct from "not attempted"
(which manifests as a key absent from capture_attempts).

Forensic outcome (ForensicOutcome enum) is intentionally decoupled from the
business outcome on ScrapeResult. ERROR_POST_NAVIGATE vs ERROR_PRE_NAVIGATE is
a forensic concept only — the caller (_finalize) maps business → forensic via
classify_error_outcome() using timer.has("navigate") as the discriminator.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from src.scraper._timer_collector import TimerCollector


# ───────────────────────────────────────────────────────────────────────
# Public exceptions (raised by orchestrator, classified by classify_error_outcome)
# ───────────────────────────────────────────────────────────────────────
class CloudflareDetected(Exception):
    """Raised by the orchestrator when a Cloudflare challenge page is detected.

    Detection heuristics (URL contains '/cdn-cgi/challenge-platform/', title
    matches 'Just a moment...', etc.) live in the orchestrator, not here.
    """


class ShapeDriftError(Exception):
    """Raised when SSR/runtime parsing finds the page structure mutated.

    Distinct from a generic parse error: this means the page returned an
    unexpected SHAPE (missing keys, new nesting), which is research-relevant
    for documenting Airbnb's payload evolution over time.
    """


# ───────────────────────────────────────────────────────────────────────
# Forensic outcome enum
# ───────────────────────────────────────────────────────────────────────
class ForensicOutcome(str, Enum):
    """Forensic categorization of a scrape result.

    Inherits from str so it serializes cleanly to JSONL without custom encoders.
    """

    SUCCESS = "SUCCESS"
    METADATA_ONLY = "METADATA_ONLY"
    SHAPE_DRIFT = "SHAPE_DRIFT"
    CLOUDFLARE = "CLOUDFLARE"
    ERROR_POST_NAVIGATE = "ERROR_POST_NAVIGATE"
    ERROR_PRE_NAVIGATE = "ERROR_PRE_NAVIGATE"
    SKIPPED = "SKIPPED"


# ───────────────────────────────────────────────────────────────────────
# Result returned by ForensicManager.capture()
# ───────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ForensicCaptureResult:
    """Result of a forensic capture attempt.

    Attributes
    ----------
    evidence_paths : dict[str, str]
        Map of evidence kind → absolute filesystem path. Keys are a subset of
        {"runtime_payload", "ssr_html", "screenshot"}. Only present when the
        corresponding artifact was successfully written. Empty dict when nothing
        was archived (not None — distinguishable from "manager not called").

    page_state : dict[str, str | None] | None
        - None if the outcome's policy doesn't require page state capture.
        - dict with keys {"url", "title"} otherwise. Inner values may be None
          on soft-fail of the individual capture (asymmetry preserved as
          forensic evidence per D36).

    capture_attempts : dict[str, bool]
        Tri-state diagnostic record. Per the convention validated by Iñaki:
        - key absent       → not attempted (policy says "doesn't apply")
        - value True       → attempted, succeeded (fully or partially)
        - value False      → attempted, total failure (soft-fail or precondition unmet)

        Possible keys: "runtime_payload", "ssr_html", "screenshot", "page_state".
        Not serialized verbatim to JSONL (operational, not forensic) — caller
        decides what to log.
    """

    evidence_paths: dict[str, str]
    page_state: Optional[dict[str, Optional[str]]]
    capture_attempts: dict[str, bool]


# ───────────────────────────────────────────────────────────────────────
# Internal: per-outcome policy
# ───────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _OutcomePolicy:
    """Internal capture policy for one ForensicOutcome.

    Drives the imperative logic in ForensicManager.capture() declaratively, so
    adding a new outcome means adding a row to _POLICIES — no branching in capture().
    """

    archive_runtime: bool
    archive_html: bool
    archive_screenshot: bool
    capture_page_state: bool
    use_diagnostic_subdir: bool
    filename_suffix: Optional[str]  # None ⇔ root archive_dir (no suffix)


_POLICIES: dict[ForensicOutcome, _OutcomePolicy] = {
    ForensicOutcome.SUCCESS: _OutcomePolicy(
        archive_runtime=True,
        archive_html=False,
        archive_screenshot=False,
        capture_page_state=False,
        use_diagnostic_subdir=False,
        filename_suffix=None,
    ),
    ForensicOutcome.METADATA_ONLY: _OutcomePolicy(
        archive_runtime=True,
        archive_html=False,
        archive_screenshot=False,
        capture_page_state=False,
        use_diagnostic_subdir=False,
        filename_suffix=None,
    ),
    ForensicOutcome.SHAPE_DRIFT: _OutcomePolicy(
        archive_runtime=True,
        archive_html=True,
        archive_screenshot=True,
        capture_page_state=False,  # mutation, not a challenge — no page_state needed
        use_diagnostic_subdir=True,
        filename_suffix="SHAPE_DRIFT",
    ),
    ForensicOutcome.CLOUDFLARE: _OutcomePolicy(
        archive_runtime=False,
        archive_html=False,
        archive_screenshot=True,
        capture_page_state=True,
        use_diagnostic_subdir=True,
        filename_suffix="CLOUDFLARE",
    ),
    ForensicOutcome.ERROR_POST_NAVIGATE: _OutcomePolicy(
        archive_runtime=True,
        archive_html=True,
        archive_screenshot=True,
        capture_page_state=True,
        use_diagnostic_subdir=True,
        filename_suffix="ERROR",  # shorter than enum value, grep-friendly
    ),
    ForensicOutcome.ERROR_PRE_NAVIGATE: _OutcomePolicy(
        archive_runtime=False,
        archive_html=False,
        archive_screenshot=False,
        capture_page_state=False,
        use_diagnostic_subdir=False,
        filename_suffix=None,
    ),
    ForensicOutcome.SKIPPED: _OutcomePolicy(
        archive_runtime=False,
        archive_html=False,
        archive_screenshot=False,
        capture_page_state=False,
        use_diagnostic_subdir=False,
        filename_suffix=None,
    ),
}


# ───────────────────────────────────────────────────────────────────────
# Browser capture surface (structural typing for testability)
# ───────────────────────────────────────────────────────────────────────
class _BrowserCaptureProtocol(Protocol):
    """Minimal surface of BrowserSession that ForensicManager depends on.

    Defined here (not imported from airbnb_scraper) to avoid coupling this module
    to the orchestrator. The real BrowserSession Protocol in airbnb_scraper.py
    satisfies this implicitly via duck typing.
    """

    def take_screenshot(self, path: Path) -> bool: ...
    def get_page_html(self) -> Optional[str]: ...
    def get_current_url(self) -> Optional[str]: ...
    def get_page_title(self) -> Optional[str]: ...


# ───────────────────────────────────────────────────────────────────────
# Error classifier
# ───────────────────────────────────────────────────────────────────────
def classify_error_outcome(
    timer: "TimerCollector",
    error: Exception,
) -> ForensicOutcome:
    """Map an in-flight error to a ForensicOutcome.

    Discriminator: timer.has("navigate") tells us whether the network was
    touched. If not, we report ERROR_PRE_NAVIGATE regardless of exception type
    — a CloudflareDetected without a navigate step is a malformed state
    (you cannot detect a challenge page you never opened), and we'd rather
    classify it as pre-navigate than fabricate a CLOUDFLARE outcome from
    inconsistent state.

    Priority order:
        1. No navigate step      → ERROR_PRE_NAVIGATE  (highest priority — gate)
        2. CloudflareDetected    → CLOUDFLARE
        3. ShapeDriftError       → SHAPE_DRIFT
        4. anything else         → ERROR_POST_NAVIGATE (fallback)
    """
    if not timer.has("navigate"):
        return ForensicOutcome.ERROR_PRE_NAVIGATE
    if isinstance(error, CloudflareDetected):
        return ForensicOutcome.CLOUDFLARE
    if isinstance(error, ShapeDriftError):
        return ForensicOutcome.SHAPE_DRIFT
    return ForensicOutcome.ERROR_POST_NAVIGATE


# ───────────────────────────────────────────────────────────────────────
# Main class
# ───────────────────────────────────────────────────────────────────────
class ForensicManager:
    """Encapsulates the D36 forensic archival matrix.

    One instance per AirbnbScraper (shared-nothing per concurrency policy).
    Cheap to construct (no I/O at __init__).

    Parameters
    ----------
    archive_dir : Path
        Root archive directory. SUCCESS / METADATA_ONLY land here directly.
    diagnostic_subdir : str, default "_diagnostic"
        Name of the subdirectory for diagnostic outcomes (SHAPE_DRIFT, CLOUDFLARE,
        ERROR_POST_NAVIGATE). Created lazily on first write.
    browser : _BrowserCaptureProtocol | None, default None
        Browser session for visual captures. Optional: outcomes that don't
        need visual capture (SUCCESS, METADATA_ONLY, SKIPPED, ERROR_PRE_NAVIGATE)
        work without a browser. Outcomes that DO need it will degrade gracefully
        (capture_attempts records False) if browser is None.
    """

    def __init__(
        self,
        archive_dir: Path,
        diagnostic_subdir: str = "_diagnostic",
        browser: Optional[_BrowserCaptureProtocol] = None,
    ) -> None:
        self._archive_dir = Path(archive_dir)
        self._diagnostic_subdir = diagnostic_subdir
        self._browser = browser

    def capture(
        self,
        *,
        outcome: ForensicOutcome,
        listing_id: str,
        ts_iso: str,
        runtime_payload: Optional[dict] = None,
        ssr_html: Optional[str] = None,
    ) -> ForensicCaptureResult:
        """Execute the capture policy for the given outcome.

        Returns ForensicCaptureResult with evidence_paths (only successful
        writes), page_state (or None if N/A), and capture_attempts (tri-state).
        """
        policy = _POLICIES[outcome]
        evidence_paths: dict[str, str] = {}
        capture_attempts: dict[str, bool] = {}
        page_state: Optional[dict[str, Optional[str]]] = None

        # 1. Runtime payload (JSON)
        if policy.archive_runtime:
            ok = self._try_write_runtime(
                runtime_payload, listing_id, ts_iso, policy, evidence_paths
            )
            capture_attempts["runtime_payload"] = ok

        # 2. SSR HTML
        if policy.archive_html:
            ok = self._try_write_html(
                ssr_html, listing_id, ts_iso, policy, evidence_paths
            )
            capture_attempts["ssr_html"] = ok

        # 3. Screenshot (browser-mediated)
        if policy.archive_screenshot:
            ok = self._try_capture_screenshot(
                listing_id, ts_iso, policy, evidence_paths
            )
            capture_attempts["screenshot"] = ok

        # 4. Page state (browser-mediated, url + title)
        if policy.capture_page_state:
            if self._browser is None:
                capture_attempts["page_state"] = False
            else:
                # Both calls are soft-fail by Protocol contract — return None on failure.
                # Asymmetry (url succeeds, title fails OR vice versa) is preserved in
                # the dict as forensic evidence; the attempt itself counts as True.
                url = self._browser.get_current_url()
                title = self._browser.get_page_title()
                page_state = {"url": url, "title": title}
                capture_attempts["page_state"] = True

        return ForensicCaptureResult(
            evidence_paths=evidence_paths,
            page_state=page_state,
            capture_attempts=capture_attempts,
        )

    # ────────────────────────────────────────────────────────────────
    # Internal write helpers (mutate evidence_paths on success, return ok bool)
    # ────────────────────────────────────────────────────────────────
    def _try_write_runtime(
        self,
        payload: Optional[dict],
        listing_id: str,
        ts_iso: str,
        policy: _OutcomePolicy,
        evidence_paths: dict[str, str],
    ) -> bool:
        if payload is None:
            return False
        try:
            path = self._build_path(listing_id, ts_iso, policy, "json")
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            return False
        evidence_paths["runtime_payload"] = str(path)
        return True

    def _try_write_html(
        self,
        html: Optional[str],
        listing_id: str,
        ts_iso: str,
        policy: _OutcomePolicy,
        evidence_paths: dict[str, str],
    ) -> bool:
        if html is None:
            return False
        try:
            path = self._build_path(listing_id, ts_iso, policy, "html")
            path.write_text(html, encoding="utf-8")
        except OSError:
            return False
        evidence_paths["ssr_html"] = str(path)
        return True

    def _try_capture_screenshot(
        self,
        listing_id: str,
        ts_iso: str,
        policy: _OutcomePolicy,
        evidence_paths: dict[str, str],
    ) -> bool:
        if self._browser is None:
            return False
        path = self._build_path(listing_id, ts_iso, policy, "png")
        # take_screenshot is soft-fail by Protocol contract (PR-1):
        # returns bool, never raises for known failure modes.
        if not self._browser.take_screenshot(path):
            return False
        # Defensive check: even if take_screenshot returned True, verify the file
        # actually materialized. Prevents false-positives if a fake/mock lies.
        if not path.exists():
            return False
        evidence_paths["screenshot"] = str(path)
        return True

    def _build_path(
        self,
        listing_id: str,
        ts_iso: str,
        policy: _OutcomePolicy,
        ext: str,
    ) -> Path:
        """Build the destination path according to policy, creating parent dirs.

        SUCCESS / METADATA_ONLY (no diagnostic_subdir, no suffix):
            archive_dir/{listing_id}_{ts_iso}.{ext}

        Diagnostic outcomes (SHAPE_DRIFT, CLOUDFLARE, ERROR_POST_NAVIGATE):
            archive_dir/_diagnostic/{listing_id}_{ts_iso}_{SUFFIX}.{ext}
        """
        if policy.use_diagnostic_subdir:
            assert policy.filename_suffix is not None, (
                "Internal invariant violated: policy with diagnostic_subdir "
                "must define a filename_suffix"
            )
            parent = self._archive_dir / self._diagnostic_subdir
            filename = f"{listing_id}_{ts_iso}_{policy.filename_suffix}.{ext}"
        else:
            parent = self._archive_dir
            filename = f"{listing_id}_{ts_iso}.{ext}"
        parent.mkdir(parents=True, exist_ok=True)
        return parent / filename