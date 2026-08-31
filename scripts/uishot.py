#!/usr/bin/env python3
"""Screenshot a page of the UI, for looking at a change rather than asserting it.

    python scripts/uishot.py /vehicles/1 -o /tmp/vehicle.png
    python scripts/uishot.py /events --click "button.expander" --full
    python scripts/uishot.py / --db tpms.db --dark

Serves the real app -- by default over a scratch database filled with synthetic
traffic, so it needs no receiver and touches nothing you care about -- drives a
real browser to the page, and writes a PNG. `tests/test_browser.py` is where a
UI rule gets *asserted*; this is for the part of design a test cannot judge.
"""

from __future__ import annotations

import argparse
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from tpms.config import Config
from tpms.service import Service
from tpms.synthetic import generate_lines
from tpms.web.app import create_app


def build(database: str | None) -> tuple[Service, Path]:
    """A service over the given database, or a throwaway one with traffic in it."""
    if database:
        return Service(Config(database=database), start_radio=False), Path(database)
    scratch = Path(tempfile.mkdtemp(prefix="tpms-shot-")) / "shot.db"
    service = Service(Config(database=str(scratch)), start_radio=False)
    service.ingestor.replay(generate_lines())
    service.ingestor.sweep(when=9e9)
    service.recluster()
    # Label one vehicle, so anything that reads wheel positions has an example.
    vehicles = [v for v in service.db.list_vehicles() if service.db.sensors_for_vehicle(v.pk)]
    if vehicles:
        for sensor, label in zip(
            service.db.sensors_for_vehicle(vehicles[0].pk), ("FR", "RR", "FL", "RL")
        ):
            service.db.execute(
                "UPDATE sensors SET wheel_label = ? WHERE pk = ?", (label, sensor.pk)
            )
    return service, scratch


def serve(service: Service) -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(service), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise SystemExit("the server never came up")
        time.sleep(0.02)
    return f"http://127.0.0.1:{port}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", nargs="?", default="/", help="page to shoot, e.g. /vehicles/1")
    parser.add_argument("-o", "--out", default="ui.png", help="PNG to write")
    parser.add_argument("--db", help="an existing database instead of synthetic traffic")
    parser.add_argument("--click", action="append", default=[],
                        help="selector to click before the shot; repeatable")
    parser.add_argument("--clip", help="screenshot this element instead of the viewport")
    parser.add_argument("--full", action="store_true", help="the whole page, not the viewport")
    parser.add_argument("--width", type=int, default=1400)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--dark", action="store_true", help="render in the dark theme")
    parser.add_argument("--wait", type=float, default=1.0,
                        help="seconds to let charts settle before the shot")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("playwright is missing: pip install -e '.[dev]' && playwright install chromium")

    service, database = build(args.db)
    url = serve(service)
    out = Path(args.out).resolve()
    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch()
            page = browser.new_context(
                viewport={"width": args.width, "height": args.height},
                color_scheme="dark" if args.dark else "light",
            ).new_page()
            problems: list[str] = []
            page.on("pageerror", lambda e: problems.append(str(e)))
            page.goto(url + args.path)
            page.wait_for_timeout(int(args.wait * 1000))
            for selector in args.click:
                page.locator(selector).first.click()
            page.wait_for_timeout(300)
            target = page.locator(args.clip).first if args.clip else page
            target.screenshot(path=str(out), **({"full_page": True} if args.full and not args.clip else {}))
            browser.close()
            for problem in problems:
                print(f"page error: {problem}", file=sys.stderr)
    finally:
        service.stop()
    print(f"{out}  ({args.path} over {database})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
