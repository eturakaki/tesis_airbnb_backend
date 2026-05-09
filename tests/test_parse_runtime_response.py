"""Tests del parser de la respuesta GraphQL runtime de Airbnb (StaysPdpSections)."""

import json
from pathlib import Path

import pytest

from src.scraper.parse_runtime_response import (
    REQUIRED_FIELDS,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_SHAPE_DRIFT,
    _extract_raw_text,
    _find_first_section_with_price,
    _safe_get,
    parse_runtime_response,
)

# Payload runtime real capturado el 09/05/2026 (durante diagnóstico de scraper)
REAL_RUNTIME_PAYLOAD = Path(
    "data/raw/airbnb_payloads/_diagnostic/StaysPdpSections_200.json"
)


# ─── FIXTURES ────────────────────────────────────────────────────────────
@pytest.fixture
def synthetic_sdp():
    """Mini structuredDisplayPrice con shape mínimo válido."""
    return {
        "__typename": "StructuredDisplayPrice",
        "primaryLine": {
            "__typename": "QualifiedDisplayPriceLine",
            "accessibilityLabel": "$637 USD por 7 noches",
            "price": "$637 USD",
            "qualifier": "por 7 noches",
        },
        "secondaryLine": None,
        "explanationData": None,
        "displayPriceStyle": "TOTAL_ONLY",
    }


@pytest.fixture
def synthetic_runtime_payload(synthetic_sdp):
    """Payload runtime sintético con shape válido y precio en sections[0]."""
    return {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_SIDEBAR",
                                "section": {
                                    "__typename": "BookItSection",
                                    "structuredDisplayPrice": synthetic_sdp,
                                },
                            },
                        ]
                    }
                }
            },
            "node": {
                "__typename": "DemandStayListing",
                "id": "U2VjdGlvbjox",
            },
        },
        "extensions": {"traceId": "fake-trace-id"},
    }


# ─── HAPPY PATH ──────────────────────────────────────────────────────────
def test_synthetic_happy_path(synthetic_runtime_payload, synthetic_sdp):
    r = parse_runtime_response(synthetic_runtime_payload)
    assert r["parse_status"] == STATUS_OK
    assert r["structured_display_price"] == synthetic_sdp
    assert r["price_raw_text"] == "$637 USD por 7 noches"
    assert r["missing_fields"] == []


# ─── DUPLICADO EN MÚLTIPLES SECTIONS: TOMAR EL PRIMERO ──────────────────
def test_takes_first_section_with_price(synthetic_sdp):
    """Si el precio aparece en múltiples sections, devuelve el primero."""
    second_sdp = {**synthetic_sdp, "primaryLine": {"accessibilityLabel": "$999 USD"}}
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "section": {
                                    "structuredDisplayPrice": synthetic_sdp,
                                }
                            },
                            {
                                "section": {
                                    "structuredDisplayPrice": second_sdp,
                                }
                            },
                        ]
                    }
                }
            },
            "node": {"__typename": "X", "id": "y"},
        }
    }
    r = parse_runtime_response(payload)
    assert r["parse_status"] == STATUS_OK
    assert r["structured_display_price"] == synthetic_sdp
    assert r["price_raw_text"] == "$637 USD por 7 noches"


# ─── SECTIONS INTERCALADAS SIN PRECIO ───────────────────────────────────
def test_skips_sections_without_price(synthetic_sdp):
    """Sections con structuredDisplayPrice = None se saltean."""
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {"section": {"structuredDisplayPrice": None}},
                            {"section": {"otherKey": "noise"}},
                            {"section": {"structuredDisplayPrice": synthetic_sdp}},
                        ]
                    }
                }
            },
            "node": {"__typename": "X", "id": "y"},
        }
    }
    r = parse_runtime_response(payload)
    assert r["parse_status"] == STATUS_OK
    assert r["structured_display_price"] == synthetic_sdp


# ─── NODO VACÍO (LISTING SIN DISPONIBILIDAD) ────────────────────────────
def test_empty_node_returns_partial_not_shape_drift():
    """Cuando Airbnb devuelve solo __typename en data.node = sin disponibilidad."""
    payload = {
        "data": {"node": {"__typename": "DemandStayListing"}},
        "extensions": {"traceId": "x"},
    }
    r = parse_runtime_response(payload)
    # PARTIAL, no SHAPE_DRIFT: es comportamiento esperado, no mutación.
    assert r["parse_status"] == STATUS_PARTIAL
    assert r["structured_display_price"] is None
    assert r["price_raw_text"] is None


# ─── FALLBACK DE RAW TEXT ───────────────────────────────────────────────
def test_raw_text_falls_back_to_price_when_no_label():
    """Si no hay accessibilityLabel, usar primaryLine.price."""
    sdp = {
        "primaryLine": {
            "price": "$15.000 ARS",
            # accessibilityLabel ausente
        }
    }
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [{"section": {"structuredDisplayPrice": sdp}}]
                    }
                }
            },
            "node": {"__typename": "X", "id": "y"},
        }
    }
    r = parse_runtime_response(payload)
    assert r["parse_status"] == STATUS_OK
    assert r["price_raw_text"] == "$15.000 ARS"


def test_raw_text_none_if_neither_label_nor_price():
    """Si ni accessibilityLabel ni price están, raw_text es None pero sdp se mantiene."""
    sdp = {"primaryLine": {"otherField": "x"}, "displayPriceStyle": "TOTAL_ONLY"}
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [{"section": {"structuredDisplayPrice": sdp}}]
                    }
                }
            },
            "node": {"__typename": "X", "id": "y"},
        }
    }
    r = parse_runtime_response(payload)
    # PARTIAL: sdp presente, raw_text faltante
    assert r["parse_status"] == STATUS_PARTIAL
    assert r["structured_display_price"] == sdp
    assert r["price_raw_text"] is None


# ─── DEFENSIVE PARSING ──────────────────────────────────────────────────
def test_empty_dict_returns_shape_drift():
    r = parse_runtime_response({})
    assert r["parse_status"] == STATUS_SHAPE_DRIFT
    assert set(r["missing_fields"]) == set(REQUIRED_FIELDS)


def test_missing_data_key():
    r = parse_runtime_response({"extensions": {"traceId": "x"}})
    assert r["parse_status"] == STATUS_SHAPE_DRIFT


def test_sections_not_a_list():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {"sections": {"sections": "not a list"}}
            },
            "node": {"__typename": "X", "id": "y"},
        }
    }
    r = parse_runtime_response(payload)
    assert r["parse_status"] == STATUS_SHAPE_DRIFT


def test_sections_empty_list():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {"sections": {"sections": []}}
            },
            "node": {"__typename": "X", "id": "y"},
        }
    }
    r = parse_runtime_response(payload)
    assert r["parse_status"] == STATUS_SHAPE_DRIFT


def test_does_not_crash_on_random_garbage():
    parse_runtime_response(None)
    parse_runtime_response([])
    parse_runtime_response("")
    parse_runtime_response(12345)
    parse_runtime_response({"data": "not a dict"})
    parse_runtime_response({"data": {"presentation": None}})


# ─── HELPERS ─────────────────────────────────────────────────────────────
def test_safe_get_dict_navigation():
    obj = {"a": {"b": {"c": 42}}}
    assert _safe_get(obj, ("a", "b", "c")) == 42
    assert _safe_get(obj, ("a", "x", "c")) is None


def test_find_first_section_with_price_returns_none_for_empty():
    assert _find_first_section_with_price([]) is None
    assert _find_first_section_with_price(None) is None
    assert _find_first_section_with_price("not a list") is None


def test_find_first_section_handles_malformed_entries():
    sections = [
        None,
        "string",
        {"section": None},
        {"section": "not a dict"},
        {"section": {"structuredDisplayPrice": {}}},  # dict vacío → no cuenta
        {"section": {"structuredDisplayPrice": {"valid": True}}},
    ]
    result = _find_first_section_with_price(sections)
    assert result == {"structuredDisplayPrice": {"valid": True}}


def test_extract_raw_text_priority():
    """accessibilityLabel tiene prioridad sobre price."""
    sdp = {
        "primaryLine": {
            "accessibilityLabel": "label-wins",
            "price": "price-loses",
        }
    }
    assert _extract_raw_text(sdp) == "label-wins"


def test_extract_raw_text_strips_whitespace():
    sdp = {"primaryLine": {"accessibilityLabel": "  $100 USD  "}}
    assert _extract_raw_text(sdp) == "$100 USD"


# ─── PAYLOAD REAL (validación de regresión) ─────────────────────────────
@pytest.mark.skipif(
    not REAL_RUNTIME_PAYLOAD.exists(),
    reason=f"No se encontró el payload runtime de referencia en {REAL_RUNTIME_PAYLOAD}.",
)
def test_real_runtime_payload():
    """
    Validación contra el payload runtime real capturado durante el diagnóstico
    del 09/05/2026 (listing 1560302277987248481, fechas 2026-07-04 a 2026-07-11,
    precio $637 USD por 7 noches).
    """
    with open(REAL_RUNTIME_PAYLOAD, "r", encoding="utf-8") as f:
        data = json.load(f)

    result = parse_runtime_response(data)

    print("\n" + "=" * 60)
    print("RESULTADO PARSEO RUNTIME PAYLOAD REAL")
    print("=" * 60)
    print(f"  parse_status: {result['parse_status']!r}")
    print(f"  price_raw_text: {result['price_raw_text']!r}")
    sdp = result["structured_display_price"]
    if sdp is not None:
        print(f"  structured_display_price keys: {list(sdp.keys())}")
        primary = sdp.get("primaryLine", {})
        if isinstance(primary, dict):
            print(f"    primaryLine.price: {primary.get('price')!r}")
            print(f"    primaryLine.qualifier: {primary.get('qualifier')!r}")
    print("=" * 60)

    assert result["parse_status"] == STATUS_OK, (
        f"Esperaba OK contra el payload real, obtuve {result['parse_status']}. "
        f"Missing: {result['missing_fields']}"
    )
    assert result["structured_display_price"] is not None
    assert isinstance(result["structured_display_price"], dict)
    assert result["price_raw_text"] is not None
    # El listing real conocido: $637 USD por 7 noches
    assert "USD" in result["price_raw_text"], (
        f"raw_text esperado con 'USD', obtuve: {result['price_raw_text']!r}"
    )