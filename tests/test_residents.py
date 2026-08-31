"""Sensors parked in range behave nothing like passing traffic.

Two on a real capture had been continuously audible for over ten hours while
everything else was heard for a minute or two. A permanently-audible sensor
shares a window with every car that drives past, which single-pass grouping
would read as evidence they belong together.
"""

import pytest

from tpms.cluster import Clusterer
from tpms.config import ClusterConfig, Config
from tpms.models import Reading
from tpms.service import Service
from tpms import queries as q

HOUR = 3600.0


def _resident(ingestor, sensor_id="0104db9c", model="PMV-107J", hours=12, every=100):
    """A sensor transmitting steadily for hours, never going quiet."""
    for k in range(int(hours * HOUR / every)):
        ingestor.ingest(
            Reading(model=model, sensor_id=sensor_id, ts=1_000_000 + k * every,
                    rssi=-12.0, freq_mhz=315.01, pressure_kpa=208.0)
        )


def _drive_by(ingestor, sensor_id, model="Ford", at=1_020_000, rssi=-12.0):
    for burst in range(3):
        ingestor.ingest(
            Reading(model=model, sensor_id=sensor_id, ts=at + burst * 40,
                    rssi=rssi, freq_mhz=315.01, pressure_kpa=230.0)
        )


def test_a_sensor_audible_for_hours_is_resident(ingestor, db):
    _resident(ingestor)
    assert Clusterer(db).residents() == {db.list_sensors()[0].pk}


def test_a_car_driving_past_is_not(ingestor, db):
    _drive_by(ingestor, "6ccfcf63")
    assert Clusterer(db).residents() == set()


def test_a_sensor_heard_briefly_is_new_not_resident(ingestor, db):
    """One burst has a duty cycle of nothing over a window of nothing."""
    ingestor.ingest(Reading(model="Ford", sensor_id="6ccfcf63", ts=1000, rssi=-9.0))
    assert Clusterer(db).residents() == set()


def test_the_span_guard_stops_a_brand_new_sensor_qualifying(ingestor, db):
    """Audible 100% of a two-minute window is not evidence of anything."""
    for k in range(12):
        ingestor.ingest(
            Reading(model="Ford", sensor_id="6ccfcf63", ts=1000 + k * 10, rssi=-9.0)
        )
    config = ClusterConfig(resident_min_span_seconds=HOUR)
    assert Clusterer(db, config).residents() == set()


def test_sensor_present_across_receiver_gaps_is_resident(ingestor, db):
    """A sensor heard during every session qualifies even when the receiver
    was off for hours between sessions."""
    base = 1_000_000
    session_len = 4 * HOUR
    gap_between = 16 * HOUR
    for day in range(3):
        start = base + day * (session_len + gap_between)
        for k in range(int(session_len / 100)):
            ingestor.ingest(
                Reading(model="PMV-107J", sensor_id="parked01",
                        ts=start + k * 100, rssi=-12.0,
                        freq_mhz=315.01, pressure_kpa=208.0)
            )
    sensors = db.list_sensors()
    assert len(sensors) == 1
    pk = sensors[0].pk
    assert pk in Clusterer(db).residents()


def test_a_resident_does_not_seed_a_single_pass_grouping(ingestor, db):
    """The failure this exists to prevent.

    Same decoder, same signal level, heard at the same moment -- everything
    single-pass grouping looks for -- but one of them lives here.
    """
    _resident(ingestor, sensor_id="36dca165", model="Ford")
    _drive_by(ingestor, "6ccfcf63", model="Ford", at=1_020_000, rssi=-12.0)

    Clusterer(db).run()
    assert db.list_vehicles() == [], "a parked sensor is not a wheel of a passing car"


def test_a_resident_still_groups_on_repeated_co_occurrence(ingestor, db):
    """Only the single-pass shortcut is withheld, not clustering itself."""
    _resident(ingestor, sensor_id="36dca165", model="Ford")
    # A second sensor genuinely alongside it across many separate sightings.
    for p in range(6):
        _drive_by(ingestor, "36dca166", model="Ford", at=1_000_000 + p * 7200)

    Clusterer(db).run()
    assert db.list_vehicles(), "repeat co-occurrence is real evidence"


def test_the_ui_and_the_clusterer_agree_on_what_resident_means(tmp_path):
    svc = Service(Config(database=str(tmp_path / "r.db")), start_radio=False)
    _resident(svc.ingestor)
    _drive_by(svc.ingestor, "6ccfcf63")

    from_ui = {r["display"] for r in q.sensor_rows(svc.db) if r["resident"]}
    from_clusterer = {
        svc.db.get_sensor(pk).display for pk in Clusterer(svc.db).residents()
    }
    assert from_ui == from_clusterer == {"PMV-107J/0104db9c"}


# -- filtering from traffic views -----------------------------------------

def test_heard_now_excludes_residents_by_default(tmp_path):
    svc = Service(Config(database=str(tmp_path / "h.db")), start_radio=False)
    _resident(svc.ingestor)
    _drive_by(svc.ingestor, "6ccfcf63", at=1_043_100)

    included = q.heard_now(svc.db, include_residents=True)
    excluded = q.heard_now(svc.db, include_residents=False)
    assert len(included) > len(excluded)
    assert all(r["display"] != "PMV-107J/0104db9c" for r in excluded)


def test_events_exclude_residents_when_asked(tmp_path):
    svc = Service(Config(database=str(tmp_path / "e.db")), start_radio=False)
    _resident(svc.ingestor)
    _drive_by(svc.ingestor, "6ccfcf63")

    with_residents = q.events(svc.db, include_residents=True)
    without = q.events(svc.db, include_residents=False)
    assert len(with_residents) > len(without)
    resident_displays = {r["display"] for r in without}
    assert "PMV-107J/0104db9c" not in resident_displays


def test_activity_excludes_residents_when_asked(tmp_path):
    svc = Service(Config(database=str(tmp_path / "a.db")), start_radio=False)
    _resident(svc.ingestor)

    with_r = q.activity(svc.db, include_residents=True)
    without = q.activity(svc.db, include_residents=False)
    total_with = sum(p["readings"] for p in with_r["points"])
    total_without = sum(p["readings"] for p in without["points"])
    assert total_with > total_without


# -- saturation -----------------------------------------------------------

def _levels(svc, count, hot, rssi_hot=-0.4, rssi_ok=-9.0):
    for k in range(count):
        svc.ingestor.ingest(
            Reading(model="Ford", sensor_id="a%d" % k, ts=1000 + k,
                    rssi=rssi_hot if k < hot else rssi_ok, freq_mhz=315.01)
        )


def test_readings_crowding_full_scale_are_flagged(tmp_path):
    svc = Service(Config(database=str(tmp_path / "s.db")), start_radio=False)
    _levels(svc, 100, 10)
    assert svc.status()["signal"]["saturated"] is True


def test_a_few_very_close_cars_are_not_a_gain_problem(tmp_path):
    svc = Service(Config(database=str(tmp_path / "s.db")), start_radio=False)
    _levels(svc, 100, 3)
    assert svc.status()["signal"]["saturated"] is False


def test_a_tiny_sample_never_raises_the_alarm(tmp_path):
    """Three strong readings out of five is a car in the driveway."""
    svc = Service(Config(database=str(tmp_path / "s.db")), start_radio=False)
    _levels(svc, 5, 3)
    assert svc.status()["signal"]["saturated"] is False


# -- cluster size limit ---------------------------------------------------

def test_a_cluster_reaching_the_limit_is_flagged(ingestor, db):
    """Six sensors with max_cluster_size 6 used to slip through unflagged."""
    for p in range(5):
        for index, sid in enumerate(["w1", "w2", "w3", "w4", "w5", "w6"]):
            ingestor.ingest(
                Reading(model="Toyota-TPMS", sensor_id=sid,
                        ts=10_000 + p * 7200 + index * 0.4, rssi=-9.0)
            )
    report = Clusterer(db, ClusterConfig(max_cluster_size=6)).run()
    assert report.oversized, "a cluster at the limit needs review"
    assert [v for v in db.list_vehicles() if v.needs_review]


def test_a_normal_four_wheeled_car_is_not_flagged(ingestor, db):
    for p in range(5):
        for index, sid in enumerate(["w1", "w2", "w3", "w4"]):
            ingestor.ingest(
                Reading(model="Toyota-TPMS", sensor_id=sid,
                        ts=10_000 + p * 7200 + index * 0.4, rssi=-9.0)
            )
    report = Clusterer(db, ClusterConfig(max_cluster_size=6)).run()
    assert not report.oversized


def test_support_is_a_share_and_never_exceeds_one(ingestor, db):
    """A resident's single long sighting overlaps many short ones.

    The count is incremented once per unique pair of sightings, so it can pass
    the rarer sensor's sighting total and produce "support=2.00" -- which is
    not a share of anything.
    """
    _resident(ingestor, sensor_id="36dca165", model="Ford")
    for p in range(4):
        _drive_by(ingestor, "6ccfcf63", model="Ford", at=1_000_000 + p * 7200)

    for edge in Clusterer(db).build_edges():
        assert 0.0 <= edge.support <= 1.0, f"support {edge.support} is not a share"
