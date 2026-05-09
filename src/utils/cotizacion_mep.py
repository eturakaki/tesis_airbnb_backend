"""
Cliente de cotizacion Dolar MEP con cache diario.
Fuente: dolarapi.com (gratuita, sin auth, MEP al cierre del dia anterior).
Doc: https://dolarapi.com/docs/argentina/operations/get-dolar-bolsa.html

Diseno:
- Cache persistente en tabla fx_diaria.
- Si la API falla, levanta del cache persistente.
- Si no hay cache para hoy, consulta, guarda, devuelve.
- Inmutable: una vez guardado el MEP de un dia, no se sobrescribe.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

DOLARAPI_URL = "https://dolarapi.com/v1/dolares/bolsa"
TIMEOUT_SEG = 10


class CotizacionMEPError(Exception):
    """Error al obtener cotizacion MEP cuando no hay fallback disponible."""


def _fetch_mep_api() -> Optional[Decimal]:
    """Consulta dolarapi.com. Devuelve el promedio (compra+venta)/2."""
    try:
        with httpx.Client(timeout=TIMEOUT_SEG) as client:
            r = client.get(DOLARAPI_URL)
            r.raise_for_status()
            data = r.json()
            compra = Decimal(str(data["compra"]))
            venta  = Decimal(str(data["venta"]))
            return (compra + venta) / Decimal("2")
    except Exception as e:
        logger.warning(f"Fallo dolarapi.com: {e}")
        return None


def _get_cached_mep(engine: Engine, fecha: date) -> Optional[Decimal]:
    """Lee MEP cacheado en DB para una fecha especifica."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT cotizacion FROM fx_diaria WHERE fecha = :f AND moneda = 'USD_MEP'"),
            {"f": fecha},
        ).fetchone()
    return Decimal(str(row[0])) if row else None


def _save_mep(engine: Engine, fecha: date, cotizacion: Decimal, fuente: str) -> None:
    """Persiste MEP del dia. ON CONFLICT DO NOTHING -> inmutable."""
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO fx_diaria (fecha, moneda, cotizacion, fuente, capturado_at)
                VALUES (:f, 'USD_MEP', :c, :src, :ts)
                ON CONFLICT (fecha, moneda) DO NOTHING
            """),
            {"f": fecha, "c": cotizacion, "src": fuente, "ts": datetime.now(timezone.utc)},
        )


def obtener_mep(engine: Engine, fecha: Optional[date] = None) -> Decimal:
    """
    API publica del modulo.

    Para una fecha dada (default: hoy):
    1. Si esta en cache DB -> devolver.
    2. Si no, consultar dolarapi.com, guardar, devolver.
    3. Si la API falla y no hay cache -> CotizacionMEPError.
    """
    fecha = fecha or date.today()

    cached = _get_cached_mep(engine, fecha)
    if cached is not None:
        logger.debug(f"MEP {fecha} desde cache: {cached}")
        return cached

    if fecha != date.today():
        raise CotizacionMEPError(
            f"No hay MEP cacheado para {fecha} y no se puede consultar APIs historicas. "
            "Cargar manualmente o usar serie historica de BCRA/Rava."
        )

    api_mep = _fetch_mep_api()
    if api_mep is None:
        raise CotizacionMEPError(
            "dolarapi.com no respondio y no hay cache para hoy. "
            "Verificar conectividad o cargar MEP manualmente."
        )

    _save_mep(engine, fecha, api_mep, "dolarapi.com")
    logger.info(f"MEP {fecha} guardado: {api_mep} ARS/USD")
    return api_mep