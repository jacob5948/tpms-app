"""Correlation is the part most likely to be quietly wrong, so it is tested
against a scenario with a deliberate trap in it."""

from tpms.cluster import Clusterer
from tpms.config import ClusterConfig
from tpms.models import Reading
from tpms.synthetic import generate_lines


def _pass(ingestor, model, ids, at, bursts=3, step=40):
    for burst in range(bursts):
        for index, sensor_id in enumerate(ids):
            ingestor.ingest(
                Reading(model=model, sensor_id=sensor_id, ts=at + burst * step + index * 0.4)
            )


def _groups(db):
    out = {}
    for vehicle in db.list_vehicles():
        out[vehicle.pk] = sorted(s.sensor_id for s in db.sensors_for_vehicle(vehicle.pk))
    return out


def test_wheels_of_one_car_cluster_together(ingestor, db):
    for i in range(5):
        _pass(ingestor, "Toyota-TPMS", ["w1", "w2", "w3", "w4"], 10_000 + i * 7200)
    Clusterer(db).run()
    assert list(_groups(db).values()) == [["w1", "w2", "w3", "w4"]]


def test_two_cars_stay_separate(ingestor, db):
    for i in range(5):
        _pass(ingestor, "Toyota-TPMS", ["a1", "a2", "a3", "a4"], 10_000 + i * 7200)
        _pass(ingestor, "Ford-TPMS", ["b1", "b2", "b3", "b4"], 13_000 + i * 7200)
    Clusterer(db).run()
    groups = sorted(_groups(db).values())
    assert groups == [["a1", "a2", "a3", "a4"], ["b1", "b2", "b3", "b4"]]


def test_occasional_companion_is_not_absorbed(ingestor, db):
    """The trap: a sensor that drives past alongside the car twice.

    Raw co-occurrence counting would merge it; the support threshold must not.
    """
    for i in range(8):
        _pass(ingestor, "Toyota-TPMS", ["w1", "w2", "w3", "w4"], 10_000 + i * 7200)
    for i in (2, 5):
        _pass(ingestor, "Toyota-TPMS", ["w1", "w2", "w3", "w4"], 500_000 + i * 7200)
        _pass(ingestor, "Schrader-TPMS", ["decoy"], 500_000 + i * 7200)
    Clusterer(db).run()

    decoy = next(s for s in db.list_sensors() if s.sensor_id == "decoy")
    assert decoy.vehicle_id is None
    assert ["w1", "w2", "w3", "w4"] in _groups(db).values()


def test_a_long_pass_cannot_manufacture_an_edge(ingestor, db):
    """One vehicle sitting in range for a long time is a single vote.

    Without per-sighting dedup, 60 bursts would look like 60 independent
    confirmations and any bystander would be swept in.
    """
    _pass(ingestor, "Toyota-TPMS", ["w1", "w2"], 10_000, bursts=60)
    report = Clusterer(db).run()
    rows = db.cooccurrence_rows()
    assert all(row["count"] == 1 for row in rows)
    assert report.components == []


def test_oversized_cluster_is_flagged_for_review(ingestor, db):
    ids = [f"w{i}" for i in range(8)]
    for i in range(5):
        _pass(ingestor, "Toyota-TPMS", ids, 10_000 + i * 7200)
    report = Clusterer(db, ClusterConfig(max_cluster_size=6)).run()
    assert report.oversized
    assert any(v.needs_review for v in db.list_vehicles())


def test_pinned_sensors_are_never_reassigned(ingestor, db):
    for i in range(5):
        _pass(ingestor, "Toyota-TPMS", ["w1", "w2", "w3", "w4"], 10_000 + i * 7200)
    Clusterer(db).run()

    sensor = next(s for s in db.list_sensors() if s.sensor_id == "w4")
    manual = db.create_vehicle(1000.0, auto_generated=False, name="Hand placed")
    db.set_sensor_vehicle(sensor.pk, manual)
    db.execute("UPDATE sensors SET pinned = 1 WHERE pk = ?", (sensor.pk,))

    Clusterer(db).run()
    assert db.get_sensor(sensor.pk).vehicle_id == manual


def test_named_vehicles_are_left_alone(ingestor, db):
    for i in range(5):
        _pass(ingestor, "Toyota-TPMS", ["w1", "w2", "w3", "w4"], 10_000 + i * 7200)
    Clusterer(db).run()
    vehicle_pk = db.list_vehicles()[0].pk
    db.execute(
        "UPDATE vehicles SET name = 'Blue wagon', auto_generated = 0 WHERE pk = ?",
        (vehicle_pk,),
    )
    report = Clusterer(db).run()
    assert len(report.skipped_manual) == 4
    assert _groups(db)[vehicle_pk] == ["w1", "w2", "w3", "w4"]


def test_clustering_is_stable_across_repeated_runs(ingestor, db):
    for i in range(5):
        _pass(ingestor, "Toyota-TPMS", ["w1", "w2", "w3", "w4"], 10_000 + i * 7200)
    Clusterer(db).run()
    before = _groups(db)
    for _ in range(3):
        Clusterer(db).run()
    assert _groups(db) == before, "reclustering must not churn vehicle identities"


def test_full_synthetic_scenario(ingestor, db):
    ingestor.replay(generate_lines())
    ingestor.sweep(when=9e9)
    Clusterer(db).run()
    groups = sorted(_groups(db).values())
    assert groups == [
        ["1a2b01", "1a2b02", "1a2b03", "1a2b04"],
        ["ff0a01", "ff0a02", "ff0a03", "ff0a04"],
    ]
    assert next(s for s in db.list_sensors() if s.sensor_id == "dec0y1").vehicle_id is None
