"""The pages as a browser sees them.

Everywhere else the tests read the HTML the server produced, which says
nothing about the half of these pages that only exists once script has run:
the charts are drawn from fetched JSON, pass rows expand, the bulk bar appears
on the first tick, and a save is posted without leaving the page. Those are
checked here, by driving the real thing.

Every test also fails on a console error, so a page that renders and quietly
does nothing cannot pass.
"""

import pytest

from tpms.config import Config
from tpms.service import Service
from tpms.synthetic import generate_lines

pytestmark = pytest.mark.browser


@pytest.fixture
def site(tmp_path, serve):
    """A running program with synthetic traffic in it, and its URL."""
    service = Service(Config(database=str(tmp_path / "browser.db")), start_radio=False)
    service.ingestor.replay(generate_lines())
    service.ingestor.sweep(when=9e9)
    service.recluster()
    # One vehicle's wheels labelled, so direction has something to say.
    vehicle = next(
        v for v in service.db.list_vehicles() if service.db.sensors_for_vehicle(v.pk)
    )
    for sensor, label in zip(
        service.db.sensors_for_vehicle(vehicle.pk), ("FR", "RR", "FL", "RL")
    ):
        service.db.execute(
            "UPDATE sensors SET wheel_label = ? WHERE pk = ?", (label, sensor.pk)
        )
    yield serve(service), service, vehicle.pk
    service.stop()


# -- opening a pass ---------------------------------------------------------


def test_a_pass_row_opens_and_closes(site, page):
    url, _, vehicle_pk = site
    page.goto(f"{url}/vehicles/{vehicle_pk}")
    toggle = page.locator("button.expander").first
    evidence = page.locator(f"#{toggle.get_attribute('data-expand')}")

    assert not evidence.is_visible(), "the evidence is showing before it is asked for"
    toggle.click()
    assert evidence.is_visible()
    assert toggle.get_attribute("aria-expanded") == "true"
    toggle.click()
    assert not evidence.is_visible()
    assert toggle.get_attribute("aria-expanded") == "false"


def test_a_one_wheel_pass_opens_too(site, page):
    """The row with nothing on it to read is the one that must open."""
    url, _, _ = site
    page.goto(f"{url}/events")
    toggle = page.get_by_role("button", name="1 sighting").first
    toggle.click()
    evidence = page.locator(f"#{toggle.get_attribute('data-expand')}")
    assert evidence.is_visible()
    assert evidence.locator("table.inner tbody tr").count() == 1


def test_the_evidence_is_visible_without_script(site, browser):
    """With JS off nothing can be expanded, so nothing may be hidden."""
    url, _, _ = site
    context = browser.new_context(java_script_enabled=False)
    try:
        page = context.new_page()
        page.goto(f"{url}/events")
        assert page.locator("tr.sub-rows").first.is_visible()
    finally:
        context.close()


# -- the parts that are only drawn ------------------------------------------


def test_the_vehicle_charts_draw(site, page):
    url, _, vehicle_pk = site
    page.goto(f"{url}/vehicles/{vehicle_pk}")
    for chart in ("#chart-presence", "#chart-frequency", "#chart"):
        canvas = page.locator(f"{chart} canvas").first
        canvas.wait_for(state="attached", timeout=10000)
        box = canvas.bounding_box()
        assert box and box["width"] > 100 and box["height"] > 20, chart


def test_the_activity_chart_draws_and_tabulates_the_same_numbers(site, page):
    """Every chart carries a table view, and it is the only part of a canvas a
    test -- or a reader who cannot see colour -- can actually read."""
    url, _, _ = site
    page.goto(url)
    page.locator("#activity-readings canvas").first.wait_for(
        state="attached", timeout=10000
    )
    page.locator("#activity-readings .chart-table summary").click()
    rows = page.locator("#activity-readings .chart-table-body tbody tr")
    rows.first.wait_for(timeout=5000)
    assert rows.count() > 1, "the table view drew nothing"


# -- acting on the page -----------------------------------------------------


def test_the_bulk_bar_appears_on_the_first_tick(site, page):
    url, _, vehicle_pk = site
    page.goto(f"{url}/vehicles/{vehicle_pk}")
    bar = page.locator("[data-tick-bar]")
    assert not bar.is_visible(), "the bar is offering to act on nothing"
    page.locator("input[name=sensor]").first.check()
    assert bar.is_visible()
    assert "1" in page.locator("[data-tick-count]").inner_text()


def test_naming_a_vehicle_sticks(site, page):
    url, _, vehicle_pk = site
    page.goto(f"{url}/vehicles/{vehicle_pk}")
    page.fill("input[name=name]", "The blue van")
    page.click("button.primary")
    page.wait_for_load_state("networkidle")
    page.goto(f"{url}/vehicles/{vehicle_pk}")
    assert "The blue van" in page.locator("h1").inner_text()
