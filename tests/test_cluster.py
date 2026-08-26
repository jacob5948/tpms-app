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


def test_a_long_pass_counts_as_one_vote(ingestor, db):
    """One vehicle sitting in range for a long time is a single vote.

    Without per-sighting dedup, 60 bursts would look like 60 independent
    confirmations and any bystander would be swept in.
    """
    _pass(ingestor, "Toyota-TPMS", ["w1", "w2"], 10_000, bursts=60)
    Clusterer(db).run()
    assert all(row["count"] == 1 for row in db.cooccurrence_rows())


def test_a_long_pass_alone_never_confirms_a_vehicle(ingestor, db):
    """One pass may group provisionally, but must never count as confirmed."""
    _pass(ingestor, "Toyota-TPMS", ["w1", "w2"], 10_000, bursts=60)

    strict = Clusterer(db, ClusterConfig(single_pass=False)).run()
    assert strict.components == []

    lenient = Clusterer(db, ClusterConfig(single_pass=True)).run()
    assert lenient.components == [[1, 2]]
    assert lenient.provisional == [[1, 2]], "a single pass must stay provisional"
    assert all(not edge.confirmed for edge in lenient.edges)
    assert all(v.provisional for v in db.list_vehicles())


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


def test_duplicate_collapse_does_not_split_a_vehicle(ingestor, db):
    """Wheels must stay together when dedup gives them different canonicals.

    From a real capture: three wheels were all decoded as Jansite, and grouped
    correctly. Each was also decoded by a second protocol, so after duplicate
    collapse their canonical models became Citroen, Citroen and Renault --
    and comparing canonical models alone split one car into two.
    """
    from tpms.aliases import AliasDetector

    def burst(when, rssi, snr, decodes):
        for model, sensor_id in decodes:
            ingestor.handle_object(
                {"time": when, "model": model, "type": "TPMS", "id": sensor_id,
                 "rssi": rssi, "snr": snr, "pressure_kPa": 230}
            )

    burst("2026-08-26 02:22:18", -7.0, 9.4, [("Renault", "1f83f9"), ("Jansite", "d94442f")])
    burst("2026-08-26 02:22:26", -9.8, 10.4, [("Citroen", "0f154d9e"), ("Jansite", "c20f154")])
    burst("2026-08-26 02:22:36", -8.1, 11.1, [("Citroen", "0f14dbd2"), ("Jansite", "c20f14d")])

    AliasDetector(db).run()
    report = Clusterer(db).run()

    canonical = [s for s in db.list_sensors() if s.alias_of is None]
    assert len(canonical) == 3, "three transmitters, six decodes"
    assert len(report.components) == 1, "the three wheels are one vehicle"
    assert len(report.components[0]) == 3

    models = {s.model for s in canonical}
    assert len(models) > 1, "the canonicals really do differ, which is the point"


# -- sensor id adjacency ---------------------------------------------------

def _single_pass(ingestor, model, ids_with_rssi, at):
    """One pass only, each sensor at its own signal level."""
    for index, (sensor_id, rssi) in enumerate(ids_with_rssi):
        ingestor.ingest(
            Reading(model=model, sensor_id=sensor_id, ts=at + index * 0.4, rssi=rssi)
        )


def test_adjacent_ids_group_a_car_whose_wheels_differ_in_signal(ingestor, db):
    """Wheels on opposite sides of a car can exceed the RSSI spread.

    The signal test alone would leave these two apart; near-consecutive ids
    are the stronger evidence when both are available.
    """
    _single_pass(ingestor, "Renault", [("f7b207", -6.0), ("f7b209", -24.0)], 10_000)
    config = ClusterConfig(single_pass_rssi_spread=10.0)
    Clusterer(db, config).run()
    assert list(_groups(db).values()) == [["f7b207", "f7b209"]]


def test_the_same_pair_stays_apart_with_id_adjacency_off(ingestor, db):
    _single_pass(ingestor, "Renault", [("f7b207", -6.0), ("f7b209", -24.0)], 10_000)
    Clusterer(db, ClusterConfig(id_adjacency=False, single_pass_rssi_spread=10.0)).run()
    assert _groups(db) == {}


def test_distant_ids_are_unaffected(ingestor, db):
    """Adjacency adds groupings; it must not rescue a pair nothing else likes."""
    _single_pass(ingestor, "Renault", [("f7b207", -6.0), ("1d3e98", -24.0)], 10_000)
    Clusterer(db, ClusterConfig(single_pass_rssi_spread=10.0)).run()
    assert _groups(db) == {}


def test_adjacent_ids_never_group_sensors_never_heard_together(ingestor, db):
    """Id proximity is a tie-breaker on co-occurrence, never a substitute.

    Without this, every wheel set on the same production run would merge into
    one vehicle regardless of where or when it was heard.
    """
    ingestor.ingest(Reading(model="Renault", sensor_id="f7b207", ts=10_000, rssi=-6.0))
    ingestor.ingest(Reading(model="Renault", sensor_id="f7b209", ts=900_000, rssi=-6.0))
    Clusterer(db).run()
    assert _groups(db) == {}, "these were never audible at the same time"


def test_a_cluster_spanning_several_id_families_is_flagged(ingestor, db):
    """Three cars that passed together look like one six-wheeled vehicle."""
    _single_pass(ingestor, "Ford", [
        ("3779daec", -8.0), ("3779dc0c", -8.4),      # car one
        ("36c17f56", -8.2), ("36c1581c", -8.6),      # car two
    ], 10_000)
    report = Clusterer(db).run()
    assert report.mixed_families, "a mixed cluster should be flagged for review"
    flagged = [v for v in db.list_vehicles() if v.needs_review]
    assert flagged, "the flag has to reach the vehicle row the UI reads"


def test_one_family_is_not_flagged(ingestor, db):
    _single_pass(ingestor, "Ford", [
        ("3779daec", -8.0), ("3779db00", -8.4), ("3779dc0c", -8.2),
    ], 10_000)
    report = Clusterer(db).run()
    assert not report.mixed_families
    assert not [v for v in db.list_vehicles() if v.needs_review]
