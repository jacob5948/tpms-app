"""Header and cell counts have to agree, or data shifts under wrong headings.

The reading fields come from one shared macro, so adding a column to it
silently desynchronises every table whose <thead> was not updated too. Jinja
will not complain; the page just renders wrong.
"""

from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from tpms.config import Config
from tpms.models import Reading, now as now_ts
from tpms.service import Service
from tpms.web.app import create_app


class _Tables(HTMLParser):
    """Collect (header count, [row cell counts]) for every table on a page."""

    def __init__(self):
        super().__init__()
        self.tables = []
        self._headers = 0
        self._rows = []
        self._cells = 0
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            self._headers, self._rows, self._cells = 0, [], 0
        elif self._depth:
            if tag == "th":
                self._headers += int(dict(attrs).get("colspan") or 1)
            elif tag == "td":
                # A placeholder row spans the table with one colspan'd cell.
                self._cells += int(dict(attrs).get("colspan") or 1)
            elif tag == "tr":
                self._cells = 0

    def handle_endtag(self, tag):
        if self._depth and tag == "tr" and self._cells:
            self._rows.append(self._cells)
        elif tag == "table" and self._depth:
            self._depth -= 1
            self.tables.append((self._headers, self._rows))


@pytest.fixture
def client(tmp_path):
    svc = Service(Config(database=str(tmp_path / "shape.db")), start_radio=False)
    for p in range(4):
        for model, sid, slot in [("Toyota-TPMS", "aaa", 0), ("Toyota-TPMS", "bbb", 1)]:
            svc.ingestor.ingest(
                Reading(model=model, sensor_id=sid, ts=now_ts() - 60 + p + slot * 3,
                        rssi=-8.0 - slot, snr=12.0, freq_mhz=315.01,
                        pressure_kpa=230.0, temperature_c=28, battery_ok=1)
            )
    svc.recluster()
    return TestClient(create_app(svc))


@pytest.mark.parametrize(
    "path", ["/", "/sensors", "/vehicles", "/vehicles/1", "/events", "/api/heard-now.html"]
)
def test_every_row_matches_its_header_count(client, path):
    parser = _Tables()
    parser.feed(client.get(path).text)
    for headers, rows in parser.tables:
        for cells in rows:
            assert cells == headers, (
                f"{path}: a row has {cells} cells under {headers} headers"
            )


def test_the_pages_that_always_have_a_table_render_one(client):
    """Guards the check above from passing vacuously."""
    for path in ["/sensors", "/vehicles/1", "/events", "/api/heard-now.html"]:
        parser = _Tables()
        parser.feed(client.get(path).text)
        assert parser.tables, f"{path} rendered no tables"
        assert any(rows for _, rows in parser.tables), f"{path} rendered no rows"


def test_the_last_rssi_reaches_every_table_that_shows_readings(client):
    for path in ["/sensors", "/vehicles/1", "/api/heard-now.html"]:
        body = client.get(path).text
        assert "Signal level of the most recent reading" in body, path


@pytest.mark.parametrize(
    "path", ["/", "/sensors", "/vehicles", "/vehicles/1", "/events", "/api/heard-now.html"]
)
def test_pages_return_200(client, path):
    """A 500 renders an error page with no tables, which would look like a pass."""
    assert client.get(path).status_code == 200
