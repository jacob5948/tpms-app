"""Collapse duplicate decodes of the same physical transmitter.

Several rtl_433 TPMS decoders will happily match the same RF burst with
different framing, so one sensor shows up two or three times under different
protocols and IDs -- Jansite/6cd2eb3 and Ford/6cd2eb33, Jansite/c20f14d and
Citroen/0f14dbd2, and so on. Left alone these phantoms inflate the sensor
list and, because they co-occur perfectly by construction, cluster into
vehicles that do not exist.

The giveaway is the signal level. rtl_433 reports RSSI and SNR per received
burst, so two decoders parsing the *same* burst report byte-identical values
at the same instant. Two genuinely different transmitters -- even two wheels
on one car -- essentially never do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .cluster import UnionFind
from .config import AliasConfig
from .db import Database

log = logging.getLogger(__name__)


@dataclass
class AliasPair:
    a: int
    b: int
    shared: int
    ratio: float
    common_id: str = ""
    rssi_delta: float | None = None
    time_delta: float | None = None


@dataclass
class AliasReport:
    pairs: list[AliasPair] = field(default_factory=list)
    groups: list[list[int]] = field(default_factory=list)
    linked: int = 0
    unlinked: int = 0

    def summary(self) -> str:
        return (
            f"{len(self.groups)} duplicate group(s) covering "
            f"{sum(len(g) for g in self.groups)} sensor(s); "
            f"{self.linked} newly linked, {self.unlinked} unlinked"
        )


def common_hex_run(a: str, b: str, minimum: int = 4) -> str:
    """Longest shared substring of two sensor IDs.

    Only used to explain a match in the UI -- decoders that read the frame at
    different bit offsets often share a recognisable run of hex, but some
    reinterpret the bits entirely, so this is never required for a match.
    """
    a, b = a.lower(), b.lower()
    best = ""
    for start in range(len(a)):
        for end in range(len(a), start + len(best), -1):
            chunk = a[start:end]
            if len(chunk) > len(best) and chunk in b:
                best = chunk
                break
    return best if len(best) >= minimum else ""


class AliasDetector:
    def __init__(self, db: Database, config: AliasConfig | None = None):
        self.db = db
        self.config = config or AliasConfig()

    def find_pairs(self) -> list[AliasPair]:
        """Sensor pairs that keep decoding the very same bursts."""
        cfg = self.config
        decoder_clause = "AND sa.model != sb.model" if cfg.require_different_decoder else ""
        rows = self.db.query(
            f"""
            SELECT a.sensor_pk AS a, b.sensor_pk AS b, COUNT(*) AS shared,
                   MIN(ABS(a.rssi - b.rssi)) AS rssi_delta,
                   MIN(ABS(a.ts - b.ts))     AS time_delta
              FROM readings a
              JOIN readings b
                ON b.sensor_pk > a.sensor_pk
               AND b.ts BETWEEN a.ts - ? AND a.ts + ?
               AND a.rssi IS NOT NULL AND b.rssi IS NOT NULL
               AND ABS(a.rssi - b.rssi) <= ?
               AND (a.snr IS NULL OR b.snr IS NULL OR ABS(a.snr - b.snr) <= ?)
              JOIN sensors sa ON sa.pk = a.sensor_pk
              JOIN sensors sb ON sb.pk = b.sensor_pk
             WHERE 1=1 {decoder_clause}
             GROUP BY a.sensor_pk, b.sensor_pk
            """,
            (cfg.time_tolerance, cfg.time_tolerance, cfg.rssi_tolerance, cfg.snr_tolerance),
        )
        if not rows:
            return []

        counts = {
            int(r["pk"]): int(r["reading_count"])
            for r in self.db.query("SELECT pk, reading_count FROM sensors")
        }
        sensors = {s.pk: s for s in self.db.list_sensors()}

        pairs: list[AliasPair] = []
        for row in rows:
            a, b, shared = int(row["a"]), int(row["b"]), int(row["shared"])
            if shared < self.config.min_shared_bursts:
                continue
            denominator = min(counts.get(a, 0), counts.get(b, 0))
            ratio = shared / denominator if denominator else 0.0
            if ratio < self.config.min_share_ratio:
                continue
            pairs.append(
                AliasPair(
                    a=a,
                    b=b,
                    shared=shared,
                    ratio=ratio,
                    common_id=common_hex_run(
                        sensors[a].sensor_id, sensors[b].sensor_id
                    )
                    if a in sensors and b in sensors
                    else "",
                    rssi_delta=row["rssi_delta"],
                    time_delta=row["time_delta"],
                )
            )
        return pairs

    def run(self, dry_run: bool = False) -> AliasReport:
        report = AliasReport(pairs=self.find_pairs())

        union = UnionFind()
        for pair in report.pairs:
            union.union(pair.a, pair.b)
        report.groups = [sorted(g) for g in union.groups().values() if len(g) > 1]
        report.groups.sort(key=lambda g: g[0])

        if dry_run:
            return report

        sensors = {s.pk: s for s in self.db.list_sensors()}
        aliased: set[int] = set()

        for group in report.groups:
            # The decoder that heard the most is the one to keep: it usually
            # has the fullest ID and the best history.
            canonical = max(group, key=lambda pk: (sensors[pk].reading_count, -pk))
            for pk in group:
                aliased.add(pk)
                target = None if pk == canonical else canonical
                if sensors[pk].alias_of != target:
                    self.db.execute(
                        "UPDATE sensors SET alias_of = ? WHERE pk = ?", (target, pk)
                    )
                    report.linked += 1
                # An alias must not also hold a vehicle assignment of its own.
                if target is not None and sensors[pk].vehicle_id is not None:
                    self.db.set_sensor_vehicle(pk, None)

        # Sensors that no longer look like duplicates get released.
        for pk, sensor in sensors.items():
            if sensor.alias_of is not None and pk not in aliased:
                self.db.execute("UPDATE sensors SET alias_of = NULL WHERE pk = ?", (pk,))
                report.unlinked += 1

        return report
