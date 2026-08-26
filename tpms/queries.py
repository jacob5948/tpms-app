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


def vehicle_presence(
    db: Database,
    vehicle_id: int,
    join_gap: float,
    start: float | None = None,
    end: float | None = None,
    buckets: int = 96,
    limit: int = 3000,
) -> dict[str, Any]:
    """When a vehicle was audible, and how often it turned up.

    Two shapes of the same fact, because one chart cannot carry both: the
    intervals are exact but a 90-second pass is sub-pixel across a month,
    while the bucket counts stay legible at any zoom and lose the detail.
    """
    sql = """
        SELECT s.started_at, s.ended_at, s.reading_count
          FROM sightings s
          JOIN sensors n ON n.pk = s.sensor_pk
         WHERE n.vehicle_id = ?
    """
    params: list[Any] = [vehicle_id]
    if start is not None:
        # Overlapping, not contained: an appearance that began before the
        # window and is still running is exactly what you want to see.
        sql += " AND (s.ended_at IS NULL OR s.ended_at >= ?)"
        params.append(start)
    if end is not None:
        sql += " AND s.started_at <= ?"
        params.append(end)
    sql += " ORDER BY s.started_at DESC LIMIT ?"
    params.append(limit)

    merged = merge_intervals(
        [
            (float(r["started_at"]), r["ended_at"], int(r["reading_count"]))
            for r in db.query(sql, tuple(params))
        ],
        join_gap,
    )
    merged.reverse()   # oldest first, the order a chart plots in

    now = now_ts()
    intervals = [
        {
            "started_at": i.started_at,
            "started_at_iso": to_iso(i.started_at),
            "ended_at": i.ended_at,
            "ended_at_iso": to_iso(i.ended_at) if i.ended_at else None,
            # An open appearance is drawn up to now; it has not ended yet.
            "until": i.ended_at if i.ended_at is not None else now,
            "open": i.open,
            "duration": i.duration,
            "sensor_count": i.sensor_count,
            "reading_count": i.reading_count,
        }
        for i in merged
    ]

    lo = start if start is not None else (intervals[0]["started_at"] if intervals else None)
    hi = end if end is not None else (max(i["until"] for i in intervals) if intervals else None)
    if lo is None or hi is None or hi <= lo:
        return {"intervals": intervals, "buckets": [], "width": 0, "start": lo, "end": hi}

    buckets = max(2, min(int(buckets), 500))
    width = (hi - lo) / buckets
    counts = [0] * buckets
    audible = [0.0] * buckets
    for interval in intervals:
        index = int((interval["started_at"] - lo) / width)
        if 0 <= index < buckets:
            counts[index] += 1
        # Airtime is spread across the buckets it actually covers, so a long
        # stay does not land entirely in the bucket it began in. Walk bucket
        # indices rather than timestamps: with unix seconds and a narrow
        # bucket, advancing a cursor by floating-point arithmetic can fail to
        # move at all, and the loop never ends.
        cursor = max(interval["started_at"], lo)
        finish = min(interval["until"], hi)
        if finish <= cursor:
            continue
        first = max(0, min(int((cursor - lo) / width), buckets - 1))
        last = max(0, min(int((finish - lo) / width), buckets - 1))
        for index in range(first, last + 1):
            edge = lo + index * width
            overlap = min(finish, edge + width) - max(cursor, edge)
            if overlap > 0:
                audible[index] += overlap

    return {
        "intervals": intervals,
        "buckets": [
            {
                "ts": lo + (i + 0.5) * width,
                "appearances": counts[i],
                "audible_seconds": round(audible[i], 1),
            }
            for i in range(buckets)
        ],
        "width": width,
        "start": lo,
        "end": hi,
    }


def vehicle_summaries(db: Database, join_gap: float) -> list[dict[str, Any]]:
    rows = db.query(
        """
        SELECT v.pk, v.name, v.notes, v.auto_generated, v.needs_review, v.provisional,
               v.review_reason,
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
                "review_reason": row["review_reason"],
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



def _bands_from(rows: list[Any]) -> list[dict[str, Any]]:
    """Snap measured frequencies onto bands and total them up.

    Measured frequencies scatter either side of the tuned band, so they are
    snapped before counting -- otherwise 314.98 and 315.03 would look like two
    different bands.
    """
    totals: dict[float, dict[str, Any]] = {}
    for row in rows:
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


def sensor_bands(db: Database, sensor_pk: int) -> list[dict[str, Any]]:
    """Bands one sensor has been heard on, most recently heard first."""
    return _bands_from(db.band_counts(sensor_pk, include_aliases=False))


def resident_pks(db: Database, config: Any = None) -> set[int]:
    """Sensors parked in range, cached per call site.

    Wraps ``Clusterer.residents`` so the UI and the clusterer cannot drift
    apart on what "resident" means.
    """
    from .cluster import Clusterer
    from .config import ClusterConfig

    return Clusterer(db, config or ClusterConfig()).residents()


def sensor_row(
    db: Database,
    sensor_pk: int,
    residents: set[int] | None = None,
    duty_cycles: dict[int, tuple[float, float]] | None = None,
    bands_by_sensor: dict[int, list[Any]] | None = None,
) -> dict[str, Any]:
    """One sensor's table row.

    ``duty_cycles`` and ``bands_by_sensor`` are whole-database aggregates. The
    caller passes them in when building many rows, because computing either
    one per row is how the Sensors page became the slowest thing in the app.
    """
    sensor = db.get_sensor(sensor_pk)
    if sensor is None:
        return {}
    resident = sensor_pk in residents if residents is not None else False
    if duty_cycles is None:
        duty_cycles = db.duty_cycles()
    duty = duty_cycles.get(sensor_pk, (0.0, 0.0))[0]
    latest = db.latest_reading(sensor_pk)
    open_sighting = db.open_sighting_for(sensor_pk)
    pressure = latest["pressure_kpa"] if latest else None
    bands = (
        _bands_from(bands_by_sensor.get(sensor_pk, []))
        if bands_by_sensor is not None
        else sensor_bands(db, sensor_pk)
    )
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
        "duty_cycle": duty,
        "resident": resident,
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
    residents = resident_pks(db)
    # Computed once for the page, not once per row.
    duty_cycles = db.duty_cycles()
    bands_by_sensor = db.all_band_counts()

    aliases: dict[int, list[str]] = {}
    for sensor in sensors:
        if sensor.alias_of is not None:
            aliases.setdefault(sensor.alias_of, []).append(sensor.display)

    out = []
    for sensor in sensors:
        if sensor.alias_of is not None and not include_aliases:
            continue
        row = sensor_row(db, sensor.pk, residents, duty_cycles, bands_by_sensor)
        row["vehicle_name"] = names.get(sensor.vehicle_id) if sensor.vehicle_id else None
        row["aliases"] = sorted(aliases.get(sensor.pk, []))
        row["alias_of_display"] = displays.get(sensor.alias_of)
        out.append(row)
    return out


def alias_groups(db: Database) -> list[dict[str, Any]]:
    """Canonical sensors that have duplicate decodes, for the sensors page."""
    sensors = db.list_sensors()
    displays = {s.pk: s.display for s in sensors}
    grouped: dict[int, list[dict[str, Any]]] = {}
    for sensor in sensors:
        if sensor.alias_of is not None:
            grouped.setdefault(sensor.alias_of, []).append(
                {"pk": sensor.pk, "display": sensor.display}
            )
    return [
        {
            "canonical_pk": pk,
            "canonical": displays.get(pk, "?"),
            "aliases": sorted(n["display"] for n in names),
            "alias_list": sorted(names, key=lambda n: n["display"]),
        }
        for pk, names in sorted(grouped.items())
    ]


def heard_now(db: Database) -> list[dict[str, Any]]:
    """Sensors with a sighting still open, newest first.

    Duplicate decodes are folded away, as everywhere else: listing them would
    show one transmitter as several audible sensors and disagree with the
    counts on every other page. Both decoders open a sighting on the same
    burst, so the canonical one is audible whenever its duplicate is.
    """
    names = {v.pk: v.display for v in db.list_vehicles()}
    out = []
    for sighting in db.list_open_sightings():
        sensor = db.get_sensor(sighting.sensor_pk)
        if sensor is None or sensor.alias_of is not None:
            continue
        # Carry the same reading fields the rest of the UI shows, so "heard
        # now" is not the one table where pressure and band are missing.
        latest = db.latest_reading(sensor.pk)
        pressure = latest["pressure_kpa"] if latest else None
        out.append(
            {
                "sensor_pk": sensor.pk,
                "display": sensor.display,
                "wheel_label": sensor.wheel_label,
                "pressure_kpa": pressure,
                "pressure_psi": (pressure / 6.894757) if pressure is not None else None,
                "temperature_c": latest["temperature_c"] if latest else None,
                "battery_ok": latest["battery_ok"] if latest else None,
                "rssi": latest["rssi"] if latest else None,
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


def _thin(rows: list[Any], limit: int) -> list[Any]:
    """Evenly sample a long series down to ``limit`` points.

    A week of readings from a resident sensor is tens of thousands of rows,
    which no 900px-wide chart can show. Taking the newest N instead would be
    cheaper but would quietly redraw the requested window as a much shorter
    one, so sample across the whole span and always keep the last point --
    that is the one the pages label as the latest reading.
    """
    if len(rows) <= limit or limit < 2:
        return rows
    step = (len(rows) - 1) / (limit - 1)
    picked = [rows[round(i * step)] for i in range(limit)]
    picked[-1] = rows[-1]
    return picked


def pressure_history(
    db: Database,
    sensor_pk: int,
    limit: int = 500,
    start: float | None = None,
    end: float | None = None,
) -> list[dict]:
    sql = (
        "SELECT ts, pressure_kpa, temperature_c FROM readings "
        "WHERE sensor_pk = ? AND pressure_kpa IS NOT NULL"
    )
    params: list[Any] = [sensor_pk]
    if start is not None:
        sql += " AND ts >= ?"
        params.append(start)
    if end is not None:
        sql += " AND ts <= ?"
        params.append(end)
    # Read generously, then thin: the cap is on what gets plotted, not on how
    # much of the window is looked at.
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(max(limit * 40, limit))
    rows = list(reversed(db.query(sql, tuple(params))))
    return [
        {
            "ts": float(r["ts"]),
            "pressure_kpa": float(r["pressure_kpa"]),
            "pressure_psi": float(r["pressure_kpa"]) / 6.894757,
            "temperature_c": r["temperature_c"],
        }
        for r in _thin(rows, limit)
    ]


def activity(
    db: Database,
    start: float | None = None,
    end: float | None = None,
    buckets: int = 96,
) -> dict[str, Any]:
    """How busy the receiver has been, bucketed over a window.

    Three quantities, because they answer different questions: readings say
    how much RF got through, transmitters say how many distinct vehicles were
    around, and passes -- sightings that began in the bucket -- say how much
    traffic actually drove by, which is the one that shows a rush hour.
    """
    span = db.query("SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM readings")[0]
    if span["lo"] is None:
        return {"start": None, "end": None, "width": 0, "points": []}

    start = float(span["lo"]) if start is None else max(float(start), 0.0)
    end = float(span["hi"]) if end is None else float(end)
    if end <= start:
        end = start + 1.0
    buckets = max(2, min(int(buckets), 500))
    width = (end - start) / buckets

    counts = {
        int(r["b"]): (int(r["readings"]), int(r["sensors"]))
        for r in db.query(
            "SELECT CAST((r.ts - ?) / ? AS INTEGER) AS b, COUNT(*) AS readings, "
            # Duplicate decodes are one transmitter, not two.
            "COUNT(DISTINCT COALESCE(s.alias_of, s.pk)) AS sensors "
            "FROM readings r JOIN sensors s ON s.pk = r.sensor_pk "
            "WHERE r.ts >= ? AND r.ts < ? GROUP BY b",
            (start, width, start, end),
        )
    }
    passes = {
        int(r["b"]): int(r["passes"])
        for r in db.query(
            "SELECT CAST((g.started_at - ?) / ? AS INTEGER) AS b, COUNT(*) AS passes "
            "FROM sightings g JOIN sensors s ON s.pk = g.sensor_pk "
            "WHERE s.alias_of IS NULL AND g.started_at >= ? AND g.started_at < ? "
            "GROUP BY b",
            (start, width, start, end),
        )
    }

    # Empty buckets are filled in rather than skipped: a gap in the capture is
    # exactly what this chart exists to make visible.
    points = []
    for i in range(buckets):
        readings, sensors = counts.get(i, (0, 0))
        points.append(
            {
                "ts": start + (i + 0.5) * width,
                "readings": readings,
                "sensors": sensors,
                "passes": passes.get(i, 0),
            }
        )
    return {"start": start, "end": end, "width": width, "points": points}


def heard_alongside(
    db: Database, sensor_pk: int, min_support: float = 0.6, limit: int = 12
) -> list[dict[str, Any]]:
    """Other sensors audible at the same moments as this one.

    This is the raw evidence clustering runs on, and until now it was invisible
    -- a vehicle could be grouped or not grouped with no way to see why. Support
    is the share of the rarer sensor's sightings the two were heard in together,
    which is the term that separates "same car" from "passed together once".
    """
    counts = db.sighting_counts()
    this = db.get_sensor(sensor_pk)
    alias_of = this.alias_of if this else None
    rows = db.query(
        """
        SELECT CASE WHEN c.a = ? THEN c.b ELSE c.a END AS other,
               c.count, c.last_at
          FROM cooccurrence c
         WHERE c.a = ? OR c.b = ?
        """,
        (sensor_pk, sensor_pk, sensor_pk),
    )
    names = {v.pk: v.display for v in db.list_vehicles()}
    out = []
    for row in rows:
        other = int(row["other"])
        sensor = db.get_sensor(other)
        if sensor is None:
            continue
        # A duplicate decode of some third sensor is not a transmitter in its
        # own right; it would list the same vehicle twice under two protocol
        # names. Duplicates of *this* sensor stay, labelled as such.
        if sensor.alias_of is not None and sensor.alias_of != sensor_pk:
            continue
        denominator = min(counts.get(sensor_pk, 0), counts.get(other, 0))
        support = min(int(row["count"]) / denominator, 1.0) if denominator else 0.0
        out.append(
            {
                "pk": other,
                "display": sensor.display,
                # Perfect co-occurrence with a duplicate decode means nothing:
                # it is the same burst, so it would always score 100%.
                "duplicate": sensor.alias_of == sensor_pk
                or sensor.pk == alias_of
                or (alias_of is not None and sensor.alias_of == alias_of),
                "vehicle_id": sensor.vehicle_id,
                "vehicle_name": names.get(sensor.vehicle_id)
                if sensor.vehicle_id
                else None,
                "count": int(row["count"]),
                "support": support,
                "strong": support >= min_support,
                "last_at": float(row["last_at"]),
                "last_at_iso": to_iso(float(row["last_at"])),
            }
        )
    out.sort(key=lambda r: (r["support"], r["count"]), reverse=True)
    return out[:limit]


def sensor_detail(
    db: Database, sensor_pk: int, min_support: float = 0.6
) -> dict[str, Any] | None:
    """Everything known about one sensor, for the page that owns it.

    The same numbers appear in half a dozen tables elsewhere; this is the one
    place they are all together, so every other mention of a sensor can just
    link here instead of picking a different subset to show.
    """
    sensor = db.get_sensor(sensor_pk)
    if sensor is None:
        return None

    row = sensor_row(db, sensor_pk, resident_pks(db))
    names = {v.pk: v.display for v in db.list_vehicles()}
    displays = {s.pk: s.display for s in db.list_sensors()}
    latest = db.latest_reading(sensor_pk)

    row.update(
        {
            "vehicle_name": names.get(sensor.vehicle_id) if sensor.vehicle_id else None,
            "aliases": [
                {"pk": s.pk, "display": s.display}
                for s in db.list_sensors()
                if s.alias_of == sensor_pk
            ],
            "alias_of_display": displays.get(sensor.alias_of),
            "sightings": db.sightings_for_sensor(sensor_pk, limit=200),
            "history": pressure_history(db, sensor_pk, limit=300),
            "heard_with": heard_alongside(db, sensor_pk, min_support),
            "raw": latest["raw"] if latest else None,
            "snr": latest["snr"] if latest else None,
            "latest_ts": float(latest["ts"]) if latest else None,
            "latest_ts_iso": to_iso(float(latest["ts"])) if latest else None,
        }
    )
    return row
