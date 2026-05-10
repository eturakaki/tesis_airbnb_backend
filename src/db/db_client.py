"""
DBClient: persistence abstraction for AirbnbScraper.

D34 — Protocol-based dependency injection for the persistence layer,
mirroring D31 (BrowserSession). Enables E2E tests with FakeDBClient
(in-memory) without touching Postgres or mocking SQLAlchemy session_scope
context managers.

Contract:
    was_scraped_recently  → dedup check (<72hs policy lives in dedup.py)
    write_metadata        → TX-A: upsert_anfitrion + upsert_inmueble (atomic)
    write_price           → TX-B: insert_precio (atomic, only Phase 3+)
    fetch_or_refresh_mep  → MEP del día (idempotente, populates fx_diaria)

Implementations:
    SQLAlchemyDBClient → production adapter wrapping src.db.database +
                         src.utils.cotizacion_mep + src.scraper.dedup.
    FakeDBClient       → in-memory test double with telemetry + failure
                         injection.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional, Protocol


# =============================================================================
# Protocol
# =============================================================================

class DBClient(Protocol):
    """
    Persistence contract for the orchestrator.

    Invariants:
    - was_scraped_recently is idempotent and side-effect-free.
    - write_metadata returns the inmueble_id (surrogate PK). Single
      transaction (TX-A): both upserts commit or roll back together.
    - write_price is a separate transaction (TX-B). Failure here does NOT
      roll back metadata.
    - fetch_or_refresh_mep is idempotent per-day (ON CONFLICT DO NOTHING
      semantics into fx_diaria).
    """

    def was_scraped_recently(self, external_listing_id: str) -> bool: ...

    def write_metadata(self, ssr_parsed: dict[str, Any]) -> int: ...

    def write_price(
        self,
        inmueble_id: int,
        runtime_parsed: dict[str, Any],
        mep_rate: Decimal,
        scraped_at: datetime,
    ) -> None: ...

    def fetch_or_refresh_mep(self, fecha: Optional[date] = None) -> Decimal: ...


# =============================================================================
# SQLAlchemyDBClient — production adapter
# =============================================================================

class SQLAlchemyDBClient:
    """
    Production adapter. Each method opens its own session_scope, so methods
    are independently transactional (TX-A and TX-B as designed).

    Assumptions about the underlying functions in src.db.database:
    - upsert_anfitrion(conn, plataforma, **anfitrion_kwargs)
    - upsert_inmueble(conn, plataforma, **inmueble_kwargs) → int
    - insert_precio(conn, inmueble_id, tiempo, ...)

    Assumed shape of ssr_parsed (output of parse_payload):
        {
          "parse_status": "OK" | "PARTIAL" | "SHAPE_DRIFT",
          "anfitrion":   {**kwargs aceptados por upsert_anfitrion},
          "inmueble":    {**kwargs aceptados por upsert_inmueble},
          "missing_fields": [...]
        }
    Si parse_payload usa otras keys (p.ej. "host" / "listing"), basta con
    ajustar las dos líneas marcadas con AJUSTAR.
    """

    def __init__(self, engine, plataforma: str = "airbnb") -> None:
        self._engine = engine
        self._plataforma = plataforma

    def was_scraped_recently(self, external_listing_id: str) -> bool:
        from src.scraper.dedup import should_skip
        with self._engine.connect() as conn:
            return should_skip(conn, external_listing_id)

    def write_metadata(self, ssr_parsed: dict[str, Any]) -> int:
        from src.db.database import (
            session_scope,
            upsert_anfitrion,
            upsert_inmueble,
        )

        # AJUSTAR: nombres de keys si parse_payload usa otros.
        anfitrion = ssr_parsed.get("anfitrion") or {}
        inmueble = ssr_parsed.get("inmueble") or {}

        with session_scope(self._engine) as conn:
            upsert_anfitrion(
                conn,
                plataforma=self._plataforma,
                **anfitrion,
            )
            inmueble_id = upsert_inmueble(
                conn,
                plataforma=self._plataforma,
                **inmueble,
            )

        if not isinstance(inmueble_id, int):
            raise TypeError(
                f"upsert_inmueble must return int (inmueble_id surrogate PK), "
                f"got {type(inmueble_id).__name__}={inmueble_id!r}"
            )
        return inmueble_id

    def write_price(
        self,
        inmueble_id: int,
        runtime_parsed: dict[str, Any],
        mep_rate: Decimal,
        scraped_at: datetime,
    ) -> None:
        # Phase 3 only. Wired in once extract_price_components.py exists.
        # Failing loudly here keeps Phase 2.3 honest: if the orchestrator
        # ever reaches this in 2.3, it means price_extractor was wired by
        # mistake.
        raise NotImplementedError(
            "write_price reserved for Phase 3 (post extract_price_components). "
            "In Phase 2.3 the orchestrator must keep price_extractor=None."
        )

    def fetch_or_refresh_mep(self, fecha: Optional[date] = None) -> Decimal:
        from src.utils.cotizacion_mep import obtener_mep
        return obtener_mep(self._engine, fecha)


# =============================================================================
# FakeDBClient — in-memory test double
# =============================================================================

class FakeDBClient:
    """
    Test double for E2E tests of AirbnbScraper.

    Telemetry (read-only, for asserts):
        metadata_writes:  list[dict]  — each call's ssr_parsed
        price_writes:     list[dict]  — each call's args
        mep_call_count:   int
        dedup_call_count: int

    Setup / failure injection (one-shot unless re-queued):
        queue_skip(listing_id)        → was_scraped_recently True for it
        queue_metadata_failure(exc?)  → next write_metadata raises
        queue_price_failure(exc?)     → next write_price raises
        queue_mep_failure(exc?)       → next fetch_or_refresh_mep raises
        set_mep_rate(rate)            → fetch_or_refresh_mep returns this
    """

    DEFAULT_MEP_RATE: Decimal = Decimal("1443.00")

    def __init__(self) -> None:
        self._skip_set: set[str] = set()
        self._next_inmueble_id: int = 1
        self._mep_rate: Decimal = self.DEFAULT_MEP_RATE

        # one-shot failure flags
        self._next_metadata_exc: Optional[Exception] = None
        self._next_price_exc: Optional[Exception] = None
        self._next_mep_exc: Optional[Exception] = None

        # telemetry
        self.metadata_writes: list[dict[str, Any]] = []
        self.price_writes: list[dict[str, Any]] = []
        self.mep_call_count: int = 0
        self.dedup_call_count: int = 0

    # ----------------- test setup API (NOT in DBClient Protocol) -----------

    def queue_skip(self, listing_id: str) -> None:
        self._skip_set.add(listing_id)

    def queue_metadata_failure(self, exc: Optional[Exception] = None) -> None:
        self._next_metadata_exc = exc or RuntimeError("simulated metadata failure")

    def queue_price_failure(self, exc: Optional[Exception] = None) -> None:
        self._next_price_exc = exc or RuntimeError("simulated price failure")

    def queue_mep_failure(self, exc: Optional[Exception] = None) -> None:
        self._next_mep_exc = exc or RuntimeError("simulated MEP failure")

    def set_mep_rate(self, rate: Decimal) -> None:
        self._mep_rate = rate

    # ----------------- DBClient Protocol implementation --------------------

    def was_scraped_recently(self, external_listing_id: str) -> bool:
        self.dedup_call_count += 1
        return external_listing_id in self._skip_set

    def write_metadata(self, ssr_parsed: dict[str, Any]) -> int:
        if self._next_metadata_exc is not None:
            exc = self._next_metadata_exc
            self._next_metadata_exc = None
            raise exc
        self.metadata_writes.append({"ssr_parsed": ssr_parsed})
        inmueble_id = self._next_inmueble_id
        self._next_inmueble_id += 1
        return inmueble_id

    def write_price(
        self,
        inmueble_id: int,
        runtime_parsed: dict[str, Any],
        mep_rate: Decimal,
        scraped_at: datetime,
    ) -> None:
        if self._next_price_exc is not None:
            exc = self._next_price_exc
            self._next_price_exc = None
            raise exc
        self.price_writes.append({
            "inmueble_id": inmueble_id,
            "runtime_parsed": runtime_parsed,
            "mep_rate": mep_rate,
            "scraped_at": scraped_at,
        })

    def fetch_or_refresh_mep(self, fecha: Optional[date] = None) -> Decimal:
        self.mep_call_count += 1
        if self._next_mep_exc is not None:
            exc = self._next_mep_exc
            self._next_mep_exc = None
            raise exc
        return self._mep_rate