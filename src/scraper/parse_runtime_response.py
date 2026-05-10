"""
parse_runtime_response.py — Parser del payload GraphQL runtime de Airbnb.

Extrae el dict `structuredDisplayPrice` del response interceptado durante la
navegación de un PDP. Complementa a `parse_payload.py` (que parsea el SSR):

    SSR (data-deferred-state-0)   →  parse_payload()           → metadata
    GraphQL runtime (StaysPdpSections) → parse_runtime_response()  → precio

DIFERENCIAS DE SHAPE CON EL SSR
================================
SSR:
    {"niobeClientData": [["StaysPdpSections:{...}", {<payload>}]]}

Runtime:
    {"data": {<payload>}, "extensions": {"traceId": "..."}}

El payload runtime trae el precio porque la dependency `BOOKING` ya está
resuelta cuando el cliente hace la query post-hidratación. El mismo
`structuredDisplayPrice` aparece duplicado en 4 placements (sidebar, floating
footer, nav, calendar). Tomamos el primero no-nulo: O(n), determinístico,
suficiente.

DECISIÓN METODOLÓGICA: NO INTERPRETAMOS EL PRECIO ACÁ
======================================================
La conversión de `structuredDisplayPrice` (dict crudo) a {price_value: float,
currency: 'ARS'|'USD', has_discount: bool, ...} se realiza en
`extract_price_components.py`. Este módulo solo extrae el dict crudo + el
`accessibilityLabel` como fallback forense. Justificación: separación de
concerns + reproducibilidad sobre payloads archivados (Stodden et al. 2016).

POLÍTICA ANTI-FALLA-SILENCIOSA
================================
Igual que `parse_payload.py`, devuelve `parse_status` ∈ {OK, PARTIAL,
SHAPE_DRIFT} + `missing_fields`. SHAPE_DRIFT actúa como alarma temprana
ante mutaciones de Airbnb durante las 12 semanas de scraping desatendido.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# ─── CONSTANTES ──────────────────────────────────────────────────────────
# Path desde el response runtime hasta la lista de sections.
SECTIONS_PATH: tuple = (
    "data",
    "presentation",
    "stayProductDetailPage",
    "sections",
    "sections",
)

# Keys dentro de section.section.structuredDisplayPrice
KEY_SDP = "structuredDisplayPrice"
KEY_PRIMARY_LINE = "primaryLine"
KEY_ACCESSIBILITY_LABEL = "accessibilityLabel"
KEY_PRICE_TEXT = "price"

# ─── D35: Discriminación PARTIAL vs SHAPE_DRIFT por sectionId ────────────
# Sections que ESPERAMOS que contengan structuredDisplayPrice. Si existen
# pero su SDP es None/vacío → PARTIAL (señal económica: 5c, no disponibilidad).
# Si NINGUNA aparece en la lista → SHAPE_DRIFT (mutación de payload).
#
# Orden = prioridad de búsqueda. SIDEBAR primero (placement principal en
# desktop), FLOATING_FOOTER como fallback (mobile/scroll).
BOOK_IT_SECTION_IDS: tuple = (
    "BOOK_IT_SIDEBAR",
    "BOOK_IT_FLOATING_FOOTER",
)

# Estados de parseo (mismo enum que parse_payload.py por consistencia)
STATUS_OK = "OK"
STATUS_PARTIAL = "PARTIAL"
STATUS_SHAPE_DRIFT = "SHAPE_DRIFT"

# Campos requeridos para considerar el parseo OK.
REQUIRED_FIELDS: tuple = (
    "structured_display_price",
    "price_raw_text",
)


# ─── HELPERS DEFENSIVOS ──────────────────────────────────────────────────
def _safe_get(obj: Any, path: Iterable, default=None):
    """Navegación defensiva por dicts/listas. Devuelve `default` ante mismatches."""
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


def _find_first_section_with_price(sections_list: Any) -> dict | None:
    """
    Recorre la lista de sections y devuelve la PRIMERA cuyo `section.structuredDisplayPrice`
    sea un dict no-vacío.

    Razón: el precio aparece duplicado en 4 placements (sidebar, floating footer,
    nav, calendar). Todas tienen el mismo dict. Tomamos la primera por
    determinismo y eficiencia O(n).
    """
    if not isinstance(sections_list, list):
        return None
    for sec in sections_list:
        if not isinstance(sec, dict):
            continue
        section_inner = sec.get("section")
        if not isinstance(section_inner, dict):
            continue
        sdp = section_inner.get(KEY_SDP)
        if isinstance(sdp, dict) and sdp:
            return section_inner
    return None

def _find_book_it_section(sections_list: Any) -> tuple[dict | None, bool]:
    """
    D35 — Búsqueda discriminante por sectionId.

    Returns:
        (section_inner, book_it_found):
        - (section_inner, True): existe BOOK_IT_* y su `section` es accesible.
                                 `section_inner` puede tener SDP no-vacío (caso OK)
                                 o SDP None/vacío (caso 5c — no-disponibilidad).
        - (None, True):          existe BOOK_IT_* pero su `section` no es dict
                                 parseable (anomalía intermedia → tratamos como
                                 sección presente pero corrupta).
        - (None, False):         NINGUNA BOOK_IT_* en la lista. Esto es
                                 SHAPE_DRIFT por D35: sin la sección esperada
                                 no podemos distinguir 'no-disponibilidad' de
                                 'mutación de payload'.

    Iteración determinística por orden de BOOK_IT_SECTION_IDS (sidebar antes
    que footer). Una sola pasada O(n·k) con k=2.
    """
    if not isinstance(sections_list, list):
        return (None, False)

    for target_id in BOOK_IT_SECTION_IDS:
        for sec in sections_list:
            if not isinstance(sec, dict):
                continue
            if sec.get("sectionId") != target_id:
                continue
            section_inner = sec.get("section")
            if isinstance(section_inner, dict):
                return (section_inner, True)
            # BOOK_IT existe pero `section` corrupto: la sección está, no la
            # podemos leer. Lo reportamos como "presente" para que caiga en
            # PARTIAL (no SHAPE_DRIFT) — Airbnb a veces manda section=null
            # transitoriamente.
            return (None, True)
    return (None, False)

def _extract_raw_text(structured_display_price: dict) -> str | None:
    """
    Extrae el `accessibilityLabel` o `price` del primaryLine como fallback forense.

    Si Airbnb muta el shape interno de `structuredDisplayPrice`, este string
    plano ("$637 USD por 7 noches") sobrevive y es parseable con regex como
    último recurso.

    Prioridad:
        1. primaryLine.accessibilityLabel  (más completo: incluye qualifier)
        2. primaryLine.price               (precio formateado, sin qualifier)
    """
    primary = structured_display_price.get(KEY_PRIMARY_LINE)
    if not isinstance(primary, dict):
        return None
    label = primary.get(KEY_ACCESSIBILITY_LABEL)
    if isinstance(label, str) and label.strip():
        return label.strip()
    price_txt = primary.get(KEY_PRICE_TEXT)
    if isinstance(price_txt, str) and price_txt.strip():
        return price_txt.strip()
    return None


# ─── FUNCIÓN PÚBLICA ─────────────────────────────────────────────────────
def parse_runtime_response(data: Any) -> dict:
    """
    Parsea el body de la respuesta GraphQL `StaysPdpSections` en runtime.

    Parameters
    ----------
    data : dict
        Body de la respuesta interceptada (response.json() de Playwright).
        Shape esperado: {"data": {"presentation": {...}}, "extensions": {...}}

    Returns
    -------
    dict
        Schema fijo:
            {
                'structured_display_price': dict | None,
                'price_raw_text': str | None,
                'parse_status': 'OK' | 'PARTIAL' | 'SHAPE_DRIFT',
                'missing_fields': list[str]
            }

        - 'OK': ambos campos presentes.
        - 'PARTIAL': uno presente, uno faltante.
        - 'SHAPE_DRIFT': ambos faltantes (probable mutación de Airbnb).

    Casos esperados de None
    ------------------------
    - Listing sin disponibilidad para las fechas (Airbnb devuelve `data.node`
      con solo `__typename`). El scraper, por la política 5c de la bitácora,
      omitirá la inserción en `precios_historicos` pero igual actualizará
      `inmuebles` y `anfitriones` con la metadata del SSR.
    """
    result = {
        "structured_display_price": None,
        "price_raw_text": None,
        "parse_status": STATUS_SHAPE_DRIFT,
        "missing_fields": list(REQUIRED_FIELDS),
    }

    # 1. Validación de shape mínimo
    if not isinstance(data, dict):
        logger.warning(
            "parse_runtime_response recibió tipo no-dict: %s",
            type(data).__name__,
        )
        return result

    # 2. Shortcut: si el node viene vacío (solo __typename), no hay precio.
    #    Esto es el comportamiento normal cuando el listing no tiene
    #    disponibilidad para las fechas pedidas.
    node = _safe_get(data, ("data", "node"))
    if isinstance(node, dict):
        non_typename_keys = [k for k in node.keys() if k != "__typename"]
        if not non_typename_keys:
            logger.info("Node vacío en runtime response (sin disponibilidad)")
            # SHAPE_DRIFT no aplica acá: es comportamiento esperado.
            # Devolvemos PARTIAL para distinguir de un payload corrupto.
            result["parse_status"] = STATUS_PARTIAL
            return result

    # 3. Navegar hasta sections
    sections_list = _safe_get(data, SECTIONS_PATH)
    if not isinstance(sections_list, list) or not sections_list:
        logger.warning("sections_list inválido o vacío en path %s", SECTIONS_PATH)
        # SHAPE_DRIFT: estructura básica del runtime no parseable.
        return result

    # 4. D35 — Búsqueda discriminante por sectionId.
    section_inner, book_it_found = _find_book_it_section(sections_list)

    if not book_it_found:
        # Caso B/C de D35: ninguna sección BOOK_IT_* en la lista.
        # Sin la sección esperada NO podemos afirmar 'no-disponibilidad';
        # podría ser una mutación del payload. SHAPE_DRIFT como alarma.
        logger.warning(
            "Ninguna sección BOOK_IT_* presente (esperadas: %s). SHAPE_DRIFT.",
            BOOK_IT_SECTION_IDS,
        )
        # result ya tiene parse_status=SHAPE_DRIFT y missing_fields completo.
        return result

    # 5. BOOK_IT presente. Extraer SDP si tiene.
    if section_inner is not None:
        sdp = section_inner.get(KEY_SDP)
        if isinstance(sdp, dict) and sdp:
            result["structured_display_price"] = sdp
            result["price_raw_text"] = _extract_raw_text(sdp)

    # 6. Calcular parse_status final.
    #    - Ambos campos presentes  → OK
    #    - Sólo SDP, sin raw_text  → PARTIAL (sub-shape interno parcial)
    #    - SDP=None pero BOOK_IT estaba → PARTIAL (caso 5c: no-disponibilidad)
    missing = [f for f in REQUIRED_FIELDS if result.get(f) is None]
    result["missing_fields"] = missing
    if not missing:
        result["parse_status"] = STATUS_OK
    else:
        # BOOK_IT existe pero algo falta → PARTIAL (no SHAPE_DRIFT).
        # Esto cubre tanto 5c (SDP=None) como sub-shape parcial
        # (SDP presente pero sin raw_text). Ambos son señales económicamente
        # válidas, no mutaciones del payload.
        result["parse_status"] = STATUS_PARTIAL

    return result