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

# rtl_433 protocol numbers whose decoders emit type=TPMS. Sourced from the
# upstream rtl_433.example.conf device list.
TPMS_PROTOCOLS: tuple[int, ...] = (
    59, 60, 82, 88, 89, 90, 95, 110, 123, 140, 156, 168, 180, 186, 201, 203,
    208, 212, 225, 226, 241, 248, 252, 257, 275, 295, 298, 299, 321, 322, 328,
    343, 352, 354, 355, 362, 365, 378, 381,
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
    "clustering": {
        "window_seconds": 10,
        "min_cooccurrences": 3,
        "min_support": 0.6,
        "max_cluster_size": 6,
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
class ClusterConfig:
    window_seconds: float = 10
    min_cooccurrences: int = 3
    min_support: float = 0.6
    max_cluster_size: int = 6
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
        clustering=ClusterConfig(**data["clustering"]),
        web=WebConfig(**data["web"]),
        base_dir=base_dir,
    )
