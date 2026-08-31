"""Infer which side each wheel is on, from passes a user confirmed.

direction.py works the other way: given wheel labels, it infers which side
faced the receiver. Those labels cannot come from the radio, since a sensor
transmits only an id and its readings. This module derives them from passes
where the user recorded which side they saw.

Two signals are combined, because each is weak alone:

Presence. The car blocks the far side, so a sensor heard on most left-side
passes and few right-side ones is on the left. This works at range and is
weighted higher.

Level. When both sides are heard, the near side is louder. Levels are taken
relative to the loudest wheel of the same pass, so one close pass does not
dominate the distant ones.

Nothing here writes to the database. It returns proposals; the vehicle page
shows the counts behind them and applies them only when the user asks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .direction import LEFT, RIGHT, side_of

#: Confirmed passes needed on each side before anything is proposed. One pass
#: each way already yields a presence score of 1.0, which looks like certainty.
MIN_PASSES_PER_SIDE = 3

#: Minimum combined score before a side is named. A sensor heard equally either
#: way cannot be placed by this method, and is reported as "no call".
MIN_SCORE = 0.2

#: Presence is weighted higher: level only applies when both sides were heard on
#: one pass, which is the minority of passes, and it is the noisier signal.
PRESENCE_WEIGHT = 0.6


@dataclass(frozen=True)
class Evidence:
    """One sensor, what the confirmed passes say about it, and how strongly."""

    sensor_pk: int
    display: str
    wheel_label: str | None
    #: Passes confirmed for each side, and how many of them heard this sensor.
    heard: dict[str, int]
    totals: dict[str, int]
    #: Mean level relative to the loudest wheel of the same pass, per side.
    levels: dict[str, float | None]
    #: "left" or "right", or None when the evidence does not support a call.
    side: str | None
    #: -1 (firmly right) to +1 (firmly left). Kept when side is None so the
    #: page can show a leaning that has not reached the threshold.
    score: float
    basis: str

    @property
    def agrees(self) -> bool:
        """True when this sensor is already labelled with the side proposed."""
        return self.side is not None and side_of(self.wheel_label) == self.side

    @property
    def changes(self) -> bool:
        """True when applying this would alter the label the sensor carries."""
        return self.side is not None and side_of(self.wheel_label) != self.side


def _relative_levels(passes: Iterable[dict[str, Any]]) -> dict[int, dict[str, list[float]]]:
    """Per sensor, per confirmed side, its level below the pass's loudest wheel."""
    out: dict[int, dict[str, list[float]]] = {}
    for one in passes:
        sightings = one["sightings"]
        levels = [s["max_rssi"] for s in sightings if s["max_rssi"] is not None]
        # With one wheel heard, that wheel is also the loudest, so the pass
        # would contribute a flat zero to whichever side it was confirmed as.
        if len(levels) < 2:
            continue
        loudest = max(levels)
        for sighting in sightings:
            if sighting["max_rssi"] is None:
                continue
            by_side = out.setdefault(int(sighting["sensor_pk"]), {})
            by_side.setdefault(one["confirmed"], []).append(
                float(sighting["max_rssi"]) - loudest
            )
    return out


def propose(
    passes: Iterable[dict[str, Any]],
    sensors: Iterable[dict[str, Any]],
    min_passes: int = MIN_PASSES_PER_SIDE,
    level_scale: float = 6.0,
) -> list[Evidence]:
    """Read confirmed passes into a proposed side for each of a vehicle's wheels.

    ``passes`` are rows from queries.vehicle_passes; unconfirmed ones are
    ignored here rather than by the caller. ``level_scale`` is the dB
    difference treated as a full-strength signal, and should be given the same
    value as direction.rssi_margin so both use one definition of "louder".
    """
    confirmed = [p for p in passes if p.get("confirmed") in (LEFT, RIGHT)]
    totals = {
        LEFT: sum(1 for p in confirmed if p["confirmed"] == LEFT),
        RIGHT: sum(1 for p in confirmed if p["confirmed"] == RIGHT),
    }
    heard_in: dict[int, dict[str, int]] = {}
    for one in confirmed:
        for sensor_pk in {int(s["sensor_pk"]) for s in one["sightings"]}:
            counts = heard_in.setdefault(sensor_pk, {LEFT: 0, RIGHT: 0})
            counts[one["confirmed"]] += 1
    levels = _relative_levels(confirmed)

    enough = totals[LEFT] >= min_passes and totals[RIGHT] >= min_passes

    out: list[Evidence] = []
    for sensor in sensors:
        pk = int(sensor["pk"])
        heard = heard_in.get(pk, {LEFT: 0, RIGHT: 0})
        rates = {
            side: (heard[side] / totals[side] if totals[side] else 0.0)
            for side in (LEFT, RIGHT)
        }
        presence = rates[LEFT] - rates[RIGHT]

        mean: dict[str, float | None] = {}
        for side in (LEFT, RIGHT):
            samples = levels.get(pk, {}).get(side, [])
            mean[side] = sum(samples) / len(samples) if samples else None
        if mean[LEFT] is not None and mean[RIGHT] is not None:
            gap = (mean[LEFT] - mean[RIGHT]) / level_scale
            level = max(-1.0, min(1.0, gap))
            score = PRESENCE_WEIGHT * presence + (1 - PRESENCE_WEIGHT) * level
        else:
            level = None
            score = presence

        side = None
        if enough and abs(score) >= MIN_SCORE:
            side = LEFT if score > 0 else RIGHT
        out.append(
            Evidence(
                sensor_pk=pk,
                display=sensor["display"],
                wheel_label=sensor.get("wheel_label"),
                heard=heard,
                totals=totals,
                levels=mean,
                side=side,
                score=round(score, 3),
                basis=_basis(heard, totals, mean, enough, min_passes),
            )
        )
    # Strongest evidence first, so the best-supported wheel is at the top.
    out.sort(key=lambda e: -abs(e.score))
    return out


def _basis(
    heard: dict[str, int],
    totals: dict[str, int],
    levels: dict[str, float | None],
    enough: bool,
    min_passes: int,
) -> str:
    """The reasoning in words, in terms that match the pass table above it."""
    if not enough:
        short = [
            f"{max(0, min_passes - totals[side])} more {side}-side"
            for side in (LEFT, RIGHT)
            if totals[side] < min_passes
        ]
        return "not enough confirmed passes yet: " + ", ".join(short) + " needed"
    parts = [
        f"heard on {heard[side]} of {totals[side]} {side}-side passes"
        for side in (LEFT, RIGHT)
    ]
    if levels[LEFT] is not None and levels[RIGHT] is not None:
        gap = levels[LEFT] - levels[RIGHT]
        louder = LEFT if gap > 0 else RIGHT
        parts.append(f"{abs(gap):.1f} dB louder on {louder}-side passes")
    return "; ".join(parts)


def accuracy(passes: Iterable[dict[str, Any]]) -> dict[str, int]:
    """How often direction.infer matched what was confirmed.

    This is how much the Direction column can be trusted on unconfirmed
    passes. Passes the heuristic declined to call are counted separately,
    since declining is not a wrong answer.
    """
    result = {"confirmed": 0, "called": 0, "right": 0, "declined": 0}
    for one in passes:
        if one.get("confirmed") not in (LEFT, RIGHT):
            continue
        result["confirmed"] += 1
        heading = one.get("heading")
        if heading is None:
            result["declined"] += 1
            continue
        result["called"] += 1
        if heading.side == one["confirmed"]:
            result["right"] += 1
    return result
