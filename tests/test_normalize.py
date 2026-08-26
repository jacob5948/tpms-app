"""Normalization has to cope with every decoder's idea of a TPMS packet."""

import pytest

from tpms.ingest import is_tpms, normalize
from tpms.models import parse_rtl_time


def test_toyota_kpa():
    reading = normalize(
        {
            "time": "2026-08-25 15:34:12",
            "model": "Toyota-TPMS",
            "type": "TPMS",
            "id": "1a2b3c4d",
            "battery_ok": 1,
            "pressure_kPa": 234.5,
            "temperature_C": 31,
            "freq": 315.012,
            "rssi": -11.2,
            "snr": 18.4,
        }
    )
    assert reading.model == "Toyota-TPMS"
    assert reading.sensor_id == "1a2b3c4d"
    assert reading.pressure_kpa == pytest.approx(234.5)
    assert reading.temperature_c == pytest.approx(31)
    assert reading.battery_ok == 1
    assert reading.freq_mhz == pytest.approx(315.012)
    assert reading.rssi == pytest.approx(-11.2)


def test_psi_and_fahrenheit_are_converted():
    reading = normalize(
        {
            "time": "2026-08-25 15:34:13",
            "model": "Ford-TPMS",
            "type": "TPMS",
            "id": 12345678,
            "pressure_PSI": 34.0,
            "temperature_F": 88.0,
        }
    )
    assert reading.pressure_kpa == pytest.approx(234.42, abs=0.05)
    assert reading.pressure_psi == pytest.approx(34.0, abs=0.01)
    assert reading.temperature_c == pytest.approx(31.11, abs=0.01)
    assert reading.sensor_id == "12345678", "integer ids must stringify stably"


def test_bar_is_converted():
    reading = normalize(
        {"model": "Schrader-TPMS", "type": "TPMS", "id": "7c1b2f", "pressure_bar": 2.4}
    )
    assert reading.pressure_kpa == pytest.approx(240.0)


def test_battery_low_is_inverted():
    reading = normalize(
        {"model": "Renault-TPMS", "type": "TPMS", "id": "x", "battery_low": 1}
    )
    assert reading.battery_ok == 0


def test_non_tpms_is_rejected():
    assert normalize({"model": "Acurite-609TXC", "id": 123, "temperature_C": 20}) is None


def test_model_name_identifies_tpms_without_type_field():
    assert is_tpms({"model": "Jansite-TPMS", "id": "abc"})
    assert normalize({"model": "Jansite-TPMS", "id": "abc"}) is not None


def test_missing_id_is_rejected():
    assert normalize({"model": "Toyota-TPMS", "type": "TPMS"}) is None


def test_missing_time_falls_back_to_now():
    reading = normalize({"model": "Toyota-TPMS", "type": "TPMS", "id": "a"})
    assert reading.ts > 0


@pytest.mark.parametrize(
    "value",
    ["2026-08-25 15:34:12", "2026-08-25 15:34:12.500000", "2026-08-25T15:34:12Z"],
)
def test_time_formats(value):
    assert parse_rtl_time(value) is not None


def test_time_is_parsed_as_utc_not_local():
    # rtl_433 runs with -M utc; treating this as local time would shift the
    # whole log by the machine's offset.
    assert parse_rtl_time("2026-08-25 15:34:12") == 1787672052.0


def test_malformed_line_counted_not_raised(ingestor):
    assert ingestor.handle_line("rtl_433 version 25.12") is None
    assert ingestor.stats["malformed"] == 1
