"""FastAPI application: pages, JSON API and the live SSE feed."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import logging
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator

from .. import config as cfg
from .. import direction as direction_mod
from .. import sides as sides_mod
from .. import queries as q
from ..models import Vehicle, now as now_ts, parse_when, to_iso
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

#: Whether that message is an outcome or a refusal. Separate from the message
#: so a turned-down action cannot arrive looking like a save that worked.
FLASH_KIND_COOKIE = "tpms_flash_kind"

#: Set by static/forms.js on the submissions it handles itself. Those want the
#: outcome as JSON rather than a whole page they are not going to render.
ASYNC_HEADER = "x-tpms-async"


def _blank_to_none(value: Any) -> Any:
    """Read an empty filter box as "no filter" rather than a bad number.

    The filter form and the links built from it spell every filter out, so an
    unset vehicle arrives as ``vehicle=`` and was rejected as an unparseable
    integer.
    """
    return None if value == "" else value


#: An id filter that may arrive absent, or present but empty.
OptionalId = Annotated[int | None, BeforeValidator(_blank_to_none)]


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
    # Exposed because lifespan shutdown is too late to release the streams:
    # uvicorn waits for in-flight responses first, and an SSE response only
    # finishes once this is set. `tpms serve` sets it from the signal handler.
    app.state.shutting_down = shutting_down
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["duration"] = _fmt_duration
    templates.env.filters["ago"] = _fmt_ago
    templates.env.filters["iso"] = to_iso
    templates.env.filters["bytes"] = human_bytes
    db = service.db
    templates.env.globals["wheel_positions"] = direction_mod.WHEEL_POSITIONS
    templates.env.globals["wheel_position_groups"] = direction_mod.WHEEL_POSITION_GROUPS

    # Settings are read per request, never captured here.
    #
    # These were once locals, bound when the app was built. That was harmless
    # while a config could only change by restarting the process -- and became
    # a bug the moment the Settings page could change one, because a saved
    # value would be written to disk, adopted by the service, and still not
    # reach the log until a restart. A page that appears to do nothing is the
    # worst of the outcomes, so nothing caches a setting.
    #
    # Exposed as callables and called in the templates -- `{{ timezone() }}`.
    # Jinja renders a bare function as its repr rather than calling it, and a
    # macro cannot see the render context, so a global that is called is the
    # one shape that works from inside _macros.html as well as from a page.
    templates.env.globals["timezone"] = lambda: service.config.timezone
    templates.env.globals["direction_names"] = lambda: service.config.direction.names

    def gap() -> float:
        return service.config.sessions.gap_seconds

    def rssi_margin() -> float:
        return service.config.direction.rssi_margin

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        context.setdefault("nav", name.replace(".html", ""))
        flash = request.cookies.get(FLASH_COOKIE)
        context.setdefault("flash", unquote(flash) if flash else None)
        context.setdefault("flash_kind", request.cookies.get(FLASH_KIND_COOKIE) or "ok")
        # On every page, not only Settings: a saved value that is not yet in
        # force is a state the whole UI is in, and the flash that announced it
        # is gone by the next click.
        context.setdefault("restart_pending", list(service.restart_pending))
        response = templates.TemplateResponse(request, name, context)
        if flash:
            response.delete_cookie(FLASH_COOKIE, path="/")
            response.delete_cookie(FLASH_KIND_COOKIE, path="/")
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
            {"nav": "", "flash": None, "flash_kind": "ok", "status": exc.status_code,
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
            {"nav": "events", "flash": None, "flash_kind": "ok", "status": 400, "detail": detail,
             "back": "/events"},
            status_code=400,
        )

    def _vehicle_choices() -> list[Vehicle]:
        """Vehicles as a pick-list: named ones A-Z, unnamed ones after.

        Insertion order is meaningless to someone hunting for a name in a
        dropdown, and the unnamed ones are numbered, so they read as a run
        rather than scattered between the names.
        """
        return sorted(
            db.list_vehicles(),
            key=lambda v: (v.name is None or not v.name.strip(),
                           (v.name or "").strip().casefold(), v.pk),
        )

    # -- pages ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def live(request: Request):
        return page(
            request,
            "live.html",
            heard=q.heard_now_groups(db),
            gap=gap(),
            now=now_ts(),
            vehicles=_vehicle_choices(),
        )

    @app.get("/vehicles", response_class=HTMLResponse)
    def vehicles(request: Request):
        return page(
            request,
            "vehicles.html",
            vehicles=q.vehicle_summaries(db, gap()),
            unassigned=[s for s in q.sensor_rows(db) if s["vehicle_id"] is None],
        )

    @app.get("/vehicles/{vehicle_id}", response_class=HTMLResponse)
    def vehicle_detail(request: Request, vehicle_id: int):
        vehicle = db.get_vehicle(vehicle_id)
        if vehicle is None:
            raise HTTPException(404, "vehicle not found")
        sensors = [q.sensor_row(db, s.pk) for s in db.sensors_for_vehicle(vehicle_id)]
        history = {s["pk"]: q.pressure_history(db, s["pk"], 200) for s in sensors}
        # The same rows the log shows, filtered to this vehicle: a pass is one
        # thing, and the page you open to study one vehicle should not know
        # less about its passes than the log does. Read once -- the table shows
        # the newest hundred, and the sides panel counts every confirmation
        # there has ever been, which is not the same set.
        passes = q.vehicle_passes(
            db, gap(), vehicle_id=vehicle_id, limit=2000, rssi_margin=rssi_margin()
        )
        return page(
            request,
            "vehicle.html",
            nav="vehicles",
            vehicle=vehicle,
            sensors=sensors,
            history=history,
            passes=passes[:100],
            all_vehicles=[v for v in _vehicle_choices() if v.pk != vehicle_id],
            **_sides_context(vehicle_id, sensors, passes),
        )

    def _sides_context(
        vehicle_id: int,
        sensors: list[dict[str, Any]] | None = None,
        passes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """What the confirmed passes say about this vehicle's wheels.

        The margin the proposal scales levels by is the one the direction
        heuristic uses. Two numbers for "louder by enough to mean something"
        would let the panel and the pills it is scoring disagree about the
        evidence they are reading.
        """
        if sensors is None:
            sensors = [q.sensor_row(db, s.pk) for s in db.sensors_for_vehicle(vehicle_id)]
        if passes is None:
            passes = q.vehicle_passes(
                db, gap(), vehicle_id=vehicle_id, limit=2000, rssi_margin=rssi_margin()
            )
        return {
            "vehicle_pk": vehicle_id,
            "sides": sides_mod.propose(passes, sensors, level_scale=rssi_margin()),
            "accuracy": sides_mod.accuracy(passes),
            "min_passes": sides_mod.MIN_PASSES_PER_SIDE,
        }

    @app.get("/api/vehicles/{vehicle_id}/sides", response_class=HTMLResponse)
    def vehicle_sides(request: Request, vehicle_id: int):
        """The sides panel on its own, so the page can refresh it in place.

        Marking a pass changes every number in that panel. Rendering it here
        rather than rebuilding it in script keeps one description of what the
        confirmations add up to -- the alternative is a second implementation
        in JavaScript that can drift from this one.
        """
        if db.get_vehicle(vehicle_id) is None:
            raise HTTPException(404, "vehicle not found")
        return templates.TemplateResponse(
            request, "_sides.html", _sides_context(vehicle_id)
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
            vehicles=_vehicle_choices(),
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
            gap=gap(),
            vehicles=_vehicle_choices(),
        )

    @app.get("/events", response_class=HTMLResponse)
    def events(
        request: Request,
        since: str | None = None,
        until: str | None = None,
        vehicle: OptionalId = None,
        sensor: OptionalId = None,
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
                db, gap(), start=start, end=end, vehicle_id=vehicle,
                sensor_pk=sensor, limit=limit, scan_limit=PASS_SCAN_LIMIT,
                include_ignored=seeing_hidden, rssi_margin=rssi_margin(),
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
            vehicles=_vehicle_choices(),
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

    # -- settings ---------------------------------------------------------

    def _grouped_settings() -> list[dict[str, Any]]:
        """The editable config, in the order the file declares it."""
        groups: list[dict[str, Any]] = []
        index: dict[str | None, dict[str, Any]] = {}
        for setting in cfg.settings(service.config):
            group = index.get(setting.section)
            if group is None:
                group = {
                    "name": setting.section,
                    "help": cfg.SECTION_HELP.get(setting.section or "", ""),
                    "restart": setting.section in cfg.NEEDS_RADIO_RESTART,
                    "process_restart": setting.section in cfg.NEEDS_PROCESS_RESTART,
                    "settings": [],
                }
                index[setting.section] = group
                groups.append(group)
            group["settings"].append(setting)
        return groups

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return page(
            request,
            "settings.html",
            groups=_grouped_settings(),
            write_path=str(service.config.write_path),
            untracked=service.config.source_path is None,
        )

    @app.post("/api/settings")
    async def save_settings(request: Request):
        """Take the form, put it into effect, then write it down.

        In that order, and both or neither. A value the running program will
        not accept must not reach the file, or the next start reads a config
        that cannot be loaded -- so everything is parsed and validated before
        anything is assigned, and the file is written from what the process
        actually holds rather than from the form.
        """
        form = await request.form()
        here = "/settings"

        values: dict[str, Any] = {}
        for setting in cfg.settings(service.config):
            if setting.read_only:
                continue
            if setting.kind == "bool":
                # An unticked checkbox sends nothing at all, which is the
                # difference between "off" and "not on this form".
                if f"seen:{setting.path}" not in form:
                    continue
                values[setting.path] = setting.path in form
                continue
            if setting.path not in form:
                continue
            try:
                values[setting.path] = cfg.coerce(setting, form.get(setting.path))
            except cfg.SettingError as error:
                return _refuse(request, here, str(error))

        changed = cfg.apply(service.config, values)
        if not changed:
            return _back(request, here, "Nothing to change.")

        try:
            path = service.config.write_path
            path.parent.mkdir(parents=True, exist_ok=True)
            # Keep the last version. Comments in this file are regenerated on
            # every save, so an accidental save is otherwise unrecoverable.
            if path.exists():
                path.with_suffix(path.suffix + ".bak").write_text(path.read_text())
            path.write_text(cfg.dump(cfg.to_dict(service.config)))
        except OSError as error:
            # The process has already adopted the change, so say both halves.
            return _back(
                request, here,
                f"Applied, but could not write {service.config.write_path}: "
                f"{error}. The change is live and will be lost on restart.",
                kind="err",
            )

        radio = sorted(p for p in changed if p.split(".")[0] in cfg.NEEDS_RADIO_RESTART)
        process = sorted(
            p for p in changed if p.split(".")[0] in cfg.NEEDS_PROCESS_RESTART
        )
        # Remembered on the service, not only said in the flash: the reminder
        # has to outlive the next click, or a setting that is saved but not in
        # force becomes invisible the moment the page changes.
        for path in process:
            if path not in service.restart_pending:
                service.restart_pending.append(path)

        message = f"Saved {len(changed)} change{'' if len(changed) == 1 else 's'}."
        if radio:
            message += " Restart the receiver for the radio settings to take effect."
        if process:
            message += (
                " Restart the service for "
                f"{', '.join(process)} to take effect."
            )
        return _back(request, here, message)

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
            {"heard": q.heard_now_groups(db), "vehicles": _vehicle_choices()},
        )

    @app.get("/api/heard-now")
    def api_heard_now():
        # The flat list, for anything reading this as data rather than
        # rendering the page's grouping.
        return {"heard": q.heard_now(db), "gap": gap(), "now": now_ts()}

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
        return q.vehicle_presence(db, vehicle_id, gap(), start, end, buckets)

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
        keep_referer: bool = True,
        kind: str = "ok",
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
        # keep_referer=False: the page the request came from may not exist
        # any more -- moving the last sensor off a vehicle deletes it.
        landing = request.headers.get("referer", fallback) if keep_referer else fallback
        response = RedirectResponse(landing, status_code=303)
        if message:
            # quote(): messages carry vehicle names, and a stray comma or
            # semicolon would truncate the cookie.
            response.set_cookie(
                FLASH_COOKIE, quote(message), path="/", max_age=30, samesite="lax"
            )
            response.set_cookie(
                FLASH_KIND_COOKIE, kind, path="/", max_age=30, samesite="lax"
            )
        return response

    def _refuse(request: Request, fallback: str, message: str):
        """Turn down a mutation without throwing away the page it came from.

        A bulk action with nothing ticked used to raise, and because these
        handlers live under /api/ the error handler answered a browser form
        post with raw JSON: no shell, no nav, and every tick the user had just
        made gone. A refusal is not a broken URL -- it is the page saying no,
        so it goes back to the page and says so there.
        """
        if request.headers.get(ASYNC_HEADER):
            return JSONResponse({"ok": False, "message": message}, status_code=400)
        return _back(request, fallback, message, kind="err")

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
        # provisional = 0: the flag means "clustering grouped these from a
        # single pass and has not seen them together since". A vehicle a
        # person has named is not that, and waiting for the clusterer to
        # notice would never have worked -- it skips the vehicles it no
        # longer owns.
        db.execute(
            "UPDATE vehicles SET name = ?, notes = ?, auto_generated = 0, "
            "provisional = 0 WHERE pk = ?",
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
        db.execute(
            "UPDATE vehicles SET auto_generated = 0, provisional = 0 WHERE pk = ?",
            (vehicle_id,),
        )
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
        here = f"/vehicles/{vehicle_id}"
        if not chosen:
            return _refuse(request, here, "Tick the sensors to split out first.")
        if chosen == members:
            return _refuse(
                request, here,
                "That is every sensor, which is a rename rather than a split. "
                "Leave at least one wheel here.",
            )

        target = db.create_vehicle(now_ts(), auto_generated=False)
        for pk in sorted(chosen):
            db.set_sensor_vehicle(pk, target)
            # Pinned for the same reason a single move is: otherwise the next
            # clustering run puts them straight back.
            db.execute("UPDATE sensors SET pinned = 1 WHERE pk = ?", (pk,))
        # The split is the review being resolved.
        db.execute("UPDATE vehicles SET needs_review = 0 WHERE pk = ?", (vehicle_id,))
        db.delete_empty_vehicles()
        # Land on the vehicle that was just created, not back on the one it
        # came out of: it has no name yet, and naming it is the next thing the
        # user is going to do. keep_referer would have kept them here, which
        # left the flash naming a vehicle they had no way to reach.
        return _back(
            request,
            f"/vehicles/{target}",
            f"Split {len(chosen)} sensor(s) out of {_vehicle_name(vehicle_id)} "
            f"into this vehicle, pinned here. Give it a name.",
            keep_referer=False,
        )

    @app.post("/api/vehicles/{vehicle_id}/move")
    async def move_sensors(request: Request, vehicle_id: int):
        """Move the ticked sensors to another vehicle, or off vehicles entirely.

        One picker under the table, not one per row. The row control was a
        select carrying every vehicle in the program -- repeated down every
        row, defaulting to a non-action ("stay here"), and with "split" and
        "unassign" mixed into the same list as the destinations.
        """
        form = await request.form()
        if db.get_vehicle(vehicle_id) is None:
            raise HTTPException(404, "vehicle not found")
        try:
            wanted = {int(v) for v in form.getlist("sensor")}
        except ValueError:
            raise HTTPException(400, "not a sensor") from None
        chosen = wanted & {s.pk for s in db.sensors_for_vehicle(vehicle_id)}
        here = f"/vehicles/{vehicle_id}"
        if not chosen:
            return _refuse(request, here, "Tick the sensors to move first.")

        raw = (form.get("target") or "").strip()
        if not raw:
            return _refuse(request, here, "Choose where to move them first.")
        if raw == "none":
            target = None
        else:
            try:
                target = int(raw)
            except ValueError:
                raise HTTPException(400, "not a vehicle") from None
            if db.get_vehicle(target) is None:
                raise HTTPException(404, "vehicle not found")
            if target == vehicle_id:
                return _refuse(request, here, "Those sensors are already here.")

        for pk in sorted(chosen):
            db.set_sensor_vehicle(pk, target)
            # A manual placement only sticks if clustering keeps its hands
            # off, so pin them as part of the same action.
            db.execute("UPDATE sensors SET pinned = 1 WHERE pk = ?", (pk,))
        db.delete_empty_vehicles()
        return _back(
            request,
            f"/vehicles/{target}" if target else "/vehicles",
            f"Moved {len(chosen)} sensor(s) to "
            + (_vehicle_name(target) if target else "no vehicle")
            + ", pinned there.",
            keep_referer=db.get_vehicle(vehicle_id) is not None,
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
                # Say the pin happened. Moving a sensor by hand pins it, and
                # a flag the user did not ask for and is not told about is
                # the whole of why pinning reads as arbitrary.
                done.append(
                    f"moved to {_vehicle_name(target)} and pinned there"
                    if target
                    else "unassigned and pinned"
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

    @app.post("/api/passes/{sighting_pk}/mark")
    async def mark_pass(request: Request, sighting_pk: int):
        """Record which side of a vehicle was seen facing the receiver.

        Anchored on a sighting because a pass is not a row: it is however many
        sightings the current join gap merges, and the answer someone gave with
        their own eyes must survive that setting being changed.
        """
        form = await request.form()
        raw = (form.get("side") or "").strip().lower()
        if raw not in ("", "none", direction_mod.LEFT, direction_mod.RIGHT):
            raise HTTPException(400, f"not a side: {raw}")
        if db.query_one("SELECT 1 FROM sightings WHERE pk = ?", (sighting_pk,)) is None:
            raise HTTPException(404, "sighting not found")

        side = None if raw in ("", "none") else raw
        db.mark_pass(sighting_pk, side, now_ts())
        names = service.config.direction.names
        message = (
            f"Pass confirmed as {names.get(side) or side + ' side'}."
            if side
            else "Confirmation cleared."
        )
        return _back(request, "/events", message)

    @app.post("/api/vehicles/{vehicle_id}/apply-sides")
    async def apply_sides(request: Request, vehicle_id: int):
        """Label the wheels with the sides the confirmed passes propose.

        A corner label keeps its front-or-rear half -- someone who worked out
        that a sensor is a rear wheel did not learn it from the radio, and this
        knows nothing about that half of the answer.
        """
        if db.get_vehicle(vehicle_id) is None:
            raise HTTPException(404, "vehicle not found")
        applied = []
        for evidence in _sides_context(vehicle_id)["sides"]:
            if not evidence.changes:
                continue
            label = direction_mod.side_label(evidence.wheel_label, evidence.side)
            db.execute(
                "UPDATE sensors SET wheel_label = ? WHERE pk = ?",
                (label, evidence.sensor_pk),
            )
            applied.append(f"{evidence.display} \u2192 {label}")
        if not applied:
            return _back(request, f"/vehicles/{vehicle_id}", "Nothing to change.")
        return _back(
            request,
            f"/vehicles/{vehicle_id}",
            f"Labelled {len(applied)} wheel{'' if len(applied) == 1 else 's'}: "
            + ", ".join(applied),
        )

    @app.post("/api/radio/restart")
    async def radio_restart(request: Request):
        service.radio.restart()
        return _back(request, "/status", "Receiver restarted.")

    @app.post("/api/service/restart")
    async def service_restart(request: Request):
        """Restart the whole program, not just the receiver.

        The narrower button covers everything the radio reads at startup; this
        covers what only a new process reads -- the address the web server is
        bound to, and a config file edited by hand outside this UI. The reply
        goes out first and the exec follows a moment later, so the browser has
        a page to reload rather than a dropped connection.
        """
        service.restart_pending.clear()
        service.restart_process()
        return _back(
            request, "/settings",
            "Restarting the service. This page will answer again in a moment.",
        )

    @app.get("/api/export.csv")
    def export_csv(
        since: str | None = None,
        until: str | None = None,
        vehicle: OptionalId = None,
        sensor: OptionalId = None,
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
                 "still_audible", "direction", "direction_basis"]
            )
            for row in q.vehicle_passes(
                db, gap(), start=start, end=end, vehicle_id=vehicle,
                sensor_pk=sensor, limit=100000, scan_limit=1000000,
                include_ignored=seeing_hidden, rssi_margin=rssi_margin(),
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
                        # The screen and the CSV are one view, so a column the
                        # log shows is a column the export carries.
                        row["heading"].name(service.config.direction.names)
                        if row["heading"] else "",
                        row["heading"].basis if row["heading"] else "",
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
