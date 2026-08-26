"""SQLite storage layer.

One connection per thread (WAL mode), so the ingest thread and the web
request threads can work concurrently without sharing a handle.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import Reading, Sensor, Sighting, Vehicle

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS sensors (
    pk            INTEGER PRIMARY KEY,
    model         TEXT NOT NULL,
    sensor_id     TEXT NOT NULL,
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    reading_count INTEGER NOT NULL DEFAULT 0,
    vehicle_id    INTEGER REFERENCES vehicles(pk) ON DELETE SET NULL,
    wheel_label   TEXT,
    pinned        INTEGER NOT NULL DEFAULT 0,
    UNIQUE (model, sensor_id)
);

CREATE TABLE IF NOT EXISTS readings (
    pk            INTEGER PRIMARY KEY,
    sensor_pk     INTEGER NOT NULL REFERENCES sensors(pk) ON DELETE CASCADE,
    ts            REAL NOT NULL,
    pressure_kpa  REAL,
    temperature_c REAL,
    battery_ok    INTEGER,
    freq_mhz      REAL,
    rssi          REAL,
    snr           REAL,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS readings_ts ON readings (ts);
CREATE INDEX IF NOT EXISTS readings_sensor_ts ON readings (sensor_pk, ts);

CREATE TABLE IF NOT EXISTS sightings (
    pk              INTEGER PRIMARY KEY,
    sensor_pk       INTEGER NOT NULL REFERENCES sensors(pk) ON DELETE CASCADE,
    started_at      REAL NOT NULL,
    last_reading_at REAL NOT NULL,
    ended_at        REAL,
    reading_count   INTEGER NOT NULL DEFAULT 1,
    max_rssi        REAL
);
CREATE INDEX IF NOT EXISTS sightings_sensor ON sightings (sensor_pk, started_at);
CREATE INDEX IF NOT EXISTS sightings_open ON sightings (ended_at) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS sightings_started ON sightings (started_at);

CREATE TABLE IF NOT EXISTS vehicles (
    pk             INTEGER PRIMARY KEY,
    name           TEXT,
    notes          TEXT,
    created_at     REAL NOT NULL,
    auto_generated INTEGER NOT NULL DEFAULT 1,
    needs_review   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cooccurrence (
    a       INTEGER NOT NULL REFERENCES sensors(pk) ON DELETE CASCADE,
    b       INTEGER NOT NULL REFERENCES sensors(pk) ON DELETE CASCADE,
    count   INTEGER NOT NULL DEFAULT 0,
    last_at REAL NOT NULL,
    PRIMARY KEY (a, b)
);

-- Guarantees a pair is counted at most once per shared sighting, so a single
-- long pass cannot manufacture a strong edge.
CREATE TABLE IF NOT EXISTS cooccurrence_seen (
    sighting_a INTEGER NOT NULL,
    sighting_b INTEGER NOT NULL,
    PRIMARY KEY (sighting_a, sighting_b)
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._local = threading.local()
        self.write_lock = threading.RLock()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # ``:memory:`` databases are per-connection, so a shared cache keeps
        # every thread looking at the same tables (used by tests).
        self._uri = str(self.path) == ":memory:"
        self._dsn = (
            "file:tpms_memdb?mode=memory&cache=shared" if self._uri else str(self.path)
        )
        self._keepalive = self.connect() if self._uri else None
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self._dsn, uri=self._uri, timeout=30, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def init_schema(self) -> None:
        conn = self.connect()
        with self.write_lock, conn:
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- generic helpers -------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.connect().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, params).fetchone()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        conn = self.connect()
        with self.write_lock, conn:
            return conn.execute(sql, params)

    # -- sensors ---------------------------------------------------------

    def upsert_sensor(self, model: str, sensor_id: str, ts: float) -> int:
        """Insert or touch a sensor, returning its primary key."""
        conn = self.connect()
        with self.write_lock, conn:
            conn.execute(
                """
                INSERT INTO sensors (model, sensor_id, first_seen, last_seen, reading_count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT (model, sensor_id) DO UPDATE SET
                    last_seen     = MAX(sensors.last_seen, excluded.last_seen),
                    first_seen    = MIN(sensors.first_seen, excluded.first_seen),
                    reading_count = sensors.reading_count + 1
                """,
                (model, sensor_id, ts, ts),
            )
            row = conn.execute(
                "SELECT pk FROM sensors WHERE model = ? AND sensor_id = ?",
                (model, sensor_id),
            ).fetchone()
        return int(row["pk"])

    def get_sensor(self, pk: int) -> Sensor | None:
        row = self.query_one("SELECT * FROM sensors WHERE pk = ?", (pk,))
        return _sensor(row) if row else None

    def list_sensors(self) -> list[Sensor]:
        rows = self.query("SELECT * FROM sensors ORDER BY last_seen DESC")
        return [_sensor(r) for r in rows]

    def sensors_for_vehicle(self, vehicle_id: int) -> list[Sensor]:
        rows = self.query(
            "SELECT * FROM sensors WHERE vehicle_id = ? ORDER BY wheel_label, sensor_id",
            (vehicle_id,),
        )
        return [_sensor(r) for r in rows]

    def set_sensor_vehicle(self, sensor_pk: int, vehicle_id: int | None) -> None:
        self.execute(
            "UPDATE sensors SET vehicle_id = ? WHERE pk = ?", (vehicle_id, sensor_pk)
        )

    # -- readings --------------------------------------------------------

    def insert_reading(self, sensor_pk: int, reading: Reading) -> int:
        cur = self.execute(
            """
            INSERT INTO readings
                (sensor_pk, ts, pressure_kpa, temperature_c, battery_ok,
                 freq_mhz, rssi, snr, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sensor_pk,
                reading.ts,
                reading.pressure_kpa,
                reading.temperature_c,
                reading.battery_ok,
                reading.freq_mhz,
                reading.rssi,
                reading.snr,
                reading.raw,
            ),
        )
        return int(cur.lastrowid)

    def latest_reading(self, sensor_pk: int) -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM readings WHERE sensor_pk = ? ORDER BY ts DESC LIMIT 1",
            (sensor_pk,),
        )

    # -- vehicles --------------------------------------------------------

    def create_vehicle(
        self, created_at: float, auto_generated: bool = True, name: str | None = None
    ) -> int:
        cur = self.execute(
            "INSERT INTO vehicles (name, created_at, auto_generated) VALUES (?, ?, ?)",
            (name, created_at, int(auto_generated)),
        )
        return int(cur.lastrowid)

    def get_vehicle(self, pk: int) -> Vehicle | None:
        row = self.query_one("SELECT * FROM vehicles WHERE pk = ?", (pk,))
        return _vehicle(row) if row else None

    def list_vehicles(self) -> list[Vehicle]:
        rows = self.query("SELECT * FROM vehicles ORDER BY pk")
        return [_vehicle(r) for r in rows]

    def delete_empty_vehicles(self) -> int:
        cur = self.execute(
            """
            DELETE FROM vehicles
            WHERE pk NOT IN (SELECT vehicle_id FROM sensors WHERE vehicle_id IS NOT NULL)
            """
        )
        return cur.rowcount

    # -- sightings -------------------------------------------------------

    def open_sighting_for(self, sensor_pk: int) -> Sighting | None:
        row = self.query_one(
            "SELECT * FROM sightings WHERE sensor_pk = ? AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (sensor_pk,),
        )
        return _sighting(row) if row else None

    def list_open_sightings(self) -> list[Sighting]:
        rows = self.query(
            "SELECT * FROM sightings WHERE ended_at IS NULL ORDER BY started_at DESC"
        )
        return [_sighting(r) for r in rows]

    def create_sighting(
        self, sensor_pk: int, ts: float, rssi: float | None
    ) -> Sighting:
        cur = self.execute(
            """
            INSERT INTO sightings
                (sensor_pk, started_at, last_reading_at, reading_count, max_rssi)
            VALUES (?, ?, ?, 1, ?)
            """,
            (sensor_pk, ts, ts, rssi),
        )
        return Sighting(int(cur.lastrowid), sensor_pk, ts, ts, None, 1, rssi)

    def extend_sighting(self, pk: int, ts: float, rssi: float | None) -> None:
        self.execute(
            """
            UPDATE sightings
               SET last_reading_at = MAX(last_reading_at, ?),
                   reading_count   = reading_count + 1,
                   max_rssi        = CASE
                                       WHEN ? IS NULL THEN max_rssi
                                       WHEN max_rssi IS NULL THEN ?
                                       ELSE MAX(max_rssi, ?)
                                     END
             WHERE pk = ?
            """,
            (ts, rssi, rssi, rssi, pk),
        )

    def close_sighting(self, pk: int) -> None:
        """Close a sighting at its last reading -- never at 'now'.

        The sensor was last *heard* then; anything later is inference.
        """
        self.execute(
            "UPDATE sightings SET ended_at = last_reading_at WHERE pk = ? AND ended_at IS NULL",
            (pk,),
        )

    def close_stale_sightings(self, cutoff: float) -> int:
        cur = self.execute(
            "UPDATE sightings SET ended_at = last_reading_at "
            "WHERE ended_at IS NULL AND last_reading_at < ?",
            (cutoff,),
        )
        return cur.rowcount

    def sightings_for_sensor(self, sensor_pk: int, limit: int = 200) -> list[Sighting]:
        rows = self.query(
            "SELECT * FROM sightings WHERE sensor_pk = ? ORDER BY started_at DESC LIMIT ?",
            (sensor_pk, limit),
        )
        return [_sighting(r) for r in rows]

    def sighting_covering(self, sensor_pk: int, ts: float) -> int | None:
        """Primary key of the sighting that spans ``ts`` for this sensor.

        Falls back to the most recent sighting starting at or before ``ts``,
        which is the case when the co-occurring reading is the one that has
        just opened a brand new sighting.
        """
        row = self.query_one(
            """
            SELECT pk FROM sightings
             WHERE sensor_pk = ? AND started_at <= ?
               AND (ended_at IS NULL OR ended_at >= ?)
             ORDER BY started_at DESC LIMIT 1
            """,
            (sensor_pk, ts, ts),
        )
        if row is not None:
            return int(row["pk"])
        row = self.query_one(
            "SELECT pk FROM sightings WHERE sensor_pk = ? ORDER BY started_at DESC LIMIT 1",
            (sensor_pk,),
        )
        return int(row["pk"]) if row else None

    def recent_sensor_pks(self, since: float, until: float, exclude: int) -> list[int]:
        """Sensors with at least one reading in the [since, until] window."""
        rows = self.query(
            "SELECT DISTINCT sensor_pk FROM readings "
            "WHERE ts >= ? AND ts <= ? AND sensor_pk != ?",
            (since, until, exclude),
        )
        return [int(r["sensor_pk"]) for r in rows]

    # -- co-occurrence ---------------------------------------------------

    def note_cooccurrence(
        self, sighting_a: int, sighting_b: int, sensor_a: int, sensor_b: int, ts: float
    ) -> bool:
        """Record a co-occurrence, counted once per shared sighting pair.

        Returns True if this was a new pairing (and the count was bumped).
        """
        lo_s, hi_s = sorted((sighting_a, sighting_b))
        lo, hi = sorted((sensor_a, sensor_b))
        conn = self.connect()
        with self.write_lock, conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO cooccurrence_seen (sighting_a, sighting_b) VALUES (?, ?)",
                (lo_s, hi_s),
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                """
                INSERT INTO cooccurrence (a, b, count, last_at) VALUES (?, ?, 1, ?)
                ON CONFLICT (a, b) DO UPDATE SET
                    count = cooccurrence.count + 1,
                    last_at = MAX(cooccurrence.last_at, excluded.last_at)
                """,
                (lo, hi, ts),
            )
        return True

    def cooccurrence_rows(self) -> list[sqlite3.Row]:
        return self.query("SELECT a, b, count, last_at FROM cooccurrence")

    def sighting_counts(self) -> dict[int, int]:
        rows = self.query(
            "SELECT sensor_pk, COUNT(*) AS n FROM sightings GROUP BY sensor_pk"
        )
        return {int(r["sensor_pk"]): int(r["n"]) for r in rows}


def _sensor(row: sqlite3.Row) -> Sensor:
    return Sensor(
        pk=int(row["pk"]),
        model=row["model"],
        sensor_id=row["sensor_id"],
        first_seen=float(row["first_seen"]),
        last_seen=float(row["last_seen"]),
        reading_count=int(row["reading_count"]),
        vehicle_id=row["vehicle_id"],
        wheel_label=row["wheel_label"],
        pinned=bool(row["pinned"]),
    )


def _sighting(row: sqlite3.Row) -> Sighting:
    return Sighting(
        pk=int(row["pk"]),
        sensor_pk=int(row["sensor_pk"]),
        started_at=float(row["started_at"]),
        last_reading_at=float(row["last_reading_at"]),
        ended_at=float(row["ended_at"]) if row["ended_at"] is not None else None,
        reading_count=int(row["reading_count"]),
        max_rssi=float(row["max_rssi"]) if row["max_rssi"] is not None else None,
    )


def _vehicle(row: sqlite3.Row) -> Vehicle:
    return Vehicle(
        pk=int(row["pk"]),
        name=row["name"],
        notes=row["notes"],
        created_at=float(row["created_at"]),
        auto_generated=bool(row["auto_generated"]),
        needs_review=bool(row["needs_review"]),
    )
