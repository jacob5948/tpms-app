"""Configuration loading.

Defaults live here so the app runs with no config file at all; a YAML file
overrides them key by key (nested dicts merge, scalars replace).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Fallback TPMS protocol numbers, used only if the installed rtl_433 cannot be
# queried. Protocol numbers are NOT stable across rtl_433 versions and older
# builds simply do not have the higher ones, so the real list is discovered at
# runtime from `rtl_433 -R help` -- see radio.discover_tpms_protocols.
FALLBACK_TPMS_PROTOCOLS: tuple[int, ...] = (
    59, 60, 82, 88, 89, 90, 95, 110, 123, 140, 156, 168, 180, 186, 201, 203,
    208, 212, 225, 226, 241, 248, 252, 257, 275,
)

DEFAULTS: dict[str, Any] = {
    "database": "tpms.db",
    "radio": {
        "binary": "rtl_433",
        "frequencies": ["315M"],
        "hop_seconds": 30,
        "device": None,
        "gain": None,
        "ppm_error": None,
        "sample_rate": None,
        "all_protocols": False,
        "extra_args": [],
        "raw_archive_dir": "raw",
        "restart_min_delay": 1,
        "restart_max_delay": 60,
    },
    "sessions": {
        "gap_seconds": 120,
        "sweep_interval_seconds": 15,
    },
    "aliases": {
        "time_tolerance": 1.0,
        "rssi_tolerance": 0.5,
        "snr_tolerance": 1.0,
        "require_different_decoder": True,
        "min_shared_bursts": 1,
        "min_share_ratio": 0.5,
        "auto_interval_seconds": 300,
    },
    "clustering": {
        "window_seconds": 10,
        "min_cooccurrences": 3,
        "min_support": 0.6,
        "max_cluster_size": 6,
        "single_pass": True,
        "single_pass_rssi_spread": 10.0,
        "auto_interval_seconds": 300,
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8080,
    },
}


@dataclass
class RadioConfig:
    binary: str = "rtl_433"
    frequencies: list[str] = field(default_factory=lambda: ["315M"])
    hop_seconds: int = 30
    device: str | None = None
    gain: float | None = None
    ppm_error: int | None = None
    sample_rate: str | None = None
    all_protocols: bool = False
    extra_args: list[str] = field(default_factory=list)
    raw_archive_dir: str | None = "raw"
    restart_min_delay: float = 1
    restart_max_delay: float = 60


@dataclass
class SessionConfig:
    gap_seconds: float = 120
    sweep_interval_seconds: float = 15


@dataclass
class AliasConfig:
    """Detection of one transmitter decoded by several protocols."""

    #: Readings this far apart can still be the same burst. rtl_433 stamps to
    #: the second unless -M time:usec is in play, so the same burst can land
    #: either side of a second boundary.
    time_tolerance: float = 1.0
    #: How far apart, in dB, two decoders may measure the same burst. Measured
    #: against real capture, genuine duplicates agree *exactly* -- both decoders
    #: read the one burst -- while the nearest false candidate differed by 1.1.
    #: The margin here is for safety, not because a spread is expected.
    rssi_tolerance: float = 0.5
    #: Same, for SNR. Ignored when either reading lacks it.
    snr_tolerance: float = 1.0
    #: Only ever treat *different* decoders as duplicates. Wheels on one car
    #: share an OEM sensor type, so a same-decoder pair is a real pair of
    #: sensors, never one sensor seen twice.
    require_different_decoder: bool = True
    #: Identical-signal bursts needed before two sensors are called duplicates.
    #: One is enough: matching RSSI *and* SNR at the same instant is already a
    #: strong fingerprint, and most vehicles are only ever heard once.
    min_shared_bursts: int = 1
    #: Share of the quieter sensor's readings that must be shared bursts.
    min_share_ratio: float = 0.5
    #: Re-run automatically this often (seconds). 0 disables.
    auto_interval_seconds: float = 300


@dataclass
class ClusterConfig:
    window_seconds: float = 10
    min_cooccurrences: int = 3
    min_support: float = 0.6
    max_cluster_size: int = 6
    #: Group sensors heard together in a *single* pass when they look like one
    #: vehicle: same decoder, comparable signal level. Most vehicles on a public
    #: road are only ever heard once, so without this they never group at all.
    #: The resulting vehicles are marked provisional until a return visit
    #: corroborates them.
    single_pass: bool = True
    #: Widest spread of mean RSSI, in dB, tolerated within one such group.
    single_pass_rssi_spread: float = 10.0
    auto_interval_seconds: float = 300


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class Config:
    database: str = "tpms.db"
    radio: RadioConfig = field(default_factory=RadioConfig)
    sessions: SessionConfig = field(default_factory=SessionConfig)
    aliases: AliasConfig = field(default_factory=AliasConfig)
    clustering: ClusterConfig = field(default_factory=ClusterConfig)
    web: WebConfig = field(default_factory=WebConfig)
    #: Directory the config file was loaded from; relative paths resolve here.
    base_dir: Path = field(default_factory=Path.cwd)

    @property
    def database_path(self) -> Path:
        return self._resolve(self.database)

    @property
    def raw_archive_path(self) -> Path | None:
        if not self.radio.raw_archive_dir:
            return None
        return self._resolve(self.radio.raw_archive_dir)

    def _resolve(self, value: str) -> Path:
        # ":memory:" is a SQLite sentinel, not a filename -- never join it to
        # base_dir or it silently becomes a real file on disk.
        if value == ":memory:":
            return Path(value)
        path = Path(value).expanduser()
        return path if path.is_absolute() else (self.base_dir / path)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = None) -> Config:
    """Load config from ``path``, falling back to built-in defaults."""
    data = DEFAULTS
    base_dir = Path.cwd()

    if path is not None:
        config_path = Path(path).expanduser().resolve()
        if not config_path.exists():
            raise FileNotFoundError(f"config file not found: {config_path}")
        loaded = yaml.safe_load(config_path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"config file must contain a mapping: {config_path}")
        data = _merge(DEFAULTS, loaded)
        base_dir = config_path.parent

    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"unknown config keys: {', '.join(sorted(unknown))}")

    return Config(
        database=data["database"],
        radio=RadioConfig(**data["radio"]),
        sessions=SessionConfig(**data["sessions"]),
        aliases=AliasConfig(**data["aliases"]),
        clustering=ClusterConfig(**data["clustering"]),
        web=WebConfig(**data["web"]),
        base_dir=base_dir,
    )
