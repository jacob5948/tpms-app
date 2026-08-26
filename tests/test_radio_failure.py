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


# --- protocol discovery ---------------------------------------------------
#
# The bug this guards: protocol numbers were hardcoded from one rtl_433
# release. Any older build rejects the unknown numbers, prints its entire
# protocol table and exits 1 -- which looks exactly like a hardware fault.


@pytest.fixture
def fake_rtl433_with_protocols(tmp_path):
    def build(listing: str) -> str:
        path = tmp_path / "rtl_433_list"
        path.write_text(f"#!/bin/sh\ncat <<'LIST'\n{listing}\nLIST\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    return build


def test_discovery_selects_tpms_decoders_by_name(fake_rtl433_with_protocols):
    from tpms.radio import discover_tpms_protocols

    binary = fake_rtl433_with_protocols(
        "    [01]  Silvercrest Remote Control\n"
        "    [59]  Steelmate TPMS\n"
        "    [88]  Toyota TPMS\n"
        "    [253] Watts WFHT-RF Thermostat"
    )
    assert discover_tpms_protocols(binary) == [59, 88]


def test_discovery_ignores_protocols_this_build_lacks(fake_rtl433_with_protocols):
    """An old build stops at a lower number; we must not ask for more."""
    from tpms.radio import discover_tpms_protocols

    binary = fake_rtl433_with_protocols(
        "    [59]  Steelmate TPMS\n    [275] GM-Aftermarket TPMS"
    )
    protocols = discover_tpms_protocols(binary)
    assert protocols == [59, 275]
    assert max(protocols) <= 275


def test_discovery_handles_the_disabled_by_default_marker(fake_rtl433_with_protocols):
    from tpms.radio import discover_tpms_protocols

    binary = fake_rtl433_with_protocols("    [201]* Unbranded SolarTPMS for trucks")
    assert discover_tpms_protocols(binary) == [201]


def test_discovery_returns_empty_when_the_binary_is_unusable():
    from tpms.radio import discover_tpms_protocols

    assert discover_tpms_protocols("definitely-not-installed-xyz") == []


def test_rejected_protocol_table_is_recognised():
    """rtl_433's response to a bad -R is to print its protocol table."""
    from tpms.radio import explain_failure

    hint = explain_failure(
        ["rtl_433 version 25.12", "= Supported device protocols =", "    [01]  Silvercrest"]
    )
    assert hint is not None
    assert "all_protocols" in hint


def test_the_first_stderr_lines_survive_a_long_protocol_dump(fake_rtl433, monkeypatch):
    """rtl_433 states the problem first, then dumps ~280 lines. Keeping only
    the tail loses the only line that explains anything."""
    noise = "\\n".join(f"    [{n}] Some Decoder" for n in range(300))
    supervisor = _run_once(fake_rtl433(f"Unknown protocol 381\\n{noise}", 1), monkeypatch)
    assert any("Unknown protocol 381" in line for line in supervisor.status.stderr_tail)


# --- -F log probe ---------------------------------------------------------
#
# Modern rtl_433 prints nothing to stderr by default, so a failed start is
# silent unless "-F log" is requested. Older builds abort on that unknown
# argument, so it must be probed rather than assumed.


def test_log_output_is_probed_not_assumed(tmp_path):
    from tpms.radio import supports_log_output

    modern = tmp_path / "modern"
    modern.write_text(
        "#!/bin/sh\necho '  [-F log|kv|json|csv] Produce decoded output.'\n"
    )
    modern.chmod(modern.stat().st_mode | stat.S_IEXEC)

    ancient = tmp_path / "ancient"
    ancient.write_text("#!/bin/sh\necho '  [-F kv|json|csv] Produce decoded output.'\n")
    ancient.chmod(ancient.stat().st_mode | stat.S_IEXEC)

    assert supports_log_output(str(modern))
    assert not supports_log_output(str(ancient))
    assert not supports_log_output("definitely-not-installed-xyz")


def test_log_flag_keeps_stdout_pure_json():
    """Diagnostics must go to stderr, or they would be parsed as readings."""
    from tpms.config import RadioConfig
    from tpms.radio import build_command

    cmd = build_command(RadioConfig(), protocols=[88], log_output=True)
    assert "log:/dev/stderr" in cmd
    assert cmd[cmd.index("-F") + 1] == "json"


def test_log_flag_omitted_for_builds_without_it():
    from tpms.config import RadioConfig
    from tpms.radio import build_command

    cmd = build_command(RadioConfig(), protocols=[88], log_output=False)
    assert not any("log" in arg for arg in cmd)
