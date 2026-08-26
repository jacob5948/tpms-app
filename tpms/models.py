"""Normalized data shapes and time helpers.

Timestamps are stored throughout as float epoch seconds in UTC. rtl_433 is
always launched with ``-M utc`` so the ``time`` field it emits is unambiguous;
anything else would be local time with no offset attached.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# rtl_433 emits "2026-08-25 15:34:12" or, with -M time:usec, a fractional part.
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")


def parse_rtl_time(value: Any) -> float | None:
    """Parse an rtl_433 UTC timestamp into epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    # Tolerate ISO-8601 with an explicit offset or trailing Z.
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = None
    if dt is None:
        for fmt in _TIME_FORMATS:
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def to_iso(ts: float | None) -> str | None:
    """Format epoch seconds as an ISO-8601 UTC string."""
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def now() -> float:
    return datetime.now(tz=timezone.utc).timestamp()


@dataclass(frozen=True)
class Reading:
    """One normalized TPMS transmission.

    ``model`` + ``sensor_id`` together form sensor identity: raw IDs collide
    across protocols, so neither is unique on its own.
    """

    model: str
    sensor_id: str
    ts: float
    pressure_kpa: float | None = None
    temperature_c: float | None = None
    battery_ok: int | None = None
    freq_mhz: float | None = None
    rssi: float | None = None
    snr: float | None = None
    raw: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.model, self.sensor_id)

    @property
    def pressure_psi(self) -> float | None:
        if self.pressure_kpa is None:
            return None
        return self.pressure_kpa / 6.894757


@dataclass
class Sensor:
    pk: int
    model: str
    sensor_id: str
    first_seen: float
    last_seen: float
    reading_count: int
    vehicle_id: int | None
    wheel_label: str | None
    pinned: bool

    @property
    def key(self) -> tuple[str, str]:
        return (self.model, self.sensor_id)

    @property
    def display(self) -> str:
        return f"{self.model}/{self.sensor_id}"


@dataclass
class Sighting:
    pk: int
    sensor_pk: int
    started_at: float
    last_reading_at: float
    ended_at: float | None
    reading_count: int
    max_rssi: float | None

    @property
    def open(self) -> bool:
        return self.ended_at is None

    @property
    def duration(self) -> float:
        return self.last_reading_at - self.started_at


@dataclass
class Vehicle:
    pk: int
    name: str | None
    notes: str | None
    created_at: float
    auto_generated: bool
    needs_review: bool = False

    @property
    def display(self) -> str:
        return self.name or f"Unnamed vehicle #{self.pk}"
