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

from . import idfamily
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
    #: False for edges inferred from a single pass, which are plausible but
    #: uncorroborated. A component containing any of these stays provisional.
    confirmed: bool = True


@dataclass
class ClusterReport:
    components: list[list[int]] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    vehicles_created: int = 0
    vehicles_removed: int = 0
    sensors_assigned: int = 0
    sensors_unassigned: int = 0
    oversized: list[list[int]] = field(default_factory=list)
    #: Clusters whose members come from several unrelated id families -- a
    #: shape that usually means two cars that drove past together.
    mixed_families: list[list[int]] = field(default_factory=list)
    skipped_manual: list[int] = field(default_factory=list)
    skipped_aliases: list[int] = field(default_factory=list)
    #: Sensors hidden by hand. Kept out of clustering entirely, so a known
    #: irrelevance cannot pull a real vehicle into its cluster.
    skipped_ignored: list[int] = field(default_factory=list)
    provisional: list[list[int]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.components)} cluster(s), "
            f"+{self.vehicles_created} vehicle(s), -{self.vehicles_removed} empty, "
            f"{self.sensors_assigned} sensor(s) assigned, "
            f"{self.sensors_unassigned} unassigned, "
            f"{len(self.provisional)} provisional, "
            f"{len(self.oversized)} oversized, "
            f"{len(self.mixed_families)} mixed id families, "
            f"{len(self.skipped_manual)} left to manual control, "
            f"{len(self.skipped_aliases)} duplicate decode(s) ignored, "
            f"{len(self.skipped_ignored)} hidden"
        )


class Clusterer:
    def __init__(self, db: Database, config: ClusterConfig | None = None):
        self.db = db
        self.config = config or ClusterConfig()

    # -- graph ------------------------------------------------------------

    def build_edges(self, eligible: set[int] | None = None) -> list[Edge]:
        """Co-occurrence pairs strong enough to imply a shared vehicle."""
        counts = self.db.sighting_counts()
        profiles = self._profiles() if self.config.single_pass else {}
        self._ids = self._parsed_ids() if self.config.single_pass else {}
        residents = self.residents() if self.config.single_pass else set()
        edges: list[Edge] = []

        for row in self.db.cooccurrence_rows():
            a, b, count = int(row["a"]), int(row["b"]), int(row["count"])
            if eligible is not None and (a not in eligible or b not in eligible):
                continue

            # Support, not raw count: two cars that commuted together three
            # times still fail this if each has been seen fifty times alone.
            # Capped at 1: one long sighting can overlap many short ones, so
            # the count can exceed the rarer sensor's sighting total, and a
            # "share" above 100% is meaningless either way.
            denominator = min(counts.get(a, 0), counts.get(b, 0))
            support = min(count / denominator, 1.0) if denominator else 0.0

            if count >= self.config.min_cooccurrences and support >= self.config.min_support:
                edges.append(Edge(a=a, b=b, count=count, support=support))
            elif (
                self.config.single_pass
                # A sensor parked in range is audible while every passing car
                # goes by, so a single shared window says nothing about them
                # belonging together. Repeat co-occurrence still counts, which
                # is why this only guards the single-pass branch.
                and a not in residents
                and b not in residents
                and self._same_vehicle_shape(a, b, profiles)
            ):
                edges.append(
                    Edge(a=a, b=b, count=count, support=support, confirmed=False)
                )
        return edges

    def residents(self) -> set[int]:
        """Sensors that live in range rather than driving past.

        Two of these on a real capture had been continuously audible for over
        ten hours while everything else was heard for a minute or two.
        """
        cfg = self.config
        return {
            pk
            for pk, (duty, span) in self.db.duty_cycles().items()
            if span >= cfg.resident_min_span_seconds and duty >= cfg.resident_duty_cycle
        }

    def _parsed_ids(self) -> dict[int, tuple[str, int]]:
        """Sensor ids as numbers, for the adjacency test."""
        if not self.config.id_adjacency:
            return {}
        return idfamily._parsed(self.db.list_sensors())

    def _ids_adjacent(self, a: int, b: int) -> bool:
        if not self.config.id_adjacency:
            return False
        return idfamily.are_near(
            getattr(self, "_ids", {}), a, b, self.config.id_max_distance
        )

    def _profiles(self) -> dict[int, tuple[set[str], float | None]]:
        """Decoders that produced each sensor, and its mean signal level.

        The decoder set spans the sensor's duplicate decodes, not just the one
        kept as canonical. Collapsing duplicates otherwise destroys the signal
        this relies on: three wheels all decoded as Jansite can end up with
        canonical models of Jansite, Citroen and Renault, and comparing only
        those makes one car look like three.
        """
        rows = self.db.query(
            """
            SELECT COALESCE(s.alias_of, s.pk) AS root, s.model,
                   (SELECT AVG(r.rssi) FROM readings r WHERE r.sensor_pk = s.pk) AS rssi
              FROM sensors s
            """
        )
        decoders: dict[int, set[str]] = {}
        levels: dict[int, list[float]] = {}
        for row in rows:
            root = int(row["root"])
            decoders.setdefault(root, set()).add(row["model"])
            if row["rssi"] is not None:
                levels.setdefault(root, []).append(float(row["rssi"]))
        return {
            root: (
                models,
                sum(levels[root]) / len(levels[root]) if levels.get(root) else None,
            )
            for root, models in decoders.items()
        }

    def _same_vehicle_shape(
        self, a: int, b: int, profiles: dict[int, tuple[set[str], float | None]]
    ) -> bool:
        """Could these two be wheels on one vehicle, seen once?

        Wheels on a car share an OEM sensor type and sit roughly the same
        distance from the receiver. Two unrelated cars passing at the same
        moment usually differ in at least one of those. Sensor type is tested
        as an overlap between decoder sets, so two wheels still match when a
        shared decoder was kept as canonical for only one of them.
        """
        left, right = profiles.get(a), profiles.get(b)
        if left is None or right is None:
            return False
        if not (left[0] & right[0]):
            return False
        # Near-consecutive ids stand in for the signal test. Wheels on opposite
        # sides of a car can differ by more than the RSSI spread allows, and a
        # shared id prefix is the stronger evidence when both are available.
        if self._ids_adjacent(a, b):
            return True
        if left[1] is None or right[1] is None:
            return True  # no signal data to judge on; timing alone will do
        return abs(left[1] - right[1]) <= self.config.single_pass_rssi_spread

    def _spans_several_id_families(self, members: list[int]) -> bool:
        """Do these sensors come from more than one block of ids?

        Wheels on one car sit in one block. A cluster covering several is
        usually two vehicles that travel together, which co-occurrence alone
        cannot tell apart -- so it gets flagged for a human rather than split
        automatically.
        """
        if not self.config.id_adjacency or len(members) < 3:
            return False
        parsed = getattr(self, "_ids", None)
        if not parsed:
            parsed = self._parsed_ids()
        known = [m for m in members if m in parsed]
        if len(known) < 3:
            return False

        uf = UnionFind()
        for pk in known:
            uf.find(pk)
        for i, a in enumerate(known):
            for b in known[i + 1:]:
                if idfamily.are_near(parsed, a, b, self.config.id_max_distance):
                    uf.union(a, b)
        return len(uf.groups()) > 1

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
            if sensor.alias_of is not None:
                # The same transmitter under another decoder's name. Including
                # it would invent a vehicle out of one physical sensor.
                report.skipped_aliases.append(pk)
            elif sensor.ignored:
                # Hidden on purpose. It is still heard, and a resident one is
                # audible while everything else drives past, so letting it
                # cluster would undo the point of hiding it.
                report.skipped_ignored.append(pk)
            elif self._is_manual(sensor, manual_vehicles):
                report.skipped_manual.append(pk)
            else:
                eligible.add(pk)

        if not dry_run:
            self._release(sensors, eligible, manual_vehicles)

        report.edges = self.build_edges(eligible)
        report.components = self.components(report.edges)

        if dry_run:
            report.oversized = [
                c for c in report.components if len(c) >= self.config.max_cluster_size
            ]
            return report

        provisional_nodes = {
            node
            for edge in report.edges
            if not edge.confirmed
            for node in (edge.a, edge.b)
        }
        confirmed_nodes = {
            node for edge in report.edges if edge.confirmed for node in (edge.a, edge.b)
        }

        clustered: set[int] = set()
        for members in report.components:
            clustered.update(members)
            # At the limit, not past it: a six-sensor cluster with
            # max_cluster_size 6 slipped through unflagged, and six is already
            # more than a four-wheeled car carries.
            oversized = len(members) >= self.config.max_cluster_size
            if oversized:
                report.oversized.append(members)
            mixed = self._spans_several_id_families(members)
            if mixed:
                report.mixed_families.append(members)
            # Provisional until every member has been corroborated by a
            # repeat sighting, not just grouped from one pass.
            provisional = any(
                m in provisional_nodes and m not in confirmed_nodes for m in members
            )
            if provisional:
                report.provisional.append(members)
            vehicle_id = self._reconcile(members, sensors, report)
            reason = "oversized" if oversized else "mixed_families" if mixed else None
            self.db.execute(
                "UPDATE vehicles SET needs_review = ?, provisional = ?, "
                "review_reason = ? WHERE pk = ?",
                (int(oversized or mixed), int(provisional), reason, vehicle_id),
            )

        # A sensor that no longer has strong ties loses its auto-assignment.
        for pk in eligible - clustered:
            if sensors[pk].vehicle_id is not None:
                self.db.set_sensor_vehicle(pk, None)
                report.sensors_unassigned += 1

        report.vehicles_removed = self.db.delete_empty_vehicles()
        return report

    def _release(
        self, sensors: dict[int, Sensor], eligible: set[int], manual: set[int]
    ) -> None:
        """Drop "provisional" from the vehicles clustering no longer owns.

        The flag means "grouped from a single pass, and not seen together
        since". It is only ever rewritten for the vehicles this run
        reconciles, so one that passed into a person's hands kept whatever it
        had at that moment -- for good. Naming a vehicle, the act that most
        says "this grouping is mine, not a guess", is exactly what took it out
        of reach of the only code that could have cleared the flag.
        """
        held = set(manual)
        members: dict[int, list[int]] = {}
        for pk, sensor in sensors.items():
            if sensor.vehicle_id is not None and sensor.alias_of is None:
                members.setdefault(sensor.vehicle_id, []).append(pk)
        # Every wheel pinned is the same statement as a name: a person put
        # these together and clustering may not take them apart.
        held |= {
            vehicle_id
            for vehicle_id, pks in members.items()
            if pks and all(pk not in eligible and sensors[pk].pinned for pk in pks)
        }
        if not held:
            return
        self.db.execute(
            "UPDATE vehicles SET provisional = 0 WHERE provisional = 1 AND pk IN "
            f"({','.join('?' * len(held))})",
            tuple(sorted(held)),
        )

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
