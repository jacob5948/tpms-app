"""Supervise the rtl_433 subprocess.

USB dropouts and dongle resets are routine on a Pi, so the process is treated
as something that will die and needs restarting, not as a one-shot launch.
"""

from __future__ import annotations

import collections
import logging
import shutil
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import FALLBACK_TPMS_PROTOCOLS, RadioConfig

log = logging.getLogger(__name__)


def build_command(
    config: RadioConfig,
    protocols: list[int] | None = None,
    log_output: bool = False,
    microseconds: bool = False,
) -> list[str]:
    """Assemble the rtl_433 command line.

    ``-M utc`` is not optional: without it rtl_433 stamps readings in local
    time with no offset, which is unusable across DST boundaries.
    """
    # Microseconds matter for duplicate-decode detection: two decoders parsing
    # one burst share a timestamp far more precisely than whole seconds can
    # express. Older builds lack the option, hence the probe.
    time_spec = "time:utc:usec" if microseconds else "utc"
    cmd = [
        config.binary, "-F", "json",
        "-M", time_spec, "-M", "level", "-M", "protocol",
    ]

    # Route rtl_433's own diagnostics to stderr, keeping stdout pure JSON.
    # Without this, modern builds report failures nowhere at all.
    if log_output:
        cmd += ["-F", "log:/dev/stderr"]

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

    # An empty list means discovery failed; decoding everything is far better
    # than passing a protocol number this build rejects.
    if not config.all_protocols and protocols:
        for protocol in protocols:
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
        "supported device protocols",
        "rtl_433 rejected one of the -R protocol numbers and printed its "
        "protocol table instead of starting. This build is older than the "
        "protocol list being passed to it. Upgrade rtl_433, or set "
        "'radio.all_protocols: true' in config.yaml to stop passing -R at all.",
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


#: Lines of `rtl_433 -R help` look like "    [088]  Toyota TPMS".
_PROTOCOL_LINE = re.compile(r"^\s*\[(\d+)\]\*?\s+(.+)$", re.MULTILINE)


def list_protocols(binary: str) -> dict[int, str]:
    """Ask the installed rtl_433 which protocols it actually supports."""
    try:
        result = subprocess.run(
            [binary, "-R", "help"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not query %s for its protocol list: %s", binary, exc)
        return {}
    # rtl_433 prints the list to stdout on some builds and stderr on others.
    text = (result.stdout or "") + (result.stderr or "")
    return {
        int(match.group(1)): match.group(2).strip()
        for match in _PROTOCOL_LINE.finditer(text)
    }


def discover_tpms_protocols(binary: str) -> list[int]:
    """TPMS protocol numbers supported by *this* rtl_433 build.

    Protocol numbers are not stable across rtl_433 versions, and passing one
    the binary does not know makes it print its whole protocol table and exit,
    which reads like a hardware fault. So the list is derived from the binary
    rather than hardcoded: decoders are selected by name, with the static
    fallback list intersected in to catch any whose name omits "TPMS".

    Returns an empty list if the binary cannot be queried; the caller then
    decodes everything rather than risking a crash loop.
    """
    available = list_protocols(binary)
    if not available:
        return []
    selected = {n for n, name in available.items() if "tpms" in name.lower()}
    selected |= set(FALLBACK_TPMS_PROTOCOLS) & set(available)
    return sorted(selected)


def supports_log_output(binary: str) -> bool:
    """Whether this rtl_433 has the `-F log` output format.

    Builds from ~v22 on print nothing to stderr by default and say so:
    'Use "-F log" if you want any messages, warnings, and errors'. Without it
    a failed start is completely silent. Older builds lack the option entirely
    and would abort on the unknown argument, so it has to be probed for.
    """
    try:
        result = subprocess.run(
            [binary, "-F", "help"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return False
    text = ((result.stdout or "") + (result.stderr or "")).lower()
    return "-f log" in text or "log|kv" in text


def supports_microsecond_time(binary: str) -> bool:
    """Whether this rtl_433 accepts `-M time:utc:usec`.

    Note the option must be spelled as one combined spec: passing `-M utc`
    alongside `-M time:usec` is rejected, and a rejected argument makes
    rtl_433 print its usage and exit, which crash-loops the supervisor.
    """
    try:
        result = subprocess.run(
            [binary, "-M", "help"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "usec" in ((result.stdout or "") + (result.stderr or "")).lower()


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
        self._stderr_head: list[str] = []
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=12)
        self._protocols: list[int] | None = None
        self._log_output: bool | None = None
        self._microseconds: bool | None = None

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

    def _protocol_args(self) -> list[int]:
        if self._protocols is None:
            self._protocols = discover_tpms_protocols(self.config.binary)
            if self._protocols:
                log.info(
                    "%s supports %d TPMS protocols", self.config.binary,
                    len(self._protocols),
                )
            elif not self.config.all_protocols:
                log.warning(
                    "could not determine which protocols %s supports; decoding "
                    "all of them instead", self.config.binary,
                )
        return self._protocols

    def _wants_log_output(self) -> bool:
        if self._log_output is None:
            self._log_output = supports_log_output(self.config.binary)
            if not self._log_output:
                log.debug("%s has no -F log; relying on its stderr", self.config.binary)
        return self._log_output

    def _wants_microseconds(self) -> bool:
        if self._microseconds is None:
            self._microseconds = supports_microsecond_time(self.config.binary)
            if not self._microseconds:
                log.info(
                    "%s has no microsecond timestamps; duplicate-decode "
                    "detection will rely on signal level alone",
                    self.config.binary,
                )
        return self._microseconds

    def _pump(self) -> None:
        cmd = build_command(
            self.config,
            self._protocol_args(),
            self._wants_log_output(),
            self._wants_microseconds(),
        )
        self.status.command = cmd
        log.info("starting: %s", " ".join(cmd))

        self._stderr_head.clear()
        self._stderr_tail.clear()
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            # Detach from our process group. Otherwise Ctrl+C at the terminal
            # goes to rtl_433 as well, it dies, and the supervisor dutifully
            # restarts it -- so the program appears unkillable. With this, the
            # child only ever stops because we told it to.
            start_new_session=True,
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
            # rtl_433 reports the actual problem first and may then dump its
            # entire 280-line protocol table, so keeping only the tail loses
            # the one line that explains anything.
            if len(self._stderr_head) < 12:
                self._stderr_head.append(line)
            else:
                self._stderr_tail.append(line)

    def _collected_stderr(self) -> list[str]:
        head, tail = self._stderr_head, list(self._stderr_tail)
        if not tail:
            return list(head)
        return [*head, f"... {len(tail)} more line(s) ...", *tail]

    def _report_failure(self, delay: float) -> None:
        tail = self._collected_stderr()
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
        for line in tail:
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
