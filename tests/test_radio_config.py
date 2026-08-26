from pathlib import Path

import pytest

from tpms.config import Config, RadioConfig, load_config
from tpms.radio import build_command


def test_default_is_315_with_no_hopping():
    cmd = build_command(RadioConfig(all_protocols=True))
    assert "-f" in cmd and "315M" in cmd
    assert "-H" not in cmd, "hopping is meaningless with a single frequency"


def test_utc_is_always_requested():
    # Without -M utc, rtl_433 stamps local time with no offset.
    cmd = build_command(RadioConfig())
    assert cmd[cmd.index("-M") + 1] == "utc"


def test_multiple_frequencies_enable_hopping():
    cmd = build_command(
        RadioConfig(frequencies=["315M", "433.92M"], hop_seconds=45, all_protocols=True)
    )
    assert cmd.count("-f") == 2
    assert cmd[cmd.index("-H") + 1] == "45"


def test_discovered_protocols_are_passed_as_R_flags():
    cmd = build_command(RadioConfig(), protocols=[59, 60, 88])
    assert cmd.count("-R") == 3
    assert cmd[cmd.index("-R") + 1] == "59"


def test_all_protocols_suppresses_the_filter():
    assert build_command(RadioConfig(all_protocols=True), protocols=[59, 60]).count("-R") == 0


def test_no_R_flags_when_discovery_failed():
    """Passing a protocol number this build rejects makes rtl_433 print its
    protocol table and exit, which crash-loops. Decoding everything is the
    safer failure mode."""
    assert build_command(RadioConfig(), protocols=[]).count("-R") == 0
    assert build_command(RadioConfig(), protocols=None).count("-R") == 0


def test_tuner_options_pass_through():
    cmd = build_command(
        RadioConfig(device="0", gain=40.2, ppm_error=12, sample_rate="1024k",
                    all_protocols=True, extra_args=["-vv"])
    )
    for flag, value in (("-d", "0"), ("-g", "40.2"), ("-p", "12"), ("-s", "1024k")):
        assert cmd[cmd.index(flag) + 1] == value
    assert cmd[-1] == "-vv"


def test_memory_database_is_not_turned_into_a_file(tmp_path):
    """`:memory:` is a SQLite sentinel; joining it to base_dir would create a
    real file named ':memory:' that silently persists between runs."""
    config = Config(database=":memory:", base_dir=tmp_path)
    assert config.database_path == Path(":memory:")


def test_relative_paths_resolve_against_the_config_file(tmp_path):
    (tmp_path / "config.yaml").write_text("database: data/tpms.db\n")
    config = load_config(tmp_path / "config.yaml")
    assert config.database_path == tmp_path / "data" / "tpms.db"


def test_yaml_overrides_merge_into_defaults(tmp_path):
    (tmp_path / "config.yaml").write_text("radio:\n  frequencies: ['433.92M']\n")
    config = load_config(tmp_path / "config.yaml")
    assert config.radio.frequencies == ["433.92M"]
    assert config.radio.binary == "rtl_433", "untouched keys keep their defaults"
    assert config.sessions.gap_seconds == 120


def test_unknown_top_level_key_is_rejected(tmp_path):
    (tmp_path / "config.yaml").write_text("nonsense: 1\n")
    with pytest.raises(ValueError, match="unknown config keys"):
        load_config(tmp_path / "config.yaml")


def test_example_config_is_valid():
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    config = load_config(example)
    assert config.radio.frequencies == ["315M"]


# -- protocol exclusion ---------------------------------------------------

PROTOCOL_TABLE = {
    59: "Toyota TPMS",
    60: "Ford TPMS",
    88: "Citroen TPMS",
    90: "Jansite TPMS",
    140: "Jansite-Solar TPMS",
    212: "Renault TPMS",
}


def _fake_table(monkeypatch, table=None):
    monkeypatch.setattr(
        "tpms.radio.list_protocols", lambda binary: dict(table or PROTOCOL_TABLE)
    )


def test_excluded_decoders_are_left_out(monkeypatch):
    """Jansite matches other makers' bursts, inventing phantom sensors."""
    from tpms.radio import discover_tpms_protocols

    _fake_table(monkeypatch)
    assert discover_tpms_protocols("rtl_433", ["Jansite"]) == [59, 60, 88, 212]


def test_exclusion_is_case_insensitive_and_matches_variants(monkeypatch):
    from tpms.radio import discover_tpms_protocols

    _fake_table(monkeypatch)
    selected = discover_tpms_protocols("rtl_433", ["jansite"])
    assert 90 not in selected and 140 not in selected


def test_nothing_is_excluded_without_a_pattern(monkeypatch):
    from tpms.radio import discover_tpms_protocols

    _fake_table(monkeypatch)
    assert discover_tpms_protocols("rtl_433", []) == [59, 60, 88, 90, 140, 212]


def test_an_unmatched_exclusion_warns_but_keeps_going(monkeypatch, caplog):
    """A typo must not silently disable nothing, nor drop every decoder."""
    from tpms.radio import discover_tpms_protocols

    _fake_table(monkeypatch)
    with caplog.at_level("WARNING"):
        selected = discover_tpms_protocols("rtl_433", ["Jansit"])  # missing 'e'
    assert selected == [59, 60, 88, 212], "substring still matches"

    with caplog.at_level("WARNING"):
        selected = discover_tpms_protocols("rtl_433", ["Nonesuch"])
    assert selected == [59, 60, 88, 90, 140, 212]
    assert "no matching" in caplog.text


def test_jansite_is_excluded_by_default():
    assert RadioConfig().exclude_protocols == ["Jansite"]


def test_exclusions_can_be_configured_away(tmp_path):
    (tmp_path / "config.yaml").write_text("radio:\n  exclude_protocols: []\n")
    config = load_config(tmp_path / "config.yaml")
    assert config.radio.exclude_protocols == []
