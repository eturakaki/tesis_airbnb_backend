"""
CLI entry point for batch scraping of CABA listings.

Usage:
    # Piloto manual (browser visible, listings explícitos)
    python scripts/run_scraper_batch.py --no-headless --listing-ids "id1,id2,id3"

    # Batch chico de prueba con browser headless
    python scripts/run_scraper_batch.py --limit 5

    # Dry run: build URLs, no network, no DB
    python scripts/run_scraper_batch.py --limit 10 --dry-run

    # Source CSV explícito
    python scripts/run_scraper_batch.py --csv data/raw/caba_listings_test.csv.gz --limit 20
"""
from __future__ import annotations

import argparse
import gzip
import sys
from pathlib import Path
from typing import Iterable

# Allow `python scripts/run_scraper_batch.py` from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.database import make_engine
from src.db.db_client import SQLAlchemyDBClient
from src.scraper.airbnb_scraper import (
    AirbnbScraper,
    BatchAbortError,
    JSONLLogger,
    PlaywrightBrowserSession,
    ScrapeOutcome,
)
from src.scraper.url_builder import build_pdp_url


DEFAULT_CSV_PATH = Path("data/raw/caba_listings_test.csv.gz")


# =============================================================================
# Listing source loaders
# =============================================================================

def _load_ids_from_csv(csv_path: Path, limit: int) -> list[str]:
    """
    Read listing IDs from gzipped CSV. Assumes first column is the ID.
    Skips header line.
    """
    if not csv_path.exists():
        raise FileNotFoundError(
            f"CSV not found at {csv_path}. Pass --csv or --listing-ids explicitly."
        )

    ids: list[str] = []
    with gzip.open(csv_path, "rt", encoding="utf-8") as f:
        next(f, None)  # drop header
        for line in f:
            if not line.strip():
                continue
            first_col = line.split(",", 1)[0].strip().strip('"')
            if first_col:
                ids.append(first_col)
            if len(ids) >= limit:
                break
    return ids


def _parse_explicit_ids(csv_string: str) -> list[str]:
    """Parse comma-separated IDs from CLI."""
    return [x.strip() for x in csv_string.split(",") if x.strip()]


# =============================================================================
# Reporting
# =============================================================================

def _print_summary(results: list, aborted: bool) -> None:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.outcome.value] = counts.get(r.outcome.value, 0) + 1

    print()
    print("=" * 60)
    print("BATCH SUMMARY")
    print("=" * 60)
    print(f"Total scraped: {len(results)}")
    for outcome in ScrapeOutcome:
        print(f"  {outcome.value:<16}: {counts.get(outcome.value, 0)}")
    print(f"Aborted (D29):  {aborted}")
    print("=" * 60)


# =============================================================================
# Dry run
# =============================================================================

def _dry_run(listing_ids: Iterable[str]) -> int:
    print("DRY RUN — building URLs only, no network, no DB.")
    for lid in listing_ids:
        try:
            url = build_pdp_url(lid)
            print(f"  {lid} → {url}")
        except Exception as exc:
            print(f"  {lid} → ERROR: {type(exc).__name__}: {exc}")
    return 0


# =============================================================================
# Main
# =============================================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a batch of Airbnb PDP scrapes. Phase 2.3: archives "
                    "metadata + raw payloads, no price extraction yet."
    )
    parser.add_argument(
        "--limit", type=int, default=5,
        help="Max number of listings to scrape from CSV (default: 5).",
    )
    parser.add_argument(
        "--csv", type=Path, default=DEFAULT_CSV_PATH,
        help=f"Path to gzipped CSV with listing IDs (default: {DEFAULT_CSV_PATH}).",
    )
    parser.add_argument(
        "--listing-ids", type=str, default=None,
        help='Comma-separated listing IDs to scrape, overrides --csv (e.g. "123,456,789").',
    )
    parser.add_argument(
        "--no-headless", action="store_true",
        help="Show browser window (for manual pilot / Cloudflare diagnosis).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build URLs only, do not open browser or touch DB.",
    )
    args = parser.parse_args(argv)

    # ---- Resolve listing IDs ----
    if args.listing_ids:
        ids = _parse_explicit_ids(args.listing_ids)
        print(f"Using {len(ids)} explicit listing IDs from --listing-ids.")
    else:
        ids = _load_ids_from_csv(args.csv, args.limit)
        print(f"Loaded {len(ids)} listing IDs from {args.csv}.")

    if not ids:
        print("No listing IDs to process. Exiting.", file=sys.stderr)
        return 1

    # ---- Dry run path ----
    if args.dry_run:
        return _dry_run(ids)

    # ---- Real run ----
    headless = not args.no_headless
    print(f"Browser headless={headless}")

    engine = make_engine()
    db = SQLAlchemyDBClient(engine)
    logger = JSONLLogger()

    results: list = []
    aborted = False

    with PlaywrightBrowserSession(headless=headless) as browser:
        scraper = AirbnbScraper(browser=browser, db=db, logger=logger)
        # Reuse the already-open browser; do NOT re-enter via `with scraper:`.
        for i, lid in enumerate(ids, start=1):
            print(f"[{i}/{len(ids)}] {lid} ...", end=" ", flush=True)
            try:
                result = scraper.scrape_listing(lid)
                results.append(result)
                print(f"{result.outcome.value} ({result.duration_ms} ms)")
            except BatchAbortError as exc:
                print(f"\nBATCH ABORTED: {exc}", file=sys.stderr)
                aborted = True
                break
            except Exception as exc:
                print(f"\nUNHANDLED EXCEPTION on {lid}: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                # Don't abort — continue with next listing
                continue

    _print_summary(results, aborted)
    return 0 if not aborted else 2


if __name__ == "__main__":
    sys.exit(main())