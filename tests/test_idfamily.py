"""Sensor IDs as a correlation signal -- and the honesty of the measurement.

The hypothesis (wheel sets get contiguous IDs) came from eyeballing one
capture, so the scorecard has to be able to say "the sample is too small" and
"this is noise" rather than always producing an encouraging number.
"""

import pytest

from tpms import idfamily
from tpms.config import ClusterConfig
from tpms.models import Reading, Sensor


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
