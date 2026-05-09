"""Tests del parser SSR de Airbnb. Adaptados al shape post-migración Niobe (08/05/2026)."""

import base64
import json
from pathlib import Path

import pytest

from src.scraper.parse_payload import (
    REQUIRED_FIELDS,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_SHAPE_DRIFT,
    _find_query,
    _find_section,
    _safe_get,
    parse_payload,
)

REFERENCE_PAYLOAD = Path("data/raw/airbnb_payloads/_reference_pdp_20260508.json")


# ─── HELPERS DE FIXTURES ────────────────────────────────────────────────
def _make_user_id(num: int) -> str:
    """Genera userId base64 al estilo Airbnb: 'DemandUser:N' → b64."""
    return base64.b64encode(f"DemandUser:{num}".encode()).decode()


@pytest.fixture
def synthetic_payload():
    """Payload sintético con shape válido StaysPdpSections."""
    return {
        "niobeClientData": [
            [
                "StaysPdpSections:{...query_args_truncated...}",
                {
                    "data": {
                        "presentation": {
                            "stayProductDetailPage": {
                                "sections": {
                                    "sections": [
                                        {
                                            "sectionId": "LOCATION_DEFAULT",
                                            "section": {
                                                "lat": -34.5795,
                                                "lng": -58.4284,
                                            },
                                        },
                                        {
                                            "sectionId": "MEET_YOUR_HOST",
                                            "section": {
                                                "cardData": {
                                                    "userId": _make_user_id(50778624),
                                                    "isSuperhost": True,
                                                    "stats": [
                                                        {"type": "RATING", "value": "4.89"},
                                                        {"type": "REVIEW_COUNT", "value": "927"},
                                                    ],
                                                }
                                            },
                                        },
                                    ]
                                }
                            }
                        }
                    }
                },
            ]
        ]
    }


def _get_sections(payload):
    """Atajo para llegar a la lista de sections en el fixture."""
    return payload["niobeClientData"][0][1]["data"]["presentation"][
        "stayProductDetailPage"
    ]["sections"]["sections"]


# ─── HAPPY PATH ─────────────────────────────────────────────────────────
def test_synthetic_happy_path(synthetic_payload):
    r = parse_payload(synthetic_payload)
    assert r["parse_status"] == STATUS_OK
    assert r["latitud"] == pytest.approx(-34.5795)
    assert r["longitud"] == pytest.approx(-58.4284)
    assert r["host_id"] == "50778624"
    assert r["is_superhost"] is True
    assert r["rating"] == pytest.approx(4.89)
    assert r["reviews_count"] == 927
    assert r["missing_fields"] == []


# ─── COERCIÓN DE TIPOS ──────────────────────────────────────────────────
def test_rating_with_spanish_comma(synthetic_payload):
    """Airbnb es-AR devuelve rating como '4,96'."""
    for s in _get_sections(synthetic_payload):
        if s["sectionId"] == "MEET_YOUR_HOST":
            for stat in s["section"]["cardData"]["stats"]:
                if stat["type"] == "RATING":
                    stat["value"] = "4,96"
    r = parse_payload(synthetic_payload)
    assert r["rating"] == pytest.approx(4.96)


def test_reviews_count_coerced_to_int(synthetic_payload):
    r = parse_payload(synthetic_payload)
    assert isinstance(r["reviews_count"], int)
    assert r["reviews_count"] == 927


def test_host_id_decoded_from_base64(synthetic_payload):
    r = parse_payload(synthetic_payload)
    assert r["host_id"] == "50778624"
    assert isinstance(r["host_id"], str)


def test_coords_string_to_float(synthetic_payload):
    """Si lat/lng vienen como string, se coercen."""
    for s in _get_sections(synthetic_payload):
        if s["sectionId"] == "LOCATION_DEFAULT":
            s["section"]["lat"] = "-34.5795"
            s["section"]["lng"] = "-58.4284"
    r = parse_payload(synthetic_payload)
    assert r["latitud"] == pytest.approx(-34.5795)
    assert r["longitud"] == pytest.approx(-58.4284)


def test_superhost_string_form(synthetic_payload):
    """is_superhost también acepta 'true' como string."""
    for s in _get_sections(synthetic_payload):
        if s["sectionId"] == "MEET_YOUR_HOST":
            s["section"]["cardData"]["isSuperhost"] = "true"
    r = parse_payload(synthetic_payload)
    assert r["is_superhost"] is True


def test_corrupted_coords_fall_to_none(synthetic_payload):
    for s in _get_sections(synthetic_payload):
        if s["sectionId"] == "LOCATION_DEFAULT":
            s["section"]["lat"] = "no-soy-numero"
    r = parse_payload(synthetic_payload)
    assert r["latitud"] is None
    assert r["parse_status"] == STATUS_PARTIAL


# ─── PRECIO SIEMPRE NULL EN SSR ─────────────────────────────────────────
def test_price_fields_always_null_from_ssr(synthetic_payload):
    """El SSR no contiene precio. Estos campos los rellena el scraper."""
    r = parse_payload(synthetic_payload)
    assert r["structured_display_price"] is None
    assert r["price_raw_text"] is None
    # Status debe seguir siendo OK porque precio NO está en REQUIRED_FIELDS
    assert r["parse_status"] == STATUS_OK


# ─── DEFENSIVE PARSING ──────────────────────────────────────────────────
def test_empty_dict_returns_shape_drift():
    r = parse_payload({})
    assert r["parse_status"] == STATUS_SHAPE_DRIFT
    assert set(r["missing_fields"]) == set(REQUIRED_FIELDS)


def test_missing_niobe_data():
    r = parse_payload({"otherKey": "something"})
    assert r["parse_status"] == STATUS_SHAPE_DRIFT


def test_niobe_data_not_list():
    r = parse_payload({"niobeClientData": "not a list"})
    assert r["parse_status"] == STATUS_SHAPE_DRIFT


def test_query_not_found():
    r = parse_payload({"niobeClientData": [["OtherQueryName:{...}", {}]]})
    assert r["parse_status"] == STATUS_SHAPE_DRIFT


def test_partial_data_only_location(synthetic_payload):
    """Solo LOCATION_DEFAULT presente, MEET_YOUR_HOST ausente → PARTIAL."""
    sections = _get_sections(synthetic_payload)
    synthetic_payload["niobeClientData"][0][1]["data"]["presentation"][
        "stayProductDetailPage"
    ]["sections"]["sections"] = [s for s in sections if s["sectionId"] == "LOCATION_DEFAULT"]
    r = parse_payload(synthetic_payload)
    assert r["parse_status"] == STATUS_PARTIAL
    assert r["latitud"] is not None
    assert r["host_id"] is None


def test_does_not_crash_on_random_garbage():
    parse_payload(None)
    parse_payload([])
    parse_payload("")
    parse_payload(12345)
    parse_payload({"niobeClientData": [None, None, None]})
    parse_payload({"niobeClientData": [["bad", "format"]]})
    parse_payload({"niobeClientData": [["StaysPdpSections", "string-not-dict"]]})


# ─── HELPERS ────────────────────────────────────────────────────────────
def test_safe_get_dict_navigation():
    obj = {"a": {"b": {"c": 42}}}
    assert _safe_get(obj, ("a", "b", "c")) == 42
    assert _safe_get(obj, ("a", "x", "c")) is None
    assert _safe_get(obj, ("a", "b", "c", "d")) is None


def test_safe_get_list_navigation():
    obj = {"items": [10, 20, 30]}
    assert _safe_get(obj, ("items", 1)) == 20
    assert _safe_get(obj, ("items", 99)) is None


def test_find_query_matches_substring():
    niobe = [["StaysPdpSections:{json...}", {"payload": "yes"}]]
    assert _find_query(niobe, "StaysPdpSections") == {"payload": "yes"}
    assert _find_query(niobe, "OtherQuery") is None


def test_find_query_handles_malformed_entries():
    niobe = [None, "string", [1], ["only one"], ["StaysPdpSections", {"ok": True}]]
    assert _find_query(niobe, "StaysPdpSections") == {"ok": True}


def test_find_section_by_id():
    sections = [
        {"sectionId": "A", "data": 1},
        {"sectionId": "B", "data": 2},
    ]
    assert _find_section(sections, "B")["data"] == 2
    assert _find_section(sections, "Z") is None


# ─── PAYLOAD REAL (08/05/2026) ──────────────────────────────────────────
@pytest.mark.skipif(
    not REFERENCE_PAYLOAD.exists(),
    reason=f"No se encontró el payload de referencia en {REFERENCE_PAYLOAD}.",
)
def test_real_payload_reference():
    """
    Validación contra payload real capturado el 08/05/2026.

    Debe pasar 100% verde con las rutas StaysPdpSections. El precio NO
    se valida acá: viene null en el SSR por diseño de Airbnb.
    """
    with open(REFERENCE_PAYLOAD, "r", encoding="utf-8") as f:
        data = json.load(f)

    result = parse_payload(data)

    print("\n" + "=" * 60)
    print("RESULTADO PARSEO PAYLOAD REAL (08/05/2026)")
    print("=" * 60)
    for key, value in result.items():
        if key == "structured_display_price" and value is not None:
            print(f"  {key}: <dict con keys: {list(value.keys())}>")
        else:
            print(f"  {key}: {value!r}")
    print("=" * 60)

    assert result["parse_status"] != STATUS_SHAPE_DRIFT, (
        "SHAPE_DRIFT total: revisá las constantes en parse_payload.py."
    )
    assert result["latitud"] is not None, "lat ausente: ruta LOCATION_DEFAULT.section.lat rota"
    assert result["longitud"] is not None, "lng ausente: ruta LOCATION_DEFAULT.section.lng rota"
    assert result["host_id"] is not None, "host_id ausente: ruta MEET_YOUR_HOST.section.cardData.userId rota"
    assert result["is_superhost"] is not None, "is_superhost ausente"
    assert result["rating"] is not None, "rating ausente: stats[type=RATING] no encontrado"
    assert result["reviews_count"] is not None, "reviews_count ausente: stats[type=REVIEW_COUNT] no encontrado"
    assert result["parse_status"] == STATUS_OK

    # Precio: confirmamos que sigue null (comportamiento esperado del SSR)
    assert result["structured_display_price"] is None, (
        "structured_display_price debería ser None en el SSR — el precio "
        "se rellena desde el interceptor GraphQL del scraper."
    )