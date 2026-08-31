"""Which way a vehicle was pointing, from the wheels that were heard.

This is the one part of the program that guesses. Everything else reports what
the receiver decoded; this reasons from an absence -- the wheels that were
*not* heard -- so the rules about when it declines to answer matter more than
the rules about when it does.
"""

import pytest
from fastapi.testclient import TestClient

from tpms import direction
from tpms import queries as q
from tpms.config import Config, DirectionConfig, load_config
from tpms.service import Service
from tpms.synthetic import generate_lines
from tpms.web.app import create_app


def _service(tmp_path, **direction_kwargs):
    config = Config(database=str(tmp_path / "direction.db"))
    config.direction = DirectionConfig(**direction_kwargs)
    service = Service(config, start_radio=False)
    service.ingestor.replay(generate_lines())
    service.ingestor.sweep(when=9e9)
    service.recluster()
    return service


@pytest.fixture
def client(tmp_path):
    service = _service(tmp_path)
    yield TestClient(create_app(service)), service
    service.stop()


@pytest.fixture
def client_named(tmp_path):
    service = _service(tmp_path, left="northbound", right="southbound")
    yield TestClient(create_app(service)), service
    service.stop()


# -- reading a label --------------------------------------------------------


def test_the_canonical_labels_place_a_wheel_on_a_side():
    for label in ("FL", "RL", "L"):
        assert direction.side_of(label) == direction.LEFT, label
    for label in ("FR", "RR", "R"):
        assert direction.side_of(label) == direction.RIGHT, label


def test_a_side_only_label_is_a_first_class_answer():
    """The point of L and R: a sensor is often known to be on one side long
    before anyone works out front from rear, and side is all direction needs.
    Without these, recording a known side means guessing FL over RL."""
    assert direction.side_of("L") == direction.LEFT
    assert direction.side_of("R") == direction.RIGHT


def test_labels_are_read_the_way_people_type_them():
    """The field is free text -- the datalist offers, it does not enforce."""
    for label in ("fl", " FL ", "front left", "Front-Left", "front_left"):
        assert direction.side_of(label) == direction.LEFT, label


def test_a_label_naming_no_side_is_not_an_error():
    """An unrecognised label is a wheel whose side is unknown, which is a
    thing the caller handles, not a thing that should raise."""
    for label in (None, "", "   ", "spare", "trailer", "???"):
        assert direction.side_of(label) is None, label


def test_the_spare_never_votes():
    """A spare is a real wheel with a real sensor and no side at all. Letting
    it vote would put a car's direction on the one wheel not on the road."""
    assert direction.side_of("spare") is None
    assert direction.infer([("spare", -40.0)]) is None


# -- inferring a side -------------------------------------------------------


def test_wheels_all_on_one_side_name_that_side():
    heading = direction.infer([("FR", -40.0), ("RR", -42.0)])
    assert heading is not None
    assert heading.side == direction.RIGHT
    assert heading.firm, "nothing heard contradicts it"


def test_one_labelled_side_plus_unlabelled_wheels_is_not_firm():
    """The unlabelled wheel is the whole risk: if it were on the other side,
    the answer would be the other side. Report it, but not as a fact."""
    heading = direction.infer([("FR", -40.0), (None, -41.0)])
    assert heading is not None and heading.side == direction.RIGHT
    assert not heading.firm
    assert "unlabelled" in heading.basis


def test_nothing_labelled_says_nothing():
    """The common case before any curation. A coin flip dressed as a finding
    is worse than an empty cell."""
    assert direction.infer([(None, -40.0), (None, -44.0)]) is None
    assert direction.infer([]) is None


def test_both_sides_heard_falls_back_to_which_was_louder():
    """At close range every wheel is audible. The near side is the louder one,
    because the far side has the vehicle between it and the antenna."""
    heading = direction.infer(
        [("FL", -70.0), ("FR", -40.0)], rssi_margin=6.0
    )
    assert heading is not None
    assert heading.side == direction.RIGHT
    assert not heading.firm, "a level comparison is a guess, not a reading"
    assert "stronger" in heading.basis


def test_both_sides_at_similar_strength_declines_to_answer():
    """A single reading's level swings a few dB on nothing at all, so a
    narrow win is noise. Answering anyway is how a guess becomes a lie."""
    assert direction.infer([("FL", -41.0), ("FR", -40.0)], rssi_margin=6.0) is None


def test_the_margin_is_the_thing_that_decides():
    wheels = [("FL", -50.0), ("FR", -40.0)]
    assert direction.infer(wheels, rssi_margin=6.0).side == direction.RIGHT
    assert direction.infer(wheels, rssi_margin=20.0) is None


def test_both_sides_with_no_levels_says_nothing():
    """Nothing to compare. A decoder that reports no level must not silently
    resolve to whichever side happened to sort first."""
    assert direction.infer([("FL", None), ("FR", None)]) is None


# -- naming it --------------------------------------------------------------


def test_an_unconfigured_side_still_reports_the_side():
    """The honest half of the answer. The radio knows which side faced it;
    only the reader knows which way that points."""
    heading = direction.infer([("FR", -40.0)])
    assert heading.name({}) == "right side"
    assert heading.name(None) == "right side"


def test_a_configured_name_replaces_the_side():
    heading = direction.infer([("FR", -40.0)])
    assert heading.name({"left": "northbound", "right": "southbound"}) == "southbound"


def test_naming_one_side_leaves_the_other_reporting_its_side():
    heading = direction.infer([("FL", -40.0)])
    assert heading.name({"right": "southbound"}) == "left side"


# -- the config -------------------------------------------------------------


def test_direction_names_default_to_unset(tmp_path):
    """The program must never invent which way the road runs."""
    assert Config().direction.left is None
    assert Config().direction.right is None


def test_direction_is_configurable(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(
        "direction:\n  left: northbound\n  right: southbound\n  rssi_margin: 9\n"
    )
    config = load_config(path)
    assert config.direction.names == {"left": "northbound", "right": "southbound"}
    assert config.direction.rssi_margin == 9


def test_the_names_map_is_what_the_templates_are_handed():
    config = DirectionConfig(left="up the hill")
    assert config.names == {"left": "up the hill", "right": None}


# -- through the log --------------------------------------------------------


def test_the_log_shows_a_direction_once_wheels_are_labelled(client):
    """End to end: label the wheels on one side, and the pass that heard only
    those wheels says which way it went."""
    api, service = client
    db = service.db
    vehicle = db.list_vehicles()[0]
    sensors = db.sensors_for_vehicle(vehicle.pk)

    # Nothing is labelled yet, so the column is there and empty.
    assert "Direction" in api.get("/events").text

    for sensor, label in zip(sensors, ("FR", "RR", "FL", "RL")):
        db.execute(
            "UPDATE sensors SET wheel_label = ? WHERE pk = ?", (label, sensor.pk)
        )

    rows = q.vehicle_passes(db, service.config.sessions.gap_seconds)
    assert any(r["heading"] is not None for r in rows), "no pass got a heading"


def test_the_export_carries_the_direction_it_showed(client):
    """The screen and the CSV are one view, so a column the log shows is a
    column the export has."""
    api, service = client
    sensors = service.db.sensors_for_vehicle(service.db.list_vehicles()[0].pk)
    for sensor in sensors:
        service.db.execute(
            "UPDATE sensors SET wheel_label = 'FR' WHERE pk = ?", (sensor.pk,)
        )
    body = api.get("/api/export.csv?view=passes").text
    assert "direction" in body.splitlines()[0]
    assert "right side" in body


def test_a_configured_name_reaches_the_log(client_named):
    """The name in config.yaml is what the table says."""
    api, service = client_named
    sensors = service.db.sensors_for_vehicle(service.db.list_vehicles()[0].pk)
    for sensor in sensors:
        service.db.execute(
            "UPDATE sensors SET wheel_label = 'FR' WHERE pk = ?", (sensor.pk,)
        )
    assert "southbound" in api.get("/events").text


# -- through the vehicle page -----------------------------------------------


def test_the_vehicle_page_shows_each_pass_direction(client_named):
    """The pass history is the same passes as the log, so it answers the same
    question. Reading a vehicle's history to see which way it comes and goes
    is the whole point of labelling its wheels."""
    api, service = client_named
    vehicle = service.db.list_vehicles()[0]
    for sensor in service.db.sensors_for_vehicle(vehicle.pk):
        service.db.execute(
            "UPDATE sensors SET wheel_label = 'FR' WHERE pk = ?", (sensor.pk,)
        )

    body = api.get(f"/vehicles/{vehicle.pk}").text
    assert "Direction" in body
    assert "southbound" in body


def test_the_vehicle_page_and_the_log_read_one_pass_log(client):
    """Not two answers kept in step: the page filters the log's passes to this
    vehicle, so a heading cannot differ between the two views."""
    api, service = client
    db = service.db
    vehicle = db.list_vehicles()[0]
    for sensor, label in zip(db.sensors_for_vehicle(vehicle.pk), ("FR", "RR", "FL")):
        db.execute("UPDATE sensors SET wheel_label = ? WHERE pk = ?", (label, sensor.pk))

    gap = service.config.sessions.gap_seconds
    margin = service.config.direction.rssi_margin
    passes = q.vehicle_passes(
        db, gap, vehicle_id=vehicle.pk, limit=100, rssi_margin=margin
    )
    assert passes, "no passes to show"

    body = api.get(f"/vehicles/{vehicle.pk}").text
    for shown in passes:
        if shown["heading"]:
            assert shown["heading"].name({}) in body
