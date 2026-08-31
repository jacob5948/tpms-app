"""Normalized data shapes and time helpers.

Timestamps are stored throughout as float epoch seconds in UTC. rtl_433 is
always launched with ``-M utc`` so the ``time`` field it emits is unambiguous;
anything else would be local time with no offset attached.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Bands we tune to. rtl_433 reports the tuner's centre frequency plus the
# decoder's frequency estimate, so a 315 MHz sensor comes back as 314.98 or
# 315.03 -- snapping to a named band keeps the UI readable and makes
# "which band was this heard on" answerable with an equality test.
KNOWN_BANDS = (315.0, 433.92)
BAND_TOLERANCE = 1.0


def band_of(freq_mhz: float | None) -> float | None:
    """Snap a measured frequency to the band it belongs to."""
    if freq_mhz is None:
        return None
    for band in KNOWN_BANDS:
        if abs(freq_mhz - band) <= BAND_TOLERANCE:
            return band
    return round(float(freq_mhz), 2)


def band_label(freq_mhz: float | None) -> str | None:
    """Human label for the band a reading arrived on."""
    band = band_of(freq_mhz)
    if band is None:
        return None
    text = f"{band:.2f}".rstrip("0").rstrip(".")
    return f"{text} MHz"


#: The zone every stamp on screen is written in. Readings are stored as epoch
#: seconds -- this is display only, and `load_config` sets it from the config
#: file. A module global because to_iso is called from templates, queries and
#: the CLI alike, none of which carry a config.
_display_tz: tzinfo = timezone.utc


def set_display_timezone(name: str) -> tzinfo:
    """Point every stamp at an IANA zone. Returns the zone it settled on."""
    global _display_tz
    try:
        _display_tz = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone: {name!r}") from exc
    return _display_tz


def display_timezone() -> tzinfo:
    return _display_tz


def to_datetime_local(ts: float) -> str:
    """Format epoch seconds as YYYY-MM-DDTHH:MM for datetime-local inputs."""
    return datetime.fromtimestamp(ts, tz=_display_tz).strftime("%Y-%m-%dT%H:%M")


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


def parse_when(value: str | None) -> float | None:
    """Accept an ISO date/time, or a relative age like '24h' / '7d'.

    Raises ValueError on anything else. The CLI turns that into a clean exit;
    the web layer turns it into a 400 next to the offending filter box. It
    used to raise SystemExit from here, which meant a typo in a date field on
    the log page came back as a 500.
    """
    if not value:
        return None
    text = value.strip()
    if text and text[-1] in "smhd" and text[:-1].replace(".", "", 1).isdigit():
        factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}[text[-1]]
        return now() - float(text[:-1]) * factor
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        # A date typed into a filter box means that date where the receiver
        # is, matching the stamps the same page prints back.
        dt = dt.replace(tzinfo=_display_tz)
    return dt.timestamp()


def to_iso(ts: float | None) -> str | None:
    """Format epoch seconds as ISO-8601 in the configured zone.

    With the offset spelled out: a stamp that says 14:03 and does not say
    where cannot be compared with anything.
    """
    if ts is None:
        return None
    stamp = datetime.fromtimestamp(ts, tz=_display_tz).isoformat(timespec="seconds")
    return stamp.replace("+00:00", "Z")


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
    #: Set when this is the same transmitter as another sensor, decoded by a
    #: different rtl_433 protocol.
    alias_of: int | None = None
    #: Hidden from the lists by hand. Still recorded, never clustered.
    ignored: bool = False

    @property
    def is_alias(self) -> bool:
        return self.alias_of is not None

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
    #: Band the sensor was heard on during this sighting (last reading wins).
    freq_mhz: float | None = None

    @property
    def band(self) -> str | None:
        return band_label(self.freq_mhz)

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
    #: "oversized" or "mixed_families"; None when nothing is wrong.
    review_reason: str | None = None
    #: Grouped from one pass only, not yet corroborated by a return visit.
    provisional: bool = False

    @property
    def display(self) -> str:
        return self.name or f"Unnamed vehicle #{self.pk}"
