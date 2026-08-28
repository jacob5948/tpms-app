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


def test_the_sensors_table_columns(client):
    """First heard was cut from this table as on-demand detail, and came back:
    "what turned up while I was out" is a question you ask of the whole list,
    and you cannot ask it on a page that shows one sensor."""
    import re

    html = client.get("/sensors").text
    head = re.search(r'<table id="sensor-table">.*?</thead>', html, re.S).group(0)
    # (?:\s…)? so the pattern does not also match <thead>.
    headers = re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", head, re.S)
    assert [h.strip() for h in headers] == [
        "Sensor", "Vehicle", "Band", "psi", "&deg;C", "Battery",
        "RSSI", "Readings", "First heard", "Last heard", "",
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


# -- vocabulary and structure ------------------------------------------------


def test_the_vocabulary_is_two_words_not_four(client):
    """A pass belongs to a vehicle, a sighting to a sensor.

    The same object was once "Events" in the nav, "Sightings" on the sensor
    page and "Appearances" on the vehicle page, with three different column
    sets -- so one thing read as three unrelated things.
    """
    assert "Appearances" not in client.get("/vehicles/1").text
    assert "Passes" in client.get("/vehicles/1").text
    assert "Sightings" in client.get("/sensors/1").text


def test_the_log_offers_both_views_as_peers(client):
    """The raw sightings are a tab beside the passes, not a hidden detail:
    matching a car you watched go past is done against unmerged rows."""
    html = client.get("/events").text
    assert 'href="/events?view=passes' in html
    assert 'href="/events?view=sightings' in html
    assert "Vehicle passes" in html and "Sensor sightings" in html


def test_the_merge_rule_has_one_implementation():
    """Two copies would let the vehicle page and the log disagree about
    where one pass ends and the next begins."""
    source = (Path(__file__).resolve().parents[1] / "tpms" / "queries.py").read_text()
    assert source.count("def merge_runs(") == 1
    # Both consumers go through it rather than re-deriving the rule.
    assert source.count("merge_runs(") >= 3


def test_detail_pages_link_into_the_log(client):
    """Both used to be dead ends: the only way to a vehicle's log was to go to
    the log and re-pick it from a dropdown."""
    assert "/events?vehicle=1" in client.get("/vehicles/1").text
    assert "/events?sensor=1" in client.get("/sensors/1").text


def test_timestamps_use_one_idiom(client):
    """Relative to read, ISO to hover, epoch to sort -- via m.when()."""
    macros = (TEMPLATES / "_macros.html").read_text()
    assert "{% macro when(" in macros
    for path in ("/events?view=sightings", "/sensors/1", "/vehicles/1"):
        html = client.get(path).text
        assert "ago<" in html or "ago " in html or "ago\n" in html, path


def test_reshaping_a_vehicle_asks_first(client):
    """Merge and split reparent every sensor involved and cannot be undone in
    one click. Labelling a wheel must not ask, because it changes nothing."""
    html = client.get("/vehicles/1").text
    assert html.count("data-confirm=") >= 2
    assert "data-confirm" not in html.split('id="wheel-form-')[1][:200]


def test_the_review_queue_is_the_tiles(client):
    """A flag with no way to gather it is a flag nobody acts on."""
    html = client.get("/vehicles").text
    assert 'data-filter-flag="review"' in html
    assert 'data-filter-flag="provisional"' in html
    assert 'data-filter-row' in html, "the cards must be filterable rows"


def test_every_filter_toggle_names_the_list_it_filters(client):
    """Two filterable lists on one page must not drive each other."""
    import re

    for path in ("/sensors", "/vehicles"):
        html = client.get(path).text
        for match in re.finditer(r'data-filter-flag="[^"]+"([^>]*)>', html):
            assert "data-filter-target=" in match.group(1), path


def test_no_toggle_is_declared_without_a_row_that_can_answer_it(client):
    """The sensors table carried data-resident on every row with no tile that
    could ever activate it -- dead weight that looked like a feature."""
    import re

    html = client.get("/sensors").text
    flags = set(re.findall(r'data-filter-flag="([^"]+)"', html))
    attrs = set(re.findall(r'data-(\w+)="[01]"', html))
    # Row-level state the page renders but offers no way to select by.
    orphans = attrs & {"resident", "new", "unassigned", "duplicates", "present"} - flags
    assert not orphans, f"{orphans} can never be switched on"


def test_errors_render_the_page_shell(client):
    """A stale bookmark used to land on raw FastAPI JSON with no way back."""
    response = client.get("/vehicles/9999")
    assert response.status_code == 404
    assert "TPMS watch" in response.text and "<nav>" in response.text


def test_a_bad_filter_value_blames_the_box(client):
    response = client.get("/events?since=not-a-date")
    assert response.status_code == 400
    assert "TPMS watch" in response.text


def test_the_sightings_behind_a_pass_are_rendered_not_fetched():
    """Without JS they are simply visible, which is the right fallback for
    "the evidence behind this row"."""
    html = (TEMPLATES / "events.html").read_text()
    assert 'class="sub-rows"' in html
    assert "row.hidden = true" in html, "hidden by script, present in the HTML"


def test_no_dead_css_for_the_removed_timeline():
    css = (STATIC / "app.css").read_text()
    for selector in (".timeline", ".tl-row", ".tl-bar", ".tl-fill"):
        assert selector not in css, f"{selector} has no markup that emits it"
