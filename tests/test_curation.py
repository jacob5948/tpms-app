"""The correction loop: the clusterer guesses, the human corrects it.

Every check here is a control that either did not exist or did the wrong
thing, and each one is the sort of bug that is invisible until a grouping you
fixed silently comes undone.
"""

import pytest
from fastapi.testclient import TestClient

from tpms import queries as q
from tpms.config import Config
from tpms.service import Service
from tpms.synthetic import generate_lines
from tpms.web.app import create_app


@pytest.fixture
def client(tmp_path):
    service = Service(Config(database=str(tmp_path / "curation.db")), start_radio=False)
    service.ingestor.replay(generate_lines())
    service.ingestor.sweep(when=9e9)
    service.recluster()
    return TestClient(create_app(service)), service


def _vehicle_with_several_sensors(db):
    for vehicle in db.list_vehicles():
        members = db.sensors_for_vehicle(vehicle.pk)
        if len(members) >= 3:
            return vehicle, members
    pytest.skip("synthetic data produced no multi-sensor vehicle")


# -- the wheel-label bug ----------------------------------------------------


def test_labelling_a_wheel_does_not_move_or_pin_it(client):
    """The form used to submit the label and the vehicle together, and the
    handler branched on the vehicle field being *present* rather than changed.
    Typing "FL" therefore pinned the sensor out of the clusterer's reach and
    reported a move that never happened."""
    api, service = client
    sensor = service.db.list_sensors()[0]
    assert not sensor.pinned

    api.post(f"/api/sensors/{sensor.pk}", data={"wheel_label": "FL"},
             follow_redirects=False)

    after = service.db.get_sensor(sensor.pk)
    assert after.wheel_label == "FL"
    assert after.vehicle_id == sensor.vehicle_id
    assert not after.pinned


def test_resubmitting_the_same_vehicle_is_not_a_move(client):
    api, service = client
    sensor = next(s for s in service.db.list_sensors() if s.vehicle_id)

    response = api.post(
        f"/api/sensors/{sensor.pk}",
        data={"wheel_label": "FR", "vehicle_id": str(sensor.vehicle_id)},
        headers={"x-tpms-async": "1"},
    )

    assert "moved" not in response.json()["message"]
    assert not service.db.get_sensor(sensor.pk).pinned


def test_a_real_move_still_pins(client):
    """The pin is what makes a manual placement survive the next run."""
    api, service = client
    sensor = next(s for s in service.db.list_sensors() if s.vehicle_id)
    other = next(v for v in service.db.list_vehicles() if v.pk != sensor.vehicle_id)

    api.post(f"/api/sensors/{sensor.pk}", data={"vehicle_id": str(other.pk)},
             follow_redirects=False)

    after = service.db.get_sensor(sensor.pk)
    assert after.vehicle_id == other.pk
    assert after.pinned


# -- unpinning, which had no UI at all --------------------------------------


def test_a_sensor_can_be_unpinned(client):
    api, service = client
    sensor = service.db.list_sensors()[0]
    api.post(f"/api/sensors/{sensor.pk}", data={"pinned": "1"}, follow_redirects=False)
    assert service.db.get_sensor(sensor.pk).pinned

    api.post(f"/api/sensors/{sensor.pk}", data={"pinned": "0"}, follow_redirects=False)
    assert not service.db.get_sensor(sensor.pk).pinned


def test_the_pin_control_is_actually_rendered(client):
    """The handler has always worked; no template offered it, so a manual
    placement could never be handed back to the clusterer."""
    api, service = client
    pk = service.db.list_sensors()[0].pk
    assert 'name="pinned"' in api.get(f"/sensors/{pk}").text


# -- splitting --------------------------------------------------------------


def test_splitting_moves_exactly_the_selected_sensors(client):
    api, service = client
    vehicle, members = _vehicle_with_several_sensors(service.db)
    chosen = [s.pk for s in members[:2]]
    kept = [s.pk for s in members[2:]]

    api.post(f"/api/vehicles/{vehicle.pk}/split",
             data={"sensor": [str(pk) for pk in chosen]}, follow_redirects=False)

    target = service.db.get_sensor(chosen[0]).vehicle_id
    assert target != vehicle.pk
    assert all(service.db.get_sensor(pk).vehicle_id == target for pk in chosen)
    assert all(service.db.get_sensor(pk).vehicle_id == vehicle.pk for pk in kept)
    assert all(service.db.get_sensor(pk).pinned for pk in chosen)


def test_a_split_survives_the_next_clustering_run(client):
    """A correction the clusterer undoes on its next pass is not a correction."""
    api, service = client
    vehicle, members = _vehicle_with_several_sensors(service.db)
    chosen = [s.pk for s in members[:2]]

    api.post(f"/api/vehicles/{vehicle.pk}/split",
             data={"sensor": [str(pk) for pk in chosen]}, follow_redirects=False)
    target = service.db.get_sensor(chosen[0]).vehicle_id
    service.recluster()

    assert all(service.db.get_sensor(pk).vehicle_id == target for pk in chosen)


def test_splitting_clears_the_review_flag(client):
    api, service = client
    vehicle, members = _vehicle_with_several_sensors(service.db)
    service.db.execute(
        "UPDATE vehicles SET needs_review = 1 WHERE pk = ?", (vehicle.pk,)
    )

    api.post(f"/api/vehicles/{vehicle.pk}/split",
             data={"sensor": [str(members[0].pk)]}, follow_redirects=False)

    assert not service.db.get_vehicle(vehicle.pk).needs_review


@pytest.mark.parametrize("payload", [{}, {"sensor": []}])
def test_splitting_nothing_is_refused(client, payload):
    api, service = client
    vehicle, _ = _vehicle_with_several_sensors(service.db)
    assert api.post(f"/api/vehicles/{vehicle.pk}/split", data=payload).status_code == 400


def test_splitting_everything_is_refused(client):
    """Moving every sensor is a rename, not a split, and would strand the
    vehicle's name and notes on an empty shell that then gets deleted."""
    api, service = client
    vehicle, members = _vehicle_with_several_sensors(service.db)
    response = api.post(
        f"/api/vehicles/{vehicle.pk}/split",
        data={"sensor": [str(s.pk) for s in members]},
    )
    assert response.status_code == 400


# -- moving in bulk ---------------------------------------------------------


def test_moving_the_ticked_sensors_to_another_vehicle(client):
    """The row control was a select carrying every vehicle in the program,
    repeated on every row, defaulting to "stay here" -- a non-action. One
    picker under the table does the same job for any number of sensors."""
    api, service = client
    vehicle, members = _vehicle_with_several_sensors(service.db)
    other = next(v for v in service.db.list_vehicles() if v.pk != vehicle.pk)
    chosen = [s.pk for s in members[:2]]

    api.post(f"/api/vehicles/{vehicle.pk}/move",
             data={"sensor": [str(pk) for pk in chosen], "target": str(other.pk)},
             follow_redirects=False)

    assert all(service.db.get_sensor(pk).vehicle_id == other.pk for pk in chosen)
    # Pinned for the same reason a split is: otherwise clustering undoes it.
    assert all(service.db.get_sensor(pk).pinned for pk in chosen)


def test_moving_sensors_to_no_vehicle_unassigns_them(client):
    api, service = client
    vehicle, members = _vehicle_with_several_sensors(service.db)

    api.post(f"/api/vehicles/{vehicle.pk}/move",
             data={"sensor": str(members[0].pk), "target": "none"},
             follow_redirects=False)

    assert service.db.get_sensor(members[0].pk).vehicle_id is None


def test_moving_the_last_sensor_off_a_vehicle_lands_somewhere_that_exists(client):
    """The vehicle is deleted once it is empty, so the page the request came
    from is gone -- following the referer back would land on a 404."""
    api, service = client
    vehicle, members = _vehicle_with_several_sensors(service.db)
    other = next(v for v in service.db.list_vehicles() if v.pk != vehicle.pk)

    response = api.post(
        f"/api/vehicles/{vehicle.pk}/move",
        data={"sensor": [str(s.pk) for s in members], "target": str(other.pk)},
        headers={"referer": f"http://testserver/vehicles/{vehicle.pk}"},
        follow_redirects=False,
    )

    assert service.db.get_vehicle(vehicle.pk) is None
    assert response.headers["location"] == f"/vehicles/{other.pk}"


@pytest.mark.parametrize("payload", [
    {"target": "1"},                     # nothing ticked
    {"sensor": [], "target": "1"},
])
def test_moving_nothing_is_refused(client, payload):
    api, service = client
    vehicle, _ = _vehicle_with_several_sensors(service.db)
    assert api.post(f"/api/vehicles/{vehicle.pk}/move", data=payload).status_code == 400


def test_moving_without_a_destination_is_refused(client):
    """The picker opens on "Move to..." -- a prompt, not a destination."""
    api, service = client
    vehicle, members = _vehicle_with_several_sensors(service.db)
    response = api.post(f"/api/vehicles/{vehicle.pk}/move",
                        data={"sensor": str(members[0].pk), "target": ""})
    assert response.status_code == 400
    assert service.db.get_sensor(members[0].pk).vehicle_id == vehicle.pk


def test_moving_sensors_where_they_already_are_is_refused(client):
    api, service = client
    vehicle, members = _vehicle_with_several_sensors(service.db)
    response = api.post(f"/api/vehicles/{vehicle.pk}/move",
                        data={"sensor": str(members[0].pk), "target": str(vehicle.pk)})
    assert response.status_code == 400


def test_one_tick_serves_every_bulk_action(client):
    """Split and move read the same checkboxes, from one form."""
    html = client[0].get("/vehicles/1").text
    body = html.split('<form method="post"')[1].split("</form>")[0]
    assert body.count('name="sensor"') >= 1
    assert "/split" in body and "/move" in body
    assert "stay here" not in html, "a control whose default does nothing"


# -- guards that used to be 500s --------------------------------------------


@pytest.mark.parametrize("data", [{}, {"other": ""}, {"other": "banana"}])
def test_a_malformed_merge_is_a_400(client, data):
    assert client[0].post("/api/vehicles/1/merge", data=data).status_code == 400


def test_merging_a_vehicle_into_itself_is_refused(client):
    assert client[0].post("/api/vehicles/1/merge", data={"other": "1"}).status_code == 400


def test_clearing_review_on_a_missing_vehicle_is_a_404(client):
    """It used to silently no-op, so a typo looked like a success."""
    assert client[0].post("/api/vehicles/9999/review-cleared").status_code == 404


# -- hiding ------------------------------------------------------------------


def test_hiding_removes_a_sensor_from_the_lists_but_not_the_record(client):
    api, service = client
    db = service.db
    sensor = next(s for s in db.list_sensors() if s.alias_of is None)

    api.post(f"/api/sensors/{sensor.pk}", data={"ignored": "1"},
             follow_redirects=False)

    assert db.get_sensor(sensor.pk).ignored
    assert sensor.pk not in [r["pk"] for r in q.sensor_rows(db)]
    assert sensor.pk not in [r["sensor_pk"] for r in q.heard_now(db)]
    # Still entirely present as a record.
    assert api.get(f"/sensors/{sensor.pk}").status_code == 200
    assert sensor.pk in [r["pk"] for r in q.sensor_rows(db, include_ignored=True)]


def test_a_hidden_sensor_is_never_clustered(client):
    """A resident transmitter is audible while everything drives past, so
    letting it cluster would undo the point of hiding it."""
    api, service = client
    sensor = next(s for s in service.db.list_sensors() if s.alias_of is None)

    api.post(f"/api/sensors/{sensor.pk}", data={"ignored": "1"},
             follow_redirects=False)
    report = service.recluster()

    assert sensor.pk in report.skipped_ignored
    assert service.db.get_sensor(sensor.pk).vehicle_id is None


def test_hiding_is_reversible(client):
    api, service = client
    sensor = next(s for s in service.db.list_sensors() if s.alias_of is None)

    api.post(f"/api/sensors/{sensor.pk}", data={"ignored": "1"}, follow_redirects=False)
    api.post(f"/api/sensors/{sensor.pk}", data={"ignored": "0"}, follow_redirects=False)

    assert not service.db.get_sensor(sensor.pk).ignored
    assert sensor.pk in [r["pk"] for r in q.sensor_rows(service.db)]


def test_hidden_sensors_are_rendered_so_they_can_be_recovered(client):
    """They are held back by the filter, not omitted -- otherwise the only way
    to un-hide one would be a URL you had to already know."""
    api, service = client
    sensor = next(s for s in service.db.list_sensors() if s.alias_of is None)
    api.post(f"/api/sensors/{sensor.pk}", data={"ignored": "1"}, follow_redirects=False)

    body = api.get("/sensors").text
    assert 'data-hidden-unless="hidden"' in body
    assert f'/sensors/{sensor.pk}' in body


def test_asking_for_a_hidden_sensor_by_name_still_answers(client):
    """Hidden means kept out of the lists you browse, not out of the answers
    you ask for -- the link into the log comes from the sensor's own page."""
    api, service = client
    sensor = next(s for s in service.db.list_sensors() if s.alias_of is None)
    api.post(f"/api/sensors/{sensor.pk}", data={"ignored": "1"}, follow_redirects=False)

    assert api.get(f"/events?sensor={sensor.pk}").status_code == 200
    assert q.events(service.db, sensor_pk=sensor.pk, include_ignored=True)


def test_vehicle_dropdowns_are_sorted_by_name(client):
    """A pick-list is scanned by eye, so it goes A-Z rather than by insertion
    order, with the still-unnamed vehicles gathered at the end."""
    api, service = client
    for name in ("zeta", "Alpha", "middle"):
        service.db.create_vehicle(name=name, created_at=0.0, auto_generated=False)
    service.db.create_vehicle(name=None, created_at=0.0, auto_generated=True)

    body = api.get("/events").text
    order = [body.index(f">{name}<") for name in ("Alpha", "middle", "zeta")]
    assert order == sorted(order), "named vehicles list alphabetically"
    assert body.index("Unnamed vehicle") > order[-1], "unnamed ones come last"


# -- the provisional flag ---------------------------------------------------


def test_naming_a_vehicle_clears_provisional(client):
    """"Provisional" means clustering grouped these from a single pass and has
    not seen them together since. Naming is a person saying otherwise -- and
    it used to be the one act that made the flag permanent, because it also
    takes the vehicle out of the reach of the code that rewrites the flag."""
    api, service = client
    vehicle, _ = _vehicle_with_several_sensors(service.db)
    service.db.execute("UPDATE vehicles SET provisional = 1 WHERE pk = ?", (vehicle.pk,))

    api.post(f"/api/vehicles/{vehicle.pk}", data={"name": "Red Estate"},
             follow_redirects=False)

    assert not service.db.get_vehicle(vehicle.pk).provisional
    service.recluster()
    assert not service.db.get_vehicle(vehicle.pk).provisional, "and it stays cleared"


def test_pinning_every_wheel_clears_provisional(client):
    """The same statement as a name, made one wheel at a time."""
    api, service = client
    vehicle, members = _vehicle_with_several_sensors(service.db)
    service.db.execute("UPDATE vehicles SET provisional = 1 WHERE pk = ?", (vehicle.pk,))
    for sensor in members[:-1]:
        service.db.execute("UPDATE sensors SET pinned = 1 WHERE pk = ?", (sensor.pk,))

    service.recluster()
    assert service.db.get_vehicle(vehicle.pk).provisional, "one wheel is still loose"

    service.db.execute(
        "UPDATE sensors SET pinned = 1 WHERE pk = ?", (members[-1].pk,)
    )
    service.recluster()
    assert not service.db.get_vehicle(vehicle.pk).provisional


def test_an_untouched_grouping_stays_provisional(client):
    """The flag is still worth something: a guess clustering made and still
    owns keeps it until a later pass corroborates the grouping."""
    api, service = client
    vehicle, members = _vehicle_with_several_sensors(service.db)
    service.db.execute(
        "UPDATE vehicles SET provisional = 1, auto_generated = 1, name = NULL "
        "WHERE pk = ?", (vehicle.pk,)
    )
    service.db.execute(
        "UPDATE sensors SET pinned = 0 WHERE vehicle_id = ?", (vehicle.pk,)
    )

    sensors = {s.pk: s for s in service.db.list_sensors()}
    service.clusterer._release(sensors, {s.pk for s in members}, manual=set())

    assert service.db.get_vehicle(vehicle.pk).provisional
