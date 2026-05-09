"""
dedup.py — Lógica de deduplicación temporal para el scraper de Airbnb.

Determina si un listing debe scrapearse en este momento o si se puede
saltear porque fue scrapeado recientemente. Reduce carga sobre Airbnb y
respeta principios de scraping responsable.

DECISIÓN METODOLÓGICA — VENTANA DE 72 HORAS
=============================================
Los precios de Airbnb en CABA tienen variación intra-semanal (lunes-jueves
vs viernes-domingo) pero variación intra-día negligible para fechas a +14
días. Re-scrapear el mismo listing en <72hs es redundante.

Threshold default: 72 horas. Configurable vía parámetro para sensitivity
analysis en el paper (ej. comparar dataset con threshold=24h vs 72h vs 168h
para evaluar robustez de los estimadores hedónicos).

DECISIÓN — SKIP ATÓMICO POR LISTING
=====================================
Cada listing se evalúa independientemente. No batchamos consultas porque
para batches de 5-20 listings (Fase 2.4) la diferencia es despreciable y
mantiene la API simple. Para batches de 5000+ (Fase 4 expansión nacional)
se optimizará cuando llegue el momento.

DECISIÓN — INYECTABILIDAD DE `now`
====================================
La función acepta `now` como parámetro opcional (default: datetime.now(utc)).
Esto garantiza tests determinísticos y consistencia dentro de un mismo batch
de scraping (todos los listings se evalúan contra el mismo "ahora").
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

# ─── CONSTANTES ──────────────────────────────────────────────────────────
DEFAULT_SKIP_THRESHOLD_HOURS = 72
PLATFORM_AIRBNB = "airbnb"


# ─── EXCEPCIONES ─────────────────────────────────────────────────────────
class DedupError(Exception):
    """Error consultando el estado de deduplicación contra la DB."""


# ─── FUNCIÓN PÚBLICA PRINCIPAL ───────────────────────────────────────────
def should_skip(
    conn,
    external_listing_id: str,
    now: datetime | None = None,
    threshold_hours: int = DEFAULT_SKIP_THRESHOLD_HOURS,
    plataforma: str = PLATFORM_AIRBNB,
) -> bool:
    """
    Determina si un listing debe saltearse en el batch actual de scraping.

    Lógica:
        1. Buscar `inmueble_id` en `inmuebles` por (plataforma, external_listing_id).
        2. Si no existe → no skip (es la primera vez que se scrapea).
        3. Si existe, buscar MAX(tiempo) en `precios_historicos` para ese inmueble.
        4. Si nunca tuvo precio registrado → no skip (vale la pena reintentar).
        5. Si MAX(tiempo) >= now - threshold → skip (scraping reciente).

    Parameters
    ----------
    conn : sqlalchemy.Connection
        Conexión activa a la DB (provista por el orquestador).
    external_listing_id : str
        ID del listing en Airbnb (string numérico).
    now : datetime, optional
        Fecha-hora de referencia (UTC). Default: `datetime.now(timezone.utc)`.
        Inyectable para tests y consistencia intra-batch.
    threshold_hours : int
        Horas mínimas que deben pasar antes de re-scrapear. Default: 72.
    plataforma : str
        Plataforma del listing. Default: 'airbnb'.

    Returns
    -------
    bool
        True si debe saltearse (scraping reciente), False si debe scrapearse.

    Raises
    ------
    DedupError
        Si la consulta SQL falla por razones distintas a "no encontré nada".
    """
    if not external_listing_id or not isinstance(external_listing_id, str):
        raise DedupError(
            f"external_listing_id inválido: {external_listing_id!r}"
        )

    if not isinstance(threshold_hours, int) or threshold_hours < 0:
        raise DedupError(
            f"threshold_hours debe ser int >= 0, recibí {threshold_hours!r}"
        )

    if now is None:
        now = datetime.now(timezone.utc)
    elif not isinstance(now, datetime):
        raise DedupError(f"now debe ser datetime, recibí {type(now).__name__}")
    elif now.tzinfo is None:
        # Forzar timezone-aware para evitar comparaciones naive vs aware
        raise DedupError("now debe ser timezone-aware (preferiblemente UTC)")

    cutoff = now - timedelta(hours=threshold_hours)

    # Query única que joinea inmuebles con precios_historicos.
    # Devuelve la fecha máxima del último precio registrado, o NULL si nunca
    # se scrapeó este listing.
    query = text(
        """
        SELECT MAX(ph.tiempo) AS ultimo_precio
        FROM inmuebles i
        LEFT JOIN precios_historicos ph ON ph.inmueble_id = i.id
        WHERE i.plataforma = :plataforma
          AND i.external_listing_id = :external_listing_id
        """
    )

    try:
        row = conn.execute(
            query,
            {
                "plataforma": plataforma,
                "external_listing_id": external_listing_id,
            },
        ).first()
    except Exception as exc:
        raise DedupError(
            f"Error consultando dedup para listing {external_listing_id}: {exc}"
        ) from exc

    # Caso 1: el LEFT JOIN no devolvió fila → el listing no existe en `inmuebles`.
    if row is None:
        logger.debug(
            "Listing %s no existe en inmuebles → no skip", external_listing_id
        )
        return False

    ultimo_precio = row[0]

    # Caso 2: el listing existe pero nunca tuvo precio registrado.
    if ultimo_precio is None:
        logger.debug(
            "Listing %s existe pero sin precios previos → no skip",
            external_listing_id,
        )
        return False

    # Caso 3: comparar con cutoff.
    # Normalizar timezone si la DB devuelve naive (TimescaleDB suele devolver
    # tz-aware, pero por las dudas).
    if ultimo_precio.tzinfo is None:
        ultimo_precio = ultimo_precio.replace(tzinfo=timezone.utc)

    if ultimo_precio >= cutoff:
        logger.info(
            "SKIP listing %s: último precio %s >= cutoff %s",
            external_listing_id,
            ultimo_precio.isoformat(),
            cutoff.isoformat(),
        )
        return True

    logger.debug(
        "SCRAPE listing %s: último precio %s < cutoff %s",
        external_listing_id,
        ultimo_precio.isoformat(),
        cutoff.isoformat(),
    )
    return False