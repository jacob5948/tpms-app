"""Duplicate-decode detection.

Several rtl_433 TPMS decoders match the same RF burst with different framing,
so one transmitter appears two or three times under different protocols and
IDs. Because those phantoms co-occur perfectly, they cluster into vehicles
that do not exist -- so they must be collapsed before correlation runs.

The cases below are taken from a real capture.
"""

import pytest

from tpms.aliases import AliasDetector, common_hex_run
from tpms.cluster import Clusterer
from tpms.config import AliasConfig


def _burst(ingestor, when, rssi, snr, decodes):
    """One RF burst seen by several decoders: identical signal, same instant."""
    for model, sensor_id in decodes:
        ingestor.handle_object(
            {
                "time": when,
                "model": model,
                "type": "TPMS",
                "id": sensor_id,
                "pressure_kPa": 230,
                "rssi": rssi,
                "snr": snr,
            }
        )


def _canonical(db):
    return sorted(s.display for s in db.list_sensors() if s.alias_of is None)


@pytest.mark.parametrize(
    "decodes",
    [
        [("Jansite-TPMS", "6cd2eb3"), ("Ford-TPMS", "6cd2eb33")],
        [("Jansite-TPMS", "20c728a"), ("Hyundai-VDO", "c728ad2d")],
        [("Jansite-TPMS", "c20f14d"), ("Citroen-TPMS", "0f14dbd2")],
        # No shared hex at all -- ID matching alone would miss this one.
        [("Jansite-TPMS", "d94442f"), ("Renault-TPMS", "1f83f9")],
    ],
)
def test_same_burst_under_two_decoders_is_one_sensor(ingestor, db, decodes):
    _burst(ingestor, "2026-08-26 02:27:37", -8.1, 12.3, decodes)
    AliasDetector(db).run()
    assert len(_canonical(db)) == 1


def test_wheels_of_one_car_are_not_treated_as_duplicates(ingestor, db):
    """Four wheels transmit at nearly the same time but at different levels."""
    for index, (sensor_id, rssi, snr) in enumerate(
        [("aa01", -11.2, 18.1), ("aa02", -13.4, 17.0),
         ("aa03", -9.8, 19.2), ("aa04", -12.1, 16.5)]
    ):
        _burst(ingestor, f"2026-08-26 02:27:3{index}", rssi, snr,
               [("Toyota-TPMS", sensor_id)])
    AliasDetector(db).run()
    assert len(_canonical(db)) == 4


def test_identical_level_at_a_different_time_is_not_a_duplicate(ingestor, db):
    _burst(ingestor, "2026-08-26 02:00:00", -8.1, 12.3, [("Ford-TPMS", "a1")])
    _burst(ingestor, "2026-08-26 05:00:00", -8.1, 12.3, [("Jansite-TPMS", "b2")])
    AliasDetector(db).run()
    assert len(_canonical(db)) == 2


def test_same_instant_but_different_level_is_not_a_duplicate(ingestor, db):
    _burst(ingestor, "2026-08-26 02:27:37", -8.1, 12.3, [("Ford-TPMS", "a1")])
    _burst(ingestor, "2026-08-26 02:27:37", -21.4, 5.0, [("Jansite-TPMS", "b2")])
    AliasDetector(db).run()
    assert len(_canonical(db)) == 2


def test_the_busiest_decoder_becomes_canonical(ingestor, db):
    """Keep the decoder that heard the most: it has the fullest history."""
    for index in range(4):
        _burst(ingestor, f"2026-08-26 02:27:3{index}", -8.1, 12.3,
               [("Ford-TPMS", "6cd2eb33"), ("Jansite-TPMS", "6cd2eb3")])
    # One extra burst that only Ford decodes.
    _burst(ingestor, "2026-08-26 02:27:39", -8.2, 12.4, [("Ford-TPMS", "6cd2eb33")])
    AliasDetector(db).run()
    assert _canonical(db) == ["Ford-TPMS/6cd2eb33"]


def test_duplicates_never_form_a_vehicle(ingestor, db):
    """The failure this whole module exists to prevent.

    Two aliases co-occur perfectly by construction, so without collapsing them
    they would cluster into a vehicle made of one physical sensor.
    """
    for index in range(5):
        _burst(ingestor, f"2026-08-26 0{index}:10:00", -8.1, 12.3,
               [("Ford-TPMS", "6cd2eb33"), ("Jansite-TPMS", "6cd2eb3")])

    AliasDetector(db).run()
    report = Clusterer(db).run()
    assert report.components == []
    assert db.list_vehicles() == []
    assert len(report.skipped_aliases) == 1


def test_aliases_lose_any_vehicle_assignment(ingestor, db):
    for index in range(3):
        _burst(ingestor, f"2026-08-26 0{index}:10:00", -8.1, 12.3,
               [("Ford-TPMS", "aaa"), ("Jansite-TPMS", "aa")])
    vehicle = db.create_vehicle(1000.0)
    for sensor in db.list_sensors():
        db.set_sensor_vehicle(sensor.pk, vehicle)

    AliasDetector(db).run()
    aliases = [s for s in db.list_sensors() if s.alias_of is not None]
    assert aliases and all(s.vehicle_id is None for s in aliases)


def test_detection_is_stable_across_repeated_runs(ingestor, db):
    for index in range(3):
        _burst(ingestor, f"2026-08-26 0{index}:10:00", -8.1, 12.3,
               [("Ford-TPMS", "aaa"), ("Jansite-TPMS", "aa")])
    detector = AliasDetector(db)
    detector.run()
    before = _canonical(db)
    second = detector.run()
    assert _canonical(db) == before
    assert second.linked == 0, "a settled database must not keep re-linking"


def test_dry_run_changes_nothing(ingestor, db):
    _burst(ingestor, "2026-08-26 02:27:37", -8.1, 12.3,
           [("Ford-TPMS", "aaa"), ("Jansite-TPMS", "aa")])
    report = AliasDetector(db).run(dry_run=True)
    assert report.groups
    assert all(s.alias_of is None for s in db.list_sensors())


def test_readings_without_signal_data_are_never_matched(ingestor, db):
    """No -M level means no fingerprint; guessing would be worse than nothing."""
    for model, sensor_id in [("Ford-TPMS", "a1"), ("Jansite-TPMS", "b2")]:
        ingestor.handle_object(
            {"time": "2026-08-26 02:27:37", "model": model, "type": "TPMS",
             "id": sensor_id, "pressure_kPa": 230}
        )
    AliasDetector(db).run()
    assert len(_canonical(db)) == 2


def test_threshold_can_demand_repeated_evidence(ingestor, db):
    _burst(ingestor, "2026-08-26 02:27:37", -8.1, 12.3,
           [("Ford-TPMS", "aaa"), ("Jansite-TPMS", "aa")])
    strict = AliasDetector(db, AliasConfig(min_shared_bursts=3)).run()
    assert strict.groups == []


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("6cd2eb3", "6cd2eb33", "6cd2eb3"),
        ("20c728a", "c728ad2d", "c728a"),
        ("c20f14d", "0f14dbd2", "0f14d"),
        ("d94442f", "1f83f9", ""),      # genuinely unrelated hex
        ("ab", "abcd", ""),             # too short to mean anything
    ],
)
def test_common_hex_run(a, b, expected):
    assert common_hex_run(a, b) == expected
