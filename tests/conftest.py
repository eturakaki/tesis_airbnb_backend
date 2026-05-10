import pytest
import random
from pathlib import Path
from datetime import datetime, timezone

from src.scraper.airbnb_scraper import AirbnbScraper, JSONLLogger, FakeBrowserSession
from src.db.db_client import FakeDBClient
FROZEN_TS = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)

def _frozen_now():
    return FROZEN_TS

#=============================================================================
# Helpers to build a fully-wired scraper
# =============================================================================

@pytest.fixture
def tmp_archive_dir(tmp_path) -> Path:
    return tmp_path / "archives"


@pytest.fixture
def tmp_logger(tmp_path) -> JSONLLogger:
    return JSONLLogger(log_dir=tmp_path / "logs", run_date=FROZEN_TS)


@pytest.fixture
def fake_browser():
    return FakeBrowserSession()


@pytest.fixture
def fake_db():
    return FakeDBClient()


@pytest.fixture
def make_scraper(fake_browser, fake_db, tmp_logger, tmp_archive_dir):
    """Factory that builds a fully-wired AirbnbScraper with deterministic clock + RNG."""
    def _factory(**overrides):
        defaults = dict(
            browser=fake_browser,
            db=fake_db,
            logger=tmp_logger,
            price_extractor=None,
            rate_limit_rng=random.Random(42),
            now_fn=_frozen_now,
            sleep_fn=lambda s: None,  # no-op sleep for speed
            archive_dir=tmp_archive_dir,
        )
        defaults.update(overrides)
        return AirbnbScraper(**defaults)
    return _factory

