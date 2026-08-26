"""Runtime wiring: radio -> ingest -> sessions -> clustering.

Owns the background threads so both `tpms serve` and the web app talk to one
shared object rather than each building their own half of the pipeline.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .cluster import ClusterReport, Clusterer
from .config import Config
from .db import Database
from .ingest import Ingestor
from .models import now as now_ts
from .radio import RadioSupervisor

log = logging.getLogger(__name__)


class Service:
    def __init__(self, config: Config, start_radio: bool = True):
        self.config = config
        self.db = Database(config.database_path)
        self.ingestor = Ingestor(self.db, config.sessions, config.clustering)
        self.clusterer = Clusterer(self.db, config.clustering)
        self.start_radio = start_radio
        self.last_cluster_report: ClusterReport | None = None
        self.started_at = now_ts()

        self.radio = RadioSupervisor(
            config.radio,
            on_line=self.ingestor.handle_line,
            raw_archive_dir=config.raw_archive_path,
        )
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        if self.start_radio:
            self.radio.start()
        self._spawn(self._sweep_loop, "sweeper")
        if self.config.clustering.auto_interval_seconds > 0:
            self._spawn(self._cluster_loop, "clusterer")

    def stop(self) -> None:
        self._stop.set()
        self.radio.stop()
        for worker in self._workers:
            worker.join(timeout=5)
        self._workers.clear()

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

    def recluster(self, dry_run: bool = False) -> ClusterReport:
        report = self.clusterer.run(dry_run=dry_run)
        if not dry_run:
            self.last_cluster_report = report
            log.info("clustering: %s", report.summary())
        return report

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
            "decoders": dict(
                sorted(
                    self.ingestor.decoder_counts.items(),
                    key=lambda kv: -kv[1],
                )
            ),
            "clustering": (
                self.last_cluster_report.summary() if self.last_cluster_report else None
            ),
            "started_at": self.started_at,
            "database": str(self.config.database_path),
        }
