"""Editing the config from the UI.

This page writes the file the program is configured by, so the tests that
matter are the ones about what happens when it should *not* write: a value the
running program would not accept must never reach disk, or the next start
reads a config that cannot be loaded.
"""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from tpms import config as cfg
from tpms.service import Service
from tpms.synthetic import generate_lines
from tpms.web.app import create_app

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"


@pytest.fixture
def client(tmp_path):
    """A real config file on disk, pointed at a scratch database."""
    path = tmp_path / "config.yaml"
    text = EXAMPLE.read_text().replace(
        "database: tpms.db", f"database: {tmp_path / 'settings.db'}"
    )
    path.write_text(text)
    config = cfg.load_config(path)
    service = Service(config, start_radio=False)
    service.ingestor.replay(generate_lines())
    service.ingestor.sweep(when=9e9)
    service.recluster()
    yield TestClient(create_app(service)), service, path
    service.stop()


def _saved(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


# -- the page ---------------------------------------------------------------


def test_every_setting_is_on_the_page(client):
    """The form is generated from the dataclasses, so a key added to the
    config cannot be quietly missing from the page that edits it."""
    api, service, _ = client
    html = api.get("/settings").text
    for setting in cfg.settings(service.config):
        assert f'set-{setting.path}"' in html, setting.path


def test_the_database_is_shown_but_not_editable(client):
    """Every component was handed a connection to the old file at startup, so
    a box here would change where the next run looks and nothing about this
    one. A control that silently does nothing is worse than no control."""
    api, service, path = client
    html = api.get("/settings").text
    assert 'name="database"' not in html
    assert str(service.config.database_path) in html

    before = _saved(path)["database"]
    api.post("/api/settings", data={"database": "/tmp/somewhere-else.db"})
    assert _saved(path)["database"] == before
    assert str(service.config.database_path) != "/tmp/somewhere-else.db"


# -- saving -----------------------------------------------------------------


def test_a_save_reaches_the_live_process_and_the_file(client):
    api, service, path = client
    api.post("/api/settings", data={"direction.left": "northbound"})
    assert service.config.direction.left == "northbound"
    assert _saved(path)["direction"]["left"] == "northbound"


def test_a_saved_setting_takes_effect_without_a_restart(client):
    """The whole point, and the bug this page invites.

    `gap`, `direction_names` and `rssi_margin` were once read once when the
    app was built and closed over. That was harmless while only a restart
    could change a config, and became a bug the moment this page could: the
    value would be written to disk, adopted by the service, and still not
    reach the log. Nothing caches a setting.
    """
    api, service, _ = client
    for sensor in service.db.sensors_for_vehicle(service.db.list_vehicles()[0].pk):
        service.db.execute(
            "UPDATE sensors SET wheel_label = 'FR' WHERE pk = ?", (sensor.pk,)
        )
    assert "right side" in api.get("/events").text

    api.post("/api/settings", data={"direction.right": "southbound"})
    assert "southbound" in api.get("/events").text, "the log still reads the old config"


def test_the_timezone_takes_effect_without_a_restart(client):
    """The zone is global state set when a Config is built, and nothing
    rebuilds one here -- so a save has to put it into effect itself."""
    api, service, _ = client
    api.post("/api/settings", data={"timezone": "UTC"})
    assert "UTC" in api.get("/status").text
    assert 'TPMS_TZ = "UTC"' in api.get("/vehicles/1").text


def test_a_checkbox_can_be_cleared(client):
    """An unticked box posts nothing at all, so without the companion field
    "off" and "not on this form" are the same request and a checkbox is a
    one-way switch."""
    api, service, path = client
    assert service.config.retention.vacuum is True

    api.post("/api/settings", data={"seen:retention.vacuum": "1"})
    assert service.config.retention.vacuum is False
    assert _saved(path)["retention"]["vacuum"] is False

    api.post(
        "/api/settings",
        data={"seen:retention.vacuum": "1", "retention.vacuum": "1"},
    )
    assert service.config.retention.vacuum is True


def test_a_list_setting_round_trips(client):
    api, service, path = client
    api.post("/api/settings", data={"radio.frequencies": "315M, 433.92M"})
    assert service.config.radio.frequencies == ["315M", "433.92M"]
    assert _saved(path)["radio"]["frequencies"] == ["315M", "433.92M"]


def test_a_radio_change_says_it_needs_a_restart(client):
    """The receiver reads these when it starts, so a save that changed one and
    said nothing would look like it had done nothing."""
    api, _, _ = client
    response = api.post(
        "/api/settings", data={"radio.gain": "35"}, follow_redirects=False
    )
    assert "Restart" in response.cookies.get("tpms_flash", "").replace("%20", " ")


def test_saving_nothing_says_so(client):
    api, service, _ = client
    response = api.post(
        "/api/settings",
        data={"timezone": service.config.timezone},
        follow_redirects=False,
    )
    assert "Nothing to change" in response.cookies["tpms_flash"].replace("%20", " ")


# -- refusing ---------------------------------------------------------------


def test_a_bad_value_never_reaches_the_file(client):
    """Everything is parsed before anything is assigned, so a config the
    program would not load cannot be half-written to disk."""
    api, service, path = client
    before = path.read_text()
    gap_before = service.config.sessions.gap_seconds

    response = api.post(
        "/api/settings",
        data={"sessions.gap_seconds": "not-a-number", "timezone": "UTC"},
        follow_redirects=False,
    )
    assert path.read_text() == before, "the file was touched by a refused save"
    assert service.config.sessions.gap_seconds == gap_before
    assert service.config.timezone != "UTC", "a later field was applied anyway"
    assert response.cookies.get("tpms_flash_kind") == "err"


def test_a_refusal_names_the_box(client):
    """Forty boxes on one page, and "invalid literal for int()" identifies
    none of them."""
    api, _, _ = client
    response = api.post(
        "/api/settings",
        data={"sessions.gap_seconds": "abc"},
        follow_redirects=False,
    )
    assert "gap seconds" in response.cookies["tpms_flash"].replace("%20", " ")


def test_a_required_value_cannot_be_emptied(client):
    api, _, _ = client
    response = api.post(
        "/api/settings", data={"timezone": ""}, follow_redirects=False
    )
    assert "cannot be empty" in response.cookies["tpms_flash"].replace("%20", " ")


def test_an_optional_value_can_be_emptied(client):
    """`gain: null` is a real setting -- it means automatic."""
    api, service, path = client
    api.post("/api/settings", data={"radio.gain": "35"})
    assert service.config.radio.gain == 35
    api.post("/api/settings", data={"radio.gain": ""})
    assert service.config.radio.gain is None
    assert _saved(path)["radio"]["gain"] is None


# -- the file it writes -----------------------------------------------------


def test_the_written_file_loads_back_unchanged(client):
    """The round trip that matters: what this page writes is what the next
    start reads."""
    api, service, path = client
    api.post(
        "/api/settings",
        data={"timezone": "UTC", "direction.left": "northbound", "radio.gain": "35"},
    )
    reloaded = cfg.load_config(path)
    assert cfg.to_dict(reloaded) == cfg.to_dict(service.config)


def test_the_written_file_still_explains_itself(client):
    """config.yaml is hand-edited, so a rewrite that emitted bare values would
    strip the reason from every knob in it. The prose lives beside the
    defaults and is re-emitted on every save."""
    api, _, path = client
    api.post("/api/settings", data={"timezone": "UTC"})
    text = path.read_text()
    assert "# IANA name." in text
    assert "# The receiver itself." in text, "section commentary too"
    assert text.count("#") > 40, "a file this size should be well annotated"


def test_the_previous_version_is_kept(client):
    """Comments are regenerated on every save, so an accidental save is
    otherwise unrecoverable."""
    api, _, path = client
    before = path.read_text()
    api.post("/api/settings", data={"timezone": "UTC"})
    backup = path.with_suffix(path.suffix + ".bak")
    assert backup.exists()
    assert backup.read_text() == before


def test_the_dump_round_trips_as_data():
    """Independent of the web layer: what dump writes, yaml reads back."""
    config = cfg.load_config(EXAMPLE)
    data = cfg.to_dict(config)
    assert yaml.safe_load(cfg.dump(data)) == data


def test_where_the_config_came_from_is_not_a_setting():
    """base_dir and source_path describe the file, not what it says. A Path in
    the dump also cannot be represented as YAML, so leaking one turns every
    save into a 500 -- which is exactly what it did."""
    config = cfg.load_config(EXAMPLE)
    paths = {s.path for s in cfg.settings(config)}
    assert not (paths & {"base_dir", "source_path"})
    assert "source_path" not in cfg.to_dict(config)
    cfg.dump(cfg.to_dict(config))  # must not raise
