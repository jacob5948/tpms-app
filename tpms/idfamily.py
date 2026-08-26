"""Do sensor IDs encode which wheels belong together?

Observation from a 13-hour capture: every pair that co-occurrence had grouped
confidently also shared a high-order run of its ID, within one decoder.
Renault f7b207 and f7b209 differ by 2; Toyota d39abf13 and d39abf5a by 0x49;
Ford 3779daec and 3779dc0c by 0x6e0. If manufacturers program a wheel set with
contiguous IDs, that is a correlation signal independent of co-occurrence --
and unlike co-occurrence it says something about a car heard exactly once,
which is most of them.

This module measures the claim before anything is allowed to depend on it.
Nothing here decides a grouping; see ``evaluate`` for the scorecard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .cluster import Clusterer, UnionFind
from .config import ClusterConfig
from .db import Database
from .models import Sensor

#: IDs this close are treated as one wheel set. The widest pair observed in
#: the field was Ford 36c17f56 / 36c1581c at 10042 apart, so 65536 leaves
#: headroom without being so wide that unrelated sensors start matching --
#: which is exactly what ``evaluate`` measures.
DEFAULT_MAX_DISTANCE = 1 << 16

_HEX_ONLY = re.compile(r"^[0-9a-f]+$")
_DIGITS_ONLY = re.compile(r"^[0-9]+$")


def detect_convention(ids: list[str]) -> str:
    """Whether a decoder writes its IDs as hex or decimal.

    Decided per decoder, not per ID: ``normalize`` renders an integer id as
    decimal text and a string id as-is, so "12345678" is ambiguous on its own
    but unambiguous once you know the decoder emitted "a1cbaf" elsewhere. One
    decoder only ever uses one convention.
    """
    cleaned = [i.strip().lower().removeprefix("0x") for i in ids if i]
    if not cleaned:
        return "unknown"
    if any(_HEX_ONLY.match(i) and not _DIGITS_ONLY.match(i) for i in cleaned):
        return "hex"
    if all(_DIGITS_ONLY.match(i) for i in cleaned):
        return "dec"
    return "unknown"


def parse_id(sensor_id: str, convention: str) -> int | None:
    text = sensor_id.strip().lower().removeprefix("0x")
    try:
        if convention == "hex":
            return int(text, 16)
        if convention == "dec":
            return int(text, 10)
    except ValueError:
        return None
    return None


def id_distance(a: int, b: int) -> int:
    """How far apart two IDs are, as plain distance.

    Not XOR: a set allocated across a carry boundary (``...ffff`` then
    ``...0000``) is two consecutive IDs, but their XOR is enormous. Distance
    gets that right, and gets the prefix case right too, since sharing a
    high-order prefix already bounds the difference.
    """
    return abs(a - b)


@dataclass
class Family:
    model: str
    members: list[int] = field(default_factory=list)  # sensor pks

    @property
    def size(self) -> int:
        return len(self.members)


def _parsed(sensors: list[Sensor]) -> dict[int, tuple[str, int]]:
    """sensor pk -> (model, parsed id), skipping anything unparseable."""
    by_model: dict[str, list[Sensor]] = {}
    for sensor in sensors:
        by_model.setdefault(sensor.model, []).append(sensor)

    out: dict[int, tuple[str, int]] = {}
    for model, group in by_model.items():
        convention = detect_convention([s.sensor_id for s in group])
        if convention == "unknown":
            continue
        for sensor in group:
            value = parse_id(sensor.sensor_id, convention)
            if value is not None:
                out[sensor.pk] = (model, value)
    return out


def are_near(
    parsed: dict[int, tuple[str, int]], a: int, b: int, max_distance: int = DEFAULT_MAX_DISTANCE
) -> bool:
    """Whether two sensors look like one wheel set by ID alone.

    Cross-decoder comparison is meaningless -- two makers' numbering schemes
    have nothing to say about each other -- so a differing model is never near.
    """
    left, right = parsed.get(a), parsed.get(b)
    if left is None or right is None or left[0] != right[0]:
        return False
    return id_distance(left[1], right[1]) <= max_distance


def families(
    sensors: list[Sensor], max_distance: int = DEFAULT_MAX_DISTANCE
) -> list[Family]:
    """Group sensors into candidate wheel sets by ID proximity alone."""
    parsed = _parsed(sensors)
    uf = UnionFind()
    pks = [s.pk for s in sensors if s.pk in parsed]
    for i, a in enumerate(pks):
        for b in pks[i + 1:]:
            if are_near(parsed, a, b, max_distance):
                uf.union(a, b)

    out = []
    for members in uf.groups().values():
        if len(members) > 1:
            out.append(Family(model=parsed[members[0]][0], members=sorted(members)))
    out.sort(key=lambda f: (-f.size, f.model))
    return out


@dataclass
class Scorecard:
    """How well ID proximity agrees with what co-occurrence already decided."""

    confirmed_pairs: int = 0
    confirmed_near: int = 0
    #: Same-decoder pairs never heard together at all -- near-certainly
    #: different vehicles, and the larger, more trustworthy sample.
    apart_pairs: int = 0
    apart_near: int = 0
    families: list[Family] = field(default_factory=list)
    #: Below this many confirmed pairs, the recall figure is not worth quoting.
    min_sample: int = 8

    @property
    def recall(self) -> float | None:
        if not self.confirmed_pairs:
            return None
        return self.confirmed_near / self.confirmed_pairs

    @property
    def false_positive_rate(self) -> float | None:
        if not self.apart_pairs:
            return None
        return self.apart_near / self.apart_pairs

    @property
    def usable(self) -> bool:
        return self.confirmed_pairs >= self.min_sample and self.apart_pairs > 0

    def verdict(self) -> str:
        """A sentence, or an honest refusal to give one."""
        if not self.usable:
            return (
                f"not enough evidence yet: {self.confirmed_pairs} confirmed pair(s), "
                f"{self.min_sample} needed. Keep capturing -- a percentage of "
                f"{self.confirmed_pairs} pairs would not mean anything."
            )
        recall, noise = self.recall, self.false_positive_rate
        if recall >= 0.8 and noise <= 0.05:
            return "strong signal: agrees with co-occurrence and rarely fires otherwise"
        if recall >= 0.6 and noise <= 0.15:
            return "usable signal, but not on its own -- keep it as a tie-breaker"
        if noise > 0.15:
            return (
                "too noisy: ID-near pairs are common among sensors never heard "
                "together, so proximity is mostly coincidence here"
            )
        return "weak: ID proximity misses most pairs co-occurrence is sure about"


def evaluate(
    db: Database, config: ClusterConfig | None = None, max_distance: int = DEFAULT_MAX_DISTANCE
) -> Scorecard:
    """Score ID proximity against groupings co-occurrence is already sure of.

    Two measurements, because either alone is easy to fool:

    * **Recall** -- of pairs joined by a *confirmed* co-occurrence edge (heard
      together repeatedly, over the support threshold), how many are ID-near.
      Small sample, but these are as close to ground truth as this data gets.
    * **False positive rate** -- of same-decoder pairs never heard together at
      all, how many are ID-near anyway. Those are almost certainly different
      vehicles, so if this is high the signal is noise. Much larger sample.
    """
    cfg = config or ClusterConfig()
    sensors = [s for s in db.list_sensors() if s.alias_of is None]
    parsed = _parsed(sensors)
    card = Scorecard(families=families(sensors, max_distance))

    clusterer = Clusterer(db, cfg)
    eligible = {s.pk for s in sensors}
    heard_together: set[tuple[int, int]] = set()
    for edge in clusterer.build_edges(eligible):
        heard_together.add((min(edge.a, edge.b), max(edge.a, edge.b)))
        if not edge.confirmed:
            continue
        card.confirmed_pairs += 1
        if are_near(parsed, edge.a, edge.b, max_distance):
            card.confirmed_near += 1

    # Every co-occurring pair, confirmed or not, is excluded from the control:
    # a weak edge is ambiguous, not known-different.
    for row in db.cooccurrence_rows():
        heard_together.add((min(int(row["a"]), int(row["b"])),
                            max(int(row["a"]), int(row["b"]))))

    pks = sorted(parsed)
    for i, a in enumerate(pks):
        for b in pks[i + 1:]:
            if (a, b) in heard_together or parsed[a][0] != parsed[b][0]:
                continue
            card.apart_pairs += 1
            if are_near(parsed, a, b, max_distance):
                card.apart_near += 1
    return card
