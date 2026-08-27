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
from urllib.parse import quote, unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import queries as q
from ..models import now as now_ts, parse_when, to_iso
from ..retention import human_bytes
from ..service import Service

log = logging.getLogger(__name__)

HERE = Path(__file__).parent

#: Seconds between SSE comment frames, which stop proxies and browsers
#: from dropping an idle stream. Module-level so tests can shorten it.
KEEPALIVE_SECONDS = 15.0

#: Carries the result of a mutation across the redirect that follows it, so
#: the page that lands can say what happened. Deleted as soon as one page has
#: shown it.
FLASH_COOKIE = "tpms_flash"

#: Set by static/forms.js on the submissions it handles itself. Those want the
#: outcome as JSON rather than a whole page they are not going to render.
ASYNC_HEADER = "x-tpms-async"


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


#: How many sightings the pass log will merge in one request. Passes are built
#: in Python from the sightings underneath them, so this bounds the work; the
#: page says so when it bites rather than silently showing a short day.
PASS_SCAN_LIMIT = 20000


class BadFilter(Exception):
    """A filter value the user typed that could not be parsed."""

    def __init__(self, field: str, value: str) -> None:
        super().__init__(f"{field}: {value!r}")
        self.field = field
        self.value = value


def _when(field: str, value: str | None) -> float | None:
    """Parse a filter box, blaming the box rather than the server."""
    try:
        return parse_when(value)
    except ValueError:
        raise BadFilter(field, value or "") from None


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
    templates.env.filters["bytes"] = human_bytes

    db = service.db
    gap = service.config.sessions.gap_seconds

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        context.setdefault("nav", name.replace(".html", ""))
        flash = request.cookies.get(FLASH_COOKIE)
        context.setdefault("flash", unquote(flash) if flash else None)
        response = templates.TemplateResponse(request, name, context)
        if flash:
            response.delete_cookie(FLASH_COOKIE, path="/")
        return response

    # -- error pages --------------------------------------------------------

    # A stale bookmark used to land on raw JSON with no header and no way
    # back. These render the same shell as everything else.
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"nav": "", "flash": None, "status": exc.status_code,
             "detail": exc.detail},
            status_code=exc.status_code,
        )

    @app.exception_handler(BadFilter)
    async def bad_filter(request: Request, exc: BadFilter):
        detail = (
            f"Could not read {exc.field} as a time. Try a relative age like "
            f"\u201c7d\u201d or \u201c24h\u201d, or a date like \u201c2026-08-01\u201d."
        )
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": detail}, status_code=400)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"nav": "events", "flash": None, "status": 400, "detail": detail,
             "back": "/events"},
            status_code=400,
        )

    # -- pages ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def live(request: Request):
        return page(
            request,
            "live.html",
            heard=q.heard_now_groups(db),
            gap=gap,
            now=now_ts(),
            vehicles=db.list_vehicles(),
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
        # Hidden sensors are rendered but held back by the filter, so the
        # "Hidden" tile can reveal them without a round trip -- otherwise the
        # only way to un-hide one would be a URL you had to already know.
        rows = q.sensor_rows(db, include_ignored=True)
        return page(
            request,
            "sensors.html",
            sensors=rows,
            visible=[r for r in rows if not r["ignored"]],
            hidden=[r for r in rows if r["ignored"]],
            duplicates=q.alias_groups(db),
            vehicles=db.list_vehicles(),
        )

    @app.get("/sensors/{sensor_pk}", response_class=HTMLResponse)
    def sensor_detail(request: Request, sensor_pk: int):
        detail = q.sensor_detail(db, sensor_pk, service.config.clustering.min_support)
        if detail is None:
            raise HTTPException(404, "sensor not found")
        return page(
            request,
            "sensor.html",
            nav="sensors",
            s=detail,
            gap=gap,
            vehicles=db.list_vehicles(),
        )

    @app.get("/events", response_class=HTMLResponse)
    def events(
        request: Request,
        since: str | None = None,
        until: str | None = None,
        vehicle: int | None = None,
        sensor: int | None = None,
        view: str = "passes",
        limit: int = 1000,
    ):
        """The traffic log, as vehicle passes or as raw sensor sightings.

        Passes are the default because the program is about vehicles going by.
        Sightings are a peer view, not a debug mode: matching a car you watched
        pass to the transmitters heard at that moment is done against the raw
        rows.
        """
        view = view if view in ("passes", "sightings") else "passes"
        limit = max(10, min(int(limit), 5000))
        start, end = _when("since", since), _when("until", until)
        # Asking for one sensor by name overrides hiding it. Hidden sensors
        # are kept out of the lists you browse, not out of the answers you ask
        # for -- the link to here comes from the sensor's own page.
        seeing_hidden = sensor is not None
        total = q.count_events(
            db, start=start, end=end, vehicle_id=vehicle, sensor_pk=sensor,
            include_ignored=seeing_hidden,
        )

        if view == "passes":
            rows = q.vehicle_passes(
                db, gap, start=start, end=end, vehicle_id=vehicle,
                sensor_pk=sensor, limit=limit, scan_limit=PASS_SCAN_LIMIT,
                include_ignored=seeing_hidden,
            )
            truncated = total > PASS_SCAN_LIMIT or len(rows) == limit
        else:
            rows = q.events(
                db, start=start, end=end, vehicle_id=vehicle,
                sensor_pk=sensor, limit=limit, include_ignored=seeing_hidden,
            )
            truncated = total > len(rows)

        focus = q.sensor_row(db, sensor) if sensor else None
        return page(
            request,
            "events.html",
            view=view,
            rows=rows,
            total=total,
            truncated=truncated,
            limit=limit,
            vehicles=db.list_vehicles(),
            focus_sensor=focus or None,
            filters={
                "since": since or "",
                "until": until or "",
                "vehicle": vehicle,
                "sensor": sensor,
            },
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

    @app.get("/api/heard-now.html", response_class=HTMLResponse)
    def api_heard_now_html(request: Request):
        """The 'heard now' panel alone, for the Live page to swap in place."""
        return templates.TemplateResponse(
            request,
            "_heard.html",
            {"heard": q.heard_now_groups(db), "vehicles": db.list_vehicles()},
        )

    @app.get("/api/heard-now")
    def api_heard_now():
        # The flat list, for anything reading this as data rather than
        # rendering the page's grouping.
        return {"heard": q.heard_now(db), "gap": gap, "now": now_ts()}

    @app.get("/api/status")
    def api_status():
        return service.status()

    # -- chart data -------------------------------------------------------
    #
    # The charts fetch their own data so the range buttons can widen the
    # window without reloading the page around them.

    def _window(since: str | None, until: str | None) -> tuple[float | None, float | None]:
        return _when("since", since), _when("until", until)

    @app.get("/api/activity")
    def api_activity(since: str | None = None, until: str | None = None, buckets: int = 96):
        start, end = _window(since, until)
        return q.activity(db, start, end, buckets)

    @app.get("/api/sensors/{sensor_pk}/history")
    def api_sensor_history(
        sensor_pk: int, since: str | None = None, until: str | None = None
    ):
        if db.get_sensor(sensor_pk) is None:
            raise HTTPException(404, "sensor not found")
        start, end = _window(since, until)
        return {"points": q.pressure_history(db, sensor_pk, 400, start, end)}

    @app.get("/api/vehicles/{vehicle_id}/presence")
    def api_vehicle_presence(
        vehicle_id: int,
        since: str | None = None,
        until: str | None = None,
        buckets: int = 96,
    ):
        if db.get_vehicle(vehicle_id) is None:
            raise HTTPException(404, "vehicle not found")
        start, end = _window(since, until)
        return q.vehicle_presence(db, vehicle_id, gap, start, end, buckets)

    @app.get("/api/vehicles/{vehicle_id}/history")
    def api_vehicle_history(
        vehicle_id: int, since: str | None = None, until: str | None = None
    ):
        if db.get_vehicle(vehicle_id) is None:
            raise HTTPException(404, "vehicle not found")
        start, end = _window(since, until)
        return {
            "series": [
                {
                    "pk": sensor.pk,
                    "name": f"{sensor.model}/{sensor.sensor_id}"
                    + (f" {sensor.wheel_label}" if sensor.wheel_label else ""),
                    "points": q.pressure_history(db, sensor.pk, 300, start, end),
                }
                for sensor in db.sensors_for_vehicle(vehicle_id)
            ]
        }

    # -- mutations --------------------------------------------------------

    def _back(
        request: Request,
        fallback: str = "/vehicles",
        message: str | None = None,
        **detail: Any,
    ):
        """Answer a mutation.

        Every mutation used to end in a bare redirect, which left the user with
        no way to tell a successful save from a click that never registered.
        Now it either answers forms.js in JSON, or carries a one-line summary
        across the redirect for the next page to show.
        """
        if request.headers.get(ASYNC_HEADER):
            return JSONResponse({"ok": True, "message": message or "Saved.", **detail})
        response = RedirectResponse(
            request.headers.get("referer", fallback), status_code=303
        )
        if message:
            # quote(): messages carry vehicle names, and a stray comma or
            # semicolon would truncate the cookie.
            response.set_cookie(
                FLASH_COOKIE, quote(message), path="/", max_age=30, samesite="lax"
            )
        return response

    def _vehicle_name(vehicle_id: int) -> str:
        vehicle = db.get_vehicle(vehicle_id)
        name = getattr(vehicle, "name", None) if vehicle else None
        return name or f"Unnamed vehicle #{vehicle_id}"

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
        return _back(
            request,
            f"/vehicles/{vehicle_id}",
            f"Saved {name}." if name else "Saved. This vehicle has no name yet.",
        )

    @app.post("/api/vehicles/{vehicle_id}/merge")
    async def merge_vehicle(request: Request, vehicle_id: int):
        form = await request.form()
        try:
            other = int(form.get("other") or "")
        except (TypeError, ValueError):
            raise HTTPException(400, "pick a vehicle to merge in") from None
        if other == vehicle_id:
            raise HTTPException(400, "a vehicle cannot be merged into itself")
        if db.get_vehicle(vehicle_id) is None or db.get_vehicle(other) is None:
            raise HTTPException(404, "vehicle not found")
        moved = _vehicle_name(other)
        into = _vehicle_name(vehicle_id)
        db.execute("UPDATE sensors SET vehicle_id = ? WHERE vehicle_id = ?", (vehicle_id, other))
        db.execute("UPDATE vehicles SET auto_generated = 0 WHERE pk = ?", (vehicle_id,))
        db.delete_empty_vehicles()
        return _back(request, f"/vehicles/{vehicle_id}", f"Merged {moved} into {into}.")

    @app.post("/api/vehicles/{vehicle_id}/split")
    async def split_vehicle(request: Request, vehicle_id: int):
        """Move several sensors out into a vehicle of their own.

        The answer to an oversized or mixed-family cluster is almost always
        "these three are one car and those four are another", which moving one
        sensor at a time made tedious enough to avoid.
        """
        form = await request.form()
        if db.get_vehicle(vehicle_id) is None:
            raise HTTPException(404, "vehicle not found")
        try:
            wanted = {int(v) for v in form.getlist("sensor")}
        except ValueError:
            raise HTTPException(400, "not a sensor") from None
        members = {s.pk for s in db.sensors_for_vehicle(vehicle_id)}
        chosen = wanted & members
        if not chosen:
            raise HTTPException(400, "select the sensors to split out")
        if chosen == members:
            raise HTTPException(400, "that would move every sensor, not split them")

        target = db.create_vehicle(now_ts(), auto_generated=False)
        for pk in sorted(chosen):
            db.set_sensor_vehicle(pk, target)
            # Pinned for the same reason a single move is: otherwise the next
            # clustering run puts them straight back.
            db.execute("UPDATE sensors SET pinned = 1 WHERE pk = ?", (pk,))
        # The split is the review being resolved.
        db.execute("UPDATE vehicles SET needs_review = 0 WHERE pk = ?", (vehicle_id,))
        db.delete_empty_vehicles()
        return _back(
            request,
            f"/vehicles/{target}",
            f"Split {len(chosen)} sensor(s) out into {_vehicle_name(target)}.",
        )

    @app.post("/api/vehicles/{vehicle_id}/review-cleared")
    async def clear_review(request: Request, vehicle_id: int):
        if db.get_vehicle(vehicle_id) is None:
            raise HTTPException(404, "vehicle not found")
        db.execute("UPDATE vehicles SET needs_review = 0 WHERE pk = ?", (vehicle_id,))
        return _back(request, f"/vehicles/{vehicle_id}", "Review flag cleared.")

    @app.post("/api/sensors/{sensor_pk}")
    async def update_sensor(request: Request, sensor_pk: int):
        form = await request.form()
        sensor = db.get_sensor(sensor_pk)
        if sensor is None:
            raise HTTPException(404, "sensor not found")

        display = f"{sensor.model}/{sensor.sensor_id}"
        done: list[str] = []
        label = sensor.wheel_label

        if "wheel_label" in form:
            label = (form.get("wheel_label") or "").strip() or None
            db.execute("UPDATE sensors SET wheel_label = ? WHERE pk = ?", (label, sensor_pk))
            done.append(f"labelled {label}" if label else "wheel label cleared")

        if "pinned" in form:
            pinned = 1 if form.get("pinned") in ("1", "true", "on") else 0
            db.execute("UPDATE sensors SET pinned = ? WHERE pk = ?", (pinned, sensor_pk))
            done.append(
                "pinned, so clustering will leave it alone"
                if pinned
                else "unpinned, so clustering can place it again"
            )

        if "ignored" in form:
            ignored = 1 if form.get("ignored") in ("1", "true", "on") else 0
            db.execute(
                "UPDATE sensors SET ignored = ? WHERE pk = ?", (ignored, sensor_pk)
            )
            if ignored:
                # A hidden sensor must not keep a vehicle alive behind the
                # scenes, and must not stay in a cluster it is excluded from.
                db.set_sensor_vehicle(sensor_pk, None)
                db.delete_empty_vehicles()
            done.append("hidden from the lists" if ignored else "shown again")

        if "vehicle_id" in form:
            raw = (form.get("vehicle_id") or "").strip()
            if raw == "new":
                target = db.create_vehicle(now_ts(), auto_generated=False)
            elif raw in ("", "none"):
                target = None
            else:
                try:
                    target = int(raw)
                except ValueError:
                    raise HTTPException(400, "not a vehicle") from None
                if db.get_vehicle(target) is None:
                    raise HTTPException(404, "vehicle not found")
            # Only act on a real change. This form used to be submitted
            # alongside the wheel label, and branching on the field being
            # *present* meant labelling a wheel silently pinned the sensor and
            # reported a move that never happened.
            if target != sensor.vehicle_id:
                db.set_sensor_vehicle(sensor_pk, target)
                # Manual moves are only durable if the clusterer keeps its
                # hands off, so pin the sensor as part of the same action.
                db.execute("UPDATE sensors SET pinned = 1 WHERE pk = ?", (sensor_pk,))
                db.delete_empty_vehicles()
                done.append(
                    f"moved to {_vehicle_name(target)}" if target else "unassigned"
                )

        message = f"{display}: {', '.join(done)}." if done else "Nothing to change."
        return _back(request, "/sensors", message, wheel_label=label)

    @app.post("/api/aliases")
    async def redetect_aliases(request: Request):
        report = service.detect_aliases()
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"summary": report.summary()})
        return _back(request, "/sensors", report.summary())

    @app.post("/api/recluster")
    async def recluster(request: Request):
        report = service.recluster()
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"summary": report.summary()})
        return _back(request, "/vehicles", report.summary())

    @app.post("/api/radio/restart")
    async def radio_restart(request: Request):
        service.radio.restart()
        return _back(request, "/status", "Receiver restarted.")

    @app.get("/api/export.csv")
    def export_csv(
        since: str | None = None,
        until: str | None = None,
        vehicle: int | None = None,
        sensor: int | None = None,
        view: str = "passes",
    ):
        """The log as a file, in whichever shape the page is showing.

        The download used to always be sensor sightings regardless of what was
        on screen, and over a different row limit, so the two disagreed.
        """
        view = view if view in ("passes", "sightings") else "passes"
        start, end = _when("since", since), _when("until", until)
        seeing_hidden = sensor is not None
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        if view == "passes":
            writer.writerow(
                ["vehicle", "sensor", "first_heard", "last_heard", "duration_s",
                 "wheels_heard", "wheels_known", "readings", "max_rssi", "band",
                 "still_audible"]
            )
            for row in q.vehicle_passes(
                db, gap, start=start, end=end, vehicle_id=vehicle,
                sensor_pk=sensor, limit=100000, scan_limit=1000000,
                include_ignored=seeing_hidden,
            ):
                writer.writerow(
                    [
                        row["vehicle_name"] or "",
                        row["display"] or "",
                        row["started_at_iso"],
                        row["last_reading_at_iso"],
                        round(row["duration"], 1),
                        row["wheels_heard"],
                        row["wheels_known"] if row["wheels_known"] is not None else "",
                        row["reading_count"],
                        row["max_rssi"] if row["max_rssi"] is not None else "",
                        row["band"] or "",
                        "yes" if row["open"] else "no",
                    ]
                )
        else:
            writer.writerow(
                ["vehicle", "sensor", "wheel", "first_heard", "last_heard",
                 "duration_s", "readings", "max_rssi", "band", "still_audible"]
            )
            for row in q.events(
                db, start=start, end=end, vehicle_id=vehicle,
                sensor_pk=sensor, limit=100000, include_ignored=seeing_hidden,
            ):
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
                        row["band"] or "",
                        "yes" if row["open"] else "no",
                    ]
                )

        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv",
            headers={
                "Content-Disposition":
                    f'attachment; filename="tpms-{view}.csv"'
            },
        )

    return app
