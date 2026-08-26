"""Housekeeping: keep the database and the raw archive from growing forever.

Measured on a year of real-shaped traffic (6k readings/day, 2.19M rows): 815 MB
of database, 63% of it the archived JSON text of each reading, plus about as
much again in raw/. None of that is a problem on day one and all of it is by
year two, so this trims it on a schedule instead of at a crisis.

The ordering of what gets dropped follows what each thing is worth:

  raw text     duplicated on disk under raw/, so dropping it loses nothing
               `tpms replay` could not put back. Two thirds of the volume.
  readings     the individual packets. Off by default -- deleting them is the
               only step here that actually forgets something.
  archives     compress, then eventually delete.

Sightings, sensors, vehicles and the band rollup are never touched. They are
the summary of everything above at a fraction of the size, so a database
trimmed to a fortnight of readings still knows every vehicle it has heard.
"""

from __future__ import annotations

import gzip
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .config import RetentionConfig
from .db import Database
from .models import now as now_ts

log = logging.getLogger(__name__)

DAY = 86400.0

#: raw/rtl433-YYYY-MM-DD.jsonl, written by RadioSupervisor.
ARCHIVE = re.compile(r"^rtl433-(\d{4})-(\d{2})-(\d{2})\.jsonl(\.gz)?$")


@dataclass
class RetentionReport:
    dry_run: bool = False
    raw_dropped: int = 0
    readings_deleted: int = 0
    archives_compressed: int = 0
    archives_deleted: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    vacuumed: bool = False
    skipped: list[str] = field(default_factory=list)

    @property
    def bytes_freed(self) -> int:
        return max(self.bytes_before - self.bytes_after, 0)

    def did_something(self) -> bool:
        return bool(
            self.raw_dropped
            or self.readings_deleted
            or self.archives_compressed
            or self.archives_deleted
        )

    def summary(self) -> str:
        parts = [
            f"{self.raw_dropped} raw payload(s) dropped",
            f"{self.readings_deleted} reading(s) deleted",
            f"{self.archives_compressed} archive(s) compressed",
            f"{self.archives_deleted} archive(s) deleted",
        ]
        if self.dry_run:
            return "would have: " + ", ".join(parts)
        if self.bytes_freed:
            parts.append(f"{human_bytes(self.bytes_freed)} freed")
        return ", ".join(parts)


def human_bytes(size: float) -> str:
    for unit in ("B", "kB", "MB", "GB"):
        if abs(size) < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def archive_date(name: str) -> date | None:
    match = ARCHIVE.match(name)
    if not match:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def run(
    db: Database,
    config: RetentionConfig,
    archive_dir: Path | None = None,
    dry_run: bool = False,
    now: float | None = None,
) -> RetentionReport:
    """Apply the retention policy. Safe to run at any time, including twice."""
    now = now_ts() if now is None else now
    report = RetentionReport(dry_run=dry_run, bytes_before=db.file_size())
    report.bytes_after = report.bytes_before

    if config.raw_days is not None:
        cutoff = now - config.raw_days * DAY
        report.raw_dropped = (
            db.count_raw_before(cutoff) if dry_run else db.drop_raw_before(cutoff)
        )

    if config.readings_days is not None:
        cutoff = now - config.readings_days * DAY
        # Guard against a config that would empty the database: keeping the
        # last day of readings is what makes the live pages work at all.
        if config.readings_days < 1:
            report.skipped.append("readings_days below 1 day ignored")
        else:
            report.readings_deleted = (
                db.count_readings_before(cutoff)
                if dry_run
                else db.delete_readings_before(cutoff)
            )

    if archive_dir is not None and archive_dir.is_dir():
        _sweep_archive(archive_dir, config, report, now, dry_run)

    # Only worth the rewrite when rows actually went away; dropping raw text
    # leaves free pages behind exactly as a delete does.
    if not dry_run and config.vacuum and (report.raw_dropped or report.readings_deleted):
        db.vacuum()
        report.vacuumed = True

    if not dry_run:
        report.bytes_after = db.file_size()
        if report.did_something():
            log.info("housekeeping: %s", report.summary())
    return report


def _sweep_archive(
    directory: Path,
    config: RetentionConfig,
    report: RetentionReport,
    now: float,
    dry_run: bool,
) -> None:
    today = datetime.fromtimestamp(now, timezone.utc).date()
    for path in sorted(directory.iterdir()):
        stamp = archive_date(path.name)
        if stamp is None:
            continue
        age = (today - stamp).days
        compressed = path.suffix == ".gz"

        if config.archive_delete_days is not None and age > config.archive_delete_days:
            report.archives_deleted += 1
            if not dry_run:
                path.unlink()
            continue

        # Never touch the file rtl_433 is still appending to today.
        if (
            config.archive_gzip_days is not None
            and not compressed
            and age > config.archive_gzip_days
        ):
            report.archives_compressed += 1
            if not dry_run:
                _compress(path)


def _compress(path: Path) -> None:
    target = path.with_suffix(path.suffix + ".gz")
    with path.open("rb") as source, gzip.open(target, "wb") as sink:
        shutil.copyfileobj(source, sink)
    # Only drop the original once the copy is complete on disk.
    path.unlink()
