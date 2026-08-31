"""Which way a vehicle was pointing, from the wheels that were heard.

The receiver sits at a fixed point beside the road. A wheel on the near side
of a passing car has clear air between it and the antenna; one on the far side
has the car in the way. So the near side is heard more often, and more
strongly, and *which wheels were heard at all* is the evidence for which side
faced the receiver.

That is the whole of what the radio can say. A side is not a direction until
someone says which way traffic on that side is going, and only the person who
owns the receiver knows that -- so the names come from `direction:` in the
config and the program never guesses them. With no names configured this
still reports the side, which is the honest half of the answer.

Nothing here is stored. Wheel labels change as a user curates, and a stored
guess would go stale the moment one did; every pass is inferred at read time
from the labels as they are now.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The wheel positions offered in the UI, grouped as the picker offers them.
#: `L` and `R` exist because a sensor is often known to be on one side long
#: before anyone works out whether it is the front or the rear -- and side is
#: the only part direction needs. Forcing a choice between FL and RL to record
#: something known would either lose the fact or invent a detail.
#:
#: The group headings carry what used to be written into the labels themselves
#: ("left, front or rear unknown"): a picker states the qualification once, over
#: the pair it applies to, rather than in every option a reader has to compare.
WHEEL_POSITION_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Corner known", (
        ("FL", "front left"),
        ("FR", "front right"),
        ("RL", "rear left"),
        ("RR", "rear right"),
    )),
    ("Side only, front or rear unknown", (
        ("L", "left"),
        ("R", "right"),
    )),
    ("Not a road wheel", (
        ("spare", "spare wheel"),
    )),
)

#: The same positions, flat, in the order they are offered. One source of
#: truth: a second list of the canonical set would drift from the picker.
WHEEL_POSITIONS: tuple[tuple[str, str], ...] = tuple(
    position for _, positions in WHEEL_POSITION_GROUPS for position in positions
)

LEFT = "left"
RIGHT = "right"

#: Labels that place a wheel on a side. A spare is deliberately absent: it is
#: a real wheel with a real sensor and no side at all, and letting it vote
#: would put a car's direction on the one wheel that is not on the road.
_SIDES: dict[str, str] = {
    "FL": LEFT, "RL": LEFT, "L": LEFT,
    "FR": RIGHT, "RR": RIGHT, "R": RIGHT,
}

_OPPOSITE = {LEFT: RIGHT, RIGHT: LEFT}


def side_of(label: str | None) -> str | None:
    """The side a wheel label names, or None if it names no side.

    Labels are free text -- the picker offers the canonical set and the API
    still accepts anything -- so this is deliberately forgiving about case and
    spacing, and silent about anything it does not recognise. An unrecognised
    label is not an error; it is a wheel whose side is unknown, which is
    exactly what the caller does with it.
    """
    if not label:
        return None
    key = label.strip().upper().replace(" ", "").replace("-", "").replace("_", "")
    if key in _SIDES:
        return _SIDES[key]
    # The spelled-out forms, since the field accepts anything a person types.
    spelled = {
        "FRONTLEFT": LEFT, "REARLEFT": LEFT, "BACKLEFT": LEFT, "LEFT": LEFT,
        "FRONTRIGHT": RIGHT, "REARRIGHT": RIGHT, "BACKRIGHT": RIGHT,
        "RIGHT": RIGHT,
    }
    return spelled.get(key)


@dataclass(frozen=True)
class Heading:
    """One pass's direction, and what it rests on."""

    #: "left" or "right" -- the side of the vehicle that faced the receiver.
    side: str
    #: True when every wheel heard was labelled and all were on one side.
    #: False when the reading is a judgement between two sides that were both
    #: audible, or when unlabelled wheels leave room for the other side.
    firm: bool
    #: Why, in a few words, for the tooltip. Always says what it rests on.
    basis: str

    def name(self, names: dict[str, str | None] | None = None) -> str:
        """What to call this heading: the configured name, else the side."""
        configured = (names or {}).get(self.side)
        return configured or f"{self.side} side"


def infer(
    wheels: list[tuple[str | None, float | None]],
    rssi_margin: float = 6.0,
) -> Heading | None:
    """Guess which side faced the receiver, from one pass's wheels.

    ``wheels`` is ``(wheel_label, max_rssi)`` for every sensor heard on the
    pass, labelled or not -- the unlabelled ones matter, because they are the
    reason a one-sided reading can still be wrong.

    Returns None when the evidence does not support a call, which is the
    common case early on: an unlabelled vehicle has nothing to reason from,
    and saying so is better than a coin flip dressed as a finding.
    """
    if not wheels:
        return None

    sided = [(side_of(label), rssi) for label, rssi in wheels]
    heard = {side for side, _ in sided if side}
    if not heard:
        return None

    unlabelled = sum(1 for side, _ in sided if side is None)

    if len(heard) == 1:
        side = heard.pop()
        if unlabelled:
            # Every wheel whose side we know is on one side -- but a wheel we
            # cannot place could be on the other, and if it were, the reading
            # would be the opposite one. Report it, and say what would
            # overturn it.
            return Heading(
                side,
                firm=False,
                basis=(
                    f"only {side}-side wheels identified, but "
                    f"{unlabelled} heard wheel{'' if unlabelled == 1 else 's'} "
                    f"{'is' if unlabelled == 1 else 'are'} unlabelled"
                ),
            )
        return Heading(side, firm=True, basis=f"only {side}-side wheels heard")

    # Both sides were audible, which happens at close range. The near side is
    # the louder one -- but only if it is louder by enough to mean something,
    # since a single reading's level swings several dB on nothing at all.
    best: dict[str, float] = {}
    for side, rssi in sided:
        if side is None or rssi is None:
            continue
        if side not in best or rssi > best[side]:
            best[side] = rssi
    if len(best) < 2:
        return None

    strong = max(best, key=lambda s: best[s])
    margin = best[strong] - best[_OPPOSITE[strong]]
    if margin < rssi_margin:
        return None
    return Heading(
        strong,
        firm=False,
        basis=(
            f"both sides heard; {strong} side stronger by "
            f"{margin:.0f} dB"
        ),
    )
