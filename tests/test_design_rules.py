"""Guards for the rules in INFO_DESIGN.md that a template edit could undo.

These are cheap structural checks, not a substitute for looking at the page:
they exist because each one has already been got wrong once.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tpms.config import Config
from tpms.service import Service
from tpms.synthetic import generate_lines
from tpms.web.app import create_app

STATIC = Path(__file__).resolve().parents[1] / "tpms" / "web" / "static"
TEMPLATES = Path(__file__).resolve().parents[1] / "tpms" / "web" / "templates"


@pytest.fixture
def client(tmp_path):
    service = Service(Config(database=str(tmp_path / "design.db")), start_radio=False)
    service.ingestor.replay(generate_lines())
    service.ingestor.sweep(when=9e9)
    service.recluster()
    return TestClient(create_app(service))


def test_no_chart_has_a_second_y_axis():
    """Two scales in one plot invent a correlation out of where they line up.

    Stacked facets with a synced cursor say the same thing honestly.
    """
    sources = list(STATIC.glob("*.js")) + list(TEMPLATES.glob("*.html"))
    offenders = [
        path.name
        for path in sources
        if "axis: 'right'" in path.read_text() or "scale: 'y2'" in path.read_text()
    ]
    assert not offenders


def test_every_chart_carries_a_table_view():
    """The accessible twin, and the relief for series colours under 3:1."""
    chart = (STATIC / "chart.js").read_text()
    assert "chart-table" in chart and "Show as table" in chart


def test_range_controls_live_above_the_charts_they_scope(client):
    """One control row per group, not a set of chips inside each card."""
    for path, holder in (
        ("/", "activity-range"),
        ("/sensors/1", "chart-range"),
        ("/vehicles/1", "chart-range"),
    ):
        assert f'id="{holder}"' in client.get(path).text, path


def test_the_sensors_table_stays_at_ten_columns(client):
    """Eleven columns at 55 rows was the complaint; First heard lives on the
    detail page."""
    import re

    html = client.get("/sensors").text
    head = re.search(r'<table id="sensor-table">.*?</thead>', html, re.S).group(0)
    # (?:\s…)? so the pattern does not also match <thead>.
    headers = re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", head, re.S)
    assert [h.strip() for h in headers] == [
        "Sensor", "Vehicle", "Band", "psi", "&deg;C", "Battery",
        "RSSI", "Readings", "Last heard", "",
    ]


def test_the_sensor_page_has_three_primary_tiles(client):
    """More than ~3 primary values means the screen has more than one job."""
    html = client.get("/sensors/1").text
    assert html.count('class="stats primary"') == 1
    assert html.count("stat secondary") == 5


def test_readings_carry_an_as_of_time(client):
    """A value from somewhere shows when it came from there."""
    assert "as of" in client.get("/sensors/1").text
    assert "data-ts=" in client.get("/").text
