"""Restarting the program from the web UI.

Most settings take effect the moment they are saved -- the components hold the
config object the page mutates. A few cannot: the web server binds its address
once, while the process is starting. For those, saving is only half the job,
and the page has to say so and offer the other half.
"""

import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from tpms import config as cfg
from tpms import service as service_mod
from tpms.service import Service
from tpms.web.app import create_app

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        EXAMPLE.read_text().replace(
            "database: tpms.db", f"database: {tmp_path / 'restart.db'}"
        )
    )
    service = Service(cfg.load_config(path), start_radio=False)
    yield TestClient(create_app(service)), service, path
    service.stop()


@pytest.fixture
def execs(monkeypatch):
    """Catch the exec instead of replacing the test runner with a receiver."""
    calls = []
    monkeypatch.setattr(service_mod.os, "execv", lambda path, argv: calls.append(argv))
    return calls


def _flash(response) -> str:
    return response.cookies.get("tpms_flash", "").replace("%20", " ")


# -- the restart itself -----------------------------------------------------


def test_the_endpoint_re_execs_this_program(client, execs):
    api, service, _ = client
    response = api.post("/api/service/restart", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert "Restart" in _flash(response)

    service._restarter.join(timeout=5)
    assert execs == [[sys.executable, *sys.argv]], "did not re-exec itself"


def test_a_restart_is_an_exec_not_an_exit(client, execs):
    """An exit is a restart under systemd and a shutdown in a terminal. The
    same button must not do two different things on two machines."""
    api, service, _ = client
    api.post("/api/service/restart")
    service._restarter.join(timeout=5)
    assert execs and execs[0][0] == sys.executable


def test_the_dongle_and_the_database_are_released_first(client, execs):
    """The new image claims both within milliseconds of the old one going."""
    api, service, _ = client
    stopped = []
    service.radio.stop = lambda: stopped.append("radio")
    service.db.close = lambda: stopped.append("db")

    api.post("/api/service/restart")
    service._restarter.join(timeout=5)
    assert stopped == ["radio", "db"]
    assert execs, "cleanup must not swallow the restart"


def test_two_clicks_do_not_queue_two_restarts(client, execs):
    api, service, _ = client
    api.post("/api/service/restart")
    api.post("/api/service/restart")
    service._restarter.join(timeout=5)
    assert len(execs) == 1


# -- knowing that one is needed ---------------------------------------------


def test_saving_a_startup_only_setting_says_a_restart_is_needed(client):
    api, service, path = client
    response = api.post(
        "/api/settings", data={"web.port": "9090"}, follow_redirects=False
    )
    assert "Restart the service" in _flash(response)
    assert service.restart_pending == ["web.port"]
    assert yaml.safe_load(path.read_text())["web"]["port"] == 9090, "not written"


def test_the_reminder_outlives_the_page_that_raised_it(client):
    """A flash is gone on the next click; the setting is still not in force."""
    api, _, _ = client
    api.post("/api/settings", data={"web.host": "127.0.0.1"})
    for page in ("/", "/vehicles", "/settings", "/status"):
        body = api.get(page).text
        assert "Restart needed" in body, page
        assert "/api/service/restart" in body, f"{page} states it and offers nothing"


def test_a_setting_that_takes_effect_now_raises_no_reminder(client):
    api, service, _ = client
    api.post("/api/settings", data={"direction.left": "northbound"})
    assert service.restart_pending == []
    assert "Restart needed" not in api.get("/settings").text


def test_restarting_clears_the_reminder(client, execs):
    api, service, _ = client
    api.post("/api/settings", data={"web.port": "9091"})
    assert service.restart_pending

    api.post("/api/service/restart")
    service._restarter.join(timeout=5)
    assert service.restart_pending == []


def test_the_settings_page_offers_both_scopes(client):
    """The receiver is the cheap restart and covers the radio; the service is
    the whole program. Naming only one leaves the other unreachable."""
    body = client[0].get("/settings").text
    assert "/api/radio/restart" in body
    assert "/api/service/restart" in body
