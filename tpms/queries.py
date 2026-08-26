"""Read models for the web UI.

Kept separate from db.py so the storage layer stays about persistence and the
shapes the templates want live in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .db import Database
from .models import band_label, band_of, now as now_ts, to_iso


@dataclass
class Interval:
    started_at: float
    ended_at: float | None
    sensor_count: int
    reading_count: int

    @property
    def open(self) -> bool:
        return self.ended_at is None

    @property
    def duration(self) -> float:
        return (self.ended_at or now_ts()) - self.started_at


def merge_intervals(
    rows: list[tuple[float, float | None, int]], join_gap: float
) -> list[Interval]:
    """Roll per-sensor sightings up into per-vehicle intervals.

    A vehicle is 'heard' from its first wheel to its last, so overlapping (or
    near-touching) wheel sightings collapse into a single appearance.
    """
    if not rows:
        return []

    ordered = sorted(rows, key=lambda r: r[0])
    merged: list[Interval] = []

    for started, ended, readings in ordered:
        if merged:
            current = merged[-1]
            # An open sighting swallows anything that starts after it.
            current_end = current.ended_at
            if current_end is None or started <= current_end + join_gap:
                current.sensor_count += 1
                current.reading_count += readings
                if ended is None or current_end is None:
                    current.ended_at = None
                else:
                    current.ended_at = max(current_end, ended)
                continue
        merged.append(Interval(started, ended, 1, readings))

    merged.reverse()  # newest first
    return merged


def vehicle_intervals(db: Database, vehicle_id: int, join_gap: float, limit: int = 100):
    rows = db.query(
        """
        SELECT s.started_at, s.ended_at, s.reading_count
          FROM sightings s
          JOIN sensors n ON n.pk = s.sensor_pk
         WHERE n.vehicle_id = ?
         ORDER BY s.started_at DESC
         LIMIT 2000
        """,
        (vehicle_id,),
    )
    intervals = merge_intervals(
        [(float(r["started_at"]), r["ended_at"], int(r["reading_count"])) for r in rows],
        join_gap,
    )
    return intervals[:limit]


def vehicle_summaries(db: Database, join_gap: float) -> list[dict[str, Any]]:
    rows = db.query(
        """
        SELECT v.pk, v.name, v.notes, v.auto_generated, v.needs_review, v.provisional,
               COUNT(n.pk)     AS sensor_count,
               MAX(n.last_seen) AS last_seen,
               SUM(n.reading_count) AS reading_count
          FROM vehicles v
          LEFT JOIN sensors n ON n.vehicle_id = v.pk
         GROUP BY v.pk
         ORDER BY last_seen DESC NULLS LAST
        """
    )
    out = []
    for row in rows:
        intervals = vehicle_intervals(db, int(row["pk"]), join_gap, limit=500)
        out.append(
            {
                "pk": int(row["pk"]),
                "name": row["name"] or f"Unnamed vehicle #{row['pk']}",
                "named": bool(row["name"]),
                "notes": row["notes"],
                "auto_generated": bool(row["auto_generated"]),
                "needs_review": bool(row["needs_review"]),
                "provisional": bool(row["provisional"]),
                "sensor_count": int(row["sensor_count"] or 0),
                "reading_count": int(row["reading_count"] or 0),
                "last_seen": row["last_seen"],
                "last_seen_iso": to_iso(row["last_seen"]),
                "appearances": len(intervals),
                "present": bool(intervals and intervals[0].open),
                "sensors": [
                    sensor_row(db, s.pk) for s in db.sensors_for_vehicle(int(row["pk"]))
                ],
            }
        )
    return out



def sensor_bands(db: Database, sensor_pk: int) -> list[dict[str, Any]]:
    """Bands a sensor has been heard on, most recently heard first.

    Measured frequencies scatter either side of the tuned band, so they are
    snapped before counting -- otherwise 314.98 and 315.03 would look like two
    different bands.
    """
    totals: dict[float, dict[str, Any]] = {}
    for row in db.band_counts(sensor_pk):
        band = band_of(row["freq_mhz"])
        if band is None:
            continue
        entry = totals.setdefault(
            band, {"band": band, "label": band_label(band), "count": 0, "last_at": 0.0}
        )
        entry["count"] += int(row["n"])
        entry["last_at"] = max(entry["last_at"], float(row["last_at"]))
    out = sorted(totals.values(), key=lambda e: e["last_at"], reverse=True)
    for entry in out:
        entry["last_at_iso"] = to_iso(entry["last_at"])
    return out


def sensor_row(db: Database, sensor_pk: int) -> dict[str, Any]:
    sensor = db.get_sensor(sensor_pk)
    if sensor is None:
        return {}
    latest = db.latest_reading(sensor_pk)
    open_sighting = db.open_sighting_for(sensor_pk)
    pressure = latest["pressure_kpa"] if latest else None
    bands = sensor_bands(db, sensor_pk)
    return {
        "pk": sensor.pk,
        "model": sensor.model,
        "sensor_id": sensor.sensor_id,
        "display": sensor.display,
        "wheel_label": sensor.wheel_label,
        "pinned": sensor.pinned,
        "alias_of": sensor.alias_of,
        "vehicle_id": sensor.vehicle_id,
        "reading_count": sensor.reading_count,
        "first_seen": sensor.first_seen,
        "first_seen_iso": to_iso(sensor.first_seen),
        "last_seen": sensor.last_seen,
        "last_seen_iso": to_iso(sensor.last_seen),
        "pressure_kpa": pressure,
        "pressure_psi": (pressure / 6.894757) if pressure is not None else None,
        "temperature_c": latest["temperature_c"] if latest else None,
        "battery_ok": latest["battery_ok"] if latest else None,
        "rssi": latest["rssi"] if latest else None,
        "freq_mhz": latest["freq_mhz"] if latest else None,
        "band": band_label(latest["freq_mhz"]) if latest else None,
        "bands": bands,
        "present": open_sighting is not None,
    }


def sensor_rows(db: Database, include_aliases: bool = False) -> list[dict[str, Any]]:
    """Sensor table rows.

    Duplicate decodes are folded into their canonical sensor by default --
    listing them as peers would triple the table and imply vehicles that do
    not exist.
    """
    names = {v.pk: v.display for v in db.list_vehicles()}
    sensors = db.list_sensors()
    displays = {s.pk: s.display for s in sensors}

    aliases: dict[int, list[str]] = {}
    for sensor in sensors:
        if sensor.alias_of is not None:
            aliases.setdefault(sensor.alias_of, []).append(sensor.display)

    out = []
    for sensor in sensors:
        if sensor.alias_of is not None and not include_aliases:
            continue
        row = sensor_row(db, sensor.pk)
        row["vehicle_name"] = names.get(sensor.vehicle_id) if sensor.vehicle_id else None
        row["aliases"] = sorted(aliases.get(sensor.pk, []))
        row["alias_of_display"] = displays.get(sensor.alias_of)
        out.append(row)
    return out


def alias_groups(db: Database) -> list[dict[str, Any]]:
    """Canonical sensors that have duplicate decodes, for the sensors page."""
    sensors = db.list_sensors()
    displays = {s.pk: s.display for s in sensors}
    grouped: dict[int, list[str]] = {}
    for sensor in sensors:
        if sensor.alias_of is not None:
            grouped.setdefault(sensor.alias_of, []).append(sensor.display)
    return [
        {"canonical": displays.get(pk, "?"), "aliases": sorted(names)}
        for pk, names in sorted(grouped.items())
    ]


def heard_now(db: Database) -> list[dict[str, Any]]:
    """Sensors with a sighting still open, newest first."""
    names = {v.pk: v.display for v in db.list_vehicles()}
    out = []
    for sighting in db.list_open_sightings():
        sensor = db.get_sensor(sighting.sensor_pk)
        if sensor is None:
            continue
        out.append(
            {
                "sensor_pk": sensor.pk,
                "display": sensor.display,
                "vehicle_id": sensor.vehicle_id,
                "vehicle_name": names.get(sensor.vehicle_id) if sensor.vehicle_id else None,
                "started_at": sighting.started_at,
                "started_at_iso": to_iso(sighting.started_at),
                "last_reading_at": sighting.last_reading_at,
                "last_reading_at_iso": to_iso(sighting.last_reading_at),
                "reading_count": sighting.reading_count,
                "max_rssi": sighting.max_rssi,
                "freq_mhz": sighting.freq_mhz,
                "band": sighting.band,
            }
        )
    out.sort(key=lambda r: r["last_reading_at"], reverse=True)
    return out


def events(
    db: Database,
    start: float | None = None,
    end: float | None = None,
    vehicle_id: int | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Flat appear / last-heard log, one row per sensor sighting."""
    clauses = []
    params: list[Any] = []
    if start is not None:
        clauses.append("s.started_at >= ?")
        params.append(start)
    if end is not None:
        clauses.append("s.started_at <= ?")
        params.append(end)
    if vehicle_id is not None:
        clauses.append("n.vehicle_id = ?")
        params.append(vehicle_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    rows = db.query(
        f"""
        SELECT s.pk, s.started_at, s.last_reading_at, s.ended_at, s.reading_count,
               s.max_rssi, s.freq_mhz, n.pk AS sensor_pk, n.model, n.sensor_id,
               n.wheel_label,
               n.vehicle_id, v.name AS vehicle_name
          FROM sightings s
          JOIN sensors n ON n.pk = s.sensor_pk
          LEFT JOIN vehicles v ON v.pk = n.vehicle_id
          {where}
         ORDER BY s.started_at DESC
         LIMIT ?
        """,
        params,
    )
    return [
        {
            "pk": int(r["pk"]),
            "sensor_pk": int(r["sensor_pk"]),
            "display": f"{r['model']}/{r['sensor_id']}",
            "wheel_label": r["wheel_label"],
            "vehicle_id": r["vehicle_id"],
            "vehicle_name": r["vehicle_name"]
            or (f"Unnamed vehicle #{r['vehicle_id']}" if r["vehicle_id"] else None),
            "started_at": float(r["started_at"]),
            "started_at_iso": to_iso(float(r["started_at"])),
            "last_reading_at": float(r["last_reading_at"]),
            "last_reading_at_iso": to_iso(float(r["last_reading_at"])),
            "ended_at": r["ended_at"],
            "open": r["ended_at"] is None,
            "duration": float(r["last_reading_at"]) - float(r["started_at"]),
            "reading_count": int(r["reading_count"]),
            "max_rssi": r["max_rssi"],
            "freq_mhz": r["freq_mhz"],
            "band": band_label(r["freq_mhz"]),
        }
        for r in rows
    ]


def pressure_history(db: Database, sensor_pk: int, limit: int = 500) -> list[dict]:
    rows = db.query(
        "SELECT ts, pressure_kpa, temperature_c FROM readings "
        "WHERE sensor_pk = ? AND pressure_kpa IS NOT NULL ORDER BY ts DESC LIMIT ?",
        (sensor_pk, limit),
    )
    return [
        {
            "ts": float(r["ts"]),
            "pressure_kpa": float(r["pressure_kpa"]),
            "pressure_psi": float(r["pressure_kpa"]) / 6.894757,
            "temperature_c": r["temperature_c"],
        }
        for r in reversed(rows)
    ]
