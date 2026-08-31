import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tpms.config import ClusterConfig, SessionConfig  # noqa: E402
from tpms.db import Database  # noqa: E402
from tpms.ingest import Ingestor  # noqa: E402
from tpms.models import display_timezone, set_display_timezone  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_display_timezone():
    """The display zone is process-wide, so a test that changes it must not
    hand the next one a different clock."""
    before = display_timezone()
    yield
    set_display_timezone(str(before))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def ingestor(db):
    return Ingestor(db, SessionConfig(gap_seconds=120), ClusterConfig())


# -- driving the real UI ----------------------------------------------------
#
# Half of what these pages do only exists once script has run: charts are drawn
# from fetched JSON, rows expand, the bulk bar appears on the first tick. A
# string in the HTML is not evidence any of it works, so UI changes are checked
# in a browser. The fixtures below serve the real app and drive it; they skip
# themselves when playwright or its browser is not installed, so a machine
# without either still runs the rest of the suite.


@pytest.fixture
def serve():
    """Run a Service's web app on a loopback port, and give back its URL."""
    import socket
    import threading
    import time

    import uvicorn

    from tpms.web.app import create_app

    servers = []

    def start(service) -> str:
        with socket.socket() as probe:            # a port nothing else holds
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(service),
                host="127.0.0.1",
                port=port,
                log_level="warning",
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        servers.append((server, thread))
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("the test server never came up")
            time.sleep(0.02)
        return f"http://127.0.0.1:{port}"

    yield start

    for server, thread in servers:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def browser():
    playwright = pytest.importorskip(
        "playwright.sync_api", reason="pip install -e '.[dev]' to drive the UI"
    )
    with playwright.sync_playwright() as driver:
        try:
            launched = driver.chromium.launch()
        except Exception as error:  # noqa: BLE001 -- the browser is not fetched
            pytest.skip(f"chromium unavailable ({error}); run: playwright install chromium")
        yield launched
        launched.close()


@pytest.fixture
def page(browser):
    """A fresh page, and a failure for anything the console reports.

    A JS error leaves a page that renders and does nothing, which is the exact
    failure these tests exist to catch -- and it is silent unless someone
    listens for it.
    """
    context = browser.new_context(viewport={"width": 1400, "height": 1000})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on(
        "console",
        lambda m: errors.append(m.text) if m.type == "error" else None,
    )
    yield page
    context.close()
    assert not errors, f"the page reported: {errors}"
