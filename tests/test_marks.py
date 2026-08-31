"""Confirming what actually went past, and what the program does with it.

Every other fact in this program came off the radio. These are the ones that
came from a person watching, which makes them the only evidence in here that
can settle an argument -- so they must be easy to enter, correctable, and
impossible to lose to a settings change.
"""

import pytest
from fastapi.testclient import TestClient

from tpms import queries as q
from tpms.config import Config, DirectionConfig
from tpms.direction import LEFT, RIGHT
from tpms.service import Service
from tpms.synthetic import generate_lines
from tpms.web.app import create_app

GAP = 120.0


@pytest.fixture
def client(tmp_path):
    config = Config(database=str(tmp_path / "marks.db"))
    config.direction = DirectionConfig(left="entering", right="exiting")
    service = Service(config, start_radio=False)
    service.ingestor.replay(generate_lines())
    service.ingestor.sweep(when=9e9)
    service.recluster()
    yield TestClient(create_app(service)), service
    service.stop()


def a_vehicle(service):
    return next(
        v for v in service.db.list_vehicles() if len(service.db.sensors_for_vehicle(v.pk)) >= 2
    )


def passes_of(service, vehicle_pk):
    return q.vehicle_passes(service.db, GAP, vehicle_id=vehicle_pk, limit=500)


# -- entering a confirmation -------------------------------------------------


def test_a_pass_can_be_confirmed_and_corrected_and_cleared(client):
    api, service = client
    vehicle = a_vehicle(service)
    anchor = passes_of(service, vehicle.pk)[0]["anchor"]

    api.post(f"/api/passes/{anchor}/mark", data={"side": "left"})
    assert passes_of(service, vehicle.pk)[0]["confirmed"] == LEFT

    api.post(f"/api/passes/{anchor}/mark", data={"side": "right"})
    assert passes_of(service, vehicle.pk)[0]["confirmed"] == RIGHT

    api.post(f"/api/passes/{anchor}/mark", data={"side": ""})
    assert passes_of(service, vehicle.pk)[0]["confirmed"] is None


def test_a_confirmation_is_named_the_way_the_page_names_directions(client):
    """The user's own word for that side, not "left" -- they told the config
    what traffic on each side is doing, and this is the same fact."""
    api, service = client
    anchor = passes_of(service, a_vehicle(service).pk)[0]["anchor"]
    response = api.post(
        f"/api/passes/{anchor}/mark", data={"side": "left"}, follow_redirects=False
    )
    assert "entering" in response.cookies.get("tpms_flash", "").replace("%20", " ")


def test_nonsense_is_refused_rather_than_stored(client):
    api, service = client
    anchor = passes_of(service, a_vehicle(service).pk)[0]["anchor"]
    assert api.post(f"/api/passes/{anchor}/mark", data={"side": "north"}).status_code == 400
    assert api.post("/api/passes/999999/mark", data={"side": "left"}).status_code == 404


def test_a_confirmation_survives_the_join_gap_changing(client):
    """A pass is not a row -- it is however many sightings the current gap
    merges. Someone's eyewitness answer must not evaporate because a setting
    re-sliced the traffic around it."""
    api, service = client
    vehicle = a_vehicle(service)
    before = passes_of(service, vehicle.pk)[0]
    api.post(f"/api/passes/{before['anchor']}/mark", data={"side": "left"})

    # A far longer gap: several passes become one.
    merged = q.vehicle_passes(service.db, 86400.0, vehicle_id=vehicle.pk, limit=500)
    assert len(merged) < len(passes_of(service, vehicle.pk)), "the gap did not re-slice"
    assert merged[0]["confirmed"] == LEFT, "the confirmation was lost in the merge"


# -- what the pages do with it -----------------------------------------------


def test_both_pass_tables_offer_the_confirmation(client):
    api, service = client
    vehicle = a_vehicle(service)
    for path in ("/events", f"/vehicles/{vehicle.pk}"):
        body = api.get(path).text
        assert "unconfirmed" in body, path
        assert "/mark" in body, path
        assert ">entering<" in body or "entering" in body, path


def test_the_guess_is_marked_against_what_was_seen(client):
    """The Direction column is only worth reading on passes nobody confirmed,
    and how far to trust it there is exactly what the agreements say."""
    api, service = client
    vehicle = a_vehicle(service)
    for sensor in service.db.sensors_for_vehicle(vehicle.pk):
        service.db.execute(
            "UPDATE sensors SET wheel_label = 'FL' WHERE pk = ?", (sensor.pk,)
        )
    first = passes_of(service, vehicle.pk)[0]
    assert first["heading"] is not None and first["heading"].side == LEFT

    api.post(f"/api/passes/{first['anchor']}/mark", data={"side": "left"})
    assert "heading matched" in api.get(f"/vehicles/{vehicle.pk}").text.replace("\n", " ")

    api.post(f"/api/passes/{first['anchor']}/mark", data={"side": "right"})
    assert "missed" in api.get(f"/vehicles/{vehicle.pk}").text


def test_the_sides_panel_is_served_on_its_own_for_refreshing(client):
    api, service = client
    vehicle = a_vehicle(service)
    body = api.get(f"/api/vehicles/{vehicle.pk}/sides").text
    assert 'id="sides-panel"' in body
    assert api.get("/api/vehicles/999999/sides").status_code == 404


# -- applying what it worked out ---------------------------------------------


def _confirm_by_side(api, service, vehicle_pk):
    """Invent a vehicle whose sides are knowable, and confirm every pass of it.

    Synthetic traffic hears all four wheels on nearly every pass at whatever
    level the generator felt like, so it carries no sides to find. This splits
    the sensors in two, alternates which half faced the receiver, writes the
    levels that would follow, and confirms each pass accordingly -- the shape
    of the real thing: an eyewitness answer per pass, and the radio's own
    record of what it heard.
    """
    sensors = service.db.sensors_for_vehicle(vehicle_pk)
    left_group = {s.pk for s in sensors[: len(sensors) // 2]}
    marked = 0
    for index, one in enumerate(passes_of(service, vehicle_pk)):
        side = "left" if index % 2 else "right"
        near = left_group if side == "left" else {s.pk for s in sensors} - left_group
        for sighting in one["sightings"]:
            level = -30.0 if int(sighting["sensor_pk"]) in near else -50.0
            service.db.execute(
                "UPDATE sightings SET max_rssi = ? WHERE pk = ?",
                (level, int(sighting["pk"])),
            )
        api.post(f"/api/passes/{one['anchor']}/mark", data={"side": side})
        marked += 1
    return left_group, marked


def test_applying_the_proposal_labels_the_wheels(client):
    api, service = client
    vehicle = a_vehicle(service)
    left_group, marked = _confirm_by_side(api, service, vehicle.pk)
    assert marked >= 6, "not enough confirmations to propose anything"

    response = api.post(f"/api/vehicles/{vehicle.pk}/apply-sides", follow_redirects=False)
    assert response.status_code in (302, 303)

    labels = {s.pk: s.wheel_label for s in service.db.sensors_for_vehicle(vehicle.pk)}
    placed = [pk for pk, label in labels.items() if label]
    assert placed, "nothing was labelled"
    for pk in placed:
        assert labels[pk] == ("L" if pk in left_group else "R"), labels


def test_applying_twice_changes_nothing_the_second_time(client):
    api, service = client
    vehicle = a_vehicle(service)
    _confirm_by_side(api, service, vehicle.pk)
    api.post(f"/api/vehicles/{vehicle.pk}/apply-sides")

    response = api.post(
        f"/api/vehicles/{vehicle.pk}/apply-sides", follow_redirects=False
    )
    assert "Nothing to change" in response.cookies.get("tpms_flash", "").replace("%20", " ")


def test_applying_keeps_the_front_or_rear_half_of_a_corner_label(client):
    api, service = client
    vehicle = a_vehicle(service)
    left_group, _ = _confirm_by_side(api, service, vehicle.pk)
    # Every wheel called a front-left corner; the sides are about to disagree
    # with half of them, and "front" is not theirs to overturn.
    for sensor in service.db.sensors_for_vehicle(vehicle.pk):
        service.db.execute(
            "UPDATE sensors SET wheel_label = 'FL' WHERE pk = ?", (sensor.pk,)
        )
    api.post(f"/api/vehicles/{vehicle.pk}/apply-sides")

    for sensor in service.db.sensors_for_vehicle(vehicle.pk):
        assert sensor.wheel_label in ("FL", "FR"), sensor.wheel_label
        if sensor.pk not in left_group:
            assert sensor.wheel_label == "FR"


def test_nothing_is_labelled_without_enough_confirmations(client):
    """The button that changes the data must not appear on two passes'
    worth of evidence."""
    api, service = client
    vehicle = a_vehicle(service)
    anchor = passes_of(service, vehicle.pk)[0]["anchor"]
    api.post(f"/api/passes/{anchor}/mark", data={"side": "left"})

    api.post(f"/api/vehicles/{vehicle.pk}/apply-sides")
    assert all(
        s.wheel_label is None for s in service.db.sensors_for_vehicle(vehicle.pk)
    )
    assert "Apply these sides" not in api.get(f"/vehicles/{vehicle.pk}").text
