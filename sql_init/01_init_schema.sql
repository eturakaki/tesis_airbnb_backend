-- =====================================================================
-- Proyecto: Airbnb & Mercado de Vivienda - Backend de datos
-- Autor:    Iñaki Etura (UNSA)
-- Stack:    PostgreSQL 16 + TimescaleDB 2.x
-- Versión:  1.0  (Fase 1 - inicialización)
-- =====================================================================

-- 0. Extensiones
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS postgis;          -- útil para queries geoespaciales
CREATE EXTENSION IF NOT EXISTS pg_trgm;          -- fuzzy match de barrios/direcciones

-- =====================================================================
-- 1. Tipos enumerados (integridad y performance)
-- =====================================================================
CREATE TYPE plataforma_enum AS ENUM (
    'airbnb', 'booking', 'vrbo',
    'zonaprop', 'argenprop', 'mercadolibre', 'inmuebles24',
    'otro'
);

CREATE TYPE tipo_listado_enum AS ENUM (
    'STR',      -- alquiler turístico de corto plazo
    'LTR',      -- alquiler residencial de largo plazo
    'venta'     -- por si querés cruzar contra precios de venta (Barron et al. 2021)
);

CREATE TYPE estado_listado_enum AS ENUM (
    'activo', 'inactivo', 'eliminado', 'pausado'
);

-- =====================================================================
-- 2. Tabla ANFITRIONES  (dimensión estática)
-- =====================================================================
CREATE TABLE anfitriones (
    anfitrion_id          BIGSERIAL PRIMARY KEY,
    plataforma            plataforma_enum NOT NULL,
    external_host_id      VARCHAR(100)    NOT NULL,
    nombre                VARCHAR(255),
    es_superhost          BOOLEAN         DEFAULT FALSE,
    fecha_alta_plataforma DATE,
    ubicacion_declarada   VARCHAR(255),
    url_perfil            TEXT,
    -- Flag heurístico de financiarización (>2 listings activos = corporativo)
    es_corporativo        BOOLEAN         DEFAULT FALSE,
    listings_count_obs    INTEGER,        -- snapshot del valor scrapeado
    created_at            TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_anfitrion_plataforma UNIQUE (plataforma, external_host_id)
);

CREATE INDEX idx_anfitriones_plataforma  ON anfitriones (plataforma);
CREATE INDEX idx_anfitriones_corporativo ON anfitriones (es_corporativo)
    WHERE es_corporativo = TRUE;

COMMENT ON COLUMN anfitriones.es_corporativo IS
    'Flag para análisis de profesionalización (variable H del Impuesto Pigouviano)';

-- =====================================================================
-- 3. Tabla INMUEBLES  (dimensión estática / slowly changing)
-- =====================================================================
CREATE TABLE inmuebles (
    inmueble_id              BIGSERIAL PRIMARY KEY,
    plataforma               plataforma_enum NOT NULL,
    external_listing_id      VARCHAR(100)    NOT NULL,
    anfitrion_id             BIGINT REFERENCES anfitriones(anfitrion_id) ON DELETE SET NULL,
    tipo_listado             tipo_listado_enum NOT NULL,

    -- Geografía (nivel país → barrio)
    pais                     VARCHAR(80)  NOT NULL,
    ciudad                   VARCHAR(120) NOT NULL,
    provincia                VARCHAR(120),
    barrio                   VARCHAR(120),
    comuna                   VARCHAR(50),    -- aplica a CABA y otras
    latitud                  NUMERIC(10, 7),
    longitud                 NUMERIC(10, 7),
    geom                     GEOGRAPHY(POINT, 4326),  -- PostGIS para queries por radio

    -- Características físicas
    tipo_propiedad           VARCHAR(80),    -- Departamento, PH, Casa
    tipo_habitacion          VARCHAR(80),    -- Entire home/apt, Private room
    ambientes                SMALLINT,
    dormitorios              SMALLINT,
    banos                    NUMERIC(3, 1),
    m2_cubiertos             INTEGER,
    m2_totales               INTEGER,
    capacidad_huespedes      SMALLINT,
    antiguedad_anos          SMALLINT,

    -- Trazabilidad
    url_listado              TEXT,
    estado                   estado_listado_enum NOT NULL DEFAULT 'activo',
    fecha_primera_observacion DATE NOT NULL,
    fecha_ultima_observacion  DATE NOT NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_listado_plataforma UNIQUE (plataforma, external_listing_id)
);

CREATE INDEX idx_inmuebles_geo          ON inmuebles (pais, ciudad, barrio);
CREATE INDEX idx_inmuebles_anfitrion    ON inmuebles (anfitrion_id);
CREATE INDEX idx_inmuebles_tipo_listado ON inmuebles (tipo_listado);
CREATE INDEX idx_inmuebles_estado       ON inmuebles (estado);
CREATE INDEX idx_inmuebles_geom         ON inmuebles USING GIST (geom);
CREATE INDEX idx_inmuebles_barrio_trgm  ON inmuebles USING GIN (barrio gin_trgm_ops);

-- =====================================================================
-- 4. HYPERTABLE precios_historicos  (serie temporal de alta frecuencia)
-- =====================================================================
CREATE TABLE precios_historicos (
    tiempo                   TIMESTAMPTZ NOT NULL,
    inmueble_id              BIGINT      NOT NULL,

    -- Precio y tipo de cambio (snapshot inmutable)
    precio_nominal           NUMERIC(14, 2) NOT NULL,
    moneda                   CHAR(3)        NOT NULL DEFAULT 'ARS',
    tipo_cambio_mep          NUMERIC(12, 4),
    precio_usd_mep           NUMERIC(14, 2),    -- columna calculada en ingesta

    -- Métricas STR
    disponibilidad_365       SMALLINT,
    minimum_nights           SMALLINT,
    maximum_nights           SMALLINT,
    tasa_ocupacion_estim     NUMERIC(5, 2),

    -- Métricas LTR
    expensas_ars             NUMERIC(12, 2),
    incluye_servicios        BOOLEAN,

    -- Demanda / reputación
    numero_resenas           INTEGER,
    rating_promedio          NUMERIC(3, 2),

    -- Trazabilidad del scraping
    fuente_scraper           VARCHAR(50),
    raw_payload_hash         CHAR(64),    -- SHA-256 del JSON crudo (auditoría)

    PRIMARY KEY (inmueble_id, tiempo),
    FOREIGN KEY (inmueble_id) REFERENCES inmuebles(inmueble_id) ON DELETE CASCADE
);

-- Convertir a hypertable (chunks semanales)
SELECT create_hypertable(
    'precios_historicos',
    'tiempo',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists       => TRUE
);

CREATE INDEX idx_precios_inmueble_tiempo ON precios_historicos (inmueble_id, tiempo DESC);
CREATE INDEX idx_precios_tiempo          ON precios_historicos (tiempo DESC);

-- Compresión: chunks de más de 30 días
ALTER TABLE precios_historicos SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'inmueble_id',
    timescaledb.compress_orderby   = 'tiempo DESC'
);
SELECT add_compression_policy('precios_historicos', INTERVAL '30 days');

-- =====================================================================
-- 5. Continuous Aggregate  (lo que va a alimentar al ABM)
-- =====================================================================
CREATE MATERIALIZED VIEW precios_semanales_barrio
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('7 days', ph.tiempo)                        AS semana,
    i.pais,
    i.ciudad,
    i.barrio,
    i.tipo_listado,
    COUNT(*)                                                AS n_obs,
    AVG(ph.precio_usd_mep)                                  AS precio_usd_avg,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ph.precio_usd_mep) AS precio_usd_mediano,
    AVG(ph.disponibilidad_365)                              AS disp_365_avg,
    AVG(ph.tasa_ocupacion_estim)                            AS ocup_avg
FROM precios_historicos ph
JOIN inmuebles i USING (inmueble_id)
GROUP BY semana, i.pais, i.ciudad, i.barrio, i.tipo_listado
WITH NO DATA;

SELECT add_continuous_aggregate_policy('precios_semanales_barrio',
    start_offset      => INTERVAL '1 year',
    end_offset        => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day');

-- =====================================================================
-- 6. Triggers: updated_at automático
-- =====================================================================
CREATE OR REPLACE FUNCTION trg_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tg_anfitriones_updated_at
    BEFORE UPDATE ON anfitriones
    FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();

CREATE TRIGGER tg_inmuebles_updated_at
    BEFORE UPDATE ON inmuebles
    FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();

-- =====================================================================
-- 7. Sincronización geom desde lat/lon
-- =====================================================================
CREATE OR REPLACE FUNCTION trg_sync_geom()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.latitud IS NOT NULL AND NEW.longitud IS NOT NULL THEN
        NEW.geom := ST_SetSRID(ST_MakePoint(NEW.longitud, NEW.latitud), 4326)::GEOGRAPHY;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tg_inmuebles_sync_geom
    BEFORE INSERT OR UPDATE OF latitud, longitud ON inmuebles
    FOR EACH ROW EXECUTE FUNCTION trg_sync_geom();

    -- Tabla de cotizaciones diarias (MEP, oficial, blue, lo que sumes)
CREATE TABLE IF NOT EXISTS fx_diaria (
    fecha          DATE         NOT NULL,
    moneda         VARCHAR(20)  NOT NULL,  -- 'USD_MEP', 'USD_OFICIAL', 'USD_BLUE'
    cotizacion     NUMERIC(12, 4) NOT NULL,
    fuente         VARCHAR(50)  NOT NULL,
    capturado_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (fecha, moneda)
);

CREATE INDEX idx_fx_diaria_fecha ON fx_diaria (fecha DESC);

COMMENT ON TABLE fx_diaria IS
  'Cotizaciones diarias inmutables. ON CONFLICT DO NOTHING garantiza replicabilidad.';