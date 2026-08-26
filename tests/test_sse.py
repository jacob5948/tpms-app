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
