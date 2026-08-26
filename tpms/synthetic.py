"""Generate synthetic rtl_433 TPMS JSON for development without a dongle.

Models the behaviour that matters for testing correlation: a vehicle's wheels
transmit in a tight burst while it is in range, bursts repeat every ~40s during
a pass, and passes are separated by long silences.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class SimVehicle:
    name: str
    model: str
    sensor_ids: list[str]
    base_pressure_kpa: float = 240.0
    #: Probability a given wheel is actually decoded in a given burst. Real
    #: capture is lossy -- a wheel shadowed by the body often drops out.
    decode_rate: float = 0.8


@dataclass
class Scenario:
    vehicles: list[SimVehicle] = field(default_factory=list)
    start: float = 1_700_000_000.0
    burst_interval: float = 40.0
    seed: int = 7

    def pass_events(
        self, vehicle: SimVehicle, at: float, bursts: int, rng: random.Random
    ) -> Iterator[dict]:
        """One drive-by: `bursts` rounds of near-simultaneous wheel packets."""
        for burst in range(bursts):
            burst_ts = at + burst * self.burst_interval
            for index, sensor_id in enumerate(vehicle.sensor_ids):
                if rng.random() > vehicle.decode_rate:
                    continue
                # Wheels within a burst land within a couple of seconds.
                ts = burst_ts + rng.uniform(0, 2.5)
                yield {
                    "time": _fmt(ts),
                    "model": vehicle.model,
                    "type": "TPMS",
                    "id": sensor_id,
                    "battery_ok": 1,
                    "pressure_kPa": round(
                        vehicle.base_pressure_kpa + index * 3 + rng.uniform(-2, 2), 1
                    ),
                    "temperature_C": round(20 + rng.uniform(-3, 8), 1),
                    "mic": "CRC",
                    "freq": 315.0 + rng.uniform(-0.02, 0.02),
                    "rssi": round(rng.uniform(-18, -6), 1),
                    "snr": round(rng.uniform(8, 22), 1),
                }


def default_scenario() -> Scenario:
    return Scenario(
        vehicles=[
            SimVehicle("Blue wagon", "Toyota-TPMS", ["1a2b01", "1a2b02", "1a2b03", "1a2b04"]),
            SimVehicle("Grey van", "Ford-TPMS", ["ff0a01", "ff0a02", "ff0a03", "ff0a04"], 280.0),
            # A single-sensor decoy that occasionally passes alongside the
            # wagon. It must NOT get absorbed into the wagon's cluster.
            SimVehicle("Decoy scooter", "Schrader-TPMS", ["dec0y1"], 200.0),
        ]
    )


def generate(scenario: Scenario | None = None, passes: int = 12) -> list[dict]:
    """Emit a chronologically sorted list of rtl_433 JSON objects."""
    scenario = scenario or default_scenario()
    rng = random.Random(scenario.seed)
    events: list[dict] = []

    wagon, van, decoy = scenario.vehicles[0], scenario.vehicles[1], scenario.vehicles[2]
    clock = scenario.start

    for i in range(passes):
        clock += rng.uniform(1800, 5400)  # long silence between passes
        events.extend(scenario.pass_events(wagon, clock, rng.randint(2, 5), rng))

        clock += rng.uniform(1800, 5400)
        events.extend(scenario.pass_events(van, clock, rng.randint(2, 5), rng))

        # The decoy shares the road with the wagon only twice out of `passes`.
        if i in (2, 6):
            events.extend(scenario.pass_events(wagon, clock + 900, 3, rng))
            events.extend(scenario.pass_events(decoy, clock + 900, 3, rng))

    events.sort(key=lambda e: e["time"])
    return events


def generate_lines(scenario: Scenario | None = None, passes: int = 12) -> list[str]:
    return [json.dumps(e, separators=(",", ":")) for e in generate(scenario, passes)]
