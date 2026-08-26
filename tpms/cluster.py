"""Correlate sensor IDs into vehicles by co-occurrence.

Four sensors bolted to the same car are heard within seconds of each other,
over and over, across independent passes. Two unrelated cars that happen to
drive past together share that property once or twice but not persistently --
which is exactly what the support threshold below tests for.

Manual territory is never overwritten: a sensor is left alone if it is pinned,
or if it belongs to a vehicle a human created or renamed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import ClusterConfig
from .db import Database
from .models import Sensor, now as now_ts

log = logging.getLogger(__name__)


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        for node in self.parent:
            out.setdefault(self.find(node), []).append(node)
        return out


@dataclass
class Edge:
    a: int
    b: int
    count: int
    support: float


@dataclass
class ClusterReport:
    components: list[list[int]] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    vehicles_created: int = 0
    vehicles_removed: int = 0
    sensors_assigned: int = 0
    sensors_unassigned: int = 0
    oversized: list[list[int]] = field(default_factory=list)
    skipped_manual: list[int] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.components)} cluster(s), "
            f"+{self.vehicles_created} vehicle(s), -{self.vehicles_removed} empty, "
            f"{self.sensors_assigned} sensor(s) assigned, "
            f"{self.sensors_unassigned} unassigned, "
            f"{len(self.oversized)} oversized, "
            f"{len(self.skipped_manual)} left to manual control"
        )


class Clusterer:
    def __init__(self, db: Database, config: ClusterConfig | None = None):
        self.db = db
        self.config = config or ClusterConfig()

    # -- graph ------------------------------------------------------------

    def build_edges(self, eligible: set[int] | None = None) -> list[Edge]:
        """Co-occurrence pairs strong enough to imply a shared vehicle."""
        counts = self.db.sighting_counts()
        edges: list[Edge] = []
        for row in self.db.cooccurrence_rows():
            a, b, count = int(row["a"]), int(row["b"]), int(row["count"])
            if eligible is not None and (a not in eligible or b not in eligible):
                continue
            if count < self.config.min_cooccurrences:
                continue
            # Support, not raw count: two cars that commuted together three
            # times still fail this if each has been seen fifty times alone.
            denominator = min(counts.get(a, 0), counts.get(b, 0))
            support = count / denominator if denominator else 0.0
            if support < self.config.min_support:
                continue
            edges.append(Edge(a=a, b=b, count=count, support=support))
        return edges

    def components(self, edges: list[Edge]) -> list[list[int]]:
        uf = UnionFind()
        for edge in edges:
            uf.union(edge.a, edge.b)
        groups = [sorted(members) for members in uf.groups().values()]
        groups.sort(key=lambda members: (-len(members), members[0]))
        return groups

    # -- reconciliation ---------------------------------------------------

    def _is_manual(self, sensor: Sensor, manual_vehicles: set[int]) -> bool:
        return sensor.pinned or (
            sensor.vehicle_id is not None and sensor.vehicle_id in manual_vehicles
        )

    def run(self, dry_run: bool = False) -> ClusterReport:
        sensors = {s.pk: s for s in self.db.list_sensors()}
        manual_vehicles = {
            v.pk for v in self.db.list_vehicles() if not v.auto_generated or v.name
        }

        eligible: set[int] = set()
        report = ClusterReport()
        for pk, sensor in sensors.items():
            if self._is_manual(sensor, manual_vehicles):
                report.skipped_manual.append(pk)
            else:
                eligible.add(pk)

        report.edges = self.build_edges(eligible)
        report.components = self.components(report.edges)

        if dry_run:
            report.oversized = [
                c for c in report.components if len(c) > self.config.max_cluster_size
            ]
            return report

        clustered: set[int] = set()
        for members in report.components:
            clustered.update(members)
            oversized = len(members) > self.config.max_cluster_size
            if oversized:
                report.oversized.append(members)
            vehicle_id = self._reconcile(members, sensors, report)
            self.db.execute(
                "UPDATE vehicles SET needs_review = ? WHERE pk = ?",
                (int(oversized), vehicle_id),
            )

        # A sensor that no longer has strong ties loses its auto-assignment.
        for pk in eligible - clustered:
            if sensors[pk].vehicle_id is not None:
                self.db.set_sensor_vehicle(pk, None)
                report.sensors_unassigned += 1

        report.vehicles_removed = self.db.delete_empty_vehicles()
        return report

    def _reconcile(
        self, members: list[int], sensors: dict[int, Sensor], report: ClusterReport
    ) -> int:
        """Map a component onto a vehicle row, reusing one where possible.

        Reuse matters: it keeps a vehicle's identity (and its sighting history
        in the UI) stable when a fifth sensor joins the cluster.
        """
        existing = {
            sensors[pk].vehicle_id for pk in members if sensors[pk].vehicle_id is not None
        }

        if existing:
            # Prefer the vehicle that already owns the most sensors here.
            weights = {
                vid: sum(1 for pk in members if sensors[pk].vehicle_id == vid)
                for vid in existing
            }
            vehicle_id = max(sorted(existing), key=lambda vid: weights[vid])
        else:
            vehicle_id = self.db.create_vehicle(now_ts(), auto_generated=True)
            report.vehicles_created += 1

        for pk in members:
            if sensors[pk].vehicle_id != vehicle_id:
                self.db.set_sensor_vehicle(pk, vehicle_id)
                report.sensors_assigned += 1
        return vehicle_id
