"""Which band a sensor was heard on -- stored per sighting, surfaced per sensor.

Frequency is the one field that distinguishes a 315 MHz factory sensor from a
433.92 MHz aftermarket one, so it has to survive both hopping and the
duplicate-decode collapse.
"""

import sqlite3

import pytest

from tpms import queries as q
from tpms.db import Database
from tpms.models import Reading, band_label, band_of


def _reading(sensor_id: str, ts: float, freq: float | None, model="Toyota-TPMS"):
    return Reading(model=model, sensor_id=sensor_id, ts=ts, rssi=-20.0, freq_mhz=freq)


@pytest.mark.parametrize(
    "measured,expected",
    [
        (315.012, 315.0),
        (314.98, 315.0),
        (433.94, 433.92),
        (None, None),
        (868.3, 868.3),  # unknown band is kept as measured, not forced to 315
    ],
)
def test_measured_frequencies_snap_to_their_band(measured, expected):
    assert band_of(measured) == expected


def test_band_labels_read_as_frequencies():
    assert band_label(315.012) == "315 MHz"
    assert band_label(433.94) == "433.92 MHz"
    assert band_label(None) is None


def test_sighting_records_the_band_it_was_heard_on(ingestor, db):
    ingestor.ingest(_reading("aaa", 1000, 315.01))
    sighting = db.sightings_for_sensor(db.list_sensors()[0].pk)[0]
    assert sighting.freq_mhz == pytest.approx(315.01)
    assert sighting.band == "315 MHz"


def test_a_hop_mid_sighting_keeps_the_latest_band(ingestor, db):
    """Hopping can catch one vehicle on both bands; the newest reading wins."""
    ingestor.ingest(_reading("aaa", 1000, 315.01))
    ingestor.ingest(_reading("aaa", 1040, 433.93))
    sensor_pk = db.list_sensors()[0].pk
    assert len(db.sightings_for_sensor(sensor_pk)) == 1
    assert db.sightings_for_sensor(sensor_pk)[0].band == "433.92 MHz"
    # ...but the per-sensor summary still shows both, so the hop is not lost.
    bands = {b["label"] for b in q.sensor_bands(db, sensor_pk)}
    assert bands == {"315 MHz", "433.92 MHz"}


def test_a_reading_without_a_frequency_does_not_erase_a_known_band(ingestor, db):
    ingestor.ingest(_reading("aaa", 1000, 315.01))
    ingestor.ingest(_reading("aaa", 1040, None))
    assert db.sightings_for_sensor(db.list_sensors()[0].pk)[0].band == "315 MHz"


def test_duplicate_decodes_do_not_inflate_the_band_count(ingestor, db):
    """An alias is the same burst, so counting it would double every reading.

    The count sits next to the sensor's reading count in the UI; if duplicates
    were folded in, the two numbers on one row would disagree.
    """
    ingestor.ingest(_reading("aaa", 1000, 315.01))
    ingestor.ingest(_reading("bbb", 1000, 315.01, model="Citroen"))
    canonical, alias = db.list_sensors()[0].pk, db.list_sensors()[1].pk
    db.execute("UPDATE sensors SET alias_of = ? WHERE pk = ?", (canonical, alias))

    bands = q.sensor_bands(db, canonical)
    assert len(bands) == 1
    assert bands[0]["label"] == "315 MHz"
    assert bands[0]["count"] == 1
    assert bands[0]["count"] == q.sensor_row(db, canonical)["reading_count"]


def test_sensor_row_and_events_surface_the_band(ingestor, db):
    ingestor.ingest(_reading("aaa", 1000, 433.93))
    sensor_pk = db.list_sensors()[0].pk
    assert q.sensor_row(db, sensor_pk)["band"] == "433.92 MHz"
    assert q.events(db)[0]["band"] == "433.92 MHz"


def test_existing_databases_backfill_the_band_from_their_readings(tmp_path):
    """A v2 database has sightings but no sightings.freq_mhz column."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sensors (
            pk INTEGER PRIMARY KEY, model TEXT NOT NULL, sensor_id TEXT NOT NULL,
            first_seen REAL NOT NULL, last_seen REAL NOT NULL,
            reading_count INTEGER NOT NULL DEFAULT 0, vehicle_id INTEGER,
            wheel_label TEXT, pinned INTEGER NOT NULL DEFAULT 0, alias_of INTEGER,
            UNIQUE (model, sensor_id));
        CREATE TABLE readings (
            pk INTEGER PRIMARY KEY, sensor_pk INTEGER NOT NULL, ts REAL NOT NULL,
            pressure_kpa REAL, temperature_c REAL, battery_ok INTEGER,
            freq_mhz REAL, rssi REAL, snr REAL, raw TEXT);
        CREATE TABLE sightings (
            pk INTEGER PRIMARY KEY, sensor_pk INTEGER NOT NULL,
            started_at REAL NOT NULL, last_reading_at REAL NOT NULL, ended_at REAL,
            reading_count INTEGER NOT NULL DEFAULT 1, max_rssi REAL);
        INSERT INTO sensors VALUES (1,'Toyota-TPMS','aaa',1000,1040,2,NULL,NULL,0,NULL);
        INSERT INTO readings (sensor_pk, ts, freq_mhz) VALUES (1,1000,315.01),(1,1040,315.02);
        INSERT INTO sightings (sensor_pk, started_at, last_reading_at, reading_count)
            VALUES (1,1000,1040,2);
        PRAGMA user_version=2;
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        assert db.sightings_for_sensor(1)[0].band == "315 MHz"
    finally:
        db.close()


def test_a_v3_database_gains_the_review_reason_column(tmp_path):
    """The Pi is running v3; opening it must migrate, not fail."""
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sensors (
            pk INTEGER PRIMARY KEY, model TEXT NOT NULL, sensor_id TEXT NOT NULL,
            first_seen REAL NOT NULL, last_seen REAL NOT NULL,
            reading_count INTEGER NOT NULL DEFAULT 0, vehicle_id INTEGER,
            wheel_label TEXT, pinned INTEGER NOT NULL DEFAULT 0, alias_of INTEGER,
            UNIQUE (model, sensor_id));
        CREATE TABLE vehicles (
            pk INTEGER PRIMARY KEY, name TEXT, notes TEXT, created_at REAL NOT NULL,
            auto_generated INTEGER NOT NULL DEFAULT 1,
            needs_review INTEGER NOT NULL DEFAULT 0,
            provisional INTEGER NOT NULL DEFAULT 0);
        INSERT INTO vehicles (pk, created_at) VALUES (1, 1000);
        PRAGMA user_version=3;
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        vehicle = db.get_vehicle(1)
        assert vehicle is not None
        assert vehicle.review_reason is None
    finally:
        db.close()
