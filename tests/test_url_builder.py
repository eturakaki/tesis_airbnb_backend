"""Tests del constructor de URLs de PDP."""

from datetime import date

import pytest

from src.scraper.url_builder import (
    DEFAULT_ADULTS,
    DEFAULT_CHECK_IN_OFFSET,
    DEFAULT_CHECK_OUT_OFFSET,
    DOMAIN,
    InvalidListingIdError,
    InvalidWindowError,
    build_pdp_url,
    build_pdp_url_with_explicit_dates,
)

# Fecha fija para todos los tests determinísticos
REF_DATE = date(2026, 5, 9)


# ─── HAPPY PATH ──────────────────────────────────────────────────────────
def test_default_window_from_reference_date():
    url = build_pdp_url(42610838, today=REF_DATE)
    expected = (
        "https://www.airbnb.com.ar/rooms/42610838"
        "?check_in=2026-05-23&check_out=2026-05-26&adults=2"
    )
    assert url == expected


def test_listing_id_int_long():
    """Los IDs nuevos de Airbnb son 19 dígitos: 1305876403852901802."""
    url = build_pdp_url(1305876403852901802, today=REF_DATE)
    assert "/rooms/1305876403852901802?" in url


def test_listing_id_string_numeric():
    url = build_pdp_url("42610838", today=REF_DATE)
    assert "/rooms/42610838?" in url


def test_listing_id_string_with_whitespace_is_stripped():
    url = build_pdp_url("  42610838  ", today=REF_DATE)
    assert "/rooms/42610838?" in url


# ─── DOMINIO Y ESTRUCTURA ───────────────────────────────────────────────
def test_domain_is_argentina():
    """Crítico: dominio .com.ar para fijar locale (D15)."""
    url = build_pdp_url(42610838, today=REF_DATE)
    assert url.startswith("https://www.airbnb.com.ar/rooms/")
    assert ".com.ar" in url
    # No debe haber redirección implícita a .com
    assert "airbnb.com/" not in url.replace("airbnb.com.ar/", "")


def test_url_uses_iso_dates():
    """check_in/check_out en formato YYYY-MM-DD (ISO 8601)."""
    url = build_pdp_url(42610838, today=REF_DATE)
    assert "check_in=2026-05-23" in url
    assert "check_out=2026-05-26" in url


def test_url_includes_adults():
    url = build_pdp_url(42610838, today=REF_DATE)
    assert "adults=2" in url


# ─── VENTANAS PERSONALIZADAS ────────────────────────────────────────────
def test_custom_window_offsets():
    url = build_pdp_url(
        42610838,
        today=REF_DATE,
        check_in_offset=30,
        check_out_offset=37,
    )
    # 2026-05-09 + 30d = 2026-06-08 ; +37d = 2026-06-15
    assert "check_in=2026-06-08" in url
    assert "check_out=2026-06-15" in url


def test_custom_adults_count():
    url = build_pdp_url(42610838, today=REF_DATE, adults=4)
    assert "adults=4" in url


def test_default_offsets_are_14_and_17():
    """Regression test: los defaults son los de la bitácora (14, 17)."""
    assert DEFAULT_CHECK_IN_OFFSET == 14
    assert DEFAULT_CHECK_OUT_OFFSET == 17
    assert DEFAULT_ADULTS == 2


# ─── DETERMINISMO ────────────────────────────────────────────────────────
def test_today_is_injectable_for_reproducibility():
    """D12: la fecha es inyectable para garantizar reproducibilidad."""
    url1 = build_pdp_url(42610838, today=date(2026, 5, 9))
    url2 = build_pdp_url(42610838, today=date(2026, 5, 9))
    assert url1 == url2


def test_today_default_is_today():
    """Sin pasar `today`, usa date.today(). No fallar al llamarlo."""
    url = build_pdp_url(42610838)
    # La fecha exacta varía, pero la URL siempre debe ser válida
    assert "/rooms/42610838?" in url
    assert "check_in=" in url
    assert "check_out=" in url


# ─── VALIDACIÓN DE listing_id ───────────────────────────────────────────
def test_listing_id_none_raises():
    with pytest.raises(InvalidListingIdError):
        build_pdp_url(None, today=REF_DATE)


def test_listing_id_empty_string_raises():
    with pytest.raises(InvalidListingIdError):
        build_pdp_url("", today=REF_DATE)


def test_listing_id_whitespace_only_raises():
    with pytest.raises(InvalidListingIdError):
        build_pdp_url("   ", today=REF_DATE)


def test_listing_id_non_numeric_string_raises():
    with pytest.raises(InvalidListingIdError):
        build_pdp_url("abc123", today=REF_DATE)


def test_listing_id_with_dash_raises():
    with pytest.raises(InvalidListingIdError):
        build_pdp_url("42-610838", today=REF_DATE)


def test_listing_id_negative_raises():
    with pytest.raises(InvalidListingIdError):
        build_pdp_url(-42, today=REF_DATE)


def test_listing_id_zero_raises():
    with pytest.raises(InvalidListingIdError):
        build_pdp_url(0, today=REF_DATE)


def test_listing_id_float_raises():
    with pytest.raises(InvalidListingIdError):
        build_pdp_url(42.0, today=REF_DATE)


def test_listing_id_bool_raises():
    """bool es subclase de int en Python — rechazar explícitamente."""
    with pytest.raises(InvalidListingIdError):
        build_pdp_url(True, today=REF_DATE)
    with pytest.raises(InvalidListingIdError):
        build_pdp_url(False, today=REF_DATE)


# ─── VALIDACIÓN DE VENTANA ──────────────────────────────────────────────
def test_check_out_before_check_in_raises():
    with pytest.raises(InvalidWindowError):
        build_pdp_url(
            42610838, today=REF_DATE, check_in_offset=20, check_out_offset=10
        )


def test_check_out_equal_check_in_raises():
    with pytest.raises(InvalidWindowError):
        build_pdp_url(
            42610838, today=REF_DATE, check_in_offset=14, check_out_offset=14
        )


def test_negative_check_in_offset_raises():
    with pytest.raises(InvalidWindowError):
        build_pdp_url(42610838, today=REF_DATE, check_in_offset=-1)


def test_adults_zero_raises():
    with pytest.raises(InvalidWindowError):
        build_pdp_url(42610838, today=REF_DATE, adults=0)


def test_adults_negative_raises():
    with pytest.raises(InvalidWindowError):
        build_pdp_url(42610838, today=REF_DATE, adults=-2)


def test_adults_float_raises():
    with pytest.raises(InvalidWindowError):
        build_pdp_url(42610838, today=REF_DATE, adults=2.5)


def test_today_not_a_date_raises():
    with pytest.raises(InvalidWindowError):
        build_pdp_url(42610838, today="2026-05-09")


# ─── VARIANTE CON FECHAS EXPLÍCITAS ─────────────────────────────────────
def test_explicit_dates_basic():
    url = build_pdp_url_with_explicit_dates(
        42610838,
        check_in=date(2026, 7, 4),
        check_out=date(2026, 7, 11),
    )
    assert "/rooms/42610838?" in url
    assert "check_in=2026-07-04" in url
    assert "check_out=2026-07-11" in url
    assert "adults=2" in url


def test_explicit_dates_invalid_window_raises():
    with pytest.raises(InvalidWindowError):
        build_pdp_url_with_explicit_dates(
            42610838,
            check_in=date(2026, 7, 11),
            check_out=date(2026, 7, 4),
        )


def test_explicit_dates_with_custom_adults():
    url = build_pdp_url_with_explicit_dates(
        42610838,
        check_in=date(2026, 7, 4),
        check_out=date(2026, 7, 11),
        adults=3,
    )
    assert "adults=3" in url


# ─── REPRODUCIBILIDAD DEL PAYLOAD DE REFERENCIA ─────────────────────────
def test_reproduces_diagnostic_payload_url():
    """
    Regression test: este builder debe poder generar la URL exacta usada
    para capturar `_reference_bookit_real_01.json` durante el diagnóstico.
    """
    url = build_pdp_url_with_explicit_dates(
        listing_id="1560302277987248481",
        check_in=date(2026, 7, 4),
        check_out=date(2026, 7, 11),
        adults=2,
    )
    expected = (
        "https://www.airbnb.com.ar/rooms/1560302277987248481"
        "?check_in=2026-07-04&check_out=2026-07-11&adults=2"
    )
    assert url == expected