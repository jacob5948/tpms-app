"""The vehicle pass log, and the sighting log it is built from.

A pass is one vehicle going by; a sighting is one transmitter being heard.
The program is about the former, so the log leads with it -- but the raw
sightings stay a peer view, because matching a car you watched pass to the
transmitters audible at that moment is done against the unmerged rows.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tpms import queries as q
from tpms.config import Config
from tpms.service import Service
from tpms.synthetic import generate_lines
from tpms.web.app import create_app

GAP = 120.0


@pytest.fixture
def service(tmp_path):
    service = Service(Config(database=str(tmp_path / "passes.db")), start_radio=False)
    service.ingestor.replay(generate_lines())
    service.ingestor.sweep(when=9e9)
    service.recluster()
    return service


@pytest.fixture
def client(service):
    return TestClient(create_app(service))


def test_wheels_of_one_vehicle_collapse_into_one_pass(service):
    """Four wheels rolling past are one event, not four."""
    db = service.db
    sightings = q.events(db, limit=5000)
    passes = q.vehicle_passes(db, GAP, limit=5000)

    assert passes, "synthetic data should produce passes"
    assert len(passes) < len(sightings), "nothing was merged"
    merged = [p for p in passes if len(p["sightings"]) > 1]
    assert merged, "no pass merged more than one wheel"


def test_every_sighting_lands_in_exactly_one_pass(service):
    """The merged view must not lose or duplicate the evidence."""
    db = service.db
    sightings = q.events(db, limit=5000)
    passes = q.vehicle_passes(db, GAP, limit=5000)

    seen = [x["pk"] for p in passes for x in p["sightings"]]
    assert len(seen) == len(set(seen)), "a sighting appeared in two passes"
    assert set(seen) == {s["pk"] for s in sightings}


def test_a_pass_never_spans_two_vehicles(service):
    """Grouping happens before merging, or two cars passing together fuse."""
    db = service.db
    for p in q.vehicle_passes(db, GAP, limit=5000):
        if p["vehicle_id"] is None:
            continue
        owners = {
            db.get_sensor(x["sensor_pk"]).vehicle_id for x in p["sightings"]
        }
        assert owners == {p["vehicle_id"]}


def test_a_pass_totals_its_own_sightings(service):
    for p in q.vehicle_passes(service.db, GAP, limit=5000):
        assert p["started_at"] == min(x["started_at"] for x in p["sightings"])
        assert p["reading_count"] == sum(x["reading_count"] for x in p["sightings"])
        assert p["wheels_heard"] == len({x["sensor_pk"] for x in p["sightings"]})


def test_an_unassigned_sensor_still_gets_a_pass(service):
    """Dropping ungrouped wheels would quietly under-count the traffic."""
    db = service.db
    sensor = next(s for s in db.list_sensors() if s.alias_of is None)
    db.set_sensor_vehicle(sensor.pk, None)

    passes = q.vehicle_passes(db, GAP, limit=5000)
    loose = [p for p in passes if p["sensor_pk"] == sensor.pk]
    assert loose, "an unassigned sensor vanished from the log"
    assert loose[0]["vehicle_id"] is None
    assert loose[0]["wheels_known"] is None


def test_the_merge_rule_is_shared_with_the_interval_summary(service):
    """vehicle_intervals and vehicle_passes must agree on where a pass ends."""
    db = service.db
    vehicle = next(v for v in db.list_vehicles() if db.sensors_for_vehicle(v.pk))
    intervals = q.vehicle_intervals(db, vehicle.pk, GAP, limit=5000)
    passes = [
        p
        for p in q.vehicle_passes(db, GAP, vehicle_id=vehicle.pk, limit=5000)
    ]
    assert len(intervals) == len(passes)
    assert [round(i.started_at, 3) for i in intervals] == [
        round(p["started_at"], 3) for p in passes
    ]


def test_log_defaults_to_passes_and_offers_sightings(client):
    body = client.get("/events").text
    assert "Vehicle passes" in body and "Sensor sightings" in body
    assert client.get("/events?view=sightings").status_code == 200


def test_log_can_filter_to_one_sensor(client, service):
    """The drill-down from a sensor's own page, which had no way to exist."""
    pk = service.db.list_sensors()[0].pk
    assert client.get(f"/events?sensor={pk}").status_code == 200
    rows = q.events(service.db, sensor_pk=pk, limit=500)
    assert rows and all(r["sensor_pk"] == pk for r in rows)


def test_a_mistyped_date_is_a_400_not_a_500(client):
    """_parse_when used to raise SystemExit from inside the request handler."""
    assert client.get("/events?since=not-a-date").status_code == 400
    assert client.get("/api/export.csv?since=not-a-date").status_code == 400


def test_csv_follows_the_view_on_screen(client):
    passes = client.get("/api/export.csv?view=passes").text.splitlines()
    sightings = client.get("/api/export.csv?view=sightings").text.splitlines()
    assert "wheels_heard" in passes[0]
    assert "wheels_heard" not in sightings[0]
    assert len(sightings) > len(passes), "sightings should outnumber passes"


def test_truncation_is_reported_rather_than_silent(client):
    body = client.get("/events?limit=10").text
    assert "Showing the most recent" in body


def test_an_unknown_page_renders_the_shell(client):
    """A stale bookmark used to land on raw JSON with no way back."""
    response = client.get("/vehicles/9999")
    assert response.status_code == 404
    assert "TPMS watch" in response.text
    assert "/sensors" in response.text


def test_empty_filters_mean_no_filter(client):
    """The filter form spells out every box, including the ones left blank.

    Submitting it with no vehicle chosen sends ``vehicle=``, which is the log
    unfiltered -- not a malformed request.
    """
    page = client.get("/events?view=passes&since=&until=&vehicle=&sensor=")
    assert page.status_code == 200

    csv_export = client.get("/api/export.csv?view=passes&since=&until=&vehicle=&sensor=")
    assert csv_export.status_code == 200
    assert len(csv_export.text.splitlines()) == len(
        client.get("/api/export.csv?view=passes").text.splitlines()
    )


def test_a_vehicles_own_history_knows_as_much_as_the_log(client, service):
    """The page you open to study one vehicle used to show four columns of a
    pass while the log showed nine, so the closer look was the poorer one."""
    vehicle = next(v for v in service.db.list_vehicles() if service.db.sensors_for_vehicle(v.pk))
    body = client.get(f"/vehicles/{vehicle.pk}").text
    for column in ("Direction", "Wheels", "Readings", "Peak RSSI", "Band"):
        assert f">{column}<" in body, column
    assert "sightings" in body, "no way to open the evidence behind a pass"


def test_the_vehicle_page_counts_the_passes_the_log_counts(client, service):
    """The stat over the table and the rows in it are one query, so 'Passes: 12'
    can never sit above eleven rows."""
    vehicle = next(v for v in service.db.list_vehicles() if service.db.sensors_for_vehicle(v.pk))
    passes = q.vehicle_passes(
        service.db, service.config.sessions.gap_seconds, vehicle_id=vehicle.pk, limit=100
    )
    body = client.get(f"/vehicles/{vehicle.pk}").text
    assert f"{len(passes)} pass" in body


def test_a_pass_heard_on_one_wheel_can_still_be_opened(client, service):
    """The expander appeared only above one sighting, so the pass with the
    least on its row -- one wheel, no fraction to read -- was the one row that
    could not be opened to see which wheel it was."""
    rows = q.vehicle_passes(service.db, GAP, limit=5000)
    single = [p for p in rows if len(p["sightings"]) == 1]
    assert single, "synthetic data should include a one-wheel pass"

    body = client.get("/events").text
    assert "1 sighting\n" in body, "no singular label, so no one-wheel expander"

    assert body.count('class="sub-rows"') == body.count('class="expander"'), (
        "every pass that offers to open has a row to open, and the reverse"
    )


def test_the_expander_reads_as_a_control(client):
    """A bare underlined count sat in a row of counts and said nothing about
    opening. It is a bordered toggle with a caret that turns."""
    body = client.get("/events").text
    assert 'class="expander"' in body
    assert 'aria-expanded="false"' in body
    assert "caret" in body
    css = (
        Path(__file__).resolve().parents[1] / "tpms" / "web" / "static" / "app.css"
    ).read_text()
    assert 'button.expander[aria-expanded="true"] .caret' in css, (
        "nothing tells the reader the open row is open"
    )
