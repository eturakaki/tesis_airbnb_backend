"""
Tests de la lógica de deduplicación temporal del scraper.

Estrategia: mockear `conn.execute(...).first()` para evitar dependencia de una
DB real en el test suite. Tests de integración con Postgres real se ejecutan
en `tests/test_mep_y_db.py` (existente).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.scraper.dedup import (
    DEFAULT_SKIP_THRESHOLD_HOURS,
    DedupError,
    should_skip,
)

# Reference time fija para todos los tests determinísticos
NOW_UTC = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)


# ─── HELPERS DE MOCK ─────────────────────────────────────────────────────
def _mock_conn(first_returns):
    """
    Construye un mock de conexión SQLAlchemy donde `conn.execute(...).first()`
    devuelve `first_returns`.

    Si `first_returns` es una excepción, la levanta al ejecutar.
    """
    conn = MagicMock()
    if isinstance(first_returns, Exception):
        conn.execute.side_effect = first_returns
    else:
        result_proxy = MagicMock()
        result_proxy.first.return_value = first_returns
        conn.execute.return_value = result_proxy
    return conn


# ─── CASOS BÁSICOS DE LA LÓGICA ─────────────────────────────────────────
def test_listing_does_not_exist_returns_no_skip():
    """LEFT JOIN sin match → conn devuelve None → no skip."""
    conn = _mock_conn(first_returns=None)
    assert should_skip(conn, "42610838", now=NOW_UTC) is False


def test_listing_exists_but_no_prices_returns_no_skip():
    """Listing en `inmuebles` pero sin filas en `precios_historicos`."""
    conn = _mock_conn(first_returns=(None,))
    assert should_skip(conn, "42610838", now=NOW_UTC) is False


def test_recent_scrape_within_threshold_returns_skip():
    """Última observación hace 24hs < threshold 72hs → skip."""
    last_seen = NOW_UTC - timedelta(hours=24)
    conn = _mock_conn(first_returns=(last_seen,))
    assert should_skip(conn, "42610838", now=NOW_UTC) is True


def test_old_scrape_beyond_threshold_returns_no_skip():
    """Última observación hace 80hs > threshold 72hs → no skip."""
    last_seen = NOW_UTC - timedelta(hours=80)
    conn = _mock_conn(first_returns=(last_seen,))
    assert should_skip(conn, "42610838", now=NOW_UTC) is False


def test_exactly_at_threshold_returns_skip():
    """Borderline: última observación = cutoff exacto → skip (>=)."""
    last_seen = NOW_UTC - timedelta(hours=72)
    conn = _mock_conn(first_returns=(last_seen,))
    assert should_skip(conn, "42610838", now=NOW_UTC) is True


def test_just_past_threshold_returns_no_skip():
    """Última observación 72hs y 1 segundo atrás → no skip."""
    last_seen = NOW_UTC - timedelta(hours=72, seconds=1)
    conn = _mock_conn(first_returns=(last_seen,))
    assert should_skip(conn, "42610838", now=NOW_UTC) is False


# ─── THRESHOLDS PERSONALIZADOS (sensitivity analysis) ───────────────────
def test_custom_threshold_24_hours():
    last_seen = NOW_UTC - timedelta(hours=12)
    conn = _mock_conn(first_returns=(last_seen,))
    assert should_skip(conn, "42610838", now=NOW_UTC, threshold_hours=24) is True

    last_seen = NOW_UTC - timedelta(hours=36)
    conn = _mock_conn(first_returns=(last_seen,))
    assert should_skip(conn, "42610838", now=NOW_UTC, threshold_hours=24) is False


def test_custom_threshold_zero_means_always_scrape():
    """threshold=0 → siempre scrapear (cutoff = now, ninguna fecha es >= now)."""
    last_seen = NOW_UTC - timedelta(seconds=1)
    conn = _mock_conn(first_returns=(last_seen,))
    assert should_skip(conn, "42610838", now=NOW_UTC, threshold_hours=0) is False


def test_threshold_one_week():
    """threshold=168h (7 días) — útil para Fase 4."""
    last_seen = NOW_UTC - timedelta(days=5)
    conn = _mock_conn(first_returns=(last_seen,))
    assert should_skip(conn, "42610838", now=NOW_UTC, threshold_hours=168) is True

    last_seen = NOW_UTC - timedelta(days=8)
    conn = _mock_conn(first_returns=(last_seen,))
    assert should_skip(conn, "42610838", now=NOW_UTC, threshold_hours=168) is False


# ─── DEFAULTS ────────────────────────────────────────────────────────────
def test_default_threshold_is_72_hours():
    assert DEFAULT_SKIP_THRESHOLD_HOURS == 72


def test_default_threshold_used_when_not_specified():
    """Si no se pasa threshold_hours, debe aplicar el default de 72."""
    last_seen = NOW_UTC - timedelta(hours=71)  # < 72h
    conn = _mock_conn(first_returns=(last_seen,))
    assert should_skip(conn, "42610838", now=NOW_UTC) is True

    last_seen = NOW_UTC - timedelta(hours=73)  # > 72h
    conn = _mock_conn(first_returns=(last_seen,))
    assert should_skip(conn, "42610838", now=NOW_UTC) is False


# ─── TIMEZONE HANDLING ───────────────────────────────────────────────────
def test_naive_timestamp_from_db_is_assumed_utc():
    """Si la DB devuelve tz-naive, se asume UTC (defensive)."""
    last_seen_naive = datetime(2026, 5, 9, 0, 0, 0)  # naive, 12hs antes de NOW_UTC
    conn = _mock_conn(first_returns=(last_seen_naive,))
    # 12hs < 72h → skip
    assert should_skip(conn, "42610838", now=NOW_UTC) is True


def test_naive_now_raises():
    """now debe ser tz-aware para evitar bugs de comparación."""
    naive_now = datetime(2026, 5, 9, 12, 0, 0)
    conn = _mock_conn(first_returns=(None,))
    with pytest.raises(DedupError, match="timezone-aware"):
        should_skip(conn, "42610838", now=naive_now)


def test_now_default_is_called_when_not_provided():
    """Si no se pasa `now`, no debe fallar (usa datetime.now(utc) internamente)."""
    conn = _mock_conn(first_returns=(None,))
    # Solo verificar que no levanta; no chequeamos el valor exacto
    result = should_skip(conn, "42610838")
    assert isinstance(result, bool)


# ─── VALIDACIÓN DE INPUTS ────────────────────────────────────────────────
def test_empty_listing_id_raises():
    conn = _mock_conn(first_returns=None)
    with pytest.raises(DedupError):
        should_skip(conn, "", now=NOW_UTC)


def test_none_listing_id_raises():
    conn = _mock_conn(first_returns=None)
    with pytest.raises(DedupError):
        should_skip(conn, None, now=NOW_UTC)


def test_non_string_listing_id_raises():
    conn = _mock_conn(first_returns=None)
    with pytest.raises(DedupError):
        should_skip(conn, 42610838, now=NOW_UTC)  # int, no str


def test_negative_threshold_raises():
    conn = _mock_conn(first_returns=None)
    with pytest.raises(DedupError):
        should_skip(conn, "42610838", now=NOW_UTC, threshold_hours=-1)


def test_non_int_threshold_raises():
    conn = _mock_conn(first_returns=None)
    with pytest.raises(DedupError):
        should_skip(conn, "42610838", now=NOW_UTC, threshold_hours=72.5)


def test_non_datetime_now_raises():
    conn = _mock_conn(first_returns=None)
    with pytest.raises(DedupError):
        should_skip(conn, "42610838", now="2026-05-09")


# ─── PROPAGACIÓN DE ERRORES DE DB ────────────────────────────────────────
def test_db_error_wraps_in_dedup_error():
    """Si la consulta SQL falla, se envuelve en DedupError sin perder el original."""
    original = RuntimeError("connection lost")
    conn = _mock_conn(first_returns=original)
    with pytest.raises(DedupError, match="42610838"):
        should_skip(conn, "42610838", now=NOW_UTC)


# ─── PARAMETRIZACIÓN: BARRIDO DE HORAS ──────────────────────────────────
@pytest.mark.parametrize(
    "hours_ago, threshold, expected_skip",
    [
        (1, 72, True),     # muy reciente
        (24, 72, True),    # 1 día atrás
        (71, 72, True),    # justo antes del threshold
        (72, 72, True),    # exactamente en el threshold
        (73, 72, False),   # justo después
        (168, 72, False),  # 1 semana atrás
        (1, 24, True),
        (25, 24, False),
        (1, 168, True),
        (169, 168, False),
    ],
)
def test_threshold_sweep(hours_ago, threshold, expected_skip):
    last_seen = NOW_UTC - timedelta(hours=hours_ago)
    conn = _mock_conn(first_returns=(last_seen,))
    assert (
        should_skip(conn, "42610838", now=NOW_UTC, threshold_hours=threshold)
        is expected_skip
    )


# ─── PLATAFORMA PARAMÉTRICA ──────────────────────────────────────────────
def test_default_platform_is_airbnb():
    """El parámetro `plataforma` debe pasarse a la query."""
    conn = _mock_conn(first_returns=None)
    should_skip(conn, "42610838", now=NOW_UTC)
    # Verificar que `plataforma=airbnb` fue pasado a la query
    call_args = conn.execute.call_args
    params = call_args[0][1]  # segundo arg posicional (dict de params)
    assert params["plataforma"] == "airbnb"
    assert params["external_listing_id"] == "42610838"


def test_custom_platform_passed_through():
    conn = _mock_conn(first_returns=None)
    should_skip(conn, "42610838", now=NOW_UTC, plataforma="booking")
    call_args = conn.execute.call_args
    params = call_args[0][1]
    assert params["plataforma"] == "booking"