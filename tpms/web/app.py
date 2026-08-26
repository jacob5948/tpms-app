"""FastAPI application: pages, JSON API and the live SSE feed."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import queries as q
from ..models import now as now_ts, to_iso
from ..service import Service

log = logging.getLogger(__name__)

HERE = Path(__file__).parent

#: Seconds between SSE comment frames, which stop proxies and browsers
#: from dropping an idle stream. Module-level so tests can shorten it.
KEEPALIVE_SECONDS = 15.0


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _fmt_ago(ts: float | None) -> str:
    if ts is None:
        return "never"
    delta = now_ts() - ts
    if delta < 0:
        return "just now"
    if delta < 90:
        return f"{int(delta)}s ago"
    if delta < 5400:
        return f"{int(delta // 60)}m ago"
    if delta < 172800:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def create_app(service: Service) -> FastAPI:
    # Set on shutdown so the SSE generators return promptly. Without it,
    # uvicorn's graceful shutdown blocks on those never-ending responses and
    # the receiver keeps running long after Ctrl+C.
    shutting_down = asyncio.Event()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        service.start()
        try:
            yield
        finally:
            # Order matters: release the streams, then stop the radio and the
            # background threads, all before uvicorn finishes shutting down.
            shutting_down.set()
            service.stop()

    app = FastAPI(title="TPMS", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["duration"] = _fmt_duration
    templates.env.filters["ago"] = _fmt_ago
    templates.env.filters["iso"] = to_iso

    db = service.db
    gap = service.config.sessions.gap_seconds

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        context.setdefault("nav", name.replace(".html", ""))
        return templates.TemplateResponse(request, name, context)

    # -- pages ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def live(request: Request):
        return page(
            request,
            "live.html",
            heard=q.heard_now(db),
            gap=gap,
            recent=q.events(db, limit=25),
        )

    @app.get("/vehicles", response_class=HTMLResponse)
    def vehicles(request: Request):
        return page(
            request,
            "vehicles.html",
            vehicles=q.vehicle_summaries(db, gap),
            unassigned=[s for s in q.sensor_rows(db) if s["vehicle_id"] is None],
        )

    @app.get("/vehicles/{vehicle_id}", response_class=HTMLResponse)
    def vehicle_detail(request: Request, vehicle_id: int):
        vehicle = db.get_vehicle(vehicle_id)
        if vehicle is None:
            raise HTTPException(404, "vehicle not found")
        sensors = [q.sensor_row(db, s.pk) for s in db.sensors_for_vehicle(vehicle_id)]
        history = {s["pk"]: q.pressure_history(db, s["pk"], 200) for s in sensors}
        return page(
            request,
            "vehicle.html",
            nav="vehicles",
            vehicle=vehicle,
            sensors=sensors,
            history=history,
            intervals=q.vehicle_intervals(db, vehicle_id, gap, limit=100),
            all_vehicles=[v for v in db.list_vehicles() if v.pk != vehicle_id],
        )

    @app.get("/sensors", response_class=HTMLResponse)
    def sensors(request: Request):
        return page(
            request,
            "sensors.html",
            sensors=q.sensor_rows(db),
            vehicles=db.list_vehicles(),
        )

    @app.get("/events", response_class=HTMLResponse)
    def events(
        request: Request,
        since: str | None = None,
        until: str | None = None,
        vehicle: int | None = None,
    ):
        from ..cli import _parse_when

        return page(
            request,
            "events.html",
            events=q.events(
                db,
                start=_parse_when(since),
                end=_parse_when(until),
                vehicle_id=vehicle,
                limit=1000,
            ),
            vehicles=db.list_vehicles(),
            filters={"since": since or "", "until": until or "", "vehicle": vehicle},
        )

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request):
        return page(request, "status.html", status=service.status())

    # -- live feed --------------------------------------------------------

    @app.get("/api/stream")
    async def stream(request: Request):
        """Server-sent events. Ingest runs on a worker thread, so events are
        handed to the event loop with call_soon_threadsafe."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)

        def on_event(event: dict) -> None:
            def push() -> None:
                if queue.full():  # drop oldest rather than block ingest
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                queue.put_nowait(event)

            loop.call_soon_threadsafe(push)

        unsubscribe = service.ingestor.subscribe(on_event)

        async def generate():
            stopping = asyncio.ensure_future(shutting_down.wait())
            # One pending getter, carried across keepalives. Cancelling and
            # recreating it each time would drop any reading that arrived
            # between the timeout firing and the cancel landing.
            nxt = asyncio.ensure_future(queue.get())
            try:
                yield ": connected\n\n"
                while not shutting_down.is_set():
                    if await request.is_disconnected():
                        break
                    done, _ = await asyncio.wait(
                        {nxt, stopping},
                        timeout=KEEPALIVE_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stopping in done:
                        break
                    if nxt in done:
                        yield f"data: {json.dumps(nxt.result())}\n\n"
                        nxt = asyncio.ensure_future(queue.get())
                    else:
                        yield ": keepalive\n\n"  # nxt stays pending, unconsumed
            finally:
                nxt.cancel()
                stopping.cancel()
                # Await the cancellations, or asyncio logs "Task was destroyed
                # but it is pending" for every stream that ever closed.
                with contextlib.suppress(BaseException):
                    await asyncio.gather(nxt, stopping, return_exceptions=True)
                unsubscribe()

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/heard-now")
    def api_heard_now():
        return {"heard": q.heard_now(db), "gap": gap, "now": now_ts()}

    @app.get("/api/status")
    def api_status():
        return service.status()

    # -- mutations --------------------------------------------------------

    def _back(request: Request, fallback: str = "/vehicles") -> RedirectResponse:
        return RedirectResponse(
            request.headers.get("referer", fallback), status_code=303
        )

    @app.post("/api/vehicles/{vehicle_id}")
    async def update_vehicle(request: Request, vehicle_id: int):
        form = await request.form()
        if db.get_vehicle(vehicle_id) is None:
            raise HTTPException(404, "vehicle not found")
        name = (form.get("name") or "").strip() or None
        notes = (form.get("notes") or "").strip() or None
        # A human-supplied name marks the vehicle as manual territory, which
        # stops the clusterer from reshaping it later.
        db.execute(
            "UPDATE vehicles SET name = ?, notes = ?, auto_generated = 0 WHERE pk = ?",
            (name, notes, vehicle_id),
        )
        return _back(request, f"/vehicles/{vehicle_id}")

    @app.post("/api/vehicles/{vehicle_id}/merge")
    async def merge_vehicle(request: Request, vehicle_id: int):
        form = await request.form()
        other = int(form.get("other"))
        if db.get_vehicle(vehicle_id) is None or db.get_vehicle(other) is None:
            raise HTTPException(404, "vehicle not found")
        db.execute("UPDATE sensors SET vehicle_id = ? WHERE vehicle_id = ?", (vehicle_id, other))
        db.execute("UPDATE vehicles SET auto_generated = 0 WHERE pk = ?", (vehicle_id,))
        db.delete_empty_vehicles()
        return _back(request, f"/vehicles/{vehicle_id}")

    @app.post("/api/vehicles/{vehicle_id}/review-cleared")
    async def clear_review(request: Request, vehicle_id: int):
        db.execute("UPDATE vehicles SET needs_review = 0 WHERE pk = ?", (vehicle_id,))
        return _back(request, f"/vehicles/{vehicle_id}")

    @app.post("/api/sensors/{sensor_pk}")
    async def update_sensor(request: Request, sensor_pk: int):
        form = await request.form()
        sensor = db.get_sensor(sensor_pk)
        if sensor is None:
            raise HTTPException(404, "sensor not found")

        if "wheel_label" in form:
            label = (form.get("wheel_label") or "").strip() or None
            db.execute("UPDATE sensors SET wheel_label = ? WHERE pk = ?", (label, sensor_pk))

        if "pinned" in form:
            pinned = 1 if form.get("pinned") in ("1", "true", "on") else 0
            db.execute("UPDATE sensors SET pinned = ? WHERE pk = ?", (pinned, sensor_pk))

        if "vehicle_id" in form:
            raw = (form.get("vehicle_id") or "").strip()
            if raw == "new":
                target = db.create_vehicle(now_ts(), auto_generated=False)
            elif raw in ("", "none"):
                target = None
            else:
                target = int(raw)
                if db.get_vehicle(target) is None:
                    raise HTTPException(404, "vehicle not found")
            db.set_sensor_vehicle(sensor_pk, target)
            # Manual moves are only durable if the clusterer keeps its hands
            # off, so pin the sensor as part of the same action.
            db.execute("UPDATE sensors SET pinned = 1 WHERE pk = ?", (sensor_pk,))
            db.delete_empty_vehicles()

        return _back(request, "/sensors")

    @app.post("/api/recluster")
    async def recluster(request: Request):
        report = service.recluster()
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"summary": report.summary()})
        return _back(request, "/vehicles")

    @app.post("/api/radio/restart")
    async def radio_restart(request: Request):
        service.radio.restart()
        return _back(request, "/status")

    @app.get("/api/export.csv")
    def export_csv(
        since: str | None = None, until: str | None = None, vehicle: int | None = None
    ):
        from ..cli import _parse_when

        rows = q.events(
            db,
            start=_parse_when(since),
            end=_parse_when(until),
            vehicle_id=vehicle,
            limit=100000,
        )
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            ["vehicle", "sensor", "wheel", "first_heard", "last_heard",
             "duration_s", "readings", "max_rssi", "still_open"]
        )
        for row in rows:
            writer.writerow(
                [
                    row["vehicle_name"] or "",
                    row["display"],
                    row["wheel_label"] or "",
                    row["started_at_iso"],
                    row["last_reading_at_iso"],
                    round(row["duration"], 1),
                    row["reading_count"],
                    row["max_rssi"] if row["max_rssi"] is not None else "",
                    "yes" if row["open"] else "no",
                ]
            )
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="tpms-events.csv"'},
        )

    return app
