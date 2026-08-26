"""Normalize rtl_433 JSON into readings and drive the storage pipeline."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Iterable

from .config import ClusterConfig, SessionConfig
from .db import Database
from .models import Reading, parse_rtl_time, now as now_ts
from .sessions import SessionEvent, SessionTracker

log = logging.getLogger(__name__)

KPA_PER_PSI = 6.894757
KPA_PER_BAR = 100.0

#: Pressure keys seen across rtl_433 TPMS decoders, with their kPa factor.
_PRESSURE_KEYS: tuple[tuple[str, float], ...] = (
    ("pressure_kPa", 1.0),
    ("pressure_PSI", KPA_PER_PSI),
    ("pressure_bar", KPA_PER_BAR),
    ("pressure_hPa", 0.1),
)


def _first_number(obj: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _pressure_kpa(obj: dict[str, Any]) -> float | None:
    for key, factor in _PRESSURE_KEYS:
        value = _first_number(obj, key)
        if value is not None:
            return value * factor
    return None


def _temperature_c(obj: dict[str, Any]) -> float | None:
    celsius = _first_number(obj, "temperature_C")
    if celsius is not None:
        return celsius
    fahrenheit = _first_number(obj, "temperature_F")
    if fahrenheit is not None:
        return (fahrenheit - 32.0) * 5.0 / 9.0
    return None


def _battery_ok(obj: dict[str, Any]) -> int | None:
    value = obj.get("battery_ok")
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    # Older decoders used an inverted "battery_low" flag.
    low = obj.get("battery_low")
    if isinstance(low, (bool, int, float)):
        return int(not int(low))
    return None


def is_tpms(obj: dict[str, Any]) -> bool:
    """True for TPMS transmissions.

    Nearly every TPMS decoder sets ``type: TPMS``; the model-name check catches
    the handful that omit it.
    """
    if str(obj.get("type", "")).upper() == "TPMS":
        return True
    return "TPMS" in str(obj.get("model", "")).upper()


def normalize(obj: dict[str, Any], raw: str | None = None) -> Reading | None:
    """Turn one rtl_433 JSON object into a Reading, or None if not usable."""
    if not isinstance(obj, dict) or not is_tpms(obj):
        return None

    model = obj.get("model")
    sensor_id = obj.get("id")
    if model is None or sensor_id is None:
        return None

    ts = parse_rtl_time(obj.get("time"))
    if ts is None:
        ts = now_ts()

    # Identity must be stable: some decoders emit the id as an int, some as a
    # hex string. Normalize ints to lowercase hex-free decimal text.
    sensor_id = f"{sensor_id:d}" if isinstance(sensor_id, int) else str(sensor_id).strip()

    return Reading(
        model=str(model),
        sensor_id=sensor_id,
        ts=ts,
        pressure_kpa=_pressure_kpa(obj),
        temperature_c=_temperature_c(obj),
        battery_ok=_battery_ok(obj),
        freq_mhz=_first_number(obj, "freq", "freq1", "freq2"),
        rssi=_first_number(obj, "rssi"),
        snr=_first_number(obj, "snr"),
        raw=raw if raw is not None else json.dumps(obj, separators=(",", ":")),
    )


class Ingestor:
    """Single entry point from raw rtl_433 line to persisted state.

    Both live capture and ``tpms replay`` go through here, so replayed data
    exercises exactly the same code path as the radio.
    """

    def __init__(
        self,
        db: Database,
        sessions: SessionConfig | None = None,
        clustering: ClusterConfig | None = None,
    ):
        self.db = db
        self.session_config = sessions or SessionConfig()
        self.cluster_config = clustering or ClusterConfig()
        self.tracker = SessionTracker(db, self.session_config.gap_seconds)
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._lock = threading.Lock()
        self.stats: dict[str, int] = {
            "lines": 0,
            "readings": 0,
            "skipped": 0,
            "malformed": 0,
        }
        self.decoder_counts: dict[str, int] = {}

    # -- subscribers (used by the web UI's SSE feed) ----------------------

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def _publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:  # a broken listener must not stall ingest
                log.exception("subscriber failed")

    # -- ingest -----------------------------------------------------------

    def handle_line(self, line: str) -> Reading | None:
        self.stats["lines"] += 1
        line = line.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            # rtl_433 interleaves human-readable notices on stdout at startup.
            self.stats["malformed"] += 1
            return None
        return self.handle_object(obj, raw=line)

    def handle_object(self, obj: dict[str, Any], raw: str | None = None) -> Reading | None:
        reading = normalize(obj, raw=raw)
        if reading is None:
            self.stats["skipped"] += 1
            return None
        return self.ingest(reading)

    def ingest(self, reading: Reading) -> Reading:
        self.stats["readings"] += 1
        self.decoder_counts[reading.model] = self.decoder_counts.get(reading.model, 0) + 1

        sensor_pk = self.db.upsert_sensor(reading.model, reading.sensor_id, reading.ts)
        self.db.insert_reading(sensor_pk, reading)
        event = self.tracker.record(sensor_pk, reading.ts, reading.rssi)
        self._record_cooccurrence(sensor_pk, event, reading.ts)

        sensor = self.db.get_sensor(sensor_pk)
        self._publish(
            {
                "type": "reading",
                "sensor_pk": sensor_pk,
                "model": reading.model,
                "sensor_id": reading.sensor_id,
                "ts": reading.ts,
                "pressure_kpa": reading.pressure_kpa,
                "pressure_psi": reading.pressure_psi,
                "temperature_c": reading.temperature_c,
                "battery_ok": reading.battery_ok,
                "rssi": reading.rssi,
                "freq_mhz": reading.freq_mhz,
                "vehicle_id": sensor.vehicle_id if sensor else None,
                "sighting_pk": event.sighting.pk,
                "opened": event.opened,
            }
        )
        return reading

    def _record_cooccurrence(
        self, sensor_pk: int, event: SessionEvent, ts: float
    ) -> None:
        """Note which other sensors were audible at the same moment.

        Each pair is counted at most once per shared sighting (enforced in the
        DB), so a car idling in range for ten minutes contributes a single
        vote, not sixty.
        """
        window = self.cluster_config.window_seconds
        others = self.db.recent_sensor_pks(ts - window, ts + window, sensor_pk)
        for other_pk in others:
            other_sighting = self.db.sighting_covering(other_pk, ts)
            if other_sighting is None:
                continue
            self.db.note_cooccurrence(
                event.sighting.pk, other_sighting, sensor_pk, other_pk, ts
            )

    def sweep(self, when: float | None = None) -> int:
        closed = self.tracker.sweep(when if when is not None else now_ts())
        if closed:
            self._publish({"type": "sweep", "closed": closed})
        return closed

    def replay(self, lines: Iterable[str]) -> int:
        """Feed an iterable of raw JSON lines through the pipeline."""
        count = 0
        for line in lines:
            if self.handle_line(line) is not None:
                count += 1
        return count
