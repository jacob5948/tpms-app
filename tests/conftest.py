import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tpms.config import ClusterConfig, SessionConfig  # noqa: E402
from tpms.db import Database  # noqa: E402
from tpms.ingest import Ingestor  # noqa: E402
from tpms.models import display_timezone, set_display_timezone  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_display_timezone():
    """The display zone is process-wide, so a test that changes it must not
    hand the next one a different clock."""
    before = display_timezone()
    yield
    set_display_timezone(str(before))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def ingestor(db):
    return Ingestor(db, SessionConfig(gap_seconds=120), ClusterConfig())
