"""The assignment workbench shows what was audible at a past moment."""

import pytest

from tpms.config import Config
from tpms.models import Reading, to_iso
from tpms.service import Service
from tpms import queries as q

BASE = 1_700_000_000.0


def _pass(ingestor, sensors, at):
    for i, sid in enumerate(sensors):
        for burst in range(3):
            ingestor.ingest(
                Reading(
                    model="Toyota-TPMS", sensor_id=sid, ts=at + i * 0.4 + burst * 30,
                    rssi=-9.0, freq_mhz=315.01, pressure_kpa=230.0,
                )
            )


# -- heard_at ----------------------------------------------------------------

def test_heard_at_returns_sensors_audible_at_a_moment(ingestor, db):
    _pass(ingestor, ["a1", "a2", "a3", "a4"], at=BASE)
    ingestor.sweep(when=BASE + 300)
    result = q.heard_at(db, BASE + 30)
    displays = {s["display"] for s in result["loose"]}
    assert displays == {
        "Toyota-TPMS/a1", "Toyota-TPMS/a2",
        "Toyota-TPMS/a3", "Toyota-TPMS/a4",
    }


def test_heard_at_returns_nothing_outside_the_sighting(ingestor, db):
    _pass(ingestor, ["b1"], at=BASE)
    ingestor.sweep(when=BASE + 300)
    result = q.heard_at(db, BASE + 500)
    assert result["count"] == 0


def test_heard_at_tolerance_catches_near_misses(ingestor, db):
    _pass(ingestor, ["c1"], at=BASE)
    ingestor.sweep(when=BASE + 300)
    last = BASE + 60
    just_after = last + 20
    result = q.heard_at(db, just_after, tolerance=30)
    assert result["count"] >= 1


def test_heard_at_groups_assigned_sensors_by_vehicle(ingestor, db):
    _pass(ingestor, ["d1", "d2"], at=BASE)
    ingestor.sweep(when=BASE + 300)
    pk1 = db.list_sensors()[0].pk
    pk2 = db.list_sensors()[1].pk
    vid = db.create_vehicle(BASE, auto_generated=False)
    db.set_sensor_vehicle(pk1, vid)
    db.set_sensor_vehicle(pk2, vid)

    result = q.heard_at(db, BASE + 30)
    assert len(result["vehicles"]) == 1
    assert len(result["vehicles"][0]["sensors"]) == 2
    assert result["loose"] == []


# -- nearest_event -----------------------------------------------------------

def test_nearest_event_finds_previous_sighting(ingestor, db):
    _pass(ingestor, ["e1"], at=BASE)
    _pass(ingestor, ["e2"], at=BASE + 3600)
    ingestor.sweep(when=BASE + 7200)

    prev = q.nearest_event(db, BASE + 3600 + 30, "prev")
    assert prev is not None
    assert prev < BASE + 3600 + 30


def test_nearest_event_finds_next_sighting(ingestor, db):
    _pass(ingestor, ["f1"], at=BASE)
    _pass(ingestor, ["f2"], at=BASE + 3600)
    ingestor.sweep(when=BASE + 7200)

    nxt = q.nearest_event(db, BASE + 30, "next")
    assert nxt is not None
    assert nxt > BASE + 30


def test_nearest_event_returns_none_at_boundaries(ingestor, db):
    _pass(ingestor, ["g1"], at=BASE)
    ingestor.sweep(when=BASE + 300)

    assert q.nearest_event(db, BASE - 100, "prev") is None
    assert q.nearest_event(db, BASE + 500, "next") is None


# -- browser test ------------------------------------------------------------

def test_assign_page_shows_sensors_and_allows_grouping(serve, page):
    svc = Service(Config(database=":memory:"), start_radio=False)
    _pass(svc.ingestor, ["w1", "w2", "w3", "w4"], at=BASE)
    svc.ingestor.sweep(when=BASE + 300)

    url = serve(svc)
    at_iso = to_iso(BASE + 30)
    page.goto(f"{url}/assign?at={at_iso}")
    page.wait_for_load_state("networkidle")

    assert page.locator("text=Toyota-TPMS/w1").count() >= 1
    assert page.locator("text=Toyota-TPMS/w4").count() >= 1
    assert "4" in page.locator(".stat .v").first.text_content()
