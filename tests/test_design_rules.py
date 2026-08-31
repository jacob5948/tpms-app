"""Guards for the rules in INFO_DESIGN.md that a template edit could undo.

These are cheap structural checks, not a substitute for looking at the page:
they exist because each one has already been got wrong once.
"""

import re
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


def test_a_wheel_position_is_chosen_from_the_closed_set(client):
    """Seven positions, all visible with their meanings, and no free-text box
    that quietly rewrites "front left" as "FL" once it is clicked."""
    from tpms.direction import WHEEL_POSITIONS

    for path in ("/vehicles/1", "/sensors/1"):
        html = client.get(path).text
        assert "<datalist" not in html, path
        assert 'name="wheel_label"' in html and "wheel-picker" in html, path
        for value, label in WHEEL_POSITIONS:
            assert f'value="{value}"' in html and label in html, (path, value)


def test_the_wheel_picker_keeps_a_label_it_did_not_offer(client):
    """The API still takes free text. A picker that dropped an unrecognised
    label would offer to erase it just by rendering the page."""
    html = client.get("/vehicles/1").text
    assert "nearside boot" not in html
    client.post("/api/sensors/1", data={"wheel_label": "nearside boot"},
                follow_redirects=False)
    assert 'value="nearside boot" selected' in client.get("/vehicles/1").text


def test_a_one_field_picker_saves_itself_but_still_renders_its_button(client):
    """Saving on change is the enhancement; the button is what is left when
    the script does not run, so it must be in the HTML and hidden from JS."""
    html = client.get("/vehicles/1").text
    assert "data-save-on-change" in html and "data-fallback-for=" in html
    forms = (STATIC / "forms.js").read_text()
    assert "data-save-on-change" in forms and "requestSubmit" in forms
    assert "fallback.hidden = true" in forms


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


@pytest.mark.parametrize("path", ("/events", "/vehicles/1"))
def test_the_sightings_behind_a_pass_are_rendered_not_fetched(client, path):
    """Without JS they are simply visible, which is the right fallback for
    "the evidence behind this row"."""
    html = client.get(path).text
    assert 'class="sub-rows"' in html
    assert "/static/expand.js" in html
    assert "row.hidden = true" in (STATIC / "expand.js").read_text(), (
        "hidden by script, present in the HTML"
    )


def test_a_pass_shows_the_same_evidence_wherever_it_is_shown(client):
    """The log and a vehicle's own history show the same object, so they show
    it the same way -- one markup, reached from both."""
    macros = (TEMPLATES / "_macros.html").read_text()
    assert 'class="sub-rows"' in macros, "the evidence row lives in one place"
    for template in ("events.html", "vehicle.html"):
        html = (TEMPLATES / template).read_text()
        assert "m.sightings_rows(" in html
        assert "m.heading_cell(" in html
        assert 'class="sub-rows"' not in html, "a second copy will drift"


def test_no_dead_css_for_the_removed_timeline():
    css = (STATIC / "app.css").read_text()
    for selector in (".timeline", ".tl-row", ".tl-bar", ".tl-fill"):
        assert selector not in css, f"{selector} has no markup that emits it"


def test_the_program_has_no_fourth_word_for_a_pass(client):
    """A vehicle going by is a pass, on every page that shows one.

    The charts on the vehicle page were filed under "comings and goings",
    which named the same object a fourth time and matched nothing else.
    """
    for path in ("/", "/vehicles", "/vehicles/1", "/events"):
        html = client.get(path).text.lower()
        for word in ("comings and goings", "drive-by", "appearances"):
            assert word not in html, f"{word} on {path}"


def test_a_flag_shared_by_most_cards_is_explained_once(client):
    """Nearly every vehicle is provisional, and the review reason is nearly
    always the same one, so a paragraph per card came out as one sentence
    repeated down the whole grid -- dwarfing the readings the card exists to
    show. The card keeps the flag, the reason as a tooltip and the action."""
    template = (TEMPLATES / "vehicles.html").read_text()
    card = template.split('<div class="grid"')[1]
    assert 'class="note"' not in card, "no explanatory boxes inside the cards"
    assert 'class="pill warn"' in card, "the flags themselves stay"
    assert "Split them" in card, "and so does the action the flag calls for"

    # A tooltip may repeat -- it costs no space and is read one at a time.
    html = client.get("/vehicles").text
    for note in re.findall(r'<div class="note">(.*?)</div>', html, re.S):
        assert "Grouped from a single pass" not in note


def test_stacked_facets_are_titled(client):
    """Two plots in one panel with no titles read as one confused figure: the
    caption of the first sits directly above the second."""
    chart = (STATIC / "chart.js").read_text()
    assert "chart-title" in chart, "chart.js must render the label it is given"
    for path in ("/", "/sensors/1", "/vehicles/1"):
        assert "label: '" in client.get(path).text, path


def test_charts_are_told_the_zone_the_tables_use(client):
    """uPlot labels its axis in the reader's zone unless told otherwise, which
    puts the chart hours away from the table beneath it."""
    assert "tzDate" in (STATIC / "chart.js").read_text()
    assert "TPMS_TZ" in (TEMPLATES / "base.html").read_text()
    assert "window.TPMS_TZ" in client.get("/vehicles/1").text


def test_the_bulk_bar_lives_inside_the_table_it_acts_on(client):
    """One selection, one bar -- and the bar has to belong to the set.

    It was rendered as a sibling of the panel holding the table, so it had no
    ground of its own and sat equidistant between that panel and the next one
    down. The eye attached it to the nearer border, which was the wrong one.
    """
    html = client.get("/vehicles/1").text
    body = html[html.index("<h2>Sensors</h2>"):]
    panel = body[body.index('<div class="panel">'):]
    bar = panel.index('class="row bulk"')
    assert panel.index("</table>") < bar, "the bar comes after the set it acts on"

    # Walk the divs: the panel that opens the section must still be open when
    # the bar starts. Counting tags is not enough -- the old markup closed the
    # panel and opened the bar, which balances.
    depth = 0
    opening = panel.rindex("<div", 0, bar)   # the bar's own tag is not nesting
    for match in re.finditer(r"<div\b|</div>", panel[:opening]):
        depth += 1 if match.group() == "<div" else -1
    assert depth > 0, "the bulk bar is outside the panel holding its table"


def test_a_refused_bulk_action_returns_the_page_it_came_from(client):
    """These handlers live under /api/, and the error handler answers /api/
    with JSON. A browser form post with nothing ticked therefore used to land
    on a bare {"detail": ...}: no shell, no nav, and every tick gone. A
    refusal is the page saying no, so it goes back to the page.
    """
    for path in ("/api/vehicles/1/split", "/api/vehicles/1/move"):
        response = client.post(path, data={}, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith(("/vehicles", "http"))
        assert response.cookies.get("tpms_flash_kind") == "err", path


def test_a_refusal_does_not_arrive_looking_like_a_save(client):
    """The toast colour comes from the flash, and the flash used to carry only
    words. A turned-down action reported itself in green."""
    forms = (STATIC / "forms.js").read_text()
    assert "flash.dataset.kind" in forms
    assert "toast(flash.textContent.trim(), 'ok')" not in forms


def test_the_wheel_column_can_be_ordered_by(client):
    """The sorter falls back to a cell's text, and this cell holds a field, so
    every row compared as blank: the header offered the cursor, the arrow and
    the accent colour for an ordering it never performed."""
    html = client.get("/vehicles/1").text
    body = html[html.index("<h2>Sensors</h2>"):]
    assert 'class="actions" data-sort=' in body


def test_the_destinations_do_not_include_not_being_a_destination(client):
    """"Unassign" among the vehicle names is what the per-row picker was taken
    out for. It came back in the bar."""
    html = client.get("/vehicles/1").text
    select = html[html.index('id="move-target"'):]
    select = select[:select.index("</select>")]
    if "unassign" in select:
        assert "<optgroup" in select, "unassign must not sit among the vehicles"
