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

from . import idfamily
from .aliases import AliasDetector, common_hex_run
from .cluster import Clusterer
from .config import load_config
from .db import Database
from .ingest import Ingestor
from .models import band_label, to_iso
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

    if args.no_radio:
        log.warning("radio disabled; serving stored data only")
    log.info("web UI on http://%s:%s", config.web.host, config.web.port)
    log.info("press Ctrl+C to stop")

    # The service is started and stopped by the app's lifespan, so shutdown
    # runs as part of uvicorn's own sequence rather than after it returns.
    try:
        uvicorn.run(app, host=config.web.host, port=config.web.port, log_level="warning")
    finally:
        service.stop()  # belt and braces if uvicorn never reached lifespan
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
    # Through Service, so this does exactly what the web UI's button does --
    # duplicate detection first, then clustering.
    service = Service(config, start_radio=False)
    db = service.db
    report = service.recluster(dry_run=args.dry_run)
    if service.last_alias_report is not None:
        print("duplicates: " + service.last_alias_report.summary())

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


def cmd_aliases(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    db = Database(config.database_path)
    detector = AliasDetector(db, config.aliases)

    if args.explain:
        cfg = config.aliases
        print(f"thresholds: dt<={cfg.time_tolerance}s dRSSI<={cfg.rssi_tolerance} "
              f"dSNR<={cfg.snr_tolerance} min_ratio={cfg.min_share_ratio}")
        print("\nevery cross-decoder pair heard within 10s, with real deltas:\n")
        rows = detector.explain()
        if not rows:
            print("  none -- no two decoders ever fired close together in time")
            return 0
        for row in rows:
            fmt = lambda v: "  n/a" if v is None else f"{v:5.1f}"
            verdict = "; ".join(row["blockers"]) if row["blockers"] else "MATCH"
            shared = f" id'{row['common_id']}'" if row["common_id"] else ""
            print(f"  {row['a']:22} ~ {row['b']:22} "
                  f"dt={fmt(row['dt'])}s dRSSI={fmt(row['drssi'])} dSNR={fmt(row['dsnr'])}"
                  f"{shared}  {verdict}")
        return 0

    report = detector.run(dry_run=args.dry_run)

    print(("[dry run] " if args.dry_run else "") + report.summary())
    for pair in sorted(report.pairs, key=lambda p: -p.shared):
        a, b = db.get_sensor(pair.a), db.get_sensor(pair.b)
        shared_id = f" shared id '{pair.common_id}'" if pair.common_id else ""
        print(
            f"  {a.display:26} == {b.display:26} "
            f"{pair.shared} identical burst(s), {pair.ratio:.0%}{shared_id}"
        )
    if not report.pairs:
        print("  no duplicate decodes found")
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    """Explain why sensors are or are not grouping into vehicles."""
    config = load_config(args.config)
    db = Database(config.database_path)
    cfg = config.clustering
    clusterer = Clusterer(db, cfg)

    sensors = {s.pk: s for s in db.list_sensors()}
    canonical = {pk for pk, s in sensors.items() if s.alias_of is None}
    counts = db.sighting_counts()

    print(f"sensors:           {len(sensors)} ({len(canonical)} after removing duplicates)")
    print(f"vehicles:          {len(db.list_vehicles())}")
    print(f"thresholds:        min_cooccurrences={cfg.min_cooccurrences} "
          f"min_support={cfg.min_support} window={cfg.window_seconds}s "
          f"single_pass={cfg.single_pass}")

    seen_once = [pk for pk in canonical if counts.get(pk, 0) <= 1]
    print(f"heard only once:   {len(seen_once)} of {len(canonical)} sensors")
    if seen_once and not cfg.single_pass:
        print("  -> these can never reach min_cooccurrences. Set "
              "clustering.single_pass: true to group them from one pass.")

    rows = db.cooccurrence_rows()
    if not rows:
        print("\nNo sensor has ever been heard alongside another.")
        print("Either only one vehicle is in range, or the co-occurrence window")
        print(f"({cfg.window_seconds}s) is shorter than the gap between wheels.")
        return 0

    # Pairs involving a duplicate decode are noise -- they are the same
    # transmitter twice and are excluded from clustering anyway.
    scored = []
    duplicates = 0
    for row in rows:
        a, b, count = int(row["a"]), int(row["b"]), int(row["count"])
        if sensors[a].alias_of or sensors[b].alias_of:
            duplicates += 1
            continue
        denominator = min(counts.get(a, 0), counts.get(b, 0))
        support = count / denominator if denominator else 0.0
        scored.append((count, support, a, b))
    scored.sort(reverse=True)

    print(f"\npairs heard together: {len(rows)} "
          f"({duplicates} of them duplicate decodes, ignored)")

    profiles = clusterer._profiles() if cfg.single_pass else {}
    print("\nclosest real pairs to becoming a vehicle:")
    for count, support, a, b in scored[: args.limit]:
        if count >= cfg.min_cooccurrences and support >= cfg.min_support:
            verdict = "GROUPED (confirmed)"
        elif cfg.single_pass and clusterer._same_vehicle_shape(a, b, profiles):
            verdict = "GROUPED (provisional -- one pass only)"
        else:
            reasons = []
            if count < cfg.min_cooccurrences:
                reasons.append(f"needs {cfg.min_cooccurrences - count} more shared pass(es)")
            if support < cfg.min_support:
                reasons.append(f"support {support:.2f} < {cfg.min_support}")
            if cfg.single_pass:
                left, right = profiles.get(a), profiles.get(b)
                if left and right and not (left[0] & right[0]):
                    reasons.append(
                        f"no shared decoder ({'/'.join(sorted(left[0]))} vs "
                        f"{'/'.join(sorted(right[0]))})"
                    )
                elif left and right and left[1] is not None and right[1] is not None:
                    spread = abs(left[1] - right[1])
                    if spread > cfg.single_pass_rssi_spread:
                        reasons.append(
                            f"signal levels {spread:.0f} dB apart "
                            f"(> {cfg.single_pass_rssi_spread:.0f})"
                        )
            verdict = "; ".join(reasons) or "below thresholds"
        print(f"  {sensors[a].display:24} ~ {sensors[b].display:24} "
              f"n={count} support={support:.2f}  {verdict}")
    if not scored:
        print("  (none -- every co-occurring pair was a duplicate decode)")

    edges = clusterer.build_edges(canonical)
    print(f"\nedges that would form: {len(edges)} "
          f"({sum(1 for e in edges if e.confirmed)} confirmed, "
          f"{sum(1 for e in edges if not e.confirmed)} provisional)")
    for component in clusterer.components(edges):
        names = ", ".join(sensors[pk].display for pk in component)
        print(f"  cluster ({len(component)}): {names}")
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
                "duration_s", "readings", "max_rssi", "band", "still_open",
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
                    row["band"] or "",
                    "yes" if row["open"] else "no",
                ]
            )
    finally:
        if args.out:
            stream.close()
            print(f"wrote {len(rows)} rows to {args.out}", file=sys.stderr)
    return 0


def cmd_ids(args: argparse.Namespace) -> int:
    """Report whether sensor IDs say anything about which wheels pair up."""
    config = load_config(args.config)
    db = Database(config.database_path)
    max_distance = config.clustering.id_max_distance
    card = idfamily.evaluate(db, config.clustering, max_distance)
    displays = {s.pk: s.display for s in db.list_sensors()}

    print(f"comparing IDs within a decoder, up to {max_distance} apart\n")

    if card.families:
        print(f"candidate wheel sets by ID alone: {len(card.families)}")
        for family in card.families:
            names = ", ".join(displays.get(pk, "?") for pk in family.members)
            print(f"  {family.model:16} {names}")
    else:
        print("no two sensors from one decoder have IDs close enough to pair.")

    print("\nagreement with co-occurrence:")
    recall = card.recall
    print(f"  confirmed pairs:      {card.confirmed_pairs}")
    if recall is not None:
        print(f"  ...of which ID-near:  {card.confirmed_near} ({recall:.0%})")
    noise = card.false_positive_rate
    print(f"  never heard together: {card.apart_pairs}")
    if noise is not None:
        print(f"  ...of which ID-near:  {card.apart_near} ({noise:.1%})  <- want this low")
    print(f"\nverdict: {card.verdict()}")

    if args.explain:
        print("\nper-pair detail:")
        sensors = [s for s in db.list_sensors() if s.alias_of is None]
        parsed = idfamily._parsed(sensors)
        for i, a in enumerate(sorted(parsed)):
            for b in sorted(parsed)[i + 1:]:
                if parsed[a][0] != parsed[b][0]:
                    continue
                distance = idfamily.id_distance(parsed[a][1], parsed[b][1])
                mark = "near" if distance <= max_distance else "    "
                shared = common_hex_run(
                    db.get_sensor(a).sensor_id, db.get_sensor(b).sensor_id
                )
                print(f"  {mark} {displays[a]:24} {displays[b]:24} "
                      f"distance={distance}" + (f" shared={shared}" if shared else ""))
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    """Delete every sensor from one decoder, with its readings and history.

    Defaults to a dry run: this destroys captured data, and the pattern is a
    substring, so it is easy to match more than intended.
    """
    config = load_config(args.config)
    service = Service(config, start_radio=False)

    preview = service.purge_decoder(args.pattern, dry_run=True)
    matches = preview["sensors"]
    if not matches:
        print(f"no sensors match {args.pattern!r}")
        available = sorted({s.model for s in service.db.list_sensors()})
        if available:
            print("decoders on record: " + ", ".join(available))
        return 0

    counts = service.db.query_one(
        "SELECT COUNT(*) AS readings FROM readings WHERE sensor_pk IN "
        "(SELECT pk FROM sensors WHERE LOWER(model) LIKE ?)",
        (f"%{args.pattern.strip().lower()}%",),
    )
    print(f"{len(matches)} sensor(s) match {args.pattern!r}, "
          f"{counts['readings']} reading(s):")
    for name in matches:
        print(f"  {name}")

    if args.dry_run:
        print("[dry run] nothing deleted")
        return 0

    if not args.yes:
        # Deleting captured data cannot be undone; the raw archive under raw/
        # is the only copy left afterwards.
        answer = input("delete these permanently? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("cancelled")
            return 1

    result = service.purge_decoder(args.pattern)
    c = result["counts"]
    print(
        f"deleted {c['sensors']} sensor(s), {c['readings']} reading(s), "
        f"{c['sightings']} sighting(s), {c['cooccurrence']} co-occurrence row(s)"
    )
    if result["vehicles_removed"]:
        print(f"removed {result['vehicles_removed']} vehicle(s) left with no sensors")
    print("clustering: " + result["clustering"])
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
    bands: dict[str, int] = {}
    for row in db.query(
        "SELECT freq_mhz, COUNT(*) AS n FROM readings "
        "WHERE freq_mhz IS NOT NULL GROUP BY freq_mhz"
    ):
        label = band_label(row["freq_mhz"])
        if label:
            bands[label] = bands.get(label, 0) + int(row["n"])
    if bands:
        summary = ", ".join(
            f"{label} ({n})" for label, n in sorted(bands.items(), key=lambda kv: -kv[1])
        )
        print(f"bands:     {summary}")
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

    ids = sub.add_parser(
        "ids", help="measure whether sensor IDs indicate which wheels pair up"
    )
    ids.add_argument(
        "--explain", action="store_true", help="show every same-decoder pair's distance"
    )
    ids.set_defaults(func=cmd_ids)

    purge = sub.add_parser(
        "purge",
        help="delete every sensor from one decoder (e.g. after excluding it)",
    )
    purge.add_argument(
        "pattern",
        help="decoder name, matched as a case-insensitive substring of the model",
    )
    purge.add_argument(
        "--dry-run", action="store_true", help="list what would go, delete nothing"
    )
    purge.add_argument(
        "-y", "--yes", action="store_true", help="skip the confirmation prompt"
    )
    purge.set_defaults(func=cmd_purge)

    aliases = sub.add_parser(
        "aliases", help="find one transmitter decoded by several protocols"
    )
    aliases.add_argument("--dry-run", action="store_true")
    aliases.add_argument(
        "--explain",
        action="store_true",
        help="show the real time/RSSI/SNR deltas and what blocked each pair",
    )
    aliases.set_defaults(func=cmd_aliases)

    diagnose = sub.add_parser(
        "diagnose", help="explain why sensors are or are not grouping"
    )
    diagnose.add_argument("--limit", type=int, default=25)
    diagnose.set_defaults(func=cmd_diagnose)

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
