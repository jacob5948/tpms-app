"""Placing wheels from passes someone confirmed by eye.

The radio can say which wheels it heard and how loudly. It cannot say which
side of the car they are on -- that is the fact everything else about direction
rests on, and the only source for it is someone watching. These tests are about
what a handful of confirmations is and is not enough to conclude.
"""

import pytest

from tpms import sides
from tpms.direction import LEFT, RIGHT, Heading, side_label


def pass_(confirmed, heard, pk=1):
    """One pass: which side was confirmed, and (sensor, level) for each wheel."""
    return {
        "confirmed": confirmed,
        "anchor": pk,
        "heading": None,
        "sightings": [
            {"sensor_pk": sensor, "max_rssi": rssi, "display": f"s{sensor}"}
            for sensor, rssi in heard
        ],
    }


def sensors(*pks, labels=None):
    labels = labels or {}
    return [
        {"pk": pk, "display": f"s{pk}", "wheel_label": labels.get(pk)} for pk in pks
    ]


def by_pk(proposals):
    return {e.sensor_pk: e for e in proposals}


# -- presence: the wheel that is not heard is the evidence -------------------


def test_a_wheel_heard_only_on_one_sides_passes_is_on_that_side():
    """The far wheels are the ones the car is in the way of, so which wheels
    went missing is the strongest thing a pass says."""
    passes = [pass_(LEFT, [(1, -40.0), (2, -42.0)]) for _ in range(4)]
    passes += [pass_(RIGHT, [(3, -40.0), (4, -41.0)]) for _ in range(4)]

    placed = by_pk(sides.propose(passes, sensors(1, 2, 3, 4)))
    assert placed[1].side == LEFT and placed[2].side == LEFT
    assert placed[3].side == RIGHT and placed[4].side == RIGHT
    assert "heard on 4 of 4 left-side passes" in placed[1].basis


def test_a_wheel_heard_on_everything_is_not_placed_by_presence_alone():
    """A resident-strength transmitter heard on every pass says nothing about
    sides, and must not be given a side because of a rounding error."""
    passes = [pass_(LEFT, [(1, -40.0), (9, -40.0)]) for _ in range(4)]
    passes += [pass_(RIGHT, [(2, -40.0), (9, -40.0)]) for _ in range(4)]

    placed = by_pk(sides.propose(passes, sensors(1, 2, 9)))
    assert placed[9].side is None
    assert placed[1].side == LEFT and placed[2].side == RIGHT


# -- level: when both sides are heard, the near one is louder ----------------


def test_levels_place_wheels_that_presence_cannot():
    """Close passes hear all four wheels, so presence is flat and the only
    thing left is which ones were louder."""
    passes = [pass_(LEFT, [(1, -30.0), (2, -31.0), (3, -45.0), (4, -46.0)]) for _ in range(4)]
    passes += [pass_(RIGHT, [(1, -45.0), (2, -46.0), (3, -30.0), (4, -31.0)]) for _ in range(4)]

    placed = by_pk(sides.propose(passes, sensors(1, 2, 3, 4)))
    assert [placed[i].side for i in (1, 2, 3, 4)] == [LEFT, LEFT, RIGHT, RIGHT]
    assert "dB louder on left-side passes" in placed[1].basis


def test_levels_are_read_relative_to_the_pass_they_came_from():
    """One very close pass must not outvote ten distant ones. Every level is
    how far below the loudest wheel *of that pass* it was."""
    near = pass_(LEFT, [(1, -20.0), (2, -28.0)])
    far = [pass_(RIGHT, [(1, -70.0), (2, -62.0)]) for _ in range(4)]

    placed = by_pk(sides.propose([near] * 4 + far, sensors(1, 2)))
    # Sensor 1 leads its pass on the left ones and trails by 8 dB on the right
    # ones. On absolute levels both wheels are 40 dB louder on the left passes,
    # which would call the pair left and learn nothing.
    assert placed[1].side == LEFT
    assert placed[2].side == RIGHT


# -- refusing to answer ------------------------------------------------------


def test_too_few_confirmations_place_nothing():
    """Two passes each way give a presence score of 1.0, which reads exactly
    like certainty and is nothing of the kind."""
    passes = [pass_(LEFT, [(1, -40.0)]), pass_(LEFT, [(1, -40.0)])]
    passes += [pass_(RIGHT, [(2, -40.0)]), pass_(RIGHT, [(2, -40.0)])]

    placed = by_pk(sides.propose(passes, sensors(1, 2)))
    assert all(e.side is None for e in placed.values())
    assert "1 more left-side" in placed[1].basis
    assert "1 more right-side" in placed[1].basis


def test_confirmations_on_one_side_only_place_nothing():
    """Ten entrances and no exits is one experiment run ten times."""
    passes = [pass_(LEFT, [(1, -40.0), (2, -60.0)]) for _ in range(10)]
    placed = by_pk(sides.propose(passes, sensors(1, 2)))
    assert all(e.side is None for e in placed.values())
    assert "3 more right-side" in placed[1].basis


def test_unconfirmed_passes_are_ignored_not_counted():
    passes = [pass_(None, [(1, -40.0)]) for _ in range(20)]
    passes += [pass_(LEFT, [(1, -40.0)]) for _ in range(3)]
    passes += [pass_(RIGHT, [(2, -40.0)]) for _ in range(3)]

    placed = by_pk(sides.propose(passes, sensors(1, 2)))
    assert placed[1].totals == {LEFT: 3, RIGHT: 3}
    assert placed[1].side == LEFT


def test_a_split_wheel_is_left_unplaced_rather_than_guessed():
    """Heard on half the passes each way, at the same level: this method has
    nothing to say, and saying so is the answer."""
    passes = [pass_(LEFT, [(1, -40.0), (5, -40.0)]) for _ in range(4)]
    passes += [pass_(LEFT, [(1, -40.0)]) for _ in range(0)]
    passes += [pass_(RIGHT, [(2, -40.0), (5, -40.0)]) for _ in range(4)]

    placed = by_pk(sides.propose(passes, sensors(1, 2, 5)))
    assert placed[5].side is None
    assert abs(placed[5].score) < sides.MIN_SCORE


# -- what applying it would do -----------------------------------------------


def test_a_proposal_knows_whether_it_changes_anything():
    passes = [pass_(LEFT, [(1, -40.0)]) for _ in range(3)]
    passes += [pass_(RIGHT, [(2, -40.0)]) for _ in range(3)]

    placed = by_pk(sides.propose(passes, sensors(1, 2, labels={1: "L", 2: "L"})))
    assert placed[1].agrees and not placed[1].changes
    assert placed[2].changes and not placed[2].agrees


def test_moving_a_corner_label_keeps_the_end_it_named():
    """Front-or-rear was never in question here, and nobody learned it from
    the radio -- so a proposal about sides must not throw it away."""
    assert side_label("FL", RIGHT) == "FR"
    assert side_label("RR", LEFT) == "RL"
    assert side_label("L", RIGHT) == "R"
    assert side_label(None, LEFT) == "L"
    assert side_label("spare", LEFT) == "L"


# -- scoring the guesswork ---------------------------------------------------


def test_accuracy_counts_declining_apart_from_being_wrong():
    """Declining to call is the caution that makes the rest worth reading;
    averaging it in with the wrong answers punishes it."""
    right = pass_(LEFT, [(1, -40.0)])
    right["heading"] = Heading(LEFT, firm=True, basis="")
    wrong = pass_(RIGHT, [(1, -40.0)])
    wrong["heading"] = Heading(LEFT, firm=False, basis="")
    quiet = pass_(LEFT, [(1, -40.0)])

    score = sides.accuracy([right, wrong, quiet, pass_(None, [(1, -40.0)])])
    assert score == {"confirmed": 3, "called": 2, "right": 1, "declined": 1}
