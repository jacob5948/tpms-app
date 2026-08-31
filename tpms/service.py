"""Runtime wiring: radio -> ingest -> sessions -> clustering.

Owns the background threads so both `tpms serve` and the web app talk to one
shared object rather than each building their own half of the pipeline.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any, Callable

from .aliases import AliasDetector, AliasReport
from .cluster import ClusterReport, Clusterer
from .config import Config
from .db import Database
from .ingest import Ingestor
from .models import band_label, now as now_ts
from .radio import RadioSupervisor
from .retention import RetentionReport, run as run_retention

log = logging.getLogger(__name__)


class Service:
    def __init__(self, config: Config, start_radio: bool = True):
        self.config = config
        self.db = Database(config.database_path)
        self.ingestor = Ingestor(self.db, config.sessions, config.clustering)
        self.clusterer = Clusterer(self.db, config.clustering)
        self.alias_detector = AliasDetector(self.db, config.aliases)
        self.start_radio = start_radio
        self.last_cluster_report: ClusterReport | None = None
        self.last_alias_report: AliasReport | None = None
        self.last_retention_report: RetentionReport | None = None
        self.last_retention_at: float | None = None
        self.started_at = now_ts()

        self.radio = RadioSupervisor(
            config.radio,
            on_line=self.ingestor.handle_line,
            raw_archive_dir=config.raw_archive_path,
        )
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        #: Settings saved that the running process cannot adopt, kept until it
        #: is restarted. The pages read it, so the reminder survives the
        #: navigation away from Settings that loses the flash message.
        self.restart_pending: list[str] = []
        self.restarting_at: float | None = None
        #: The thread counting down to the exec, kept so a caller can wait on
        #: it -- there is exactly one, and starting a second is the bug the
        #: `restarting_at` guard exists to prevent.
        self._restarter: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        if self.start_radio:
            self.radio.start()
        self._spawn(self._sweep_loop, "sweeper")
        if self.config.clustering.auto_interval_seconds > 0:
            self._spawn(self._cluster_loop, "clusterer")
        if self.config.retention.run_daily:
            self._spawn(self._retention_loop, "housekeeper")

    def stop(self) -> None:
        self._stop.set()
        self.radio.stop()
        for worker in self._workers:
            worker.join(timeout=5)
        self._workers.clear()

    def restart_process(self, delay: float = 0.75) -> None:
        """Replace this process with a fresh one, once the reply is out.

        Some settings are only read while the program is starting -- the web
        server's address, above all -- so saving them and staying up leaves a
        page insisting on a port nothing is listening on. The Settings page can
        say "restart"; this is what lets it do it.

        An exec rather than an exit, so it works the same whether a supervisor
        is watching or someone is running `tpms serve` in a terminal: exiting
        would be a restart under systemd and a shutdown everywhere else, which
        is the sort of button that does different things on different machines.
        The delay is for the HTTP response -- the caller is a request handler,
        and an exec mid-reply is a restart the user only sees as a dead tab.
        """
        if self.restarting_at is not None:
            return                      # already on its way; do not queue two
        self.restarting_at = now_ts()
        argv = [sys.executable, *sys.argv]
        log.info("restart requested; re-exec in %.1fs: %s", delay, " ".join(argv))

        def run() -> None:
            # Its own event, not self._stop: that one is already set when the
            # service was never started, which would exec before the reply.
            threading.Event().wait(delay)
            try:
                # Let go of the dongle and close the database: the new image
                # claims both within milliseconds, and a WAL left mid-write is
                # a restart that costs readings.
                self.stop()
                self.db.close()
            except Exception:  # noqa: BLE001
                log.exception("restart cleanup failed; execing anyway")
            os.execv(argv[0], argv)

        self._restarter = threading.Thread(target=run, name="restarter", daemon=True)
        self._restarter.start()

    def _spawn(self, target: Callable[[], None], name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._workers.append(thread)

    # -- background loops -------------------------------------------------

    def _sweep_loop(self) -> None:
        interval = self.config.sessions.sweep_interval_seconds
        while not self._stop.wait(interval):
            try:
                self.ingestor.sweep()
            except Exception:  # noqa: BLE001
                log.exception("sweep failed")

    def _cluster_loop(self) -> None:
        interval = self.config.clustering.auto_interval_seconds
        while not self._stop.wait(interval):
            try:
                self.recluster()
            except Exception:  # noqa: BLE001
                log.exception("clustering failed")

    def _retention_loop(self) -> None:
        # Not at start-up: a service that restarts often would VACUUM every
        # time. Settle first, then once a day.
        if self._stop.wait(300):
            return
        while True:
            try:
                self.housekeep()
            except Exception:  # noqa: BLE001
                log.exception("housekeeping failed")
            if self._stop.wait(86400):
                return

    def housekeep(self, dry_run: bool = False) -> RetentionReport:
        report = run_retention(
            self.db,
            self.config.retention,
            archive_dir=self.config.raw_archive_path,
            dry_run=dry_run,
        )
        if not dry_run:
            self.last_retention_report = report
            self.last_retention_at = now_ts()
        return report

    def detect_aliases(self, dry_run: bool = False) -> AliasReport:
        report = self.alias_detector.run(dry_run=dry_run)
        if not dry_run:
            self.last_alias_report = report
            log.info("duplicate decodes: %s", report.summary())
        return report

    def recluster(self, dry_run: bool = False) -> ClusterReport:
        # Duplicates first: an unmerged duplicate co-occurs with its twin
        # perfectly and would cluster into a vehicle that does not exist.
        if not dry_run:
            self.detect_aliases()
        report = self.clusterer.run(dry_run=dry_run)
        if not dry_run:
            self.last_cluster_report = report
            log.info("clustering: %s", report.summary())
        return report

    def purge_decoder(self, pattern: str, dry_run: bool = False) -> dict[str, Any]:
        """Remove every sensor whose decoder name matches *pattern*.

        Excluding a protocol in the config stops new phantom sensors being
        created but leaves the ones already recorded, so this cleans up after
        it. Vehicles left with no sensors are removed too, and clustering is
        re-run because deleting a member can change a grouping.
        """
        matches = self.db.sensors_matching(pattern)
        result: dict[str, Any] = {
            "pattern": pattern,
            "sensors": [s.display for s in matches],
            "dry_run": dry_run,
        }
        if dry_run or not matches:
            result["counts"] = {"sensors": len(matches)}
            return result

        result["counts"] = self.db.purge_sensors([s.pk for s in matches])
        result["vehicles_removed"] = self.db.delete_empty_vehicles()
        result["clustering"] = self.recluster().summary()
        log.info(
            "purged %s: %s sensor(s), %s reading(s)",
            pattern,
            result["counts"]["sensors"],
            result["counts"]["readings"],
        )
        return result

    # -- status -----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        radio = self.radio.status
        counts = self.db.query_one(
            """
            SELECT
                (SELECT COUNT(*) FROM readings) AS readings,
                (SELECT COUNT(*) FROM sensors)  AS sensors,
                (SELECT COUNT(*) FROM vehicles) AS vehicles,
                (SELECT COUNT(*) FROM sightings) AS sightings,
                (SELECT COUNT(*) FROM sightings WHERE ended_at IS NULL) AS open_sightings
            """
        )
        recent = self.db.query_one(
            "SELECT COUNT(*) AS n FROM readings WHERE ts >= ?", (now_ts() - 300,)
        )
        # rtl_433 reports RSSI in dB below full scale, so readings crowding
        # zero mean the front end is saturating and the AGC is not coping.
        # Bounded to the last week of *capture* rather than the whole table:
        # this used to scan every reading ever taken on each page load, and a
        # gain problem from last winter is not news anyway.
        levels = self.db.query_one(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN rssi > -1.0 THEN 1 ELSE 0 END) AS hot "
            "FROM readings WHERE rssi IS NOT NULL "
            "AND ts >= (SELECT MAX(ts) FROM readings) - 604800"
        )
        total = int(levels["n"] or 0) if levels else 0
        hot = int(levels["hot"] or 0) if levels else 0
        saturation = (hot / total) if total else 0.0

        # Which bands packets are actually arriving on -- the answer that
        # matters when hopping, since a configured band is not a heard one.
        # From the rollup, not the readings table: same answer, and it does
        # not get slower as the capture grows.
        bands: dict[str, int] = {}
        for row in self.db.query(
            "SELECT freq_mhz, SUM(n) AS n FROM band_counts GROUP BY freq_mhz"
        ):
            label = band_label(row["freq_mhz"])
            if label:
                bands[label] = bands.get(label, 0) + int(row["n"])
        return {
            "radio": {
                "running": radio.running,
                "pid": radio.pid,
                "restarts": radio.restarts,
                "started_at": radio.started_at,
                "last_line_at": radio.last_line_at,
                "last_error": radio.last_error,
                "exit_code": radio.exit_code,
                "hint": radio.hint,
                "stderr_tail": radio.stderr_tail,
                "command": " ".join(radio.command),
                "frequencies": self.config.radio.frequencies,
                "hopping": len(self.config.radio.frequencies) > 1,
                "enabled": self.start_radio,
            },
            "counts": dict(counts) if counts else {},
            "readings_per_min": round((recent["n"] if recent else 0) / 5.0, 2),
            "ingest": dict(self.ingestor.stats),
            "bands": dict(sorted(bands.items(), key=lambda kv: -kv[1])),
            "signal": {
                "readings": total,
                "near_full_scale": hot,
                "saturation": round(saturation, 4),
                # Below this it is a handful of very close cars, not a problem.
                "saturated": total >= 50 and saturation > 0.05,
                "gain": self.config.radio.gain,
            },
            "decoders": dict(
                sorted(
                    self.ingestor.decoder_counts.items(),
                    key=lambda kv: -kv[1],
                )
            ),
            "clustering": (
                self.last_cluster_report.summary() if self.last_cluster_report else None
            ),
            "aliases": (
                self.last_alias_report.summary() if self.last_alias_report else None
            ),
            "started_at": self.started_at,
            "database": str(self.config.database_path),
            "storage": self._storage(),
        }

    def _storage(self) -> dict[str, Any]:
        """Size on disk and how fast it is growing.

        Growth is measured from the capture window rather than assumed: what
        the database costs per day depends on how busy the road is.
        """
        lo, hi, rows = self.db.readings_span()
        size = self.db.file_size()
        days = ((hi - lo) / 86400.0) if (lo and hi and hi > lo) else 0.0
        archive = self.config.raw_archive_path
        archive_bytes = 0
        if archive and archive.is_dir():
            archive_bytes = sum(f.stat().st_size for f in archive.iterdir() if f.is_file())
        return {
            "bytes": size,
            "archive_bytes": archive_bytes,
            "readings": rows,
            "oldest_reading": lo,
            "capture_days": round(days, 2),
            "bytes_per_day": round(size / days) if days >= 1 else None,
            "readings_per_day": round(rows / days) if days >= 1 else None,
            "last_housekeeping": self.last_retention_at,
            "last_housekeeping_summary": (
                self.last_retention_report.summary() if self.last_retention_report else None
            ),
            "policy": {
                "raw_days": self.config.retention.raw_days,
                "readings_days": self.config.retention.readings_days,
                "archive_gzip_days": self.config.retention.archive_gzip_days,
                "archive_delete_days": self.config.retention.archive_delete_days,
            },
        }
