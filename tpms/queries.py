"""Read models for the web UI.

Kept separate from db.py so the storage layer stays about persistence and the
shapes the templates want live in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import direction
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


def merge_runs(
    items: list[Any],
    join_gap: float,
    bounds: Any = lambda item: (item[0], item[1]),
) -> list[dict[str, Any]]:
    """Group sightings into runs separated by more than ``join_gap``.

    The single definition of what makes several sightings one appearance. A
    vehicle is 'heard' from its first wheel to its last, so overlapping (or
    near-touching) wheel sightings collapse together. Both the interval
    summaries and the pass log are built on this, so the two can never drift
    apart on where one pass ends and the next begins.

    ``bounds`` pulls ``(started_at, ended_at)`` out of whatever the caller is
    grouping; ``ended_at`` of None means still open.
    """
    runs: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda i: bounds(i)[0]):
        started, ended = bounds(item)
        if runs:
            current_end = runs[-1]["ended_at"]
            # An open sighting swallows anything that starts after it.
            if current_end is None or started <= current_end + join_gap:
                runs[-1]["items"].append(item)
                if ended is None or current_end is None:
                    runs[-1]["ended_at"] = None
                else:
                    runs[-1]["ended_at"] = max(current_end, ended)
                continue
        runs.append({"started_at": started, "ended_at": ended, "items": [item]})
    return runs


def merge_intervals(
    rows: list[tuple[float, float | None, int]], join_gap: float
) -> list[Interval]:
    """Roll per-sensor sightings up into per-vehicle intervals, newest first."""
    merged = [
        Interval(
            run["started_at"],
            run["ended_at"],
            len(run["items"]),
            sum(item[2] for item in run["items"]),
        )
        for run in merge_runs(rows, join_gap)
    ]
    merged.reverse()  # newest first
    return merged


def vehicle_intervals(db: Database, vehicle_id: int, join_gap: float, limit: int = 100):
    """When a vehicle was audible, one row per pass, newest first.

    Times and counts only. What each pass *was* -- which wheels, how loudly,
    which way it was pointing -- is `vehicle_passes`, and the pages that show
    a pass read it from there rather than growing a second answer here.
    """
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
    # Ending the axis at the last appearance makes "not seen for three days"
    # look identical to "seen a minute ago" -- the gap since is the reading,
    # so the window runs to now unless the caller asked for a fixed end.
    hi = end if end is not None else (now if intervals else None)
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
                "passes": counts[i],
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
          LEFT JOIN sensors n ON n.vehicle_id = v.pk AND n.ignored = 0
         GROUP BY v.pk
         ORDER BY last_seen DESC NULLS LAST
        """
    )
    # Computed once for the page, not once per sensor of every vehicle. Each
    # of these is a scan of the whole sensors table, and sensor_row falls back
    # to recomputing them when they are not supplied.
    residents = resident_pks(db)
    duty_cycles = db.duty_cycles()
    bands_by_sensor = db.all_band_counts()

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
                "passes": len(intervals),
                "present": bool(intervals and intervals[0].open),
                "sensors": [
                    sensor_row(db, s.pk, residents, duty_cycles, bands_by_sensor)
                    for s in db.sensors_for_vehicle(int(row["pk"]))
                    if not s.ignored
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


# "New" answers "did anything turn up while I was out", not "is this sensor
# young" -- so it is scoped to the last day rather than to the install date.
NEW_WITHIN = 86400.0


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
        "ignored": sensor.ignored,
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
        "new": (now_ts() - sensor.first_seen) < NEW_WITHIN,
    }


def sensor_rows(
    db: Database,
    include_aliases: bool = False,
    include_ignored: bool = False,
) -> list[dict[str, Any]]:
    """Sensor table rows.

    Duplicate decodes are folded into their canonical sensor by default --
    listing them as peers would triple the table and imply vehicles that do
    not exist. Sensors hidden by hand are folded out the same way, for the
    same reason: they are known, and knowing about them is the point of
    hiding them.
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
        if sensor.ignored and not include_ignored:
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


def heard_now(db: Database, include_ignored: bool = False) -> list[dict[str, Any]]:
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
        if sensor.ignored and not include_ignored:
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


def heard_now_groups(db: Database) -> dict[str, Any]:
    """What is audible, gathered the way the log gathers it.

    One entry per vehicle rather than one per wheel, with unassigned sensors
    listed separately. A car going past is one thing happening, and the flat
    list made four wheels of the same car look like four events -- which is
    exactly the confusion this page exists to resolve when you are matching a
    car you can see to what the receiver is hearing.
    """
    rows = heard_now(db)
    vehicles: dict[int, dict[str, Any]] = {}
    loose: list[dict[str, Any]] = []
    for row in rows:
        if row["vehicle_id"] is None:
            loose.append(row)
            continue
        group = vehicles.setdefault(
            int(row["vehicle_id"]),
            {
                "vehicle_id": int(row["vehicle_id"]),
                "vehicle_name": row["vehicle_name"]
                or f"Unnamed vehicle #{row['vehicle_id']}",
                "named": bool(row["vehicle_name"]),
                "sensors": [],
            },
        )
        group["sensors"].append(row)

    known = {
        int(r["vehicle_id"]): int(r["n"])
        for r in db.query(
            """
            SELECT vehicle_id, COUNT(*) AS n FROM sensors
             WHERE vehicle_id IS NOT NULL AND alias_of IS NULL AND ignored = 0
             GROUP BY vehicle_id
            """
        )
    }
    out = []
    for group in vehicles.values():
        group["wheels_known"] = known.get(group["vehicle_id"])
        group["started_at"] = min(s["started_at"] for s in group["sensors"])
        group["last_reading_at"] = max(s["last_reading_at"] for s in group["sensors"])
        out.append(group)
    out.sort(key=lambda g: g["last_reading_at"], reverse=True)
    loose.sort(key=lambda r: r["last_reading_at"], reverse=True)
    return {"vehicles": out, "loose": loose, "count": len(rows)}


def _sighting_filters(
    start: float | None,
    end: float | None,
    vehicle_id: int | None,
    sensor_pk: int | None,
    include_ignored: bool,
) -> tuple[str, list[Any]]:
    """The WHERE shared by the sighting log, the pass log and their counts.

    One definition, so the count under the table can never disagree with the
    table, and the CSV can never disagree with either.
    """
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
    if sensor_pk is not None:
        # Follow the alias link, so filtering to a sensor from its own page
        # includes the bursts its duplicate decoders logged.
        clauses.append("COALESCE(n.alias_of, n.pk) = ?")
        params.append(sensor_pk)
    if not include_ignored:
        clauses.append("n.ignored = 0")
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params


def count_events(
    db: Database,
    start: float | None = None,
    end: float | None = None,
    vehicle_id: int | None = None,
    sensor_pk: int | None = None,
    include_ignored: bool = False,
) -> int:
    """How many sightings match, so a truncated page can say so."""
    where, params = _sighting_filters(start, end, vehicle_id, sensor_pk, include_ignored)
    row = db.query_one(
        f"""
        SELECT COUNT(*) AS n
          FROM sightings s
          JOIN sensors n ON n.pk = s.sensor_pk
          {where}
        """,
        tuple(params),
    )
    return int(row["n"]) if row else 0


def events(
    db: Database,
    start: float | None = None,
    end: float | None = None,
    vehicle_id: int | None = None,
    limit: int = 500,
    sensor_pk: int | None = None,
    include_ignored: bool = False,
) -> list[dict[str, Any]]:
    """Flat appear / last-heard log, one row per sensor sighting.

    The raw view: what the receiver actually decoded, unmerged. Kept as a peer
    of the pass log rather than an internal detail, because matching a car you
    watched go past to the transmitters heard at that moment is done against
    these rows, not against the merged summary.
    """
    where, params = _sighting_filters(start, end, vehicle_id, sensor_pk, include_ignored)
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


def vehicle_passes(
    db: Database,
    join_gap: float,
    start: float | None = None,
    end: float | None = None,
    vehicle_id: int | None = None,
    sensor_pk: int | None = None,
    limit: int = 500,
    scan_limit: int = 20000,
    include_ignored: bool = False,
    rssi_margin: float = 6.0,
) -> list[dict[str, Any]]:
    """The traffic log: one row per vehicle going past, newest first.

    ``events`` answers "what did the receiver decode"; this answers "what drove
    by", which is the question the program exists for. Four wheels rolling
    past are one pass here and four rows there, and each pass carries the
    sightings it was built from so the raw evidence is one click away.

    A sensor with no vehicle still gets a pass of its own -- an unclustered
    wheel is a thing that drove past, and dropping it would quietly under-count
    the traffic.
    """
    where, params = _sighting_filters(
        start, end, vehicle_id, sensor_pk, include_ignored
    )
    params.append(scan_limit)
    rows = db.query(
        f"""
        SELECT s.pk, s.started_at, s.last_reading_at, s.ended_at, s.reading_count,
               s.max_rssi, s.freq_mhz, n.pk AS sensor_pk, n.model, n.sensor_id,
               n.wheel_label, n.vehicle_id, v.name AS vehicle_name
          FROM sightings s
          JOIN sensors n ON n.pk = s.sensor_pk
          LEFT JOIN vehicles v ON v.pk = n.vehicle_id
          {where}
         ORDER BY s.started_at DESC
         LIMIT ?
        """,
        params,
    )

    # How many wheels each vehicle is known to have, so a pass can say "3 of 4"
    # -- a vehicle that usually shows four and showed one is worth noticing.
    known: dict[int, int] = {
        int(r["vehicle_id"]): int(r["n"])
        for r in db.query(
            """
            SELECT vehicle_id, COUNT(*) AS n FROM sensors
             WHERE vehicle_id IS NOT NULL AND alias_of IS NULL AND ignored = 0
             GROUP BY vehicle_id
            """
        )
    }

    # Group before merging: sightings only belong to the same pass if they
    # belong to the same vehicle. Unassigned sensors group by themselves.
    groups: dict[tuple[str, int], list[Any]] = {}
    for row in rows:
        key = (
            ("v", int(row["vehicle_id"]))
            if row["vehicle_id"] is not None
            else ("s", int(row["sensor_pk"]))
        )
        groups.setdefault(key, []).append(row)

    out: list[dict[str, Any]] = []
    for (kind, ident), items in groups.items():
        for run in merge_runs(
            items,
            join_gap,
            bounds=lambda r: (float(r["started_at"]), r["ended_at"]),
        ):
            members = sorted(run["items"], key=lambda r: float(r["started_at"]))
            newest = max(members, key=lambda r: float(r["last_reading_at"]))
            rssis = [r["max_rssi"] for r in members if r["max_rssi"] is not None]
            wheels = {int(r["sensor_pk"]) for r in members}
            started = float(run["started_at"])
            ended = run["ended_at"]
            last_reading = max(float(r["last_reading_at"]) for r in members)
            out.append(
                {
                    "vehicle_id": ident if kind == "v" else None,
                    "vehicle_name": (
                        (newest["vehicle_name"] or f"Unnamed vehicle #{ident}")
                        if kind == "v"
                        else None
                    ),
                    "sensor_pk": ident if kind == "s" else None,
                    "display": (
                        f"{newest['model']}/{newest['sensor_id']}"
                        if kind == "s"
                        else None
                    ),
                    "started_at": started,
                    "started_at_iso": to_iso(started),
                    "ended_at": ended,
                    "last_reading_at": last_reading,
                    "last_reading_at_iso": to_iso(last_reading),
                    "open": ended is None,
                    # Ends at last heard, never at an inferred departure.
                    "duration": last_reading - started,
                    "reading_count": sum(int(r["reading_count"]) for r in members),
                    "wheels_heard": len(wheels),
                    "wheels_known": known.get(ident) if kind == "v" else None,
                    # Which way it was pointing, from the wheels heard. Read
                    # from the labels as they are now rather than stored: a
                    # pass's direction changes the moment someone corrects a
                    # wheel, and a cached one would be quietly wrong.
                    "heading": direction.infer(
                        [(r["wheel_label"], r["max_rssi"]) for r in members],
                        rssi_margin,
                    ),
                    "max_rssi": max(rssis) if rssis else None,
                    "band": band_label(newest["freq_mhz"]),
                    "sightings": [
                        {
                            "pk": int(r["pk"]),
                            "sensor_pk": int(r["sensor_pk"]),
                            "display": f"{r['model']}/{r['sensor_id']}",
                            "wheel_label": r["wheel_label"],
                            "started_at": float(r["started_at"]),
                            "started_at_iso": to_iso(float(r["started_at"])),
                            "last_reading_at": float(r["last_reading_at"]),
                            "last_reading_at_iso": to_iso(float(r["last_reading_at"])),
                            "open": r["ended_at"] is None,
                            "duration": float(r["last_reading_at"])
                            - float(r["started_at"]),
                            "reading_count": int(r["reading_count"]),
                            "max_rssi": r["max_rssi"],
                            "band": band_label(r["freq_mhz"]),
                        }
                        for r in members
                    ],
                }
            )

    out.sort(key=lambda p: p["started_at"], reverse=True)
    return out[:limit]


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
    # rssi rides along with the pressure rows rather than being read
    # separately: the three facets on the sensor page are one figure over one
    # window, and pulling signal from a different row set would let the
    # cursor land on a moment the other two never saw.
    sql = (
        "SELECT ts, pressure_kpa, temperature_c, rssi FROM readings "
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
            "rssi": r["rssi"],
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
            "WHERE s.ignored = 0 AND r.ts >= ? AND r.ts < ? GROUP BY b",
            (start, width, start, end),
        )
    }
    passes = {
        int(r["b"]): int(r["passes"])
        for r in db.query(
            "SELECT CAST((g.started_at - ?) / ? AS INTEGER) AS b, COUNT(*) AS passes "
            "FROM sightings g JOIN sensors s ON s.pk = g.sensor_pk "
            "WHERE s.alias_of IS NULL AND s.ignored = 0 "
            "AND g.started_at >= ? AND g.started_at < ? "
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
