"""
Airbnb scraper orchestrator.

Orquesta el ciclo completo de un scrape de listing:
  1. Dedup check (skip <72hs).
  2. Build URL via url_builder.
  3. Browser navigation con response interceptor pre-registrado.
  4. Extract SSR payload (HTML → dict via _ssr_extractor).
  5. Parse SSR (parse_payload v2).
  6. Capture runtime GraphQL response (StaysPdpSections).
  7. Parse runtime (parse_runtime_response).
  8. SHA-256 hash + raw archive (forense, hash sobre payload completo).
  9. Two independent transactions:
        - TX-A: upsert anfitrion + upsert inmueble (always, if SSR OK).
        - TX-B: insert_precio (only if runtime OK + price_extractor available).
 10. JSONL log of the whole event.
 11. Rate-limit sleep (lognormal truncated, mean=12s, std=4s, support [6,25]).
 12. Browser cycle every 50 listings (proactive, D27).

Política 5c (D28): SSR OK pero runtime ausente o PARTIAL → upsert metadata sin row
en precios_historicos. Señal económica válida (ej: listing sin disponibilidad).

Política abort batch (D29): 5 SHAPE_DRIFTs consecutivos → BatchAbortError.

Phase 2.3 vs Phase 3:
- Phase 2.3 (actual): price_extractor=None. NO se llama a insert_precio. Se
  archivan los payloads runtime crudos para construir extract_price_components.py
  con corpus real (≥10 fixtures).
- Phase 3: price_extractor real → TX-B activa.

D24 — obtener_mep() se llama en TODOS los scrapes exitosos (incluso Phase 2.3)
para poblar fx_diaria día a día. Vital para backfill posterior.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from contextlib import ExitStack
from typing import TYPE_CHECKING
from decimal import Decimal

from src.scraper._ssr_extractor import extract_ssr_payload, SSRExtractionError
from src.scraper.parse_payload import parse_payload
from src.scraper.parse_runtime_response import parse_runtime_response
from src.scraper.url_builder import build_pdp_url
from src.db.db_client import DBClient

if TYPE_CHECKING:
    # Type-only imports para evitar dependencia hard de Playwright en
    # entornos donde solo se usa FakeBrowserSession (CI rápida, etc).
    from playwright.sync_api import BrowserContext, Page, Response
# ==========================================
# =============================================================================
# Constantes
# =============================================================================

# Browser cycle (D27 — proactive)
BROWSER_CYCLE_EVERY_N_LISTINGS: int = 50
BROWSER_RESTART_COOLDOWN_SEC_RANGE: tuple[float, float] = (30.0, 90.0)

# Rate limiting (lognormal truncated, MEAN/STD of the variable itself, not of the log)
RATE_LIMIT_MEAN: float = 12.0
RATE_LIMIT_STD: float = 4.0
RATE_LIMIT_TRUNCATE_LO: float = 6.0
RATE_LIMIT_TRUNCATE_HI: float = 25.0
# Conversion to lognormal underlying-normal params:
#   E[X]  = exp(mu + sigma^2/2)
#   Var[X] = (exp(sigma^2) - 1) * exp(2*mu + sigma^2)
# Solving for (mu, sigma) given (mean, std):
#   sigma^2 = ln(1 + (std/mean)^2)
#   mu      = ln(mean) - sigma^2 / 2
_VAR_LOG = math.log(1.0 + (RATE_LIMIT_STD / RATE_LIMIT_MEAN) ** 2)
RATE_LIMIT_SIGMA_LOG: float = math.sqrt(_VAR_LOG)
RATE_LIMIT_MU_LOG: float = math.log(RATE_LIMIT_MEAN) - _VAR_LOG / 2.0
# Safety guard: en la práctica con μ_log≈2.43, σ_log≈0.324, P(X ∈ [6,25]) > 0.85,
# así que el rejection sampling converge en 1-3 intentos.
RATE_LIMIT_MAX_RESAMPLE_ATTEMPTS: int = 100

# SHAPE_DRIFT abort threshold (D29)
MAX_CONSECUTIVE_SHAPE_DRIFT: int = 5

# Network
RUNTIME_RESPONSE_URL_PATTERN: str = "/api/v3/StaysPdpSections"
NAVIGATION_TIMEOUT_MS: int = 30_000
NETWORKIDLE_TIMEOUT_MS: int = 10_000
CLOUDFLARE_HTTP_STATUS_CODES: frozenset[int] = frozenset({503, 403, 429})

# Cloudflare detection heuristics (HTML body sniffing)
CLOUDFLARE_TITLE_MARKERS: tuple[str, ...] = (
    "Just a moment",
    "Attention Required",
    "Please Wait",
)
CLOUDFLARE_BODY_MARKERS: tuple[str, ...] = (
    "cf-challenge",
    "cdn-cgi/challenge-platform",
    "Checking your browser",
)
CLOUDFLARE_BODY_SCAN_PREFIX_BYTES: int = 5000

# Filesystem
RAW_PAYLOADS_DIR: Path = Path("data/raw/airbnb_payloads")
LOGS_DIR: Path = Path("logs")
BROWSER_PROFILE_DIR: Path = Path("data/browser_profile")

# Defaults
DEFAULT_FUENTE_SCRAPER: str = "airbnb_scraper_v1"
DEFAULT_PLATAFORMA: str = "airbnb"
DEFAULT_PAIS: str = "Argentina"
DEFAULT_CIUDAD: str = "CABA"


# =============================================================================
# Custom exceptions
# =============================================================================

class ScraperError(Exception):
    """Base exception for orchestrator failures."""


class BatchAbortError(ScraperError):
    """Raised when MAX_CONSECUTIVE_SHAPE_DRIFT is reached (D29 circuit breaker)."""


class CloudflareDetectedError(ScraperError):
    """Raised when CF challenge is detected on a PDP navigation."""


class BrowserSessionError(ScraperError):
    """Raised on unrecoverable browser-level failures."""


# =============================================================================
# Result types
# =============================================================================

class ScrapeOutcome(str, Enum):
    """
    Outcomes of a single listing scrape attempt.

    SUCCESS         → SSR OK + runtime OK (price archived; insert_precio if Phase 3)
    METADATA_ONLY   → SSR OK + runtime ausente o PARTIAL → política 5c (D28)
    SKIPPED         → dedup hit (<72hs)
    SHAPE_DRIFT     → SSR no extraíble, parse_status SHAPE_DRIFT, o runtime SHAPE_DRIFT
    CLOUDFLARE      → CF detectado, sin scrape posible
    ERROR           → cualquier otra excepción no recuperable
    """
    SUCCESS = "SUCCESS"
    METADATA_ONLY = "METADATA_ONLY"
    SKIPPED = "SKIPPED"
    SHAPE_DRIFT = "SHAPE_DRIFT"
    CLOUDFLARE = "CLOUDFLARE"
    ERROR = "ERROR"


@dataclass(frozen=True)
class ScrapeResult:
    """Immutable result of one scrape_listing() call."""
    listing_id: str
    outcome: ScrapeOutcome
    duration_ms: int
    url: Optional[str] = None
    ssr_parse_status: Optional[str] = None
    runtime_parse_status: Optional[str] = None
    inmueble_id: Optional[int] = None
    raw_payload_hash: Optional[str] = None
    mep_rate: Optional[str] = None  # Decimal as str for JSON serialization
    cloudflare_detected: bool = False
    error_class: Optional[str] = None
    missing_fields: list[str] = field(default_factory=list)

# =============================================================================
# JSONL logger
# =============================================================================

class JSONLLogger:
    """
    Append-only JSONL logger. One file per scraper run, named by start date.
    Inyectable en AirbnbScraper para que tests usen un FakeLogger.

    Schema mínimo defendible (cada línea):
      {
        "ts": ISO-8601 UTC,
        "listing_id": str,
        "url": str | null,
        "outcome": str (ScrapeOutcome value),
        "ssr_parse_status": str | null,
        "runtime_parse_status": str | null,
        "decision_5c": bool,
        "mep_rate": str | null,
        "raw_payload_hash": str | null,
        "duration_ms": int,
        "cloudflare_detected": bool,
        "error_class": str | null,
        "missing_fields": list[str]
      }
    """

    def __init__(
        self,
        log_dir: Path = LOGS_DIR,
        run_date: Optional[datetime] = None,
    ) -> None:
        if run_date is None:
            run_date = datetime.now(timezone.utc)
        elif run_date.tzinfo is None:
            raise ValueError("run_date must be tz-aware (use datetime.now(timezone.utc))")

        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        filename = f"scraper_{run_date.strftime('%Y%m%d')}.jsonl"
        self._path: Path = self._log_dir / filename

    @property
    def path(self) -> Path:
        return self._path

    def log(self, event: dict[str, Any]) -> None:
        """Append one event as a single JSON line. Atomic at the line level via O_APPEND."""
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


# =============================================================================
# Pure helpers (modular, testeable sin browser ni DB)
# =============================================================================

def _hash_payloads(
    ssr_dict: dict[str, Any],
    runtime_dict: Optional[dict[str, Any]],
) -> str:
    """
    SHA-256 over canonical JSON of (ssr || runtime). Forense estricto:
    todo el contenido entra al hash. Si Airbnb agrega timestamps internos,
    el hash cambia — eso es deseado para auditoría.

    Canonical = sort_keys=True, ensure_ascii=False, no whitespace.
    """
    canonical = json.dumps(
        {"ssr": ssr_dict, "runtime": runtime_dict},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sample_rate_limit_sleep(rng: random.Random) -> float:
    """
    Truncated lognormal sampling via rejection sampling.

    Distribution: X ~ LogNormal(mu=RATE_LIMIT_MU_LOG, sigma=RATE_LIMIT_SIGMA_LOG)
                  truncated to [RATE_LIMIT_TRUNCATE_LO, RATE_LIMIT_TRUNCATE_HI].

    Resamples until X ∈ [LO, HI]. With (mean=12, std=4, support=[6,25]),
    P(X ∈ support) > 0.85, so ≤3 attempts in practice.

    Raises:
        RuntimeError: if RATE_LIMIT_MAX_RESAMPLE_ATTEMPTS exceeded (should be unreachable).
    """
    for _ in range(RATE_LIMIT_MAX_RESAMPLE_ATTEMPTS):
        sample = rng.lognormvariate(RATE_LIMIT_MU_LOG, RATE_LIMIT_SIGMA_LOG)
        if RATE_LIMIT_TRUNCATE_LO <= sample <= RATE_LIMIT_TRUNCATE_HI:
            return sample
    raise RuntimeError(
        f"Rejection sampling failed after {RATE_LIMIT_MAX_RESAMPLE_ATTEMPTS} attempts. "
        "Check RATE_LIMIT_* parameters."
    )


def _archive_payload(
    archive_dir: Path,
    listing_id: str,
    scraped_at: datetime,
    url: str,
    ssr_dict: dict[str, Any],
    runtime_dict: Optional[dict[str, Any]],
    raw_hash: str,
    fuente_scraper: str = DEFAULT_FUENTE_SCRAPER,
) -> Path:
    """
    Archive raw payloads atomically.

    Path: {archive_dir}/{listing_id}_{ISO_ts}.json
    ISO_ts: %Y%m%dT%H%M%SZ (Windows-friendly, no `:`).

    Atomic write: tmp file + os.replace.

    Raises:
        ValueError: if scraped_at is naive (must be tz-aware).
        TypeError: if listing_id contains path separators or is empty.
    """
    if scraped_at.tzinfo is None:
        raise ValueError("scraped_at must be tz-aware")
    if not listing_id or "/" in listing_id or "\\" in listing_id:
        raise ValueError(f"Invalid listing_id for filename: {listing_id!r}")

    archive_dir.mkdir(parents=True, exist_ok=True)
    iso_ts = scraped_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{listing_id}_{iso_ts}.json"
    final_path = archive_dir / filename
    tmp_path = archive_dir / (filename + ".tmp")

    document = {
        "listing_id": listing_id,
        "scraped_at": scraped_at.astimezone(timezone.utc).isoformat(),
        "url": url,
        "ssr_payload": ssr_dict,
        "runtime_payload": runtime_dict,
        "raw_payload_hash": raw_hash,
        "fuente_scraper": fuente_scraper,
    }

    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(document, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, final_path)
    return final_path


def _detect_cloudflare_in_html(html: str) -> bool:
    """
    Heuristic CF detection on HTML title + first N bytes of body.
    Layered with HTTP status check in the orchestrator (defense in depth).
    """
    if not html:
        return False
    # Cheap title extraction (avoids BS4 dependency).
    title_lower = ""
    title_start = html.find("<title")
    if title_start != -1:
        title_open_end = html.find(">", title_start)
        title_end = html.find("</title>", title_open_end)
        if title_open_end != -1 and title_end != -1:
            title_lower = html[title_open_end + 1:title_end].lower()

    if any(marker.lower() in title_lower for marker in CLOUDFLARE_TITLE_MARKERS):
        return True

    body_prefix = html[:CLOUDFLARE_BODY_SCAN_PREFIX_BYTES].lower()
    return any(marker.lower() in body_prefix for marker in CLOUDFLARE_BODY_MARKERS)


def _is_cloudflare_status(status: Optional[int]) -> bool:
    """Layer 2 of CF detection: HTTP status codes typical of CF challenges."""
    return status is not None and status in CLOUDFLARE_HTTP_STATUS_CODES

# === Agregar al final del archivo, después de _is_cloudflare_status ===

# =============================================================================
# Browser session — Protocol expandido
# =============================================================================
# (Esta sección REEMPLAZA el `class BrowserSession(Protocol)` skeleton de Entrega 1.
#  El Protocol original era un placeholder; este es el contrato definitivo.)


class _BrowserSessionContract:
    """
    NO-OP class. Sirve solo como anclaje de docstring para el contrato del
    Protocol. Documentar acá las invariantes que tanto PlaywrightBrowserSession
    como FakeBrowserSession deben respetar:

    INVARIANTES DEL CONTRATO:
    1. navigate(url) MUST clear any captured runtime response from previous calls
       BEFORE the network request fires. Esto evita leak entre listings.
    2. navigate(url) MUST register the response interceptor BEFORE the request
       fires (decisión técnica #1 sesión 09/05).
    3. get_intercepted_runtime_response() returns the FIRST response whose URL
       matches RUNTIME_RESPONSE_URL_PATTERN since last navigate(). Subsequent
       matching responses are dropped.
    4. get_last_runtime_raw_body() returns bytes if a matching response was
       captured but its body could NOT be parsed as JSON. Returns None if no
       matching response, OR if it was parsed successfully (then the parsed
       dict is in get_intercepted_runtime_response()).
       (Mutual exclusion: at most one of get_intercepted_runtime_response() and
        get_last_runtime_raw_body() returns non-None per navigate() call.)
    5. get_last_response_status() returns the HTTP status of the MAIN document
       response (the navigation itself), not of the runtime GraphQL response.
    6. detect_cloudflare() is a heuristic combining HTML title/body sniffing.
       Status-code-based detection is the orchestrator's responsibility (uses
       _is_cloudflare_status on get_last_response_status()).
    7. restart() reuses the SAME persistent profile dir (D27).
    """


# =============================================================================
# PlaywrightBrowserSession (production)
# =============================================================================

class PlaywrightBrowserSession:
    """
    Production BrowserSession backed by Playwright sync API + playwright-stealth 2.x.

    Lifecycle:
        with PlaywrightBrowserSession(headless=True) as browser:
            browser.navigate(url)
            html = browser.get_html()
            runtime = browser.get_intercepted_runtime_response()
            ...

    Stealth integration:
        Stealth().use_sync(sync_playwright()) → playwright instance with stealth
        scripts pre-injected at the context level. Then we launch_persistent_context
        with our profile dir, and Stealth's hook_playwright_context auto-applies
        evasion scripts to all pages in that context.
    """

    def __init__(
        self,
        profile_dir: Path = BROWSER_PROFILE_DIR,
        headless: bool = True,
        runtime_url_pattern: str = RUNTIME_RESPONSE_URL_PATTERN,
        navigation_timeout_ms: int = NAVIGATION_TIMEOUT_MS,
        networkidle_timeout_ms: int = NETWORKIDLE_TIMEOUT_MS,
    ) -> None:
        self._profile_dir = profile_dir
        self._headless = headless
        self._runtime_url_pattern = runtime_url_pattern
        self._navigation_timeout_ms = navigation_timeout_ms
        self._networkidle_timeout_ms = networkidle_timeout_ms

        # Lifecycle objects (None until __enter__)
        self._stack: Optional[ExitStack] = None
        self._playwright: Any = None  # playwright.sync_api.Playwright
        self._context: Optional["BrowserContext"] = None
        self._page: Optional["Page"] = None

        # Per-navigate buffers
        self._captured_runtime_dict: Optional[dict[str, Any]] = None
        self._last_runtime_raw_body: Optional[bytes] = None
        self._last_response_status: Optional[int] = None
        self._last_navigated_url: Optional[str] = None

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def __enter__(self) -> "PlaywrightBrowserSession":
        # Lazy import to keep playwright optional in test environments.
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth

        self._stack = ExitStack()
        try:
            # 1. Stealth + Playwright (nested context manager via ExitStack)
            stealth_ctx = Stealth().use_sync(sync_playwright())
            self._playwright = self._stack.enter_context(stealth_ctx)

            # 2. Persistent context (mismo profile dir en cada restart)
            self._profile_dir.mkdir(parents=True, exist_ok=True)
            self._context = self._stack.enter_context(
                self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self._profile_dir),
                    headless=self._headless,
                    # Locale + timezone consistentes con dominio .com.ar
                    locale="es-AR",
                    timezone_id="America/Argentina/Buenos_Aires",
                )
            )

            # 3. Single page reused across navigates
            self._page = self._context.new_page()

            # 4. Register handler for the lifetime of this page
            self._page.on("response", self._on_response)
            return self
        except Exception:
            # Cleanup parcial si algo falla mid-enter
            self._stack.close()
            self._stack = None
            raise

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._stack is not None:
            self._stack.close()
            self._stack = None
        self._page = None
        self._context = None
        self._playwright = None

    # -------------------------------------------------------------------------
    # BrowserSession API
    # -------------------------------------------------------------------------

    def navigate(self, url: str) -> None:
        """
        Navigate to URL. Clears per-navigate buffers BEFORE firing the request
        so the response handler captures only this navigation's responses.
        """
        if self._page is None:
            raise BrowserSessionError("navigate() called before __enter__")

        # 1. Clear buffers (invariant #1 + #4)
        self._captured_runtime_dict = None
        self._last_runtime_raw_body = None
        self._last_response_status = None
        self._last_navigated_url = url

        # 2. Navigate. wait_until="networkidle" → bloqueamos hasta que la red
        #    esté quieta. Si para entonces no llegó el StaysPdpSections, no
        #    va a llegar (decisión #3, sesión 09/05).
        try:
            response = self._page.goto(
                url,
                timeout=self._navigation_timeout_ms,
                wait_until="networkidle",
            )
            if response is not None:
                self._last_response_status = response.status
        except Exception as exc:
            raise BrowserSessionError(f"Navigation failed for {url!r}: {exc}") from exc

    def get_html(self) -> str:
        if self._page is None:
            raise BrowserSessionError("get_html() called before __enter__")
        return self._page.content()

    def get_intercepted_runtime_response(self) -> Optional[dict[str, Any]]:
        return self._captured_runtime_dict

    def get_last_runtime_raw_body(self) -> Optional[bytes]:
        """
        Body crudo de una response runtime que NO se pudo parsear como JSON.
        Mutuamente exclusivo con get_intercepted_runtime_response():
        si se parseó, esto es None. Si la response existe pero no es JSON,
        este devuelve los bytes y el otro devuelve None.

        Distinción metodológica (D29):
        - None + dict no-None  → runtime OK
        - None + None          → runtime ausente (política 5c, no cuenta D29)
        - bytes + None         → runtime SHAPE_DRIFT (cuenta para D29)
        """
        return self._last_runtime_raw_body

    def get_last_response_status(self) -> Optional[int]:
        """HTTP status of the MAIN navigation response (not runtime)."""
        return self._last_response_status

    def detect_cloudflare(self) -> bool:
        if self._page is None:
            return False
        try:
            html = self._page.content()
        except Exception:
            return False
        return _detect_cloudflare_in_html(html)

    def restart(self) -> None:
        """
        Cierra browser, espera U[*BROWSER_RESTART_COOLDOWN_SEC_RANGE], relanza
        con el MISMO profile dir. Buffers se vacían en el próximo navigate().
        """
        if self._stack is None:
            raise BrowserSessionError("restart() called before __enter__")

        # 1. Close current stack
        self._stack.close()
        self._page = None
        self._context = None
        self._playwright = None

        # 2. Cooldown
        lo, hi = BROWSER_RESTART_COOLDOWN_SEC_RANGE
        time.sleep(random.uniform(lo, hi))

        # 3. Re-enter (same logic as __enter__, mismo profile_dir)
        self.__enter__()

    # -------------------------------------------------------------------------
    # Internal: response handler
    # -------------------------------------------------------------------------

    def _on_response(self, response: "Response") -> None:
        """
        Playwright response event handler.

        Filtra por URL pattern. Captura solo la PRIMERA response matching
        (decisión: O(1), determinística). Distingue parse-OK de parse-FAIL
        para que el orquestador pueda diferenciar política 5c de SHAPE_DRIFT
        runtime.
        """
        try:
            url = response.url
        except Exception:
            return  # Defensive — ignoramos responses con metadata corrupta

        if self._runtime_url_pattern not in url:
            return

        # Solo capturamos la primera matching response (invariant #3)
        if self._captured_runtime_dict is not None or self._last_runtime_raw_body is not None:
            return

        # Intentamos parse JSON
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                self._captured_runtime_dict = parsed
                return
            # Top-level array/string → trat as malformed for our contract
        except Exception:
            pass

        # Fallback: capturamos body crudo para diagnóstico
        try:
            self._last_runtime_raw_body = response.body()
        except Exception:
            # No pudimos ni parsear ni leer body → tratamos como ausente.
            # El orquestador verá None+None y aplicará política 5c.
            pass


# =============================================================================
# FakeBrowserSession (test double — usable en tests del orquestador en Entrega 3)
# =============================================================================

class FakeBrowserSession:
    """
    Test double that respects the BrowserSession contract.

    Usage:
        fake = FakeBrowserSession()
        fake.queue_navigation(
            url="https://airbnb.com.ar/rooms/123",
            html="<html>...</html>",
            runtime_response={"data": {...}},  # or None for política 5c
            response_status=200,
        )
        with fake as browser:
            browser.navigate("https://airbnb.com.ar/rooms/123")
            assert browser.get_html() == "<html>...</html>"

    Multiple queued responses are consumed in FIFO order. Useful for testing
    batches with mixed outcomes (success, 5c, shape_drift, cloudflare).
    """

    def __init__(self) -> None:
        self._queued: list[dict[str, Any]] = []
        self._is_open: bool = False

        # Per-navigate state (mirror PlaywrightBrowserSession exactly)
        self._current_html: Optional[str] = None
        self._captured_runtime_dict: Optional[dict[str, Any]] = None
        self._last_runtime_raw_body: Optional[bytes] = None
        self._last_response_status: Optional[int] = None
        self._last_navigated_url: Optional[str] = None

        # Telemetry (for test assertions)
        self.navigate_calls: list[str] = []
        self.restart_count: int = 0

    # -------------------------------------------------------------------------
    # Test setup API (NOT part of BrowserSession Protocol)
    # -------------------------------------------------------------------------

    def queue_navigation(
        self,
        html: str = "<html><body></body></html>",
        runtime_response: Optional[dict[str, Any]] = None,
        runtime_raw_body: Optional[bytes] = None,
        response_status: Optional[int] = 200,
        url: Optional[str] = None,
    ) -> None:
        """
        Queue the result of the next navigate() call.

        Mutual exclusion: runtime_response and runtime_raw_body cannot both
        be non-None (mirrors invariant #4).
        """
        if runtime_response is not None and runtime_raw_body is not None:
            raise ValueError(
                "runtime_response and runtime_raw_body are mutually exclusive "
                "(invariant #4)"
            )
        self._queued.append({
            "html": html,
            "runtime_response": runtime_response,
            "runtime_raw_body": runtime_raw_body,
            "response_status": response_status,
            "expected_url": url,
        })

    def queue_navigation_failure(self, exc: Exception) -> None:
        """Queue an exception to be raised by the next navigate() call."""
        self._queued.append({"_raise": exc})

    # -------------------------------------------------------------------------
    # BrowserSession Protocol implementation
    # -------------------------------------------------------------------------

    def __enter__(self) -> "FakeBrowserSession":
        self._is_open = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._is_open = False

    def navigate(self, url: str) -> None:
        if not self._is_open:
            raise BrowserSessionError("navigate() called before __enter__")
        if not self._queued:
            raise AssertionError(
                f"FakeBrowserSession.navigate({url!r}) called but no response queued"
            )

        # Clear buffers (invariant #1)
        self._current_html = None
        self._captured_runtime_dict = None
        self._last_runtime_raw_body = None
        self._last_response_status = None
        self._last_navigated_url = url
        self.navigate_calls.append(url)

        item = self._queued.pop(0)

        # Failure path
        if "_raise" in item:
            raise item["_raise"]

        # Success path
        self._current_html = item["html"]
        self._captured_runtime_dict = item["runtime_response"]
        self._last_runtime_raw_body = item["runtime_raw_body"]
        self._last_response_status = item["response_status"]

    def get_html(self) -> str:
        if not self._is_open:
            raise BrowserSessionError("get_html() called before __enter__")
        if self._current_html is None:
            raise BrowserSessionError("get_html() called before navigate()")
        return self._current_html

    def get_intercepted_runtime_response(self) -> Optional[dict[str, Any]]:
        return self._captured_runtime_dict

    def get_last_runtime_raw_body(self) -> Optional[bytes]:
        return self._last_runtime_raw_body

    def get_last_response_status(self) -> Optional[int]:
        return self._last_response_status

    def detect_cloudflare(self) -> bool:
        if self._current_html is None:
            return False
        return _detect_cloudflare_in_html(self._current_html)

    def restart(self) -> None:
        if not self._is_open:
            raise BrowserSessionError("restart() called before __enter__")
        self.restart_count += 1
        # No actual cooldown sleep en el fake — los tests no esperan 30-90s.

# ===========================================================================
# == y class AirbnbScraper:

# =============================================================================
# AirbnbScraper — Capa 3: orquestador end-to-end
# =============================================================================

class AirbnbScraper:
    """
    End-to-end scrape orchestrator.

    Lifecycle (delegates to browser, per bitácora):
        with AirbnbScraper(browser, db, logger) as scraper:
            for listing_id in listings:
                result = scraper.scrape_listing(listing_id)

    Phase awareness:
        price_extractor=None  → Phase 2.3 (no insert_precio; runtime payloads
                                are archived for corpus building).
        price_extractor=...   → Phase 3 (TX-B active).
    """

    def __init__(
        self,
        browser,                              # BrowserSession Protocol
        db: DBClient,
        logger: JSONLLogger,
        *,
        price_extractor: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
        rate_limit_rng: Optional[random.Random] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        archive_dir: Path = RAW_PAYLOADS_DIR,
        fuente_scraper: str = DEFAULT_FUENTE_SCRAPER,
    ) -> None:
        self._browser = browser
        self._db = db
        self._logger = logger
        self._price_extractor = price_extractor
        self._rng = rate_limit_rng or random.Random()
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._sleep_fn = sleep_fn or time.sleep
        self._archive_dir = archive_dir
        self._fuente_scraper = fuente_scraper

        # Per-batch mutable state
        self._consecutive_shape_drifts: int = 0
        self._listings_since_restart: int = 0

    # ----------------- lifecycle (delegates to browser) --------------------

    def __enter__(self) -> "AirbnbScraper":
        self._browser.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._browser.__exit__(exc_type, exc_val, exc_tb)

    # ----------------- public API ------------------------------------------

    def scrape_listing(self, listing_id: str) -> ScrapeResult:
        """
        Scrape one listing end-to-end. Always logs one JSONL event before
        returning, success or failure.

        Flow:
          1. Dedup check                       → SKIPPED (no network, no MEP)
          2. Build URL                         → ERROR if invalid
          3. Browser cycle (D27) every 50      → ERROR on restart failure
          4. Navigate                          → ERROR (network)
          5. Cloudflare check (HTML + status)  → CLOUDFLARE
          6. SSR extract + parse               → SHAPE_DRIFT (counts D29)
          7. Runtime intercept + parse (D30)   → SHAPE_DRIFT | 5c (5c does NOT)
          8. Hash + atomic archive
          9. fetch_or_refresh_mep (D24, soft)  → missing_fields if it fails
         10. TX-A: write_metadata              → ERROR
         11. Decide 5c vs SUCCESS
         12. TX-B: write_price (gated)         → ERROR
         13. Reset SHAPE_DRIFT counter on success
         14. Rate-limit sleep (truncated lognormal)
         15. Build + log + return ScrapeResult

        Sleep policy: only after we touched the network (success, 5c,
        SHAPE_DRIFT, post-navigate ERROR). NOT for SKIPPED, CLOUDFLARE,
        or pre-navigate ERROR.

        Raises:
            BatchAbortError: if MAX_CONSECUTIVE_SHAPE_DRIFT reached (D29).
        """
        started = self._now_fn()
        url: Optional[str] = None
        ssr_parse_status: Optional[str] = None
        runtime_parse_status: Optional[str] = None
        inmueble_id: Optional[int] = None
        raw_hash: Optional[str] = None
        mep_rate: Optional[Decimal] = None
        missing: list[str] = []

        # ---- 1. Dedup ----
        try:
            if self._db.was_scraped_recently(listing_id):
                return self._finalize(
                    listing_id=listing_id,
                    outcome=ScrapeOutcome.SKIPPED,
                    started=started,
                    sleep_after=False,
                )
        except Exception as exc:
            return self._finalize(
                listing_id=listing_id,
                outcome=ScrapeOutcome.ERROR,
                started=started,
                error_class=type(exc).__name__,
                sleep_after=False,
            )

        # ---- 2. Build URL ----
        try:
            url = build_pdp_url(listing_id)
        except Exception as exc:
            return self._finalize(
                listing_id=listing_id,
                outcome=ScrapeOutcome.ERROR,
                started=started,
                error_class=type(exc).__name__,
                sleep_after=False,
            )

        # ---- 3. Browser cycle (proactive, D27) ----
        # Increment FIRST: this listing counts toward the cycle whether it
        # succeeds or fails post-navigate.
        self._listings_since_restart += 1
        if self._listings_since_restart > BROWSER_CYCLE_EVERY_N_LISTINGS:
            try:
                self._browser.restart()
            except Exception as exc:
                return self._finalize(
                    listing_id=listing_id,
                    outcome=ScrapeOutcome.ERROR,
                    started=started,
                    url=url,
                    error_class=type(exc).__name__,
                    sleep_after=False,
                )
            self._listings_since_restart = 1  # this listing is post-restart

        # ---- 4. Navigate ----
        try:
            self._browser.navigate(url)
        except BrowserSessionError as exc:
            return self._finalize(
                listing_id=listing_id,
                outcome=ScrapeOutcome.ERROR,
                started=started,
                url=url,
                error_class=type(exc).__name__,
                sleep_after=True,
            )
        
        # ---- 5. Cloudflare check (layered: HTML body + HTTP status) ----
        try:
            cf_html = self._browser.detect_cloudflare()
        except Exception:
            cf_html = False
        cf_status = _is_cloudflare_status(self._browser.get_last_response_status())
        if cf_html or cf_status:
            return self._finalize(
                listing_id=listing_id,
                outcome=ScrapeOutcome.CLOUDFLARE,
                started=started,
                url=url,
                cloudflare_detected=True,
                sleep_after=False,
            )

        # ---- 6. SSR extract + parse ----
        try:
            html = self._browser.get_html()
            ssr_dict = extract_ssr_payload(html)
        except SSRExtractionError as exc:
            self._consecutive_shape_drifts += 1
            self._check_abort_threshold()
            return self._finalize(
                listing_id=listing_id,
                outcome=ScrapeOutcome.SHAPE_DRIFT,
                started=started,
                url=url,
                error_class=type(exc).__name__,
                sleep_after=True,
            )
        except Exception as exc:
            return self._finalize(
                listing_id=listing_id,
                outcome=ScrapeOutcome.ERROR,
                started=started,
                url=url,
                error_class=type(exc).__name__,
                sleep_after=True,
            )

        ssr_parsed = parse_payload(ssr_dict)
        ssr_parse_status = ssr_parsed.get("parse_status")
        if ssr_parse_status == "SHAPE_DRIFT":
            self._consecutive_shape_drifts += 1
            self._check_abort_threshold()
            return self._finalize(
                listing_id=listing_id,
                outcome=ScrapeOutcome.SHAPE_DRIFT,
                started=started,
                url=url,
                ssr_parse_status=ssr_parse_status,
                missing_fields=list(ssr_parsed.get("missing_fields") or []),
                sleep_after=True,
            )

        # ---- 7. Runtime (D30 doble buffer) ----
        runtime_dict = self._browser.get_intercepted_runtime_response()
        runtime_raw_body = self._browser.get_last_runtime_raw_body()
        runtime_parsed: Optional[dict[str, Any]] = None

        if runtime_dict is not None:
            runtime_parsed = parse_runtime_response(runtime_dict)
            runtime_parse_status = runtime_parsed.get("parse_status")
            if runtime_parse_status == "SHAPE_DRIFT":
                self._consecutive_shape_drifts += 1
                self._check_abort_threshold()
                return self._finalize(
                    listing_id=listing_id,
                    outcome=ScrapeOutcome.SHAPE_DRIFT,
                    started=started,
                    url=url,
                    ssr_parse_status=ssr_parse_status,
                    runtime_parse_status=runtime_parse_status,
                    sleep_after=True,
                )
        elif runtime_raw_body is not None:
            # D30: response present but unparseable JSON → SHAPE_DRIFT runtime.
            # Distinct from "both None" (política 5c).
            self._consecutive_shape_drifts += 1
            self._check_abort_threshold()
            return self._finalize(
                listing_id=listing_id,
                outcome=ScrapeOutcome.SHAPE_DRIFT,
                started=started,
                url=url,
                ssr_parse_status=ssr_parse_status,
                runtime_parse_status="SHAPE_DRIFT_RAW_BODY",
                sleep_after=True,
            )
        # else: both None → política 5c. NO incrementa SHAPE_DRIFT counter.

        # ---- 8. Hash + atomic archive ----
        raw_hash = _hash_payloads(ssr_dict, runtime_dict)
        try:
            _archive_payload(
                archive_dir=self._archive_dir,
                listing_id=listing_id,
                scraped_at=started,
                url=url,
                ssr_dict=ssr_dict,
                runtime_dict=runtime_dict,
                raw_hash=raw_hash,
                fuente_scraper=self._fuente_scraper,
            )
        except Exception:
            # Archive failure is non-fatal; data is still in-memory and
            # will be persisted via TX-A. Surface it via missing_fields.
            missing.append("archive_payload")

        # ---- 9. MEP (D24) — soft-fail ----
        try:
            mep_rate = self._db.fetch_or_refresh_mep()
        except Exception:
            mep_rate = None
            missing.append("mep_rate")

        # ---- 10. TX-A: write_metadata ----
        try:
            inmueble_id = self._db.write_metadata(ssr_parsed)
        except Exception as exc:
            return self._finalize(
                listing_id=listing_id,
                outcome=ScrapeOutcome.ERROR,
                started=started,
                url=url,
                ssr_parse_status=ssr_parse_status,
                runtime_parse_status=runtime_parse_status,
                raw_hash=raw_hash,
                mep_rate=mep_rate,
                error_class=type(exc).__name__,
                missing_fields=missing,
                sleep_after=True,
            )

        # ---- 11. Política 5c decision ----
        has_runtime_price = (
            runtime_parsed is not None
            and runtime_parsed.get("parse_status") == "OK"
            and runtime_parsed.get("structured_display_price") is not None
        )

        # ---- 12. TX-B: write_price (gated by Phase 3 + runtime OK + MEP OK) ----
        if (
            self._price_extractor is not None
            and has_runtime_price
            and mep_rate is not None
        ):
            try:
                self._db.write_price(
                    inmueble_id=inmueble_id,
                    runtime_parsed=runtime_parsed,
                    mep_rate=mep_rate,
                    scraped_at=started,
                )
            except Exception as exc:
                # Metadata persisted, price failed → ERROR with partial state.
                return self._finalize(
                    listing_id=listing_id,
                    outcome=ScrapeOutcome.ERROR,
                    started=started,
                    url=url,
                    ssr_parse_status=ssr_parse_status,
                    runtime_parse_status=runtime_parse_status,
                    inmueble_id=inmueble_id,
                    raw_hash=raw_hash,
                    mep_rate=mep_rate,
                    error_class=type(exc).__name__,
                    missing_fields=missing,
                    sleep_after=True,
                )

        # ---- 13. Reset SHAPE_DRIFT counter on successful scrape ----
        self._consecutive_shape_drifts = 0

        # ---- 14. Outcome decision ----
        outcome = (
            ScrapeOutcome.SUCCESS if has_runtime_price else ScrapeOutcome.METADATA_ONLY
        )

        return self._finalize(
            listing_id=listing_id,
            outcome=outcome,
            started=started,
            url=url,
            ssr_parse_status=ssr_parse_status,
            runtime_parse_status=runtime_parse_status,
            inmueble_id=inmueble_id,
            raw_hash=raw_hash,
            mep_rate=mep_rate,
            missing_fields=missing,
            sleep_after=True,
        )

    # ----------------- private helpers -------------------------------------

    def _check_abort_threshold(self) -> None:
        """D29 circuit breaker: 5 consecutive SHAPE_DRIFTs → abort batch."""
        if self._consecutive_shape_drifts >= MAX_CONSECUTIVE_SHAPE_DRIFT:
            raise BatchAbortError(
                f"Aborting batch: {self._consecutive_shape_drifts} consecutive "
                f"SHAPE_DRIFTs reached threshold MAX_CONSECUTIVE_SHAPE_DRIFT="
                f"{MAX_CONSECUTIVE_SHAPE_DRIFT}. Likely Airbnb payload mutation "
                f"— manual review of recent payloads required."
            )

    def _finalize(
        self,
        *,
        listing_id: str,
        outcome: ScrapeOutcome,
        started: datetime,
        url: Optional[str] = None,
        ssr_parse_status: Optional[str] = None,
        runtime_parse_status: Optional[str] = None,
        inmueble_id: Optional[int] = None,
        raw_hash: Optional[str] = None,
        mep_rate: Optional[Decimal] = None,
        cloudflare_detected: bool = False,
        error_class: Optional[str] = None,
        missing_fields: Optional[list[str]] = None,
        sleep_after: bool = False,
    ) -> ScrapeResult:
        """Build ScrapeResult, log JSONL event, optional rate-limit sleep."""
        ended = self._now_fn()
        duration_ms = int((ended - started).total_seconds() * 1000)

        result = ScrapeResult(
            listing_id=listing_id,
            outcome=outcome,
            duration_ms=duration_ms,
            url=url,
            ssr_parse_status=ssr_parse_status,
            runtime_parse_status=runtime_parse_status,
            inmueble_id=inmueble_id,
            raw_payload_hash=raw_hash,
            mep_rate=str(mep_rate) if mep_rate is not None else None,
            cloudflare_detected=cloudflare_detected,
            error_class=error_class,
            missing_fields=list(missing_fields or []),
        )

        event = {
            "ts": ended.isoformat(),
            "listing_id": listing_id,
            "url": url,
            "outcome": outcome.value,
            "ssr_parse_status": ssr_parse_status,
            "runtime_parse_status": runtime_parse_status,
            "decision_5c": outcome == ScrapeOutcome.METADATA_ONLY,
            "mep_rate": str(mep_rate) if mep_rate is not None else None,
            "raw_payload_hash": raw_hash,
            "duration_ms": duration_ms,
            "cloudflare_detected": cloudflare_detected,
            "error_class": error_class,
            "missing_fields": list(missing_fields or []),
        }
        try:
            self._logger.log(event)
        except Exception:
            # Logging must never mask the scrape result.
            pass

        if sleep_after:
            try:
                seconds = _sample_rate_limit_sleep(self._rng)
                self._sleep_fn(seconds)
            except Exception:
                pass

        return result