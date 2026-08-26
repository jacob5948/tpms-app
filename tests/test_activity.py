"""The activity rollup behind the Live page chart, and the windowed history
the range buttons fetch."""

import pytest

from tpms import queries as q
from tpms.models import Reading


def _reading(sensor_id: str, ts: float, kpa: float | None = 240.0) -> Reading:
    return Reading(
        model="Toyota-TPMS", sensor_id=sensor_id, ts=ts, rssi=-20.0, pressure_kpa=kpa
    )


@pytest.fixture
def busy(ingestor, db):
    """Two sensors, one bucket busier than the next."""
    for ts in (1000, 1010, 1020):
        ingestor.ingest(_reading("aaa", ts))
    ingestor.ingest(_reading("bbb", 1005))
    ingestor.ingest(_reading("aaa", 5000))       # a long gap, then one more
    return db


def test_buckets_count_readings_and_transmitters(busy):
    report = q.activity(busy, start=900, end=1100, buckets=2)
    quiet, busiest = report["points"]          # 900-1000, then 1000-1100
    assert (quiet["readings"], quiet["sensors"]) == (0, 0)
    assert (busiest["readings"], busiest["sensors"]) == (4, 2)


def test_quiet_buckets_are_reported_as_zero_not_skipped(busy):
    """A gap in the capture is the thing this chart exists to show."""
    report = q.activity(busy, start=1000, end=5000, buckets=8)
    assert len(report["points"]) == 8
    assert sum(1 for p in report["points"] if p["readings"] == 0) >= 6


def test_passes_count_sightings_that_began_in_the_bucket(busy):
    report = q.activity(busy, start=1000, end=5100, buckets=2)
    # Three sightings start: aaa and bbb at ~1000, then aaa again after the gap.
    assert report["points"][0]["passes"] == 2
    assert report["points"][1]["passes"] == 1


def test_duplicate_decodes_are_one_transmitter(ingestor, db):
    ingestor.ingest(_reading("aaa", 1000))
    ingestor.ingest(Reading(model="Other-TPMS", sensor_id="aaa", ts=1000, rssi=-20.0))
    canonical, alias = sorted(db.list_sensors(), key=lambda s: s.pk)
    db.execute("UPDATE sensors SET alias_of = ? WHERE pk = ?", (canonical.pk, alias.pk))
    report = q.activity(db, start=900, end=1100, buckets=2)
    assert sum(p["readings"] for p in report["points"]) == 2
    assert max(p["sensors"] for p in report["points"]) == 1


def test_an_empty_database_reports_no_window(db):
    assert q.activity(db)["points"] == []


def test_history_honours_the_window(busy):
    sensor_pk = min(s.pk for s in busy.list_sensors())
    everything = q.pressure_history(busy, sensor_pk)
    recent = q.pressure_history(busy, sensor_pk, start=4000)
    assert len(everything) == 4 and len(recent) == 1


def test_a_long_series_is_thinned_but_keeps_its_ends(ingestor, db):
    for i in range(900):
        ingestor.ingest(_reading("aaa", 1000 + i, kpa=240.0 + i))
    sensor_pk = db.list_sensors()[0].pk
    rows = q.pressure_history(db, sensor_pk, limit=100)
    assert len(rows) == 100
    # Thinning must not quietly redraw the window as a shorter one.
    assert rows[0]["ts"] == 1000
    assert rows[-1]["ts"] == 1899


def test_chart_endpoints_answer(tmp_path):
    from fastapi.testclient import TestClient

    from tpms.config import Config
    from tpms.service import Service
    from tpms.synthetic import generate_lines
    from tpms.web.app import create_app

    service = Service(Config(database=str(tmp_path / "charts.db")), start_radio=False)
    service.ingestor.replay(generate_lines())
    service.ingestor.sweep(when=9e9)
    service.recluster()
    api = TestClient(create_app(service))

    activity = api.get("/api/activity?buckets=12").json()
    assert len(activity["points"]) == 12
    assert sum(p["readings"] for p in activity["points"]) > 0

    sensor_pk = service.db.list_sensors()[0].pk
    assert api.get(f"/api/sensors/{sensor_pk}/history").json()["points"]
    assert api.get("/api/vehicles/1/history").json()["series"]
    assert api.get("/api/sensors/99999/history").status_code == 404
    assert api.get("/api/vehicles/99999/history").status_code == 404
