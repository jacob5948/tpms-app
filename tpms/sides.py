"""Which side each wheel is on, learned from passes someone confirmed.

`direction.py` runs this the other way round: given wheel labels, it says which
way a vehicle was pointing. That is the useful direction of travel once the
wheels are labelled -- and labelling them is the part nobody can do from the
radio, because a sensor announces an id and nothing else.

Confirmation breaks the circle. Someone watching the camera knows which side of
the car faced the receiver on a given pass; the receiver knows which wheels it
heard, and how loudly. Do that over a dozen passes and the wheels sort
themselves into two groups: the ones heard when the left side was near, and the
ones heard when the right was.

Two independent signals, because each is weak alone:

*Presence.* A wheel on the far side is often not heard at all -- the car is in
the way. So a sensor heard on most left-side passes and few right-side ones is
on the left. This is the stronger signal and the one that works at range.

*Level.* When both sides are heard, the near side is louder. Levels are taken
relative to the loudest wheel *on that same pass*, so a close pass and a
distant one contribute the same kind of number: how far down from the top of
this pass was it. Without that, one very close pass dominates every other.

Nothing here writes anything. It proposes, the page shows its working, and a
person presses the button -- an inference that silently relabels the thing it
inferred from is one nobody can check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .direction import LEFT, RIGHT, side_of

#: Confirmed passes needed on *each* side before a proposal is made at all.
#: Below this the numbers still read like an answer -- one pass each way gives a
#: presence score of 1.0 -- so the bar is about honesty, not arithmetic.
MIN_PASSES_PER_SIDE = 3

#: How far the two scores must part before this will name a side. A sensor
#: heard equally either way is a wheel this method cannot place, which is a
#: real outcome and better said than dressed up.
MIN_SCORE = 0.2

#: Presence over level. Level only speaks when both sides were audible on one
#: pass, which is the minority of them, and it is the noisier of the two.
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
    #: -1 (firmly right) to +1 (firmly left). Kept even when side is None:
    #: the page shows a leaning that has not yet earned a label.
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
        # One wheel heard says nothing about *relative* strength; the best
        # reading of the pass is also the only one, so every such pass would
        # contribute a flat zero to whichever side it was and swamp the rest.
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

    ``passes`` are rows from `queries.vehicle_passes`; the unconfirmed ones are
    ignored rather than filtered out by the caller, so nobody has to remember
    to. ``level_scale`` is the dB difference treated as a full-strength reading
    -- the same quantity `direction.rssi_margin` sets, and it should be given
    the same value, so the two halves of the program agree on what "louder by
    enough to mean something" is.
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
    # Strongest evidence first: the wheel the confirmations are surest about is
    # the one worth reading, and a list in database order buries it.
    out.sort(key=lambda e: -abs(e.score))
    return out


def _basis(
    heard: dict[str, int],
    totals: dict[str, int],
    levels: dict[str, float | None],
    enough: bool,
    min_passes: int,
) -> str:
    """Why, in the terms the reader can check against the pass table."""
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
    """How often the radio's guess matched what was confirmed.

    The one number that says whether the direction pills on the *unconfirmed*
    passes are worth reading. Passes the heuristic declined to call are counted
    separately: declining is not a wrong answer, and averaging it in with the
    wrong ones would punish the caution that makes the rest trustworthy.
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
