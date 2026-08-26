"""Command line entry points."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .cluster import Clusterer
from .config import load_config
from .db import Database
from .ingest import Ingestor
from .models import to_iso
from .service import Service
from . import queries as q
from .synthetic import generate_lines

log = logging.getLogger("tpms")


def _parse_when(value: str | None) -> float | None:
    """Accept an ISO date/time, or a relative age like '24h' / '7d'."""
    if not value:
        return None
    text = value.strip()
    if text and text[-1] in "smhd" and text[:-1].replace(".", "", 1).isdigit():
        factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}[text[-1]]
        return time.time() - float(text[:-1]) * factor
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"could not parse time {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .web.app import create_app

    config = load_config(args.config)
    service = Service(config, start_radio=not args.no_radio)
    app = create_app(service)

    service.start()
    if args.no_radio:
        log.warning("radio disabled; serving stored data only")
    log.info("web UI on http://%s:%s", config.web.host, config.web.port)
    try:
        uvicorn.run(app, host=config.web.host, port=config.web.port, log_level="warning")
    finally:
        service.stop()
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    db = Database(config.database_path)
    ingestor = Ingestor(db, config.sessions, config.clustering)

    if args.synthetic:
        lines = generate_lines(passes=args.passes)
        log.info("generated %d synthetic events", len(lines))
    else:
        path = Path(args.file)
        if not path.exists():
            raise SystemExit(f"no such file: {path}")
        lines = path.read_text().splitlines()

    started = time.monotonic()
    count = ingestor.replay(lines)
    elapsed = time.monotonic() - started

    # Close every sighting: replayed data is historical, so nothing is still
    # audible now, and leaving sightings open would fake a live presence.
    ingestor.sweep(when=time.time() + 3.15e10)

    print(f"ingested {count} readings from {ingestor.stats['lines']} lines in {elapsed:.2f}s")
    print(f"  skipped (non-TPMS): {ingestor.stats['skipped']}")
    print(f"  malformed lines:    {ingestor.stats['malformed']}")

    if not args.no_cluster:
        report = Clusterer(db, config.clustering).run()
        print(f"clustering: {report.summary()}")
    return 0


def cmd_recluster(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    db = Database(config.database_path)
    report = Clusterer(db, config.clustering).run(dry_run=args.dry_run)

    print(("[dry run] " if args.dry_run else "") + report.summary())
    for members in report.components:
        names = [db.get_sensor(pk).display for pk in members]
        flag = "  << oversized, review" if len(members) > config.clustering.max_cluster_size else ""
        print(f"  cluster ({len(members)}): {', '.join(names)}{flag}")
    if args.verbose:
        for edge in sorted(report.edges, key=lambda e: -e.count):
            a, b = db.get_sensor(edge.a), db.get_sensor(edge.b)
            print(f"    edge {a.display} ~ {b.display}: n={edge.count} support={edge.support:.2f}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    db = Database(config.database_path)
    rows = q.events(
        db,
        start=_parse_when(args.since),
        end=_parse_when(args.until),
        vehicle_id=args.vehicle,
        limit=args.limit,
    )
    stream = open(args.out, "w", newline="") if args.out else sys.stdout
    try:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "vehicle", "sensor", "wheel", "first_heard", "last_heard",
                "duration_s", "readings", "max_rssi", "still_open",
            ]
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
    finally:
        if args.out:
            stream.close()
            print(f"wrote {len(rows)} rows to {args.out}", file=sys.stderr)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    db = Database(config.database_path)
    counts = db.query_one(
        """
        SELECT (SELECT COUNT(*) FROM readings)  AS readings,
               (SELECT COUNT(*) FROM sensors)   AS sensors,
               (SELECT COUNT(*) FROM vehicles)  AS vehicles,
               (SELECT COUNT(*) FROM sightings) AS sightings,
               (SELECT MAX(ts) FROM readings)   AS latest
        """
    )
    print(f"database:  {config.database_path}")
    print(f"readings:  {counts['readings']}")
    print(f"sensors:   {counts['sensors']}")
    print(f"vehicles:  {counts['vehicles']}")
    print(f"sightings: {counts['sightings']}")
    print(f"latest:    {to_iso(counts['latest']) or '-'}")
    for vehicle in q.vehicle_summaries(db, config.sessions.gap_seconds):
        print(
            f"  {vehicle['name']:28} {vehicle['sensor_count']} sensors, "
            f"{vehicle['appearances']} appearances, last {vehicle['last_seen_iso'] or '-'}"
        )
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    lines = generate_lines(passes=args.passes)
    target = Path(args.out)
    target.write_text("\n".join(lines) + "\n")
    print(f"wrote {len(lines)} synthetic events to {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tpms", description="Capture, log and correlate TPMS transmissions."
    )
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run capture and the web UI")
    serve.add_argument(
        "--no-radio",
        action="store_true",
        help="serve stored data without launching rtl_433",
    )
    serve.set_defaults(func=cmd_serve)

    replay = sub.add_parser("replay", help="ingest recorded rtl_433 JSON lines")
    replay.add_argument("file", nargs="?", help="path to a .jsonl capture")
    replay.add_argument("--synthetic", action="store_true", help="use generated data")
    replay.add_argument("--passes", type=int, default=12)
    replay.add_argument("--no-cluster", action="store_true")
    replay.set_defaults(func=cmd_replay)

    recluster = sub.add_parser("recluster", help="rebuild vehicle clusters")
    recluster.add_argument("--dry-run", action="store_true")
    recluster.set_defaults(func=cmd_recluster)

    export = sub.add_parser("export", help="write the sighting log as CSV")
    export.add_argument("--since", help="ISO time or relative age, e.g. 7d")
    export.add_argument("--until", help="ISO time or relative age")
    export.add_argument("--vehicle", type=int, help="restrict to one vehicle id")
    export.add_argument("--limit", type=int, default=100000)
    export.add_argument("-o", "--out", help="output file (default stdout)")
    export.set_defaults(func=cmd_export)

    status = sub.add_parser("status", help="summarise the database")
    status.set_defaults(func=cmd_status)

    simulate = sub.add_parser("simulate", help="write synthetic capture data")
    simulate.add_argument("-o", "--out", default="synthetic.jsonl")
    simulate.add_argument("--passes", type=int, default=12)
    simulate.set_defaults(func=cmd_simulate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if getattr(args, "command", None) == "replay" and not args.file and not args.synthetic:
        raise SystemExit("replay needs a file argument or --synthetic")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
