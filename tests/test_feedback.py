"""Every mutation has to tell the user what it did.

A bare 303 back to the same page is indistinguishable from a click that never
registered, which is what these guard against.
"""

from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from tpms.config import Config
from tpms.service import Service
from tpms.synthetic import generate_lines
from tpms.web.app import ASYNC_HEADER, FLASH_COOKIE, create_app


@pytest.fixture
def client(tmp_path):
    service = Service(Config(database=str(tmp_path / "feedback.db")), start_radio=False)
    service.ingestor.replay(generate_lines())
    service.ingestor.sweep(when=9e9)
    service.recluster()
    yield TestClient(create_app(service)), service


def flash_of(response) -> str:
    raw = response.cookies.get(FLASH_COOKIE)
    assert raw, "mutation set no flash message"
    return unquote(raw)


def test_saving_a_name_says_so(client):
    api, _ = client
    response = api.post(
        "/api/vehicles/1", data={"name": "Blue wagon"}, follow_redirects=False
    )
    assert "Blue wagon" in flash_of(response)


def test_a_wheel_label_names_the_sensor_and_the_label(client):
    api, service = client
    sensor = service.db.sensors_for_vehicle(1)[0]
    response = api.post(
        f"/api/sensors/{sensor.pk}", data={"wheel_label": "FL"}, follow_redirects=False
    )
    message = flash_of(response)
    assert sensor.sensor_id in message and "FL" in message


def test_the_flash_is_shown_once_and_then_cleared(client):
    api, _ = client
    api.post("/api/vehicles/1", data={"name": "Blue wagon"}, follow_redirects=False)
    first = api.get("/vehicles/1")
    assert 'id="flash"' in first.text and "Blue wagon" in first.text
    # The cookie is deleted with the page that showed it, so a refresh is clean.
    assert 'id="flash"' not in api.get("/vehicles/1").text


def test_async_submissions_get_json_instead_of_a_page(client):
    """forms.js handles wheel labels in place; it wants the outcome, not HTML."""
    api, service = client
    sensor = service.db.sensors_for_vehicle(1)[0]
    response = api.post(
        f"/api/sensors/{sensor.pk}",
        data={"wheel_label": "RR"},
        headers={ASYNC_HEADER: "1"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] and body["wheel_label"] == "RR"
    assert FLASH_COOKIE not in response.cookies
    assert service.db.get_sensor(sensor.pk).wheel_label == "RR"


def test_clearing_a_label_is_reported_as_a_change(client):
    api, service = client
    sensor = service.db.sensors_for_vehicle(1)[0]
    api.post(f"/api/sensors/{sensor.pk}", data={"wheel_label": "FL"})
    response = api.post(
        f"/api/sensors/{sensor.pk}",
        data={"wheel_label": ""},
        headers={ASYNC_HEADER: "1"},
    )
    assert "cleared" in response.json()["message"]


def test_slow_actions_report_their_summary(client):
    api, _ = client
    assert "cluster(s)" in flash_of(
        api.post("/api/recluster", follow_redirects=False)
    )
    assert "duplicate group(s)" in flash_of(
        api.post("/api/aliases", follow_redirects=False)
    )


def test_every_mutating_form_declares_a_busy_state(client):
    """Whether it navigates or not, the button must stop looking idle."""
    api, _ = client
    import re

    for path in ("/vehicles", "/vehicles/1", "/sensors", "/sensors/1", "/status"):
        html = api.get(path).text
        for form in re.findall(r"<form[^>]*method=\"post\"[^>]*>", html):
            assert "data-busy" in form or "data-async" in form, (path, form)
