"""
parse_payload.py — Parser defensivo del SSR de Airbnb (PDP).

Extrae los campos disponibles en `<script id="data-deferred-state-0">`:
coordenadas, identificación del host, badge de superanfitrión, rating y
cantidad de reseñas.

NOTA METODOLÓGICA — PRECIO NO SE EXTRAE ACÁ
============================================
A partir de la captura del 08/05/2026 (Airbnb post-migración Niobe), el SSR
no incluye `structuredDisplayPrice` en ninguna sección. Las secciones
BOOK_IT_SIDEBAR / BOOK_IT_FLOATING_FOOTER / BOOK_IT_NAV vienen con
`sectionContentStatus: "NOT_COMPLETE"` y `sectionDependencies: ["BOOKING"]`,
indicando que el precio se carga vía POST GraphQL a /api/v3/StaysPdpSections
después de la hidratación del cliente.

Decisión: separación de concerns. Este módulo parsea SOLO el SSR. El
scraper (airbnb_scraper.py) intercepta la respuesta GraphQL y rellena
`structured_display_price` y `price_raw_text` en el dict resultante antes
del UPSERT a base de datos. Los dos campos se devuelven aquí siempre como
None con propósito documental (esquema de output estable).
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# ─── CONSTANTES ──────────────────────────────────────────────────────────
NIOBE_KEY = "niobeClientData"
QUERY_SECTIONS_PREFIX = "StaysPdpSections"

# Path desde el payload (entry[1] de niobeClientData) hasta la lista de sections.
SECTIONS_PATH: tuple = (
    "data",
    "presentation",
    "stayProductDetailPage",
    "sections",
    "sections",
)

# Section IDs relevantes
SECTION_LOCATION = "LOCATION_DEFAULT"
SECTION_HOST = "MEET_YOUR_HOST"

# Stat types dentro de MEET_YOUR_HOST.section.cardData.stats[]
STAT_RATING = "RATING"
STAT_REVIEWS = "REVIEW_COUNT"

# Estados de parseo
STATUS_OK = "OK"
STATUS_PARTIAL = "PARTIAL"
STATUS_SHAPE_DRIFT = "SHAPE_DRIFT"

# Campos requeridos para considerar el parseo OK. NO incluye precio porque
# el precio no proviene del SSR (ver docstring del módulo).
REQUIRED_FIELDS: tuple = (
    "latitud",
    "longitud",
    "host_id",
    "is_superhost",
    "rating",
    "reviews_count",
)


# ─── HELPERS DEFENSIVOS ──────────────────────────────────────────────────
def _safe_get(obj: Any, path: Iterable, default=None):
    """Navegación defensiva por dicts/listas siguiendo keys/índices.

    Devuelve `default` ante cualquier mismatch de tipo o índice fuera de
    rango, sin levantar excepciones.
    """
    cur = obj
    for step in path:
        if isinstance(cur, dict) and isinstance(step, str):
            cur = cur.get(step)
        elif isinstance(cur, list) and isinstance(step, int):
            if -len(cur) <= step < len(cur):
                cur = cur[step]
            else:
                return default
        else:
            return default
        if cur is None:
            return default
    return cur


def _find_query(niobe_data: Any, query_prefix: str):
    """Busca una entrada de niobeClientData cuya query contenga `query_prefix`.

    Robusto a reordenamiento (busca por substring, no por índice) y a
    entradas malformadas (None, strings sueltas, listas mal formadas).
    """
    if not isinstance(niobe_data, list):
        return None
    for entry in niobe_data:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        query_str = entry[0]
        if not isinstance(query_str, str):
            continue
        if query_prefix in query_str:
            return entry[1]
    return None


def _find_section(sections_list: Any, section_id: str):
    """Busca una section por sectionId. Acepta también la key 'id' como fallback."""
    if not isinstance(sections_list, list):
        return None
    for sec in sections_list:
        if not isinstance(sec, dict):
            continue
        if sec.get("sectionId") == section_id or sec.get("id") == section_id:
            return sec
    return None


def _coerce_float(value):
    """Convierte a float aceptando strings con coma decimal (es-AR: '4,96')."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "."))
        except (ValueError, AttributeError):
            return None
    return None


def _coerce_int(value):
    """Convierte a int aceptando strings con separadores de miles."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.replace(".", "").replace(",", "").strip()
        try:
            return int(cleaned)
        except (ValueError, AttributeError):
            return None
    return None


def _decode_user_id(raw):
    """Decodifica userId de Airbnb: base64('DemandUser:NUMBER') → 'NUMBER'.

    Si el input no es base64 válido o no tiene el formato esperado, devuelve
    el string original como fallback (mejor preservar info ambigua que
    perderla).
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        return str(raw)
    try:
        decoded = base64.b64decode(raw, validate=False).decode("utf-8", errors="replace")
        if ":" in decoded:
            candidate = decoded.split(":")[-1].strip()
            if candidate:
                return candidate
    except Exception:
        pass
    return raw


# ─── FUNCIÓN PÚBLICA ─────────────────────────────────────────────────────
def parse_payload(data: Any) -> dict:
    """
    Parsea el contenido del <script id="data-deferred-state-0"> de un PDP.

    Returns
    -------
    dict
        Schema fijo:
            {
                'latitud': float | None,
                'longitud': float | None,
                'host_id': str | None,
                'is_superhost': bool | None,
                'rating': float | None,
                'reviews_count': int | None,
                'structured_display_price': None,    # rellenado por el scraper
                'price_raw_text': None,              # rellenado por el scraper
                'parse_status': 'OK' | 'PARTIAL' | 'SHAPE_DRIFT',
                'missing_fields': list[str]
            }
    """
    result = {
        "latitud": None,
        "longitud": None,
        "host_id": None,
        "is_superhost": None,
        "rating": None,
        "reviews_count": None,
        # Campos de precio: NO provienen del SSR. El scraper los rellena
        # interceptando la respuesta GraphQL POST a /api/v3/StaysPdpSections.
        # Ver docstring del módulo y bitácora 08/05/2026.
        "structured_display_price": None,
        "price_raw_text": None,
        "parse_status": STATUS_SHAPE_DRIFT,
        "missing_fields": list(REQUIRED_FIELDS),
    }

    # 1. Validación de shape mínimo
    if not isinstance(data, dict):
        logger.warning("parse_payload recibió tipo no-dict: %s", type(data).__name__)
        return result

    niobe = data.get(NIOBE_KEY)
    if not isinstance(niobe, list) or not niobe:
        logger.warning("niobeClientData ausente, vacío, o no es lista")
        return result

    # 2. Localizar la query StaysPdpSections (post-migración 2024-2025: query
    #    unificada, ya no hay SEO separada).
    pdp_payload = _find_query(niobe, QUERY_SECTIONS_PREFIX)
    if pdp_payload is None:
        logger.warning("Query %s no encontrada en niobeClientData", QUERY_SECTIONS_PREFIX)
        return result

    # 3. Extraer la lista de sections
    sections_list = _safe_get(pdp_payload, SECTIONS_PATH)
    if not isinstance(sections_list, list):
        logger.warning("sections_list no es lista en path %s", SECTIONS_PATH)
        return result

    # 4. LOCATION_DEFAULT → lat / lng
    loc_section = _find_section(sections_list, SECTION_LOCATION)
    if loc_section is not None:
        sec = _safe_get(loc_section, ("section",))
        if isinstance(sec, dict):
            result["latitud"] = _coerce_float(sec.get("lat"))
            result["longitud"] = _coerce_float(sec.get("lng"))

    # 5. MEET_YOUR_HOST → host_id, is_superhost, rating, reviews_count
    host_section = _find_section(sections_list, SECTION_HOST)
    if host_section is not None:
        card = _safe_get(host_section, ("section", "cardData"))
        if isinstance(card, dict):
            result["host_id"] = _decode_user_id(card.get("userId"))

            superhost_raw = card.get("isSuperhost")
            if isinstance(superhost_raw, bool):
                result["is_superhost"] = superhost_raw
            elif isinstance(superhost_raw, str):
                result["is_superhost"] = superhost_raw.strip().lower() == "true"

            stats = card.get("stats")
            if isinstance(stats, list):
                for stat in stats:
                    if not isinstance(stat, dict):
                        continue
                    stat_type = stat.get("type")
                    stat_value = stat.get("value")
                    if stat_type == STAT_RATING:
                        result["rating"] = _coerce_float(stat_value)
                    elif stat_type == STAT_REVIEWS:
                        result["reviews_count"] = _coerce_int(stat_value)

    # 6. Calcular parse_status y missing_fields
    missing = [f for f in REQUIRED_FIELDS if result.get(f) is None]
    result["missing_fields"] = missing
    if not missing:
        result["parse_status"] = STATUS_OK
    elif len(missing) < len(REQUIRED_FIELDS):
        result["parse_status"] = STATUS_PARTIAL
    else:
        result["parse_status"] = STATUS_SHAPE_DRIFT

    return result