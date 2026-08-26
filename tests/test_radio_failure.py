"""A receiver that dies silently is unfixable, so failure reporting is tested.

The original bug: rtl_433's stderr was logged at DEBUG, so the service log
showed only "rtl_433 exited; restarting in 1s" with no reason.
"""

import os
import stat
import time

import pytest

from tpms.config import RadioConfig
from tpms.radio import RadioSupervisor, explain_failure


@pytest.fixture
def fake_rtl433(tmp_path):
    """A stand-in binary that fails the way real rtl_433 fails."""

    def build(stderr: str, code: int) -> str:
        path = tmp_path / "rtl_433"
        path.write_text(f'#!/bin/sh\nprintf %s "{stderr}" >&2\nexit {code}\n')
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    return build


def _run_once(binary, monkeypatch) -> RadioSupervisor:
    supervisor = RadioSupervisor(
        RadioConfig(binary=binary, all_protocols=True, restart_min_delay=30),
        on_line=lambda line: None,
    )
    supervisor.start()
    deadline = time.time() + 5
    while supervisor.status.restarts == 0 and time.time() < deadline:
        time.sleep(0.05)
    supervisor.stop()
    return supervisor


def test_stderr_is_captured_and_exposed(fake_rtl433, monkeypatch):
    binary = fake_rtl433("No supported devices found.", 1)
    supervisor = _run_once(binary, monkeypatch)
    assert supervisor.status.exit_code == 1
    assert any("No supported devices" in line for line in supervisor.status.stderr_tail)
    assert supervisor.status.last_error


def test_missing_dongle_gets_a_hint(fake_rtl433, monkeypatch):
    supervisor = _run_once(fake_rtl433("No supported devices found.", 1), monkeypatch)
    assert "dongle" in supervisor.status.hint.lower()


def test_busy_device_hint_names_the_dvb_driver(fake_rtl433, monkeypatch):
    supervisor = _run_once(fake_rtl433("usb_claim_interface error -6", 1), monkeypatch)
    assert "dvb_usb_rtl28xxu" in supervisor.status.hint


def test_silent_failure_still_reports_the_exit_code(fake_rtl433, monkeypatch):
    supervisor = _run_once(fake_rtl433("", 3), monkeypatch)
    assert supervisor.status.exit_code == 3
    assert supervisor.status.stderr_tail == []
    assert supervisor.status.hint is None


def test_stderr_does_not_leak_between_attempts(fake_rtl433, monkeypatch):
    supervisor = RadioSupervisor(
        RadioConfig(
            binary=fake_rtl433("No supported devices found.", 1),
            all_protocols=True,
            restart_min_delay=0.05,
        ),
        on_line=lambda line: None,
    )
    supervisor.start()
    deadline = time.time() + 5
    while supervisor.status.restarts < 3 and time.time() < deadline:
        time.sleep(0.05)
    supervisor.stop()
    # One run's worth of output, not three concatenated.
    assert len(supervisor.status.stderr_tail) == 1


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("No supported devices found.", "dongle"),
        ("Kernel driver is active", "dvb_usb_rtl28xxu"),
        ("rtl_433: permission denied", "udev"),
        ("unknown protocol 381", "all_protocols"),
        ("something nobody has ever seen", None),
    ],
)
def test_hint_matching(stderr, expected):
    hint = explain_failure([stderr])
    if expected is None:
        assert hint is None
    else:
        assert expected in hint


def test_missing_binary_is_reported_distinctly():
    supervisor = RadioSupervisor(
        RadioConfig(binary="definitely-not-installed-xyz", restart_max_delay=0.1),
        on_line=lambda line: None,
    )
    supervisor.start()
    deadline = time.time() + 5
    while supervisor.status.last_error is None and time.time() < deadline:
        time.sleep(0.05)
    supervisor.stop()
    assert "not found on PATH" in supervisor.status.last_error
