"""Sighting sessions: when a sensor appeared and when it was last heard.

A *sighting* is a bounded interval during which a sensor was audible. It is
deliberately not called "presence": TPMS sensors sleep when the wheel stops
rolling, so a sighting ending tells you the sensor went quiet, which usually
but not always means the vehicle drove out of range.
"""

from __future__ import annotations

from dataclasses import dataclass

from .db import Database
from .models import Sighting


@dataclass
class SessionEvent:
    """What happened to a sensor's sighting when a reading arrived."""

    sighting: Sighting
    opened: bool
    #: Set when this reading closed a previous sighting.
    closed: Sighting | None = None


class SessionTracker:
    def __init__(self, db: Database, gap_seconds: float = 120):
        self.db = db
        self.gap_seconds = gap_seconds

    def record(
        self,
        sensor_pk: int,
        ts: float,
        rssi: float | None,
        freq_mhz: float | None = None,
    ) -> SessionEvent:
        """Extend the sensor's open sighting, or start a new one."""
        current = self.db.open_sighting_for(sensor_pk)

        if current is not None and ts - current.last_reading_at <= self.gap_seconds:
            self.db.extend_sighting(current.pk, ts, rssi, freq_mhz)
            current.last_reading_at = max(current.last_reading_at, ts)
            current.reading_count += 1
            if freq_mhz is not None:
                current.freq_mhz = freq_mhz
            if rssi is not None:
                current.max_rssi = (
                    rssi if current.max_rssi is None else max(current.max_rssi, rssi)
                )
            return SessionEvent(sighting=current, opened=False)

        closed = None
        if current is not None:
            self.db.close_sighting(current.pk)
            current.ended_at = current.last_reading_at
            closed = current

        return SessionEvent(
            sighting=self.db.create_sighting(sensor_pk, ts, rssi, freq_mhz),
            opened=True,
            closed=closed,
        )

    def sweep(self, now_ts: float) -> int:
        """Close sightings that have gone quiet past the gap.

        Without this the UI would show phantom "heard now" rows forever after
        a vehicle leaves, because nothing else triggers a close.
        """
        return self.db.close_stale_sightings(now_ts - self.gap_seconds)
