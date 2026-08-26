"""Comings and goings: when a vehicle was audible, and how often it turned up."""

import time

import pytest
from fastapi.testclient import TestClient

from tpms import queries as q
from tpms.config import Config
from tpms.models import Reading
from tpms.service import Service
from tpms.synthetic import generate_lines
from tpms.web.app import create_app

GAP = 120.0


def _pass(ingestor, sensor_id, start, readings=4, step=20):
    for k in range(readings):
        ingestor.ingest(
            Reading(model="Toyota-TPMS", sensor_id=sensor_id, ts=start + k * step,
                    rssi=-20.0, pressure_kpa=240.0, freq_mhz=315.01)
        )


@pytest.fixture
def visits(ingestor, db):
    """One vehicle, two wheels, arriving three times."""
    for start in (1000, 10_000, 50_000):
        _pass(ingestor, "aaa", start)
        _pass(ingestor, "bbb", start + 5)
    ingestor.sweep(when=100_000)
    vehicle_id = db.execute("INSERT INTO vehicles (created_at) VALUES (0)").lastrowid
    for sensor in db.list_sensors():
        db.set_sensor_vehicle(sensor.pk, vehicle_id)
    return db, vehicle_id


def test_both_wheels_of_one_arrival_are_one_appearance(visits):
    db, vehicle_id = visits
    presence = q.vehicle_presence(db, vehicle_id, GAP)
    assert len(presence["intervals"]) == 3
    assert [i["sensor_count"] for i in presence["intervals"]] == [2, 2, 2]


def test_intervals_are_oldest_first_for_plotting(visits):
    db, vehicle_id = visits
    starts = [i["started_at"] for i in q.vehicle_presence(db, vehicle_id, GAP)["intervals"]]
    assert starts == sorted(starts)


def test_an_open_appearance_runs_up_to_now(ingestor, db):
    now = time.time()
    _pass(ingestor, "aaa", now - 30, readings=2, step=10)
    vehicle_id = db.execute("INSERT INTO vehicles (created_at) VALUES (0)").lastrowid
    db.set_sensor_vehicle(db.list_sensors()[0].pk, vehicle_id)

    interval = q.vehicle_presence(db, vehicle_id, GAP)["intervals"][0]
    assert interval["open"] and interval["ended_at"] is None
    # "until" is what the chart draws to; it must not be null.
    assert interval["until"] >= now - 1


def test_buckets_count_arrivals(visits):
    db, vehicle_id = visits
    presence = q.vehicle_presence(db, vehicle_id, GAP, buckets=5)
    assert sum(b["appearances"] for b in presence["buckets"]) == 3
    # Empty buckets are kept: a quiet week is the point of the chart.
    assert len(presence["buckets"]) == 5


def test_airtime_spreads_across_the_buckets_it_covers(ingestor, db):
    """A vehicle parked for hours belongs in every bucket it was there for,
    not only the one it arrived in."""
    for ts in range(0, 3600, 30):
        ingestor.ingest(Reading(model="Toyota-TPMS", sensor_id="aaa", ts=ts, rssi=-20.0))
    ingestor.sweep(when=100_000)
    vehicle_id = db.execute("INSERT INTO vehicles (created_at) VALUES (0)").lastrowid
    db.set_sensor_vehicle(db.list_sensors()[0].pk, vehicle_id)

    buckets = q.vehicle_presence(db, vehicle_id, GAP, start=0, end=3600, buckets=4)["buckets"]
    assert all(b["audible_seconds"] > 600 for b in buckets)
    assert sum(b["appearances"] for b in buckets) == 1


def test_a_window_keeps_an_appearance_that_straddles_its_edge(visits):
    db, vehicle_id = visits
    # Starts at 10_000; asking from 10_030 must still show it, mid-pass.
    presence = q.vehicle_presence(db, vehicle_id, GAP, start=10_030, end=20_000)
    assert [i["started_at"] for i in presence["intervals"]] == [10_000]


def test_a_vehicle_never_heard_reports_nothing_rather_than_failing(db):
    vehicle_id = db.execute("INSERT INTO vehicles (created_at) VALUES (0)").lastrowid
    presence = q.vehicle_presence(db, vehicle_id, GAP)
    assert presence["intervals"] == [] and presence["buckets"] == []


def test_the_endpoint_answers(tmp_path):
    service = Service(Config(database=str(tmp_path / "p.db")), start_radio=False)
    service.ingestor.replay(generate_lines())
    service.ingestor.sweep(when=9e9)
    service.recluster()
    api = TestClient(create_app(service))

    body = api.get("/api/vehicles/1/presence?buckets=24").json()
    assert body["intervals"] and len(body["buckets"]) == 24
    assert {"started_at", "until", "open", "duration"} <= set(body["intervals"][0])
    assert api.get("/api/vehicles/9999/presence").status_code == 404


def test_the_vehicle_page_hosts_the_chart(tmp_path):
    service = Service(Config(database=str(tmp_path / "p.db")), start_radio=False)
    service.ingestor.replay(generate_lines())
    service.ingestor.sweep(when=9e9)
    service.recluster()
    html = TestClient(create_app(service)).get("/vehicles/1").text
    assert 'id="chart-presence"' in html and 'id="chart-appearances"' in html
    # One range row for the page, not one per chart.
    assert html.count('id="chart-range"') == 1
