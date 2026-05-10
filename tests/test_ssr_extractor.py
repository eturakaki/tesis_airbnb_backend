"""Tests for src/scraper/_ssr_extractor.py"""
from __future__ import annotations

import json

import pytest

from src.scraper._ssr_extractor import (
    SSRExtractionError,
    SSRPayloadNotJSONError,
    SSRScriptNotFoundError,
    extract_ssr_payload,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wrap_html(
    script_content: str,
    script_attrs: str = 'id="data-deferred-state-0"',
) -> str:
    """Builds a minimal HTML document with one <script> tag containing JSON."""
    return (
        "<!DOCTYPE html>\n"
        "<html><head><title>Test</title></head>\n"
        "<body>\n"
        "<div>some content</div>\n"
        f"<script {script_attrs}>{script_content}</script>\n"
        "</body></html>"
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_simple_json_object(self):
        html = _wrap_html('{"a": 1}')
        assert extract_ssr_payload(html) == {"a": 1}

    def test_empty_json_object(self):
        html = _wrap_html("{}")
        assert extract_ssr_payload(html) == {}

    def test_nested_json(self):
        payload = {"data": {"x": [1, 2, {"y": "value"}]}, "meta": None}
        html = _wrap_html(json.dumps(payload))
        assert extract_ssr_payload(html) == payload

    def test_id_with_single_quotes(self):
        html = _wrap_html('{"a": 1}', script_attrs="id='data-deferred-state-0'")
        assert extract_ssr_payload(html) == {"a": 1}

    def test_id_as_first_attribute(self):
        html = _wrap_html('{"a": 1}', script_attrs='id="data-deferred-state-0"')
        assert extract_ssr_payload(html) == {"a": 1}

    def test_id_with_attributes_before(self):
        html = _wrap_html(
            '{"a": 1}',
            script_attrs='type="application/json" id="data-deferred-state-0"',
        )
        assert extract_ssr_payload(html) == {"a": 1}

    def test_id_with_attributes_after(self):
        html = _wrap_html(
            '{"a": 1}',
            script_attrs='id="data-deferred-state-0" type="application/json"',
        )
        assert extract_ssr_payload(html) == {"a": 1}

    def test_id_surrounded_by_attributes(self):
        html = _wrap_html(
            '{"a": 1}',
            script_attrs='nonce="abc" id="data-deferred-state-0" type="application/json"',
        )
        assert extract_ssr_payload(html) == {"a": 1}

    def test_id_with_whitespace_around_equals(self):
        html = _wrap_html('{"a": 1}', script_attrs='id = "data-deferred-state-0"')
        assert extract_ssr_payload(html) == {"a": 1}

    def test_content_with_unicode(self):
        payload = {"city": "Buenos Aires", "barrio": "Palermo Soho", "emoji": "🏠"}
        html = _wrap_html(json.dumps(payload, ensure_ascii=False))
        assert extract_ssr_payload(html) == payload

    def test_content_with_escaped_forward_slashes(self):
        # Next.js escapes URL slashes inside JSON state to avoid regex pitfalls
        # like </script> inside content.
        json_str = '{"url": "https:\\/\\/airbnb.com.ar\\/rooms\\/123"}'
        html = _wrap_html(json_str)
        assert extract_ssr_payload(html) == {"url": "https://airbnb.com.ar/rooms/123"}

    def test_content_with_leading_and_trailing_whitespace(self):
        html = _wrap_html('   \n  {"a": 1}  \n  ')
        assert extract_ssr_payload(html) == {"a": 1}

    def test_content_spanning_multiple_lines(self):
        payload = {"a": 1, "b": [1, 2, 3], "c": {"nested": True}}
        # Pretty-printed JSON — verifies DOTALL flag works.
        html = _wrap_html(json.dumps(payload, indent=2))
        assert extract_ssr_payload(html) == payload

    def test_realistic_niobe_shape(self):
        """Mimics the structure of a real Airbnb niobeClientData container."""
        payload = {
            "niobeClientData": [
                ['{"operationName":"StaysPdpSections"}', {"data": {"x": 1}}],
            ],
            "meta": {"timestamp": "2026-05-09T00:00:00Z"},
        }
        html = _wrap_html(json.dumps(payload))
        assert extract_ssr_payload(html) == payload

    def test_multiple_scripts_only_target_extracted(self):
        html = (
            "<html><body>"
            '<script id="other-script">{"wrong": true}</script>'
            '<script id="data-deferred-state-0">{"right": true}</script>'
            '<script id="another">{"also_wrong": true}</script>'
            "</body></html>"
        )
        assert extract_ssr_payload(html) == {"right": True}


# ---------------------------------------------------------------------------
# Error: SSRScriptNotFoundError
# ---------------------------------------------------------------------------

class TestScriptNotFound:
    def test_html_without_any_script(self):
        html = "<html><body><div>nothing here</div></body></html>"
        with pytest.raises(SSRScriptNotFoundError):
            extract_ssr_payload(html)

    def test_html_with_different_script_id(self):
        html = '<html><body><script id="other-id">{"a": 1}</script></body></html>'
        with pytest.raises(SSRScriptNotFoundError):
            extract_ssr_payload(html)

    def test_html_with_legacy_next_data_id(self):
        # __NEXT_DATA__ is the OLD container Airbnb migrated away from.
        # If a response only has that, treat as SHAPE_DRIFT — we don't
        # silently fall back to the legacy path.
        html = '<html><body><script id="__NEXT_DATA__">{"a": 1}</script></body></html>'
        with pytest.raises(SSRScriptNotFoundError):
            extract_ssr_payload(html)

    def test_html_with_data_id_attribute_does_not_match(self):
        # `data-id="data-deferred-state-0"` should NOT match `id="..."`.
        # This is the bug the regex `\bid=` would have introduced.
        html = (
            '<html><body>'
            '<script data-id="data-deferred-state-0">{"sneaky": true}</script>'
            '</body></html>'
        )
        with pytest.raises(SSRScriptNotFoundError):
            extract_ssr_payload(html)

    def test_html_with_aria_id_attribute_does_not_match(self):
        html = (
            '<html><body>'
            '<script aria-id="data-deferred-state-0">{"sneaky": true}</script>'
            '</body></html>'
        )
        with pytest.raises(SSRScriptNotFoundError):
            extract_ssr_payload(html)

    def test_uppercase_id_does_not_match(self):
        # We deliberately do NOT use IGNORECASE — uppercase is SHAPE_DRIFT signal.
        html = (
            '<html><body>'
            '<script id="DATA-DEFERRED-STATE-0">{"a": 1}</script>'
            '</body></html>'
        )
        with pytest.raises(SSRScriptNotFoundError):
            extract_ssr_payload(html)

    def test_empty_html(self):
        with pytest.raises(SSRScriptNotFoundError):
            extract_ssr_payload("")

    def test_cloudflare_challenge_page(self):
        html = (
            "<html>"
            "<head><title>Just a moment...</title></head>"
            "<body><div>Checking your browser before accessing airbnb.com.ar...</div></body>"
            "</html>"
        )
        with pytest.raises(SSRScriptNotFoundError):
            extract_ssr_payload(html)

    def test_subclass_of_base_exception(self):
        # Allow callers to catch the base class.
        with pytest.raises(SSRExtractionError):
            extract_ssr_payload("")


# ---------------------------------------------------------------------------
# Error: SSRPayloadNotJSONError
# ---------------------------------------------------------------------------

class TestPayloadNotJSON:
    def test_javascript_code_in_script(self):
        html = _wrap_html("var x = 1; console.log(x);")
        with pytest.raises(SSRPayloadNotJSONError):
            extract_ssr_payload(html)

    def test_empty_script_content(self):
        html = _wrap_html("")
        with pytest.raises(SSRPayloadNotJSONError):
            extract_ssr_payload(html)

    def test_whitespace_only_content(self):
        html = _wrap_html("   \n  \t  ")
        with pytest.raises(SSRPayloadNotJSONError):
            extract_ssr_payload(html)

    def test_partial_json(self):
        html = _wrap_html('{"a": 1, "b":')
        with pytest.raises(SSRPayloadNotJSONError):
            extract_ssr_payload(html)

    def test_html_inside_script(self):
        html = _wrap_html("<div>not json</div>")
        with pytest.raises(SSRPayloadNotJSONError):
            extract_ssr_payload(html)

    def test_top_level_array_rejected(self):
        # parse_payload contract requires dict; top-level list breaks downstream.
        html = _wrap_html('[1, 2, 3]')
        with pytest.raises(SSRPayloadNotJSONError):
            extract_ssr_payload(html)

    def test_top_level_string_rejected(self):
        html = _wrap_html('"just a string"')
        with pytest.raises(SSRPayloadNotJSONError):
            extract_ssr_payload(html)

    def test_json_error_chains_original(self):
        html = _wrap_html("not json at all")
        with pytest.raises(SSRPayloadNotJSONError) as exc_info:
            extract_ssr_payload(html)
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)

    def test_subclass_of_base_exception(self):
        html = _wrap_html("not json")
        with pytest.raises(SSRExtractionError):
            extract_ssr_payload(html)


# ---------------------------------------------------------------------------
# Type validation
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_none_raises_typeerror(self):
        with pytest.raises(TypeError):
            extract_ssr_payload(None)  # type: ignore[arg-type]

    def test_bytes_raises_typeerror(self):
        with pytest.raises(TypeError):
            extract_ssr_payload(b"<html></html>")  # type: ignore[arg-type]

    def test_dict_raises_typeerror(self):
        with pytest.raises(TypeError):
            extract_ssr_payload({"html": "<html></html>"})  # type: ignore[arg-type]

    def test_int_raises_typeerror(self):
        with pytest.raises(TypeError):
            extract_ssr_payload(42)  # type: ignore[arg-type]