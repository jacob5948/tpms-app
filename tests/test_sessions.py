"""Sighting boundaries decide every appearance/disappearance number in the UI."""

from tpms.models import Reading
from tpms.sessions import SessionTracker


def _reading(sensor_id: str, ts: float, rssi: float | None = -20.0) -> Reading:
    return Reading(model="Toyota-TPMS", sensor_id=sensor_id, ts=ts, rssi=rssi)


def test_readings_inside_the_gap_extend_one_sighting(ingestor, db):
    for offset in (0, 40, 80, 119):
        ingestor.ingest(_reading("aaa", 1000 + offset))
    sensor_pk = db.list_sensors()[0].pk
    sightings = db.sightings_for_sensor(sensor_pk)
    assert len(sightings) == 1
    assert sightings[0].reading_count == 4
    assert sightings[0].duration == 119


def test_a_gap_beyond_the_threshold_starts_a_new_sighting(ingestor, db):
    ingestor.ingest(_reading("aaa", 1000))
    ingestor.ingest(_reading("aaa", 1000 + 121))
    sensor_pk = db.list_sensors()[0].pk
    sightings = db.sightings_for_sensor(sensor_pk)
    assert len(sightings) == 2


def test_gap_boundary_is_inclusive(ingestor, db):
    ingestor.ingest(_reading("aaa", 1000))
    ingestor.ingest(_reading("aaa", 1120))  # exactly the gap
    sensor_pk = db.list_sensors()[0].pk
    assert len(db.sightings_for_sensor(sensor_pk)) == 1


def test_closing_uses_last_heard_not_now(ingestor, db):
    """A sighting must end when the sensor was last audible.

    Stamping 'now' would invent airtime the sensor never used.
    """
    ingestor.ingest(_reading("aaa", 1000))
    ingestor.ingest(_reading("aaa", 1050))
    ingestor.sweep(when=99999)
    sighting = db.sightings_for_sensor(db.list_sensors()[0].pk)[0]
    assert sighting.ended_at == 1050


def test_sweep_only_closes_quiet_sightings(ingestor, db):
    ingestor.ingest(_reading("aaa", 1000))
    ingestor.ingest(_reading("bbb", 5000))
    closed = ingestor.sweep(when=5060)  # 'aaa' is stale, 'bbb' is not
    assert closed == 1
    assert len(db.list_open_sightings()) == 1


def test_single_reading_still_records_an_appearance(ingestor, db):
    ingestor.ingest(_reading("aaa", 1000))
    ingestor.sweep(when=99999)
    sightings = db.sightings_for_sensor(db.list_sensors()[0].pk)
    assert len(sightings) == 1
    assert sightings[0].duration == 0


def test_max_rssi_tracks_the_strongest_reading(ingestor, db):
    ingestor.ingest(_reading("aaa", 1000, rssi=-30.0))
    ingestor.ingest(_reading("aaa", 1010, rssi=-12.0))
    ingestor.ingest(_reading("aaa", 1020, rssi=-25.0))
    sighting = db.sightings_for_sensor(db.list_sensors()[0].pk)[0]
    assert sighting.max_rssi == -12.0


def test_identity_is_model_plus_id(ingestor, db):
    """Raw IDs collide across protocols, so the model must be part of identity."""
    ingestor.ingest(Reading(model="Toyota-TPMS", sensor_id="1234", ts=1000))
    ingestor.ingest(Reading(model="Ford-TPMS", sensor_id="1234", ts=1000))
    assert len(db.list_sensors()) == 2
