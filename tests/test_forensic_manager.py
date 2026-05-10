"""
Tests del ForensicManager (CIF PR-3).

TDD RED PHASE — Estos tests están diseñados para fallar hasta que se implementen:

    src/scraper/_forensic_manager.py
        - class ForensicManager
        - class ForensicOutcome (Enum)
        - class ForensicCaptureResult (dataclass frozen)
        - def classify_error_outcome(timer, error) -> ForensicOutcome
        - class CloudflareDetected(Exception)
        - class ShapeDriftError(Exception)

    src/scraper/airbnb_scraper.py
        FakeBrowserSession extensiones (CIF PR-3):
            - get_current_url() -> str | None
            - get_page_title() -> str | None
            - url_calls: int
            - title_calls: int
            - queue_url_failure() / queue_title_failure()
            - configurable url/title via constructor o attributes

Cubre la matriz forense pactada en D36:

    Outcome              | Runtime | HTML | Screenshot | page_state
    ---------------------|---------|------|------------|------------
    SUCCESS              |   ✅    |  ❌  |     ❌     |     ❌
    METADATA_ONLY        |   ✅    |  ❌  |     ❌     |     ❌
    SHAPE_DRIFT          |   ✅    |  ✅  |     ✅     |     ❌
    CLOUDFLARE           |   ❌    |  ❌  |     ✅     |     ✅
    ERROR_POST_NAVIGATE  |  ✅*    | ✅*  |    ✅*     |     ✅
    ERROR_PRE_NAVIGATE   |   ❌    |  ❌  |     ❌     |     ❌
    SKIPPED              |   ❌    |  ❌  |     ❌     |     ❌

    (✅* = "si disponible"; soft-fail no aborta el resto de la captura)

Convenciones verificadas por estos tests:
    - SUCCESS/METADATA_ONLY archivan en archive_dir/ (no en _diagnostic/)
    - Resto archiva en archive_dir/_diagnostic/
    - Filename SUCCESS/METADATA_ONLY: {listing_id}_{ts_iso}.json
    - Filename diagnostic: {listing_id}_{ts_iso}_{SUFFIX}.{ext}
    - ERROR_POST_NAVIGATE usa sufijo "ERROR" en filename (no el value completo del enum)
    - evidence_paths solo lista archivos efectivamente escritos
    - capture_attempts lista las capturas INTENTADAS (success o soft-fail), no las "no aplica"
    - page_state es None cuando la matriz no aplica; dict con url/title (posiblemente None) cuando aplica
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

# ───────────────────────────────────────────────────────────────────────
# Imports que fallarán hasta que se implemente PR-3 (esperado en red phase)
# ───────────────────────────────────────────────────────────────────────
from src.scraper._forensic_manager import (
    CloudflareDetected,
    ForensicCaptureResult,
    ForensicManager,
    ForensicOutcome,
    ShapeDriftError,
    classify_error_outcome,
)
from src.scraper._timer_collector import TimerCollector

if TYPE_CHECKING:
    from src.scraper.airbnb_scraper import FakeBrowserSession


# ───────────────────────────────────────────────────────────────────────
# Constantes de test
# ───────────────────────────────────────────────────────────────────────
LISTING_ID = "12345678"
TS_ISO = "20260512T183045Z"  # ISO 8601 básico, filesystem-safe

# Mapeo enum → sufijo en filename (single source of truth para los tests).
# Si el manager produce sufijos distintos, los tests fallan correctamente.
EXPECTED_FILENAME_SUFFIX = {
    ForensicOutcome.SHAPE_DRIFT: "SHAPE_DRIFT",
    ForensicOutcome.CLOUDFLARE: "CLOUDFLARE",
    ForensicOutcome.ERROR_POST_NAVIGATE: "ERROR",
}

SAMPLE_URL = "https://www.airbnb.com/rooms/12345678"
SAMPLE_TITLE = "Test Listing — Palermo CABA"
CLOUDFLARE_URL = "https://www.airbnb.com/cdn-cgi/challenge-platform/h/g/orchestrate/jsd/v1"
CLOUDFLARE_TITLE = "Just a moment..."


# ───────────────────────────────────────────────────────────────────────
# Fixtures locales (complementan conftest.py)
# ───────────────────────────────────────────────────────────────────────
@pytest.fixture
def sample_runtime_payload() -> dict:
    """Payload runtime sintético mínimo, no realista pero válido para tests de I/O."""
    return {"data": {"presentation": {"stayProductDetailPage": {"sections": []}}}}


@pytest.fixture
def sample_ssr_html() -> str:
    """HTML sintético mínimo para tests de archivado de HTML."""
    return (
        "<html><head><title>Test</title></head>"
        "<body><script id='data-deferred-state'>{}</script></body></html>"
    )


@pytest.fixture
def forensic_manager(tmp_archive_dir, fake_browser) -> ForensicManager:
    """Manager con browser inyectado — caso normal."""
    return ForensicManager(archive_dir=tmp_archive_dir, browser=fake_browser)


@pytest.fixture
def forensic_manager_no_browser(tmp_archive_dir) -> ForensicManager:
    """Manager sin browser — verifica que outcomes sin captura visual funcionan igual."""
    return ForensicManager(archive_dir=tmp_archive_dir, browser=None)


# ───────────────────────────────────────────────────────────────────────
# Helpers de aserción
# ───────────────────────────────────────────────────────────────────────
def _assert_diagnostic_filename(
    path_str: str,
    expected_listing: str,
    expected_ts: str,
    expected_suffix: str,
    expected_ext: str,
) -> None:
    """Verifica que el filename cumple {listing_id}_{ts_iso}_{SUFFIX}.{ext} en _diagnostic/."""
    path = Path(path_str)
    assert path.parent.name == "_diagnostic", (
        f"Diagnostic files deben ir a _diagnostic/, no a {path.parent.name}"
    )
    pattern = rf"^{expected_listing}_{expected_ts}_{expected_suffix}\.{expected_ext}$"
    assert re.match(pattern, path.name), (
        f"Filename {path.name!r} no matchea pattern {pattern!r}"
    )


def _assert_success_filename(
    path_str: str,
    archive_dir: Path,
    expected_listing: str,
    expected_ts: str,
) -> None:
    """Verifica que SUCCESS/METADATA_ONLY van a archive_dir/ (no _diagnostic/) con nombre simple."""
    path = Path(path_str)
    assert path.parent == archive_dir, (
        f"SUCCESS/METADATA_ONLY deben ir a archive_dir/, no a {path.parent}"
    )
    assert path.name == f"{expected_listing}_{expected_ts}.json"


# ═══════════════════════════════════════════════════════════════════════
# CLASE 1 — Matriz forense por outcome (7 tests, uno por outcome)
# ═══════════════════════════════════════════════════════════════════════
class TestForensicManagerMatrix:
    """Un test por cada celda de la matriz D36. Verifica QUÉ se archiva (no cómo)."""

    def test_success_archives_only_runtime(
        self, forensic_manager, sample_runtime_payload, tmp_archive_dir
    ):
        result = forensic_manager.capture(
            outcome=ForensicOutcome.SUCCESS,
            listing_id=LISTING_ID,
            ts_iso=TS_ISO,
            runtime_payload=sample_runtime_payload,
            ssr_html=None,
        )

        # Evidence paths: solo runtime
        assert set(result.evidence_paths.keys()) == {"runtime_payload"}
        _assert_success_filename(
            result.evidence_paths["runtime_payload"],
            tmp_archive_dir, LISTING_ID, TS_ISO,
        )

        # Archivo físicamente creado y con contenido correcto
        runtime_path = Path(result.evidence_paths["runtime_payload"])
        assert runtime_path.exists()
        assert json.loads(runtime_path.read_text()) == sample_runtime_payload

        # No se solicitó nada al browser
        assert result.page_state is None
        assert "screenshot" not in result.capture_attempts
        assert "ssr_html" not in result.capture_attempts
        assert "page_state" not in result.capture_attempts

    def test_metadata_only_archives_only_runtime(
        self, forensic_manager, sample_runtime_payload, tmp_archive_dir
    ):
        result = forensic_manager.capture(
            outcome=ForensicOutcome.METADATA_ONLY,
            listing_id=LISTING_ID,
            ts_iso=TS_ISO,
            runtime_payload=sample_runtime_payload,
            ssr_html=None,
        )

        assert set(result.evidence_paths.keys()) == {"runtime_payload"}
        _assert_success_filename(
            result.evidence_paths["runtime_payload"],
            tmp_archive_dir, LISTING_ID, TS_ISO,
        )
        assert result.page_state is None

    def test_shape_drift_archives_triple_runtime_html_screenshot(
        self, forensic_manager, fake_browser, sample_runtime_payload, sample_ssr_html
    ):
        result = forensic_manager.capture(
            outcome=ForensicOutcome.SHAPE_DRIFT,
            listing_id=LISTING_ID,
            ts_iso=TS_ISO,
            runtime_payload=sample_runtime_payload,
            ssr_html=sample_ssr_html,
        )

        # Los 3 archivos deben existir
        assert set(result.evidence_paths.keys()) == {
            "runtime_payload", "ssr_html", "screenshot"
        }
        for kind in ("runtime_payload", "ssr_html", "screenshot"):
            path = Path(result.evidence_paths[kind])
            assert path.exists(), f"Falta archivo {kind} en {path}"

        # Filenames con sufijo SHAPE_DRIFT en _diagnostic/
        _assert_diagnostic_filename(
            result.evidence_paths["runtime_payload"], LISTING_ID, TS_ISO, "SHAPE_DRIFT", "json"
        )
        _assert_diagnostic_filename(
            result.evidence_paths["ssr_html"], LISTING_ID, TS_ISO, "SHAPE_DRIFT", "html"
        )
        _assert_diagnostic_filename(
            result.evidence_paths["screenshot"], LISTING_ID, TS_ISO, "SHAPE_DRIFT", "png"
        )

        # Browser fue invocado para screenshot
        assert len(fake_browser.screenshot_calls) == 1

        # SHAPE_DRIFT no captura page_state (es una mutación estructural, no un challenge)
        assert result.page_state is None

        # capture_attempts refleja qué se intentó
        assert result.capture_attempts == {
            "runtime_payload": True,
            "ssr_html": True,
            "screenshot": True,
        }

    def test_cloudflare_archives_screenshot_and_page_state(
        self, forensic_manager, fake_browser
    ):
        # Configurar el fake con URL/title de challenge
        fake_browser.current_url = CLOUDFLARE_URL
        fake_browser.page_title = CLOUDFLARE_TITLE

        result = forensic_manager.capture(
            outcome=ForensicOutcome.CLOUDFLARE,
            listing_id=LISTING_ID,
            ts_iso=TS_ISO,
            runtime_payload=None,
            ssr_html=None,
        )

        # Solo screenshot, no runtime ni html
        assert set(result.evidence_paths.keys()) == {"screenshot"}
        _assert_diagnostic_filename(
            result.evidence_paths["screenshot"], LISTING_ID, TS_ISO, "CLOUDFLARE", "png"
        )

        # page_state capturado
        assert result.page_state == {"url": CLOUDFLARE_URL, "title": CLOUDFLARE_TITLE}

        # Telemetría del browser
        assert len(fake_browser.screenshot_calls) == 1
        assert fake_browser.url_calls == 1
        assert fake_browser.title_calls == 1

        # capture_attempts
        assert "screenshot" in result.capture_attempts
        assert "page_state" in result.capture_attempts
        assert "runtime_payload" not in result.capture_attempts
        assert "ssr_html" not in result.capture_attempts

    def test_error_post_navigate_archives_available_artifacts(
        self, forensic_manager, fake_browser, sample_runtime_payload, sample_ssr_html
    ):
        fake_browser.current_url = SAMPLE_URL
        fake_browser.page_title = SAMPLE_TITLE

        result = forensic_manager.capture(
            outcome=ForensicOutcome.ERROR_POST_NAVIGATE,
            listing_id=LISTING_ID,
            ts_iso=TS_ISO,
            runtime_payload=sample_runtime_payload,
            ssr_html=sample_ssr_html,
        )

        # Tripleta + page_state
        assert set(result.evidence_paths.keys()) == {
            "runtime_payload", "ssr_html", "screenshot"
        }

        # Filename usa sufijo "ERROR" (no "ERROR_POST_NAVIGATE")
        _assert_diagnostic_filename(
            result.evidence_paths["runtime_payload"], LISTING_ID, TS_ISO, "ERROR", "json"
        )
        _assert_diagnostic_filename(
            result.evidence_paths["ssr_html"], LISTING_ID, TS_ISO, "ERROR", "html"
        )
        _assert_diagnostic_filename(
            result.evidence_paths["screenshot"], LISTING_ID, TS_ISO, "ERROR", "png"
        )

        assert result.page_state == {"url": SAMPLE_URL, "title": SAMPLE_TITLE}

    def test_error_pre_navigate_archives_nothing(
        self, forensic_manager_no_browser, sample_runtime_payload
    ):
        # Pasamos runtime_payload por si acaso — debe ignorarse (red no se tocó)
        result = forensic_manager_no_browser.capture(
            outcome=ForensicOutcome.ERROR_PRE_NAVIGATE,
            listing_id=LISTING_ID,
            ts_iso=TS_ISO,
            runtime_payload=sample_runtime_payload,
            ssr_html=None,
        )

        assert result.evidence_paths == {}
        assert result.page_state is None
        assert result.capture_attempts == {}

    def test_skipped_archives_nothing(self, forensic_manager_no_browser):
        result = forensic_manager_no_browser.capture(
            outcome=ForensicOutcome.SKIPPED,
            listing_id=LISTING_ID,
            ts_iso=TS_ISO,
            runtime_payload=None,
            ssr_html=None,
        )

        assert result.evidence_paths == {}
        assert result.page_state is None
        assert result.capture_attempts == {}


# ═══════════════════════════════════════════════════════════════════════
# CLASE 2 — Convenciones de filenames y layout
# ═══════════════════════════════════════════════════════════════════════
class TestForensicManagerFilenamePattern:

    def test_success_files_go_to_archive_dir_root_not_diagnostic(
        self, forensic_manager, sample_runtime_payload, tmp_archive_dir
    ):
        result = forensic_manager.capture(
            outcome=ForensicOutcome.SUCCESS,
            listing_id=LISTING_ID,
            ts_iso=TS_ISO,
            runtime_payload=sample_runtime_payload,
        )
        runtime_path = Path(result.evidence_paths["runtime_payload"])
        assert runtime_path.parent == tmp_archive_dir
        assert "_diagnostic" not in runtime_path.parts

    def test_all_diagnostic_outcomes_use_subdir_and_outcome_suffix(
        self, forensic_manager, fake_browser, sample_runtime_payload, sample_ssr_html
    ):
        """Verifica los 3 outcomes que SÍ archivan en _diagnostic/ comparten convención."""
        outcomes_with_suffix = [
            (ForensicOutcome.SHAPE_DRIFT, "SHAPE_DRIFT"),
            (ForensicOutcome.CLOUDFLARE, "CLOUDFLARE"),
            (ForensicOutcome.ERROR_POST_NAVIGATE, "ERROR"),
        ]
        for outcome, expected_suffix in outcomes_with_suffix:
            fake_browser.current_url = SAMPLE_URL
            fake_browser.page_title = SAMPLE_TITLE
            result = forensic_manager.capture(
                outcome=outcome,
                listing_id=LISTING_ID,
                ts_iso=TS_ISO,
                runtime_payload=sample_runtime_payload,
                ssr_html=sample_ssr_html,
            )
            for kind, path_str in result.evidence_paths.items():
                path = Path(path_str)
                assert path.parent.name == "_diagnostic", (
                    f"{outcome.name}/{kind} no fue a _diagnostic/"
                )
                assert f"_{expected_suffix}." in path.name, (
                    f"{outcome.name}/{kind} no tiene sufijo {expected_suffix}: {path.name}"
                )


# ═══════════════════════════════════════════════════════════════════════
# CLASE 3 — Soft-fail handling
# ═══════════════════════════════════════════════════════════════════════
class TestForensicManagerSoftFail:

    def test_screenshot_softfail_records_attempt_without_path(
        self, forensic_manager, fake_browser, sample_runtime_payload, sample_ssr_html
    ):
        fake_browser.queue_screenshot_failure()

        result = forensic_manager.capture(
            outcome=ForensicOutcome.SHAPE_DRIFT,
            listing_id=LISTING_ID,
            ts_iso=TS_ISO,
            runtime_payload=sample_runtime_payload,
            ssr_html=sample_ssr_html,
        )

        # Runtime y HTML deben estar; screenshot NO
        assert "runtime_payload" in result.evidence_paths
        assert "ssr_html" in result.evidence_paths
        assert "screenshot" not in result.evidence_paths

        # capture_attempts refleja: se intentó (True) pero soft-fail no agrega path
        assert result.capture_attempts["screenshot"] is False
        assert result.capture_attempts["runtime_payload"] is True
        assert result.capture_attempts["ssr_html"] is True

        # El browser fue invocado (telemetría del fake)
        assert len(fake_browser.screenshot_calls) == 1

    def test_html_softfail_in_error_post_navigate_keeps_screenshot(
        self, forensic_manager, fake_browser, sample_runtime_payload
    ):
        """Si ssr_html viene None (porque get_page_html falló upstream), screenshot y runtime persisten."""
        fake_browser.current_url = SAMPLE_URL
        fake_browser.page_title = SAMPLE_TITLE

        result = forensic_manager.capture(
            outcome=ForensicOutcome.ERROR_POST_NAVIGATE,
            listing_id=LISTING_ID,
            ts_iso=TS_ISO,
            runtime_payload=sample_runtime_payload,
            ssr_html=None,  # no había DOM HTML al fallar
        )

        assert "runtime_payload" in result.evidence_paths
        assert "ssr_html" not in result.evidence_paths
        assert "screenshot" in result.evidence_paths
        assert result.capture_attempts["ssr_html"] is False
        assert result.page_state == {"url": SAMPLE_URL, "title": SAMPLE_TITLE}

    def test_partial_page_state_when_url_succeeds_title_fails(
        self, forensic_manager, fake_browser
    ):
        """Si url se captura pero title soft-fail, page_state queda con title=None (asimetría preservada)."""
        fake_browser.current_url = CLOUDFLARE_URL
        fake_browser.queue_title_failure()

        result = forensic_manager.capture(
            outcome=ForensicOutcome.CLOUDFLARE,
            listing_id=LISTING_ID,
            ts_iso=TS_ISO,
        )

        assert result.page_state is not None
        assert result.page_state["url"] == CLOUDFLARE_URL
        assert result.page_state["title"] is None
        # capture_attempts: page_state se intentó (aunque parcial)
        assert result.capture_attempts["page_state"] is True


# ═══════════════════════════════════════════════════════════════════════
# CLASE 4 — Clasificación de errores (taxonomía)
# ═══════════════════════════════════════════════════════════════════════
class TestForensicErrorClassification:

    def test_classify_no_navigate_step_is_pre_navigate(self):
        """Si el timer NO registra 'navigate', el error es pre-navigate independiente del tipo."""
        timer = TimerCollector()
        # Solo dedup_check, sin navigate
        with timer.step("dedup_check"):
            pass

        error = ValueError("URL builder failed")
        assert classify_error_outcome(timer, error) == ForensicOutcome.ERROR_PRE_NAVIGATE

    def test_classify_cloudflare_exception_routes_to_cloudflare(self):
        """Aunque se haya navegado, CloudflareDetected tiene prioridad sobre genérico."""
        timer = TimerCollector()
        with timer.step("navigate"):
            pass

        error = CloudflareDetected("Just a moment...")
        assert classify_error_outcome(timer, error) == ForensicOutcome.CLOUDFLARE

    def test_classify_shape_drift_exception_routes_to_shape_drift(self):
        timer = TimerCollector()
        with timer.step("navigate"):
            pass
        with timer.step("extract_ssr"):
            pass

        error = ShapeDriftError("Missing key 'niobeClientData'")
        assert classify_error_outcome(timer, error) == ForensicOutcome.SHAPE_DRIFT

    def test_classify_generic_exception_post_navigate(self):
        """Cualquier excepción no-categorizada con navigate registrado → ERROR_POST_NAVIGATE."""
        timer = TimerCollector()
        with timer.step("navigate"):
            pass

        error = RuntimeError("Unexpected parser state")
        assert classify_error_outcome(timer, error) == ForensicOutcome.ERROR_POST_NAVIGATE

    def test_classify_priority_pre_navigate_beats_exception_type(self):
        """Edge case: incluso si la excepción es CloudflareDetected, sin navigate → pre-navigate.

        Esto NO debería ocurrir en producción (¿cómo detectás CF sin navegar?), pero el
        clasificador debe ser determinístico: navigate check primero.
        """
        timer = TimerCollector()  # sin navigate
        error = CloudflareDetected("imposible pero testeable")
        assert classify_error_outcome(timer, error) == ForensicOutcome.ERROR_PRE_NAVIGATE


# ═══════════════════════════════════════════════════════════════════════
# CLASE 5 — Contrato del ForensicCaptureResult para serialización JSONL
# ═══════════════════════════════════════════════════════════════════════
class TestForensicCaptureResultContract:

    def test_evidence_paths_is_empty_dict_not_none_when_nothing_archived(
        self, forensic_manager_no_browser
    ):
        """Distinción semántica: {} significa 'no se archivó', None no es un valor válido."""
        result = forensic_manager_no_browser.capture(
            outcome=ForensicOutcome.SKIPPED,
            listing_id=LISTING_ID,
            ts_iso=TS_ISO,
        )
        assert result.evidence_paths == {}
        assert result.evidence_paths is not None

    def test_page_state_is_none_when_outcome_doesnt_require_it(
        self, forensic_manager, sample_runtime_payload
    ):
        """SUCCESS, METADATA_ONLY, SHAPE_DRIFT, SKIPPED, ERROR_PRE_NAVIGATE → page_state is None."""
        for outcome in [
            ForensicOutcome.SUCCESS,
            ForensicOutcome.METADATA_ONLY,
            ForensicOutcome.SKIPPED,
            ForensicOutcome.ERROR_PRE_NAVIGATE,
        ]:
            result = forensic_manager.capture(
                outcome=outcome,
                listing_id=LISTING_ID,
                ts_iso=TS_ISO,
                runtime_payload=sample_runtime_payload if outcome in {
                    ForensicOutcome.SUCCESS, ForensicOutcome.METADATA_ONLY
                } else None,
            )
            assert result.page_state is None, (
                f"{outcome.name}: page_state debería ser None, fue {result.page_state}"
            )

    def test_result_is_frozen_dataclass(
        self, forensic_manager, sample_runtime_payload
    ):
        """ForensicCaptureResult debe ser frozen — los call-sites no deben mutarlo."""
        result = forensic_manager.capture(
            outcome=ForensicOutcome.SUCCESS,
            listing_id=LISTING_ID,
            ts_iso=TS_ISO,
            runtime_payload=sample_runtime_payload,
        )
        with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError es subclass
            result.evidence_paths = {}  # type: ignore[misc]

    def test_capture_attempts_lists_only_attempted_keys_not_unapplicable(
        self, forensic_manager, sample_runtime_payload
    ):
        """SUCCESS no intenta screenshot — la key NO debe aparecer en capture_attempts.

        Distinción clave: capture_attempts.get("screenshot") es None (no intentado) vs
        False (intentado pero soft-fail) vs True (intentado y exitoso).
        """
        result = forensic_manager.capture(
            outcome=ForensicOutcome.SUCCESS,
            listing_id=LISTING_ID,
            ts_iso=TS_ISO,
            runtime_payload=sample_runtime_payload,
        )
        assert "screenshot" not in result.capture_attempts
        assert "ssr_html" not in result.capture_attempts
        assert "page_state" not in result.capture_attempts
        assert result.capture_attempts.get("runtime_payload") is True