"""Supervise the rtl_433 subprocess.

USB dropouts and dongle resets are routine on a Pi, so the process is treated
as something that will die and needs restarting, not as a one-shot launch.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import TPMS_PROTOCOLS, RadioConfig

log = logging.getLogger(__name__)


def build_command(config: RadioConfig) -> list[str]:
    """Assemble the rtl_433 command line.

    ``-M utc`` is not optional: without it rtl_433 stamps readings in local
    time with no offset, which is unusable across DST boundaries.
    """
    cmd = [config.binary, "-F", "json", "-M", "utc", "-M", "level", "-M", "protocol"]

    if config.device is not None:
        cmd += ["-d", str(config.device)]
    if config.gain is not None:
        cmd += ["-g", str(config.gain)]
    if config.ppm_error is not None:
        cmd += ["-p", str(config.ppm_error)]
    if config.sample_rate is not None:
        cmd += ["-s", str(config.sample_rate)]

    frequencies = config.frequencies or ["315M"]
    for freq in frequencies:
        cmd += ["-f", str(freq)]
    if len(frequencies) > 1:
        # Only meaningful with multiple -f; rtl_433 dwells this long per band.
        cmd += ["-H", str(config.hop_seconds)]

    if not config.all_protocols:
        for protocol in TPMS_PROTOCOLS:
            cmd += ["-R", str(protocol)]

    cmd += [str(a) for a in config.extra_args]
    return cmd


@dataclass
class RadioStatus:
    running: bool = False
    pid: int | None = None
    started_at: float | None = None
    restarts: int = 0
    last_error: str | None = None
    last_line_at: float | None = None
    command: list[str] = field(default_factory=list)


class RadioSupervisor:
    """Runs rtl_433 in a background thread, feeding stdout lines to a sink."""

    def __init__(
        self,
        config: RadioConfig,
        on_line: Callable[[str], None],
        raw_archive_dir: Path | None = None,
    ):
        self.config = config
        self.on_line = on_line
        self.raw_archive_dir = raw_archive_dir
        self.status = RadioStatus(command=build_command(config))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[str] | None = None
        self._archive: tuple[str, object] | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rtl433", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._terminate()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._close_archive()

    def restart(self) -> None:
        """Kill the child; the supervise loop brings it straight back."""
        self._terminate()

    def _terminate(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    # -- supervise loop ---------------------------------------------------

    def _run(self) -> None:
        delay = self.config.restart_min_delay
        while not self._stop.is_set():
            if shutil.which(self.config.binary) is None:
                self.status.last_error = (
                    f"{self.config.binary} not found on PATH -- install rtl_433"
                )
                log.error(self.status.last_error)
                if self._stop.wait(self.config.restart_max_delay):
                    return
                continue

            started = time.monotonic()
            try:
                self._pump()
            except Exception as exc:  # noqa: BLE001 - supervisor must not die
                self.status.last_error = str(exc)
                log.exception("rtl_433 pump failed")
            finally:
                self.status.running = False
                self.status.pid = None

            if self._stop.is_set():
                return

            # A process that stayed up a while is a fresh failure, not a
            # crash loop, so reset the backoff.
            if time.monotonic() - started > 60:
                delay = self.config.restart_min_delay
            self.status.restarts += 1
            log.warning("rtl_433 exited; restarting in %.0fs", delay)
            if self._stop.wait(delay):
                return
            delay = min(delay * 2, self.config.restart_max_delay)

    def _pump(self) -> None:
        cmd = build_command(self.config)
        self.status.command = cmd
        log.info("starting: %s", " ".join(cmd))

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._process = process
        self.status.running = True
        self.status.pid = process.pid
        self.status.started_at = time.time()

        stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(process,), daemon=True
        )
        stderr_thread.start()

        assert process.stdout is not None
        for line in process.stdout:
            if self._stop.is_set():
                break
            line = line.rstrip("\n")
            if not line:
                continue
            self.status.last_line_at = time.time()
            self._archive_line(line)
            try:
                self.on_line(line)
            except Exception:  # noqa: BLE001 - one bad packet must not stop capture
                log.exception("ingest failed for line: %s", line[:200])

        process.wait()

    def _drain_stderr(self, process: subprocess.Popen[str]) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            line = line.strip()
            if line:
                log.debug("rtl_433: %s", line)
                self.status.last_error = line

    # -- raw archive ------------------------------------------------------

    def _archive_line(self, line: str) -> None:
        """Append every raw line to a daily file.

        This is the safety net: if normalization ever drops a field, the
        original capture is still on disk to re-import.
        """
        if self.raw_archive_dir is None:
            return
        day = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        if self._archive is None or self._archive[0] != day:
            self._close_archive()
            self.raw_archive_dir.mkdir(parents=True, exist_ok=True)
            handle = (self.raw_archive_dir / f"rtl433-{day}.jsonl").open("a")
            self._archive = (day, handle)
        handle = self._archive[1]
        handle.write(line + "\n")
        handle.flush()

    def _close_archive(self) -> None:
        if self._archive is not None:
            try:
                self._archive[1].close()
            finally:
                self._archive = None
