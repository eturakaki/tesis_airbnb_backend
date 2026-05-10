"""Tests del parser de la respuesta GraphQL runtime de Airbnb (StaysPdpSections)."""

import json
from pathlib import Path

import pytest

from src.scraper.parse_runtime_response import (
    BOOK_IT_SECTION_IDS,
    REQUIRED_FIELDS,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_SHAPE_DRIFT,
    _extract_raw_text,
    _find_book_it_section,
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
    """Si BOOK_IT_SIDEBAR aparece, se devuelve su SDP (prioridad sobre FOOTER)."""
    second_sdp = {**synthetic_sdp, "primaryLine": {"accessibilityLabel": "$999 USD"}}
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_SIDEBAR",
                                "section": {"structuredDisplayPrice": synthetic_sdp},
                            },
                            {
                                "sectionId": "BOOK_IT_FLOATING_FOOTER",
                                "section": {"structuredDisplayPrice": second_sdp},
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
    """Sections con structuredDisplayPrice = None se saltean si la BOOK_IT con SDP existe."""
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "DESCRIPTION_DEFAULT",
                                "section": {"otherKey": "noise"},
                            },
                            {
                                "sectionId": "BOOK_IT_SIDEBAR",
                                "section": {"structuredDisplayPrice": synthetic_sdp},
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
                        "sections": [{"sectionId": "BOOK_IT_SIDEBAR", "section": {"structuredDisplayPrice": sdp}}]
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
                        "sections": [{"sectionId": "BOOK_IT_SIDEBAR", "section": {"structuredDisplayPrice": sdp}}]
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


# ─── D35: Discriminación PARTIAL vs SHAPE_DRIFT por sectionId ────────────

class TestD35Discrimination:
    """
    D35 — Bajo regla estricta:
    - BOOK_IT_* presente con SDP=None  → PARTIAL  (señal económica: 5c)
    - BOOK_IT_* presente con SDP=dict  → OK
    - Ninguna BOOK_IT_* en sections    → SHAPE_DRIFT (mutación payload)
    """

    def test_book_it_sidebar_with_null_sdp_yields_partial_not_shape_drift(self):
        """
        Caso A (5c): BOOK_IT_SIDEBAR existe pero structuredDisplayPrice=None.
        Interpretación económica: listing existe, no disponible en la ventana.
        El orquestador upserts metadata sin precio (política 5c).
        """
        payload = {
            "data": {
                "presentation": {
                    "stayProductDetailPage": {
                        "sections": {
                            "sections": [
                                {
                                    "sectionId": "BOOK_IT_SIDEBAR",
                                    "section": {
                                        "__typename": "BookItSection",
                                        "structuredDisplayPrice": None,
                                    },
                                },
                                {
                                    "sectionId": "DESCRIPTION_DEFAULT",
                                    "section": {"otherKey": "noise"},
                                },
                            ]
                        }
                    }
                },
                "node": {"__typename": "DemandStayListing", "id": "abc"},
            }
        }
        r = parse_runtime_response(payload)
        assert r["parse_status"] == STATUS_PARTIAL
        assert r["structured_display_price"] is None
        assert r["price_raw_text"] is None

    def test_no_book_it_section_yields_shape_drift(self):
        """
        Caso B: hay sections, pero NINGUNA es BOOK_IT_*.
        Sin la sección esperada no podemos afirmar no-disponibilidad.
        D35 dictamina SHAPE_DRIFT como alarma temprana de mutación.
        """
        payload = {
            "data": {
                "presentation": {
                    "stayProductDetailPage": {
                        "sections": {
                            "sections": [
                                {
                                    "sectionId": "DESCRIPTION_DEFAULT",
                                    "section": {"text": "Hermoso depto"},
                                },
                                {
                                    "sectionId": "AMENITIES_DEFAULT",
                                    "section": {"items": []},
                                },
                            ]
                        }
                    }
                },
                "node": {"__typename": "DemandStayListing", "id": "abc"},
            }
        }
        r = parse_runtime_response(payload)
        assert r["parse_status"] == STATUS_SHAPE_DRIFT
        assert r["structured_display_price"] is None
        assert set(r["missing_fields"]) == set(REQUIRED_FIELDS)

    def test_only_unrelated_sections_yields_shape_drift(self):
        """
        Caso C: una sola sección no relacionada con precio.
        Mismo veredicto que Caso B — sin BOOK_IT_*, no hay señal económica
        defendible. SHAPE_DRIFT.
        """
        payload = {
            "data": {
                "presentation": {
                    "stayProductDetailPage": {
                        "sections": {
                            "sections": [
                                {
                                    "sectionId": "DESCRIPTION_DEFAULT",
                                    "section": {"text": "Solo descripción"},
                                },
                            ]
                        }
                    }
                },
                "node": {"__typename": "DemandStayListing", "id": "abc"},
            }
        }
        r = parse_runtime_response(payload)
        assert r["parse_status"] == STATUS_SHAPE_DRIFT

    def test_floating_footer_used_when_sidebar_absent(self, synthetic_sdp):
        """
        BOOK_IT_FLOATING_FOOTER es fallback válido si SIDEBAR no aparece.
        Payload mobile típico: solo footer, no sidebar.
        """
        payload = {
            "data": {
                "presentation": {
                    "stayProductDetailPage": {
                        "sections": {
                            "sections": [
                                {
                                    "sectionId": "BOOK_IT_FLOATING_FOOTER",
                                    "section": {"structuredDisplayPrice": synthetic_sdp},
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

    def test_sidebar_with_corrupt_inner_section_yields_partial(self):
        """
        Edge case: BOOK_IT_SIDEBAR existe pero `section` no es dict (Airbnb
        a veces devuelve null transitoriamente). Tratamos como "sección
        presente pero ilegible" → PARTIAL, no SHAPE_DRIFT.
        """
        payload = {
            "data": {
                "presentation": {
                    "stayProductDetailPage": {
                        "sections": {
                            "sections": [
                                {"sectionId": "BOOK_IT_SIDEBAR", "section": None},
                            ]
                        }
                    }
                },
                "node": {"__typename": "X", "id": "y"},
            }
        }
        r = parse_runtime_response(payload)
        assert r["parse_status"] == STATUS_PARTIAL


class TestFindBookItSection:
    """Unit tests del helper _find_book_it_section."""

    def test_returns_none_false_when_no_book_it(self):
        sections = [
            {"sectionId": "DESCRIPTION_DEFAULT", "section": {}},
            {"sectionId": "AMENITIES_DEFAULT", "section": {}},
        ]
        result = _find_book_it_section(sections)
        assert result == (None, False)

    def test_returns_section_true_when_sidebar_present(self):
        target_section = {"structuredDisplayPrice": {"x": 1}}
        sections = [
            {"sectionId": "DESCRIPTION_DEFAULT", "section": {}},
            {"sectionId": "BOOK_IT_SIDEBAR", "section": target_section},
        ]
        section_inner, found = _find_book_it_section(sections)
        assert found is True
        assert section_inner == target_section

    def test_sidebar_priority_over_footer(self):
        sidebar_section = {"structuredDisplayPrice": {"id": "sidebar"}}
        footer_section = {"structuredDisplayPrice": {"id": "footer"}}
        sections = [
            {"sectionId": "BOOK_IT_FLOATING_FOOTER", "section": footer_section},
            {"sectionId": "BOOK_IT_SIDEBAR", "section": sidebar_section},
        ]
        section_inner, found = _find_book_it_section(sections)
        assert found is True
        assert section_inner == sidebar_section

    def test_footer_used_when_sidebar_absent(self):
        footer_section = {"structuredDisplayPrice": {"id": "footer"}}
        sections = [
            {"sectionId": "BOOK_IT_FLOATING_FOOTER", "section": footer_section},
        ]
        section_inner, found = _find_book_it_section(sections)
        assert found is True
        assert section_inner == footer_section

    def test_book_it_with_corrupt_section_returns_none_true(self):
        """BOOK_IT existe pero section no es dict → (None, True)."""
        sections = [{"sectionId": "BOOK_IT_SIDEBAR", "section": "not a dict"}]
        result = _find_book_it_section(sections)
        assert result == (None, True)

    def test_handles_non_list_input(self):
        assert _find_book_it_section(None) == (None, False)
        assert _find_book_it_section("string") == (None, False)
        assert _find_book_it_section({}) == (None, False)

    def test_constant_order_matches_expectation(self):
        """SIDEBAR antes que FOOTER (decisión metodológica D35)."""
        assert BOOK_IT_SECTION_IDS[0] == "BOOK_IT_SIDEBAR"
        assert BOOK_IT_SECTION_IDS[1] == "BOOK_IT_FLOATING_FOOTER"


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