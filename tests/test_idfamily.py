"""Sensor IDs as a correlation signal -- and the honesty of the measurement.

The hypothesis (wheel sets get contiguous IDs) came from eyeballing one
capture, so the scorecard has to be able to say "the sample is too small" and
"this is noise" rather than always producing an encouraging number.
"""

import pytest

from tpms import idfamily
from tpms.config import ClusterConfig
from tpms.models import Reading, Sensor


def _pass(ingestor, model, ids, at, bursts=3, step=40):
    """One vehicle going by, the same shape the clustering tests use."""
    for burst in range(bursts):
        for index, sensor_id in enumerate(ids):
            ingestor.ingest(
                Reading(model=model, sensor_id=sensor_id, ts=at + burst * step + index * 0.4)
            )


def _sensor(pk, model, sensor_id):
    return Sensor(pk=pk, model=model, sensor_id=sensor_id, first_seen=0, last_seen=0,
                  reading_count=1, vehicle_id=None, wheel_label=None, pinned=False)


# -- parsing --------------------------------------------------------------

def test_a_decoder_using_letters_is_hex():
    assert idfamily.detect_convention(["d39abf13", "a1cbaf"]) == "hex"


def test_a_decoder_using_only_digits_is_decimal():
    """normalize() renders an integer id as decimal, so this is a real case."""
    assert idfamily.detect_convention(["12345678", "12345699"]) == "dec"


def test_the_convention_is_decided_per_decoder_not_per_id(): 
    """"030bcd32" is hex, but "0304" alone would look decimal."""
    assert idfamily.detect_convention(["030bcd32", "0304"]) == "hex"


def test_an_id_that_is_neither_is_left_alone():
    assert idfamily.detect_convention(["ZZ-9"]) == "unknown"
    assert idfamily.parse_id("ZZ-9", "hex") is None


@pytest.mark.parametrize("convention,text,expected", [
    ("hex", "f7b209", 0xf7b209),
    ("hex", "0xf7b209", 0xf7b209),
    ("dec", "12345678", 12345678),
])
def test_ids_parse(convention, text, expected):
    assert idfamily.parse_id(text, convention) == expected


# -- distance -------------------------------------------------------------

def test_distance_survives_a_carry_boundary():
    """Consecutive IDs across a boundary are adjacent; their XOR is not.

    ...ffff and ...0000 are one apart, which is why distance is plain
    subtraction rather than the bitwise difference.
    """
    a, b = 0x3779ffff, 0x377a0000
    assert idfamily.id_distance(a, b) == 1
    assert (a ^ b) == 0x3ffff, "XOR would have called these unrelated"


def test_the_widest_pair_seen_in_the_field_sets_the_scale():
    """Ford 36c17f56/36c1581c, the loosest genuine pair observed."""
    assert idfamily.id_distance(0x36c17f56, 0x36c1581c) == 10042
    assert 10042 < idfamily.DEFAULT_MAX_DISTANCE


@pytest.mark.parametrize("a,b", [
    ("f7b207", "f7b209"),      # observed Renault pair, differ by 2
    ("d39abf13", "d39abf5a"),  # observed Toyota pair
    ("3779daec", "3779dc0c"),  # observed Ford pair
    ("36c17f56", "36c1581c"),  # the widest observed pair
])
def test_every_pair_observed_in_the_field_is_near(a, b):
    sensors = [_sensor(1, "Ford", a), _sensor(2, "Ford", b)]
    parsed = idfamily._parsed(sensors)
    assert idfamily.are_near(parsed, 1, 2)


def test_unrelated_ids_are_not_near():
    sensors = [_sensor(1, "Ford", "6ccfcf63"), _sensor(2, "Ford", "3779daec")]
    assert not idfamily.are_near(idfamily._parsed(sensors), 1, 2)


def test_ids_from_different_decoders_are_never_near():
    """Two makers' numbering schemes say nothing about each other."""
    sensors = [_sensor(1, "Ford", "f7b207"), _sensor(2, "Renault", "f7b209")]
    assert not idfamily.are_near(idfamily._parsed(sensors), 1, 2)


def test_families_group_transitively():
    sensors = [_sensor(1, "Ford", "3779da00"), _sensor(2, "Ford", "3779db00"),
               _sensor(3, "Ford", "3779dc00"), _sensor(4, "Toyota", "d39abf13")]
    found = idfamily.families(sensors)
    assert len(found) == 1
    assert found[0].members == [1, 2, 3]
    assert found[0].model == "Ford"


# -- the scorecard --------------------------------------------------------

def _capture(service, model, sid, passes, start=1_000_000, slot=0.0):
    for p in range(passes):
        service.ingestor.ingest(
            Reading(model=model, sensor_id=sid, ts=start + p * 600 + slot,
                    rssi=-8.0, snr=12.0, freq_mhz=315.01, pressure_kpa=230.0)
        )


def test_a_tiny_sample_refuses_to_give_a_verdict(db):
    """Two pairs cannot support a percentage, and saying so beats inventing one."""
    card = idfamily.Scorecard(confirmed_pairs=2, confirmed_near=2, apart_pairs=5)
    assert not card.usable
    assert "not enough evidence" in card.verdict()
    assert card.recall == 1.0, "the ratio still computes; it is just not reportable"


def test_a_signal_that_fires_everywhere_is_called_noise():
    card = Scorecard = idfamily.Scorecard(
        confirmed_pairs=10, confirmed_near=9, apart_pairs=100, apart_near=40
    )
    assert card.usable
    assert "too noisy" in card.verdict()


def test_a_signal_that_agrees_and_is_quiet_is_called_strong():
    card = idfamily.Scorecard(
        confirmed_pairs=10, confirmed_near=9, apart_pairs=100, apart_near=2
    )
    assert "strong signal" in card.verdict()


def test_a_signal_that_misses_most_real_pairs_is_called_weak():
    card = idfamily.Scorecard(
        confirmed_pairs=10, confirmed_near=3, apart_pairs=100, apart_near=1
    )
    assert "weak" in card.verdict()
    assert "misses most" in card.verdict()


def test_a_signal_that_finds_most_pairs_is_not_told_it_misses_them():
    """The band between "weak" and "usable". A real capture scored 58% here
    and was told ID proximity missed most of what it had just found -- a claim
    about a majority, made where there was not one."""
    card = idfamily.Scorecard(
        confirmed_pairs=100, confirmed_near=58, apart_pairs=1000, apart_near=5
    )
    assert "misses most" not in card.verdict()
    assert "58%" in card.verdict() and "tie-breaker" in card.verdict()


def test_evaluate_scores_a_real_capture(service_with_history):
    """One ID-near pair heard together repeatedly; one distant pair never was."""
    card = idfamily.evaluate(service_with_history.db, ClusterConfig())
    assert card.confirmed_pairs >= 1
    assert card.confirmed_near == card.confirmed_pairs
    assert card.apart_pairs >= 1, "needs a control sample to mean anything"


@pytest.fixture
def service_with_history(tmp_path):
    from tpms.config import Config
    from tpms.service import Service

    svc = Service(Config(database=str(tmp_path / "ids.db")), start_radio=False)
    # Two wheels of one car, IDs adjacent, heard together on five passes.
    for p in range(5):
        for sid, slot in [("d39abf13", 0.0), ("d39abf5a", 2.0)]:
            _capture(svc, "Toyota", sid, 1, start=1_000_000 + p * 600, slot=slot)
    # A distant sensor from the same decoder, never heard with them.
    _capture(svc, "Toyota", "0104db9c", 3, start=5_000_000)
    svc.ingestor.sweep(when=9e9)
    return svc


def test_recall_and_its_control_come_from_one_population(ingestor, db):
    """A cross-decoder pair is one `are_near` can never call near, whatever
    the distance -- so counting it in the recall denominator measured how many
    cars decode under two protocols, not whether IDs say anything.

    The control has always been same-decoder pairs. A rate and its baseline
    have to be drawn from the same population; on a real capture this alone
    was most of the gap between 46% and the truth.
    """
    for i in range(5):
        _pass(ingestor, "Toyota-TPMS", ["d9f1e496", "d9f1e4a3"], 10_000 + i * 7200)
        _pass(ingestor, "Ford-TPMS", ["36dca165"], 10_000 + i * 7200)

    card = idfamily.evaluate(db, ClusterConfig())
    assert card.confirmed_cross_decoder, "the Ford/Toyota pairs must be set aside"
    assert card.recall == 1.0, "every same-decoder confirmed pair is ID-near"


# -- the density cap ------------------------------------------------------

def _many(model, ids, first_pk=1):
    return [_sensor(pk, model, sensor_id) for pk, sensor_id in enumerate(ids, first_pk)]


def _spread(count, width, digits):
    """`count` ids spread evenly across a space `digits` hex digits wide."""
    step = (16 ** digits) // (count + 1)
    return [format(step * (i + 1), f"0{digits}x") for i in range(count)]


def _limits(sensors, max_distance=65536, limit=0.02):
    return idfamily.thresholds(
        idfamily._parsed(sensors), max_distance, limit,
        ids={s.pk: s.sensor_id for s in sensors},
    )


def test_a_narrow_id_space_is_capped_tighter_than_a_wide_one():
    """65536 was measured on 32-bit IDs. Renault prints six hex digits, so the
    same number covers a 256th of everything it can address: with fifty of them
    heard, a sensor has better than a one in three chance of finding an
    unrelated "neighbour", which is coincidence rather than evidence.
    """
    sensors = _many("Renault", _spread(50, None, 6)) + _many(
        "Toyota", _spread(50, None, 8), first_pk=101
    )
    limits = _limits(sensors)
    assert limits["Toyota"] == 65536, "a wide, sparse space keeps the measured number"
    assert limits["Renault"] == int(0.02 * 16 ** 6 / (2 * 49))
    assert limits["Renault"] < 65536 / 10


def test_the_cap_still_clears_the_widest_genuine_set_observed():
    """Renault 73041b/7309cc, 1457 apart, is one car -- so scaling by ID width
    alone (65536 / 256 = 256) would cut straight through it. The cap is on
    coincidence instead, which leaves room for the sets that exist."""
    sensors = _many("Renault", _spread(48, None, 6) + ["73041b", "7309cc"])
    parsed = idfamily._parsed(sensors)
    a, b = len(sensors) - 1, len(sensors)
    assert idfamily.id_distance(parsed[a][1], parsed[b][1]) == 1457
    assert idfamily.are_near(parsed, a, b, 65536, _limits(sensors))


def test_the_cap_rejects_a_coincidence_the_flat_distance_accepted():
    """Two Renault sensors 20000 apart share nothing but a crowded space."""
    sensors = _many("Renault", _spread(48, None, 6) + ["300000", "304e20"])
    parsed = idfamily._parsed(sensors)
    a, b = len(sensors) - 1, len(sensors)
    assert idfamily.id_distance(parsed[a][1], parsed[b][1]) == 20000
    assert idfamily.are_near(parsed, a, b, 65536), "the flat distance calls these near"
    assert not idfamily.are_near(parsed, a, b, 65536, _limits(sensors))


def test_the_cap_never_scales_consecutive_ids_apart():
    """Contiguous IDs are the strongest form of the whole signal, so a crowded
    space must not be able to throw them away."""
    sensors = _many("Renault", _spread(3000, None, 6) + ["abc123", "abc124"])
    limits = _limits(sensors)
    assert limits["Renault"] == idfamily.MIN_MAX_DISTANCE
    parsed = idfamily._parsed(sensors)
    assert idfamily.are_near(parsed, 3001, 3002, 65536, limits)


def test_the_cap_can_be_turned_off():
    sensors = _many("Renault", _spread(50, None, 6))
    assert _limits(sensors, limit=0.0) == {}
