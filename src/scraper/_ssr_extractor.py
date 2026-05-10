"""
SSR payload extractor for Airbnb PDP HTML.

Extracts the JSON content embedded in <script id="data-deferred-state-0"> from a
PDP HTML response and parses it into a Python dict. Pure string-parsing function
with no I/O — fully testable without a browser.

The extracted dict is the input expected by parse_payload.parse_payload().

Context (sesión 09/05/2026): Airbnb migró de __NEXT_DATA__ a data-deferred-state-0
como contenedor del estado SSR. El precio NO está acá — viene por una respuesta
GraphQL POST a /api/v3/StaysPdpSections que se intercepta en runtime y se parsea
con parse_runtime_response.py (no con este módulo).

Failure modes:
- Script tag absent → SSRScriptNotFoundError
  Causas probables: Cloudflare challenge, redirect, o mutación del id (SHAPE_DRIFT real).
- Script tag presente pero contenido no es JSON → SSRPayloadNotJSONError
  Causa probable: Airbnb cambió el formato de serialización (SHAPE_DRIFT real).

Ambas son clasificadas como SHAPE_DRIFT por el orquestador.
"""
from __future__ import annotations

import json
import re
from typing import Any


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class SSRExtractionError(Exception):
    """Base exception for any failure extracting the SSR payload from HTML."""


class SSRScriptNotFoundError(SSRExtractionError):
    """Raised when <script id="data-deferred-state-0"> is not present in the HTML."""


class SSRPayloadNotJSONError(SSRExtractionError):
    """Raised when the script tag exists but its content is not valid JSON."""


# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------
#
# Patrón:
#   <script
#   (?:[^>]*\s)?           ← cualquier atributo previo, terminando en whitespace.
#                             El grupo es opcional para permitir id como primer atributo.
#                             El whitespace final garantiza que `id` esté precedido
#                             por separador (rechaza `data-id="..."`, `aria-id="..."`).
#   id\s*=\s*              ← `id =` con whitespace tolerante alrededor del `=`.
#   ["\']data-deferred-state-0["\']
#   [^>]*>                 ← resto de atributos hasta `>`.
#   (.*?)                  ← captura no-greedy del contenido (DOTALL → cruza líneas).
#   </script>
#
# No usamos IGNORECASE: HTML5 case-sensitiviza el VALOR del id, y Next.js
# emite siempre minúsculas. Si aparece `<SCRIPT ID="DATA-...">` queremos que
# falle ruidosamente — es una señal de mutación real.

_SSR_SCRIPT_PATTERN = re.compile(
    r'<script(?:[^>]*\s)?id\s*=\s*["\']data-deferred-state-0["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def extract_ssr_payload(html: str) -> dict[str, Any]:
    """
    Extract the JSON payload from <script id="data-deferred-state-0"> in HTML.

    Args:
        html: The full HTML response body of an Airbnb PDP page.

    Returns:
        The parsed JSON content of the script tag as a dict. This dict is the
        expected input shape for parse_payload.parse_payload().

    Raises:
        TypeError: html is not a str.
        SSRScriptNotFoundError: The script tag was not found in the HTML.
        SSRPayloadNotJSONError: The script tag was present but its content
            could not be parsed as JSON. Wraps the original json.JSONDecodeError
            as __cause__ for forensic inspection.
    """
    if not isinstance(html, str):
        raise TypeError(f"html must be str, got {type(html).__name__}")

    match = _SSR_SCRIPT_PATTERN.search(html)
    if match is None:
        raise SSRScriptNotFoundError(
            'No <script id="data-deferred-state-0"> found in HTML. '
            "Possible causes: Cloudflare challenge, page redirect, "
            "or DOM mutation (real SHAPE_DRIFT)."
        )

    raw_content = match.group(1).strip()
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        snippet = raw_content[:200]
        raise SSRPayloadNotJSONError(
            f"Script tag found but content is not valid JSON: {exc.msg} "
            f"at line {exc.lineno} col {exc.colno}. "
            f"Content snippet (first 200 chars): {snippet!r}"
        ) from exc

    if not isinstance(parsed, dict):
        # Defensive: JSON could be a top-level array, number, etc.
        # We only support dict (consistent with parse_payload contract).
        raise SSRPayloadNotJSONError(
            f"Script content parsed to {type(parsed).__name__}, expected dict. "
            f"Snippet: {raw_content[:200]!r}"
        )

    return parsed