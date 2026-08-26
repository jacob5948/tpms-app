"""Supervise the rtl_433 subprocess.

USB dropouts and dongle resets are routine on a Pi, so the process is treated
as something that will die and needs restarting, not as a one-shot launch.
"""

from __future__ import annotations

import collections
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


#: Signatures of the ways rtl_433 commonly fails to start, mapped to the fix.
#: Matched against its stderr, which is otherwise easy to miss in a service log.
_FAILURE_HINTS: tuple[tuple[str, str], ...] = (
    (
        "no supported devices found",
        "No SDR dongle detected. Check it is plugged in (and passed through, "
        "if this runs in a container or VM).",
    ),
    (
        "usb_claim_interface error",
        "The dongle is claimed by something else. On Linux this is normally the "
        "DVB-T kernel driver: blacklist it with "
        "'echo blacklist dvb_usb_rtl28xxu | sudo tee /etc/modprobe.d/blacklist-dvb.conf' "
        "then reboot. Otherwise, another rtl_433/SDR process is already running.",
    ),
    (
        "kernel driver is active",
        "The DVB-T kernel driver has the dongle. Blacklist dvb_usb_rtl28xxu and reboot.",
    ),
    (
        "failed to open rtlsdr device",
        "Could not open the dongle: it is either busy or the user lacks permission. "
        "Install systemd/99-rtl-sdr.rules and make sure the service user is in "
        "the plugdev group.",
    ),
    (
        "permission denied",
        "Permission denied on the USB device. Install systemd/99-rtl-sdr.rules, "
        "then 'sudo udevadm control --reload-rules && sudo udevadm trigger'.",
    ),
    (
        "unknown protocol",
        "rtl_433 rejected a -R protocol number. Your rtl_433 is likely older than "
        "the protocol list in tpms/config.py -- set 'radio.all_protocols: true' "
        "in config.yaml, or upgrade rtl_433.",
    ),
    (
        "invalid option",
        "rtl_433 rejected an argument. Compare the command on the Status page "
        "against 'rtl_433 -h'; check radio.extra_args in config.yaml.",
    ),
)


def explain_failure(stderr_lines: list[str]) -> str | None:
    """Turn rtl_433's stderr into one actionable sentence, if we recognise it."""
    blob = " ".join(stderr_lines).lower()
    for signature, hint in _FAILURE_HINTS:
        if signature in blob:
            return hint
    return None


@dataclass
class RadioStatus:
    running: bool = False
    pid: int | None = None
    started_at: float | None = None
    restarts: int = 0
    last_error: str | None = None
    last_line_at: float | None = None
    command: list[str] = field(default_factory=list)
    exit_code: int | None = None
    hint: str | None = None
    stderr_tail: list[str] = field(default_factory=list)


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
        self._stderr: collections.deque[str] = collections.deque(maxlen=25)

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
            self._report_failure(delay)
            if self._stop.wait(delay):
                return
            delay = min(delay * 2, self.config.restart_max_delay)

    def _pump(self) -> None:
        cmd = build_command(self.config)
        self.status.command = cmd
        log.info("starting: %s", " ".join(cmd))

        self._stderr.clear()
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

        self.status.exit_code = process.wait()
        stderr_thread.join(timeout=2)  # let the reason arrive before we report it

    def _drain_stderr(self, process: subprocess.Popen[str]) -> None:
        """Collect rtl_433's own diagnostics.

        These are the only explanation of a failed start, so they are kept in
        a ring buffer and replayed at WARNING when the process dies rather
        than being logged at debug and lost.
        """
        if process.stderr is None:
            return
        for line in process.stderr:
            line = line.strip()
            if not line:
                continue
            log.debug("rtl_433: %s", line)
            self._stderr.append(line)

    def _report_failure(self, delay: float) -> None:
        tail = list(self._stderr)
        self.status.stderr_tail = tail
        self.status.hint = explain_failure(tail)
        if tail:
            self.status.last_error = tail[-1]

        code = self.status.exit_code
        log.warning(
            "rtl_433 exited (code %s); restarting in %.0fs",
            "unknown" if code is None else code,
            delay,
        )
        for line in tail[-8:]:
            log.warning("  rtl_433: %s", line)
        if self.status.hint:
            log.warning("  hint: %s", self.status.hint)
        elif not tail:
            log.warning(
                "  rtl_433 printed nothing to stderr. Try running the command "
                "from the Status page by hand to see what it does."
            )

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
