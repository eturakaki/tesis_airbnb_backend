"""
url_builder.py — Constructor de URLs de PDP de Airbnb para el scraper.

Genera URLs determinísticas con ventana de check-in/check-out fija desde una
fecha de referencia, garantizando reproducibilidad del paper.

DECISIÓN METODOLÓGICA — VENTANA TEMPORAL
=========================================
Default: check_in = today + 14 días, check_out = today + 17 días (3 noches).

Justificación:
- 14 días: lejos del "instant booking horizon" (Airbnb prioriza disponibilidad
  inmediata <7 días). Reduce ruido de cancelaciones de último momento.
- 3 noches: estadía mínima estándar en CABA (Inside Airbnb 2024-Q4: mediana
  CABA = 2 noches, p75 = 4). Largo suficiente para garantizar precio
  devuelto, corto suficiente para no activar descuentos semanales/mensuales
  que distorsionarían la comparabilidad entre listings.

DECISIÓN — DOMINIO airbnb.com.ar (NO .com)
============================================
Forzamos `.com.ar` para fijar locale=es-AR y currency=mixed (ARS/USD post-cepo).
El dominio `.com` redirige según geolocalización IP, introduciendo varianza no
controlada en el dataset. Citable como control de confounders en el apéndice.

DECISIÓN — adults=2 HARDCODEADO
=================================
Cambiar `adults` cambia el universo de listings (mínimos por listing). Fijamos
`adults=2` como caso de referencia para comparabilidad cross-listing.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Union

# ─── CONSTANTES ──────────────────────────────────────────────────────────
DOMAIN = "https://www.airbnb.com.ar"
BASE_PATH = "/rooms"

# Ventana temporal default (días desde la fecha de referencia)
DEFAULT_CHECK_IN_OFFSET = 14
DEFAULT_CHECK_OUT_OFFSET = 17  # 3 noches de estadía

# Cantidad de adultos (ver D14)
DEFAULT_ADULTS = 2

# Tipo unión para listing_id
ListingId = Union[int, str]


class InvalidListingIdError(ValueError):
    """Listing ID malformado, vacío, o no numérico."""


class InvalidWindowError(ValueError):
    """Ventana de check-in/check-out inconsistente."""


# ─── HELPERS DE VALIDACIÓN ───────────────────────────────────────────────
def _validate_listing_id(listing_id: ListingId) -> str:
    """
    Valida y normaliza un listing_id a string numérico.

    Acepta:
        int positivo:   42610838 → "42610838"
        str numérico:   "42610838" → "42610838"
        int largo:      1305876403852901802 → "1305876403852901802"

    Rechaza:
        None, "", "abc", "42-610838", floats, negativos, espacios.
    """
    if listing_id is None:
        raise InvalidListingIdError("listing_id no puede ser None")

    if isinstance(listing_id, bool):
        # bool es subclase de int en Python — rechazar explícitamente
        raise InvalidListingIdError(
            f"listing_id no puede ser bool: {listing_id!r}"
        )

    if isinstance(listing_id, int):
        if listing_id <= 0:
            raise InvalidListingIdError(
                f"listing_id debe ser positivo, recibí {listing_id}"
            )
        return str(listing_id)

    if isinstance(listing_id, str):
        cleaned = listing_id.strip()
        if not cleaned:
            raise InvalidListingIdError("listing_id es string vacío")
        if not cleaned.isdigit():
            raise InvalidListingIdError(
                f"listing_id debe contener solo dígitos, recibí {listing_id!r}"
            )
        return cleaned

    raise InvalidListingIdError(
        f"listing_id debe ser int o str numérico, recibí {type(listing_id).__name__}"
    )


def _validate_window(check_in: date, check_out: date) -> None:
    """Valida que check_out > check_in y que ambas fechas sean futuras razonables."""
    if not isinstance(check_in, date) or not isinstance(check_out, date):
        raise InvalidWindowError(
            "check_in y check_out deben ser instancias de datetime.date"
        )
    if check_out <= check_in:
        raise InvalidWindowError(
            f"check_out ({check_out}) debe ser posterior a check_in ({check_in})"
        )


# ─── FUNCIÓN PÚBLICA PRINCIPAL ───────────────────────────────────────────
def build_pdp_url(
    listing_id: ListingId,
    today: date | None = None,
    check_in_offset: int = DEFAULT_CHECK_IN_OFFSET,
    check_out_offset: int = DEFAULT_CHECK_OUT_OFFSET,
    adults: int = DEFAULT_ADULTS,
) -> str:
    """
    Construye la URL de un PDP de Airbnb para una ventana de fechas.

    Parameters
    ----------
    listing_id : int | str
        ID numérico del listing. Acepta int o str numérico.
    today : date, optional
        Fecha de referencia. Default: `date.today()`. Inyectable para tests
        y para garantizar determinismo dentro de un mismo batch (ver D12).
    check_in_offset : int
        Días desde `today` hasta check_in. Default: 14.
    check_out_offset : int
        Días desde `today` hasta check_out. Default: 17.
    adults : int
        Cantidad de huéspedes adultos. Default: 2.

    Returns
    -------
    str
        URL completa del PDP, ej:
        'https://www.airbnb.com.ar/rooms/42610838?check_in=2026-05-23&check_out=2026-05-26&adults=2'

    Raises
    ------
    InvalidListingIdError
        Si listing_id es None, vacío, no numérico, o no positivo.
    InvalidWindowError
        Si check_out_offset <= check_in_offset o si los offsets son inválidos.
    """
    # Validar listing_id
    listing_id_str = _validate_listing_id(listing_id)

    # Validar offsets
    if not isinstance(check_in_offset, int) or not isinstance(check_out_offset, int):
        raise InvalidWindowError("check_in_offset y check_out_offset deben ser int")
    if check_in_offset < 0:
        raise InvalidWindowError(
            f"check_in_offset debe ser ≥ 0, recibí {check_in_offset}"
        )
    if check_out_offset <= check_in_offset:
        raise InvalidWindowError(
            f"check_out_offset ({check_out_offset}) debe ser > check_in_offset "
            f"({check_in_offset})"
        )

    # Validar adults
    if not isinstance(adults, int) or isinstance(adults, bool):
        raise InvalidWindowError(f"adults debe ser int, recibí {type(adults).__name__}")
    if adults < 1:
        raise InvalidWindowError(f"adults debe ser ≥ 1, recibí {adults}")

    # Calcular fechas
    if today is None:
        today = date.today()
    elif not isinstance(today, date):
        raise InvalidWindowError(
            f"today debe ser datetime.date, recibí {type(today).__name__}"
        )

    check_in = today + timedelta(days=check_in_offset)
    check_out = today + timedelta(days=check_out_offset)
    _validate_window(check_in, check_out)

    # Construir URL
    return (
        f"{DOMAIN}{BASE_PATH}/{listing_id_str}"
        f"?check_in={check_in.isoformat()}"
        f"&check_out={check_out.isoformat()}"
        f"&adults={adults}"
    )


def build_pdp_url_with_explicit_dates(
    listing_id: ListingId,
    check_in: date,
    check_out: date,
    adults: int = DEFAULT_ADULTS,
) -> str:
    """
    Variante para tests y diagnóstico: fechas explícitas en lugar de offsets.

    Útil cuando se quiere replicar una captura específica del paper a partir
    de las fechas archivadas en `data/raw/airbnb_payloads/`.
    """
    listing_id_str = _validate_listing_id(listing_id)
    _validate_window(check_in, check_out)
    if not isinstance(adults, int) or isinstance(adults, bool):
        raise InvalidWindowError(f"adults debe ser int, recibí {type(adults).__name__}")
    if adults < 1:
        raise InvalidWindowError(f"adults debe ser ≥ 1, recibí {adults}")

    return (
        f"{DOMAIN}{BASE_PATH}/{listing_id_str}"
        f"?check_in={check_in.isoformat()}"
        f"&check_out={check_out.isoformat()}"
        f"&adults={adults}"
    )