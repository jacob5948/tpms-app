"""The live feed must not drop readings, and must not orphan asyncio tasks.

The original bug: a fresh `queue.get()` task was created each loop iteration
and cancelled on every keepalive. If a reading arrived between the timeout
expiring and the cancel landing, the task had already dequeued it, the cancel
failed, and the reading was discarded -- lost from the live feed with no trace.
Cancelling without awaiting also produced a "Task was destroyed but it is
pending" error per closed stream.
"""

import json
import threading
import time
import urllib.request

import pytest
import uvicorn

from tpms.config import Config
from tpms.service import Service
from tpms.synthetic import generate_lines
from tpms.web import app as appmod


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    """A real uvicorn server: TestClient cannot exercise streaming timing."""
    # Force many keepalive cycles into a short test.
    monkeypatch.setattr(appmod, "KEEPALIVE_SECONDS", 0.2)

    service = Service(Config(database=str(tmp_path / "sse.db")), start_radio=False)
    config = uvicorn.Config(
        appmod.create_app(service), host="127.0.0.1", port=8894, log_level="error"
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()

    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "server did not start"

    yield service
    server.should_exit = True
    time.sleep(1)


def test_no_readings_are_dropped_across_keepalives(live_server):
    service = live_server
    received: list[dict] = []

    def reader():
        with urllib.request.urlopen(
            "http://127.0.0.1:8894/api/stream", timeout=20
        ) as response:
            for raw in response:
                text = raw.decode().strip()
                if text.startswith("data:"):
                    received.append(json.loads(text[5:]))
                    if len(received) >= 3:
                        return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(2)  # ~10 keepalive cycles with nothing to send

    lines = generate_lines(passes=1)[:3]
    for line in lines:
        service.ingestor.handle_line(line)
        time.sleep(0.3)  # straddle the keepalive boundary
    thread.join(timeout=10)

    assert len(received) == 3, f"dropped {3 - len(received)} reading(s)"
    assert all(event["type"] == "reading" for event in received)


def test_an_open_stream_does_not_hold_the_server_open(tmp_path, monkeypatch):
    """The bug behind a slow `systemctl stop`.

    uvicorn waits for in-flight responses *before* it runs lifespan shutdown,
    and the feed only ends once that shutdown sets the flag -- so one open
    Live page held the server until systemd's timeout ran out and SIGKILLed
    it. Setting the flag first, as the signal handler now does, releases it.
    """
    monkeypatch.setattr(appmod, "KEEPALIVE_SECONDS", 0.2)
    service = Service(Config(database=str(tmp_path / "stop.db")), start_radio=False)
    app = appmod.create_app(service)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=8895, log_level="error",
        timeout_graceful_shutdown=10,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "server did not start"

    opened = threading.Event()

    def reader():
        with urllib.request.urlopen(
            "http://127.0.0.1:8895/api/stream", timeout=20
        ) as response:
            opened.set()
            for _ in response:
                pass

    threading.Thread(target=reader, daemon=True).start()
    assert opened.wait(10), "stream never opened"

    app.state.shutting_down.set()      # what the signal handler does
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive(), "an open feed kept the server running"


def test_serve_releases_the_streams_before_uvicorn_waits(tmp_path, monkeypatch):
    """The handler must set the flag itself, and still hand off to uvicorn."""
    import argparse
    import signal
    import types

    from tpms import cli

    (tmp_path / "config.yaml").write_text(f"database: {tmp_path / 'serve.db'}\n")
    captured: dict = {}

    class FakeServer:
        """Everything except actually listening."""

        def __init__(self, config):
            captured["app"] = config.app
            captured["timeout"] = config.timeout_graceful_shutdown

        def handle_exit(self, sig, frame):
            captured["graceful"] = True

        def run(self):
            captured["handler"] = self.handle_exit

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    cli.cmd_serve(argparse.Namespace(config=str(tmp_path / "config.yaml"), no_radio=True))

    app = captured["app"]
    assert captured["timeout"] is not None, "a wedged response must not wait forever"
    assert not app.state.shutting_down.is_set()
    captured["handler"](signal.SIGTERM, types.SimpleNamespace())
    assert app.state.shutting_down.is_set(), "the streams must be released first"
    assert captured["graceful"], "and uvicorn's own handler must still run"
