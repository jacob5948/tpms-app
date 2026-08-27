"""Spotting sensors that turned up while you were out.

Four transmitters first heard at the moment you got home is most of an
identification: the sensors list has to be able to answer "what is new" as
well as "what is here".
"""

import re

import pytest
from fastapi.testclient import TestClient

from tpms import queries as q
from tpms.config import Config
from tpms.models import Reading, now as now_ts
from tpms.service import Service
from tpms.web.app import create_app


@pytest.fixture
def heard(tmp_path):
    """One sensor first heard last week, one first heard a minute ago."""
    service = Service(Config(database=str(tmp_path / "new.db")), start_radio=False)
    now = now_ts()
    for sensor_id, first in (("old1", now - 7 * 86400), ("new1", now - 60)):
        for k in range(4):
            service.ingestor.ingest(
                Reading(model="Toyota-TPMS", sensor_id=sensor_id, ts=first + k * 20,
                        rssi=-20.0, pressure_kpa=240.0, freq_mhz=315.01)
            )
    return service


def test_new_marks_only_what_arrived_within_the_day(heard):
    rows = {r["sensor_id"]: r for r in q.sensor_rows(heard.db)}
    assert rows["new1"]["new"] is True
    assert rows["old1"]["new"] is False


def test_a_sensor_stops_being_new_without_going_quiet(heard):
    """New is about first_seen, not last_seen -- a sensor heard constantly
    since last week is not news, and would drown the ones that are."""
    rows = {r["sensor_id"]: r for r in q.sensor_rows(heard.db)}
    assert rows["old1"]["last_seen"] > rows["old1"]["first_seen"]
    assert rows["old1"]["new"] is False


def test_the_table_carries_a_sortable_first_heard(heard):
    html = TestClient(create_app(heard)).get("/sensors").text
    body = re.search(r"<tbody>.*</tbody>", html, re.S).group(0)
    # The sort key is the timestamp; the cell shows "3 days ago".
    assert re.search(r'data-sort="\d+\.?\d*">[^<]*ago', body)
    assert 'data-new="1"' in body and 'data-new="0"' in body


def test_the_new_tile_filters_the_table(heard):
    html = TestClient(create_app(heard)).get("/sensors").text
    assert 'data-filter-flag="new"' in html
    tile = re.search(r'data-filter-flag="new".*?</div>\s*</div>', html, re.S).group(0)
    assert ">1<" in tile          # one of the two sensors is new
