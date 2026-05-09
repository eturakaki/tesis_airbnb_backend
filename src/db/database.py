"""
Capa de acceso a datos. Engine SQLAlchemy + UPSERTs idempotentes.

Filosofia:
- El scraper NO sabe SQL. Llama a upsert_anfitrion(), upsert_inmueble(), insert_precio().
- Todos los saves son idempotentes (ON CONFLICT). Re-correr el scraper no rompe nada.
- Transacciones cortas: una por listing, no una por corrida.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

load_dotenv()


def make_engine() -> Engine:
    """Crea engine usando variables del .env."""
    user = os.getenv("DB_USER")
    pwd  = os.getenv("DB_PASSWORD")
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    db   = os.getenv("DB_NAME")

    if not all([user, pwd, db]):
        raise RuntimeError("Faltan variables DB_* en .env")

    url = f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{db}"
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)


@contextmanager
def session_scope(engine: Engine):
    """Context manager con commit/rollback automatico."""
    with engine.begin() as conn:
        yield conn


def upsert_anfitrion(
    conn,
    plataforma: str,
    external_host_id: str,
    nombre: Optional[str] = None,
    es_superhost: bool = False,
    listings_count: Optional[int] = None,
) -> int:
    """
    Inserta o actualiza anfitrion. Devuelve anfitrion_id.
    Marca es_corporativo automaticamente si listings_count > 2.
    """
    es_corp = listings_count is not None and listings_count > 2
    row = conn.execute(
        text("""
            INSERT INTO anfitriones (
                plataforma, external_host_id, nombre, es_superhost,
                listings_count_obs, es_corporativo
            )
            VALUES (
                CAST(:plat AS plataforma_enum), :ehid, :nom, :sh, :lc, :corp
            )
            ON CONFLICT (plataforma, external_host_id) DO UPDATE SET
                nombre             = COALESCE(EXCLUDED.nombre, anfitriones.nombre),
                es_superhost       = EXCLUDED.es_superhost,
                listings_count_obs = EXCLUDED.listings_count_obs,
                es_corporativo     = EXCLUDED.es_corporativo,
                updated_at         = NOW()
            RETURNING anfitrion_id
        """),
        {
            "plat": plataforma, "ehid": external_host_id, "nom": nombre,
            "sh": es_superhost, "lc": listings_count, "corp": es_corp,
        },
    ).fetchone()
    return row[0]


def upsert_inmueble(
    conn,
    plataforma: str,
    external_listing_id: str,
    anfitrion_id: int,
    tipo_listado: str,
    pais: str,
    ciudad: str,
    barrio: Optional[str] = None,
    latitud: Optional[float] = None,
    longitud: Optional[float] = None,
    tipo_propiedad: Optional[str] = None,
    tipo_habitacion: Optional[str] = None,
    ambientes: Optional[int] = None,
    capacidad_huespedes: Optional[int] = None,
    url_listado: Optional[str] = None,
    fecha_observacion: Optional[datetime] = None,
) -> int:
    """Inserta o actualiza inmueble. Devuelve inmueble_id."""
    fecha_obs = fecha_observacion or datetime.now(timezone.utc).date()

    row = conn.execute(
        text("""
            INSERT INTO inmuebles (
                plataforma, external_listing_id, anfitrion_id,
                tipo_listado, pais, ciudad, barrio,
                latitud, longitud, tipo_propiedad, tipo_habitacion,
                ambientes, capacidad_huespedes, url_listado,
                fecha_primera_observacion, fecha_ultima_observacion
            )
            VALUES (
                CAST(:plat AS plataforma_enum), :elid, :aid,
                CAST(:tl AS tipo_listado_enum), :pais, :ciu, :bar,
                :lat, :lon, :tp, :th,
                :amb, :cap, :url,
                :fpo, :fuo
            )
            ON CONFLICT (plataforma, external_listing_id) DO UPDATE SET
                anfitrion_id             = EXCLUDED.anfitrion_id,
                barrio                   = COALESCE(EXCLUDED.barrio, inmuebles.barrio),
                tipo_propiedad           = COALESCE(EXCLUDED.tipo_propiedad, inmuebles.tipo_propiedad),
                ambientes                = COALESCE(EXCLUDED.ambientes, inmuebles.ambientes),
                capacidad_huespedes      = COALESCE(EXCLUDED.capacidad_huespedes, inmuebles.capacidad_huespedes),
                fecha_ultima_observacion = EXCLUDED.fecha_ultima_observacion,
                updated_at               = NOW()
            RETURNING inmueble_id
        """),
        {
            "plat": plataforma, "elid": external_listing_id, "aid": anfitrion_id,
            "tl": tipo_listado, "pais": pais, "ciu": ciudad, "bar": barrio,
            "lat": latitud, "lon": longitud, "tp": tipo_propiedad, "th": tipo_habitacion,
            "amb": ambientes, "cap": capacidad_huespedes, "url": url_listado,
            "fpo": fecha_obs, "fuo": fecha_obs,
        },
    ).fetchone()
    return row[0]


def insert_precio(
    conn,
    inmueble_id: int,
    tiempo: datetime,
    precio_nominal: Decimal,
    moneda: str,
    tipo_cambio_mep: Optional[Decimal] = None,
    disponibilidad_365: Optional[int] = None,
    minimum_nights: Optional[int] = None,
    numero_resenas: Optional[int] = None,
    rating_promedio: Optional[Decimal] = None,
    fuente_scraper: str = "airbnb_v2",
    raw_payload_hash: Optional[str] = None,
) -> None:
    """
    Inserta observacion de precio. Calcula precio_usd_mep automaticamente.
    Idempotente via PK (inmueble_id, tiempo).
    """
    precio_usd: Optional[Decimal] = None
    if moneda == "ARS" and tipo_cambio_mep:
        precio_usd = precio_nominal / tipo_cambio_mep
    elif moneda == "USD":
        precio_usd = precio_nominal

    conn.execute(
        text("""
            INSERT INTO precios_historicos (
                tiempo, inmueble_id, precio_nominal, moneda,
                tipo_cambio_mep, precio_usd_mep,
                disponibilidad_365, minimum_nights,
                numero_resenas, rating_promedio,
                fuente_scraper, raw_payload_hash
            )
            VALUES (
                :t, :iid, :pn, :mon,
                :tcm, :pusd,
                :d365, :mn,
                :nr, :rp,
                :fs, :rph
            )
            ON CONFLICT (inmueble_id, tiempo) DO NOTHING
        """),
        {
            "t": tiempo, "iid": inmueble_id, "pn": precio_nominal, "mon": moneda,
            "tcm": tipo_cambio_mep, "pusd": precio_usd,
            "d365": disponibilidad_365, "mn": minimum_nights,
            "nr": numero_resenas, "rp": rating_promedio,
            "fs": fuente_scraper, "rph": raw_payload_hash,
        },
    )