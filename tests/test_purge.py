"""Purging a decoder deletes captured data, so its edges matter.

Excluding a protocol in the config stops new phantom sensors appearing but
leaves everything already recorded; this is the cleanup for that.
"""

import pytest

from tpms.config import Config
from tpms.models import Reading
from tpms.service import Service


@pytest.fixture
def service(tmp_path):
    svc = Service(Config(database=str(tmp_path / "purge.db")), start_radio=False)
    for p in range(4):
        base = 1_000_000 + p * 600
        for model, sid, slot in [
            ("Toyota-TPMS", "aaa", 0),
            ("Toyota-TPMS", "bbb", 1),
            ("Jansite", "ff01", 1),     # duplicate decode of the wheel above
        ]:
            svc.ingestor.ingest(
                Reading(model=model, sensor_id=sid, ts=base + slot * 3.0,
                        rssi=-8.0 - slot * 2.0, snr=12.0, freq_mhz=315.01,
                        pressure_kpa=230.0, battery_ok=1)
            )
    svc.ingestor.sweep(when=9e9)
    svc.detect_aliases()
    svc.recluster()
    return svc


def _models(svc):
    return sorted(s.model for s in svc.db.list_sensors())


def test_purge_removes_the_decoders_sensors_and_leaves_the_rest(service):
    assert "Jansite" in _models(service)
    result = service.purge_decoder("Jansite")

    assert result["counts"]["sensors"] == 1
    assert result["counts"]["readings"] == 4
    assert "Jansite" not in _models(service)
    assert _models(service) == ["Toyota-TPMS", "Toyota-TPMS"], "real sensors survive"


def test_purge_is_case_insensitive_and_matches_substrings(service):
    assert service.purge_decoder("jansite")["counts"]["sensors"] == 1


def test_dry_run_deletes_nothing(service):
    before = len(service.db.list_sensors())
    result = service.purge_decoder("Jansite", dry_run=True)
    assert result["sensors"] == ["Jansite/ff01"]
    assert len(service.db.list_sensors()) == before


def test_a_pattern_matching_nothing_is_harmless(service):
    before = len(service.db.list_sensors())
    result = service.purge_decoder("Nonesuch")
    assert result["counts"]["sensors"] == 0
    assert len(service.db.list_sensors()) == before


def test_purge_cleans_up_the_rows_with_no_foreign_key(service):
    """cooccurrence_seen holds bare sighting ids; nothing cascades to it."""
    orphans_before = service.db.query_one(
        """
        SELECT COUNT(*) AS n FROM cooccurrence_seen
         WHERE sighting_a NOT IN (SELECT pk FROM sightings)
            OR sighting_b NOT IN (SELECT pk FROM sightings)
        """
    )["n"]
    assert orphans_before == 0

    service.purge_decoder("Jansite")

    orphans = service.db.query_one(
        """
        SELECT COUNT(*) AS n FROM cooccurrence_seen
         WHERE sighting_a NOT IN (SELECT pk FROM sightings)
            OR sighting_b NOT IN (SELECT pk FROM sightings)
        """
    )["n"]
    assert orphans == 0, "purge left dangling co-occurrence bookkeeping"


def test_purge_drops_cooccurrence_edges_to_the_deleted_sensors(service):
    service.purge_decoder("Jansite")
    survivors = {s.pk for s in service.db.list_sensors()}
    for row in service.db.cooccurrence_rows():
        assert int(row["a"]) in survivors and int(row["b"]) in survivors


def test_a_vehicle_left_empty_is_removed(tmp_path):
    """A vehicle made only of purged sensors must not linger."""
    svc = Service(Config(database=str(tmp_path / "v.db")), start_radio=False)
    for p in range(4):
        for sid in ("ff01", "ff02"):
            svc.ingestor.ingest(
                Reading(model="Jansite", sensor_id=sid, ts=1_000_000 + p * 600,
                        rssi=-8.0, snr=12.0, freq_mhz=315.01, pressure_kpa=230.0)
            )
    svc.ingestor.sweep(when=9e9)
    svc.recluster()
    assert svc.db.list_vehicles(), "fixture should produce a vehicle to orphan"

    svc.purge_decoder("Jansite")
    assert svc.db.list_sensors() == []
    assert svc.db.list_vehicles() == []


def test_purging_a_canonical_leaves_its_alias_standalone(service):
    """The alias points at a row that is about to vanish."""
    alias = next(s for s in service.db.list_sensors() if s.model == "Jansite")
    canonical = service.db.get_sensor(alias.alias_of)
    assert canonical is not None

    service.purge_decoder("Toyota")
    survivor = next(s for s in service.db.list_sensors() if s.model == "Jansite")
    assert survivor.alias_of is None, "alias_of must not dangle"
