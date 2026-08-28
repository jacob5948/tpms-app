import pytest
from fastapi.testclient import TestClient

from tpms.config import Config
from tpms.service import Service
from tpms.synthetic import generate_lines
from tpms.web.app import create_app


@pytest.fixture
def client(tmp_path):
    service = Service(Config(database=str(tmp_path / "web.db")), start_radio=False)
    service.ingestor.replay(generate_lines())
    service.ingestor.sweep(when=9e9)
    service.recluster()
    yield TestClient(create_app(service)), service


@pytest.mark.parametrize(
    "path",
    ["/", "/vehicles", "/vehicles/1", "/sensors", "/events", "/status",
     "/api/heard-now", "/api/status", "/api/export.csv", "/events?since=7d&vehicle=1"],
)
def test_pages_render(client, path):
    response = client[0].get(path)
    assert response.status_code == 200


def test_missing_vehicle_is_404(client):
    assert client[0].get("/vehicles/9999").status_code == 404


def test_csv_export_has_a_header_and_rows(client):
    """The download follows whichever view is on screen.

    It used to always be sensor sightings over a different row limit than the
    page, so the file and the table disagreed about the same window.
    """
    body = client[0].get("/api/export.csv").text
    lines = body.strip().splitlines()
    assert lines[0].startswith("vehicle,sensor,first_heard,last_heard,duration_s,wheels_heard")
    assert len(lines) > 1

    body = client[0].get("/api/export.csv?view=sightings").text
    lines = body.strip().splitlines()
    assert lines[0].startswith("vehicle,sensor,wheel,first_heard,last_heard")
    assert len(lines) > 1


def test_naming_a_vehicle_marks_it_manual(client):
    api, service = client
    api.post("/api/vehicles/1", data={"name": "Blue wagon"}, follow_redirects=False)
    vehicle = service.db.get_vehicle(1)
    assert vehicle.name == "Blue wagon"
    assert not vehicle.auto_generated


def test_moving_a_sensor_pins_it(client):
    """A manual move that clustering could undo on the next pass is a bug."""
    api, service = client
    sensor = service.db.sensors_for_vehicle(1)[0]
    api.post(
        f"/api/sensors/{sensor.pk}", data={"vehicle_id": "new"}, follow_redirects=False
    )
    moved = service.db.get_sensor(sensor.pk)
    assert moved.pinned
    assert moved.vehicle_id != 1

    service.recluster()
    assert service.db.get_sensor(sensor.pk).vehicle_id == moved.vehicle_id


def test_merge_combines_two_vehicles(client):
    api, service = client
    before = len(service.db.sensors_for_vehicle(1)) + len(service.db.sensors_for_vehicle(2))
    api.post("/api/vehicles/1/merge", data={"other": 2}, follow_redirects=False)
    assert len(service.db.sensors_for_vehicle(1)) == before
    assert service.db.get_vehicle(2) is None


def test_wheel_label_round_trips(client):
    api, service = client
    sensor = service.db.sensors_for_vehicle(1)[0]
    api.post(f"/api/sensors/{sensor.pk}", data={"wheel_label": "FL"}, follow_redirects=False)
    assert service.db.get_sensor(sensor.pk).wheel_label == "FL"


def test_times_are_shown_in_the_configured_zone(tmp_path):
    """Stamps are display, not storage: readings stay epoch seconds, and the
    zone decides only how they are written back out."""
    from tpms.config import Config
    from tpms.models import to_iso

    Config(database=str(tmp_path / "tz.db"), timezone="UTC")
    assert to_iso(1756400000) == "2025-08-28T16:53:20Z"

    Config(database=str(tmp_path / "tz.db"), timezone="America/Chicago")
    assert to_iso(1756400000) == "2025-08-28T11:53:20-05:00"


def test_a_date_typed_into_a_filter_is_read_in_that_zone(tmp_path):
    """Otherwise "since 2026-08-01" starts six hours off the midnight the
    same page prints, and the reader has no way to see why."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from tpms.config import Config
    from tpms.models import parse_when

    Config(database=str(tmp_path / "tz.db"), timezone="America/Chicago")
    midnight = datetime(2026, 8, 1, tzinfo=ZoneInfo("America/Chicago")).timestamp()
    assert parse_when("2026-08-01") == midnight
    # An explicit offset still wins: it says what it means.
    assert parse_when("2026-08-01T00:00:00Z") != midnight


def test_an_unknown_timezone_is_refused(tmp_path):
    from tpms.config import Config

    with pytest.raises(ValueError):
        Config(database=str(tmp_path / "tz.db"), timezone="Mars/Olympus")
