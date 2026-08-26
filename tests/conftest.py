import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tpms.config import ClusterConfig, SessionConfig  # noqa: E402
from tpms.db import Database  # noqa: E402
from tpms.ingest import Ingestor  # noqa: E402


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def ingestor(db):
    return Ingestor(db, SessionConfig(gap_seconds=120), ClusterConfig())
