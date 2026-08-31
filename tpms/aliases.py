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


#: Same decoder, same burst, and ids that differ only in their leading
#: character. From a real capture: Toyota/d157cd57 and Toyota/f157cd57,
#: Toyota/d157ca8a and Toyota/f157ca8a, Toyota/d18d99b0 and Toyota/f18d99b0.
#:
#: `require_different_decoder` exists because two wheels on one car share an
#: OEM sensor type, so a same-decoder pair is normally a real pair of sensors.
#: These are the exception it did not foresee -- one decoder reading one burst
#: two ways, differing in a top nibble that is evidently not part of the id --
#: and because nothing could ever fold them, they co-occurred perfectly for
#: ever and confirmed their way into whatever vehicle stood beside them.
#:
#: Only the *leading* character may differ. Wheels programmed as a set differ
#: in their low digits (d39abf13 / d39abf5a), so a rule that allowed a trailing
#: difference would fold a whole car into one sensor.
_SAME_TRANSMITTER_ID = (
    "LENGTH(sa.sensor_id) = LENGTH(sb.sensor_id)"
    " AND LENGTH(sa.sensor_id) > 4"
    " AND SUBSTR(LOWER(sa.sensor_id), 2) = SUBSTR(LOWER(sb.sensor_id), 2)"
)


def one_leading_digit_apart(a: str, b: str, minimum: int = 4) -> bool:
    """The Python twin of ``_SAME_TRANSMITTER_ID``, for callers not in SQL."""
    a, b = a.lower(), b.lower()
    return len(a) == len(b) and len(a) > minimum and a[0] != b[0] and a[1:] == b[1:]


class AliasDetector:
    def __init__(self, db: Database, config: AliasConfig | None = None):
        self.db = db
        self.config = config or AliasConfig()

    def find_pairs(self) -> list[AliasPair]:
        """Sensor pairs that keep decoding the very same bursts."""
        cfg = self.config
        decoder_clause = (
            f"AND (sa.model != sb.model OR ({_SAME_TRANSMITTER_ID}))"
            if cfg.require_different_decoder
            else ""
        )
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

    def explain(self, window: float = 10.0) -> list[dict]:
        """Why each candidate pair did or did not match.

        "Candidate" is every cross-decoder pair, plus the same-decoder pairs
        whose ids differ only in a leading digit -- see
        ``_SAME_TRANSMITTER_ID``. The blind spot was invisible here as well as
        in the detector, which is most of why it went unnoticed for so long.

        Reports the deltas actually present in the data rather than assuming
        what they ought to be -- the thresholds are guesses until measured
        against a real capture.
        """
        cfg = self.config
        rows = self.db.query(
            """
            SELECT a.sensor_pk AS a, b.sensor_pk AS b,
                   COUNT(*)                    AS pairs,
                   MIN(ABS(a.ts - b.ts))       AS dt,
                   MIN(ABS(a.rssi - b.rssi))   AS drssi,
                   MIN(ABS(a.snr  - b.snr))    AS dsnr,
                   SUM(a.rssi IS NULL OR b.rssi IS NULL) AS missing_rssi,
                   SUM(a.snr  IS NULL OR b.snr  IS NULL) AS missing_snr
              FROM readings a
              JOIN readings b
                ON b.sensor_pk > a.sensor_pk
               AND b.ts BETWEEN a.ts - ? AND a.ts + ?
              JOIN sensors sa ON sa.pk = a.sensor_pk
              JOIN sensors sb ON sb.pk = b.sensor_pk
             WHERE (sa.model != sb.model OR (LENGTH(sa.sensor_id) = LENGTH(sb.sensor_id) AND LENGTH(sa.sensor_id) > 4 AND SUBSTR(LOWER(sa.sensor_id), 2) = SUBSTR(LOWER(sb.sensor_id), 2)))
             GROUP BY a.sensor_pk, b.sensor_pk
             ORDER BY dt
            """,
            (window, window),
        )
        counts = {
            int(r["pk"]): int(r["reading_count"])
            for r in self.db.query("SELECT pk, reading_count FROM sensors")
        }
        sensors = {s.pk: s for s in self.db.list_sensors()}

        out = []
        for row in rows:
            a, b = int(row["a"]), int(row["b"])
            blockers = []
            if row["missing_rssi"]:
                blockers.append("no RSSI recorded")
            if row["dt"] is not None and row["dt"] > cfg.time_tolerance:
                blockers.append(f"dt {row['dt']:.1f}s > {cfg.time_tolerance}")
            if row["drssi"] is not None and row["drssi"] > cfg.rssi_tolerance:
                blockers.append(f"dRSSI {row['drssi']:.1f} > {cfg.rssi_tolerance}")
            if row["dsnr"] is not None and row["dsnr"] > cfg.snr_tolerance:
                blockers.append(f"dSNR {row['dsnr']:.1f} > {cfg.snr_tolerance}")
            denominator = min(counts.get(a, 0), counts.get(b, 0))
            out.append(
                {
                    "a": sensors[a].display if a in sensors else str(a),
                    "b": sensors[b].display if b in sensors else str(b),
                    "pairs": int(row["pairs"]),
                    "dt": row["dt"],
                    "drssi": row["drssi"],
                    "dsnr": row["dsnr"],
                    "readings": denominator,
                    "common_id": common_hex_run(
                        sensors[a].sensor_id, sensors[b].sensor_id
                    ) if a in sensors and b in sensors else "",
                    "blockers": blockers,
                }
            )
        return out

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
