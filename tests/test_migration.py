"""Upgrading a database that already holds a capture.

The migration is a column-presence pass rather than a numbered sequence, so
the thing worth proving is that opening an older file adds what is missing and
leaves the history alone.
"""

import sqlite3

from tpms.db import SCHEMA_VERSION, Database

# The v5 sensors table: everything the current one has, minus `ignored`.
V5_SENSORS = """
CREATE TABLE sensors (
    pk            INTEGER PRIMARY KEY,
    model         TEXT NOT NULL,
    sensor_id     TEXT NOT NULL,
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    reading_count INTEGER NOT NULL DEFAULT 0,
    vehicle_id    INTEGER,
    wheel_label   TEXT,
    pinned        INTEGER NOT NULL DEFAULT 0,
    alias_of      INTEGER,
    UNIQUE (model, sensor_id)
);
CREATE TABLE vehicles (
    pk             INTEGER PRIMARY KEY,
    name           TEXT,
    notes          TEXT,
    created_at     REAL NOT NULL,
    auto_generated INTEGER NOT NULL DEFAULT 1,
    needs_review   INTEGER NOT NULL DEFAULT 0,
    review_reason  TEXT,
    provisional    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE sightings (
    pk              INTEGER PRIMARY KEY,
    sensor_pk       INTEGER NOT NULL,
    started_at      REAL NOT NULL,
    last_reading_at REAL NOT NULL,
    ended_at        REAL,
    reading_count   INTEGER NOT NULL DEFAULT 1,
    max_rssi        REAL,
    freq_mhz        REAL
);
"""


def _make_v5(path):
    conn = sqlite3.connect(path)
    conn.executescript(V5_SENSORS)
    conn.execute(
        "INSERT INTO vehicles (pk, name, created_at, auto_generated) "
        "VALUES (1, 'Blue wagon', 1000.0, 0)"
    )
    conn.execute(
        "INSERT INTO sensors (pk, model, sensor_id, first_seen, last_seen, "
        "reading_count, vehicle_id, wheel_label, pinned) "
        "VALUES (1, 'Toyota-TPMS', '1a2b', 1000.0, 2000.0, 42, 1, 'FL', 1)"
    )
    conn.execute(
        "INSERT INTO sightings (pk, sensor_pk, started_at, last_reading_at, "
        "ended_at, reading_count) VALUES (1, 1, 1000.0, 1100.0, 1100.0, 7)"
    )
    conn.execute("PRAGMA user_version=5")
    conn.commit()
    conn.close()


def test_a_v5_database_gains_the_new_column_and_keeps_its_history(tmp_path):
    path = tmp_path / "old.db"
    _make_v5(path)

    db = Database(path)
    db.init_schema()

    assert int(db.query_one("PRAGMA user_version")[0]) == SCHEMA_VERSION

    sensor = db.get_sensor(1)
    assert sensor.model == "Toyota-TPMS"
    assert sensor.reading_count == 42
    assert sensor.wheel_label == "FL"
    assert sensor.pinned
    assert sensor.vehicle_id == 1
    # The new column exists and defaults to "not hidden", so nothing that was
    # visible before the upgrade disappears after it.
    assert sensor.ignored is False

    assert db.get_vehicle(1).name == "Blue wagon"
    assert len(db.sightings_for_sensor(1)) == 1
    db.close()


def test_the_new_indexes_are_created_on_an_existing_database(tmp_path):
    path = tmp_path / "old.db"
    _make_v5(path)

    db = Database(path)
    db.init_schema()

    names = {
        row["name"]
        for row in db.query("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert {"sensors_vehicle", "sensors_alias"} <= names
    db.close()


def test_migrating_twice_is_a_no_op(tmp_path):
    """init_schema runs on every start-up, so it has to be idempotent."""
    path = tmp_path / "old.db"
    _make_v5(path)

    for _ in range(3):
        db = Database(path)
        db.init_schema()
        db.close()

    db = Database(path)
    assert db.get_sensor(1).reading_count == 42
    db.close()


def test_a_fresh_database_is_stamped_current(tmp_path):
    db = Database(tmp_path / "new.db")
    db.init_schema()
    assert int(db.query_one("PRAGMA user_version")[0]) == SCHEMA_VERSION
    db.close()
