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

from .models import set_display_timezone

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
    # Every time on screen is written in this zone. Readings are stored as
    # epoch seconds and always have been -- this is a display setting, and
    # changing it re-reads history rather than rewriting it.
    "timezone": "America/Chicago",
    "radio": {
        "binary": "rtl_433",
        "frequencies": ["315M"],
        "hop_seconds": 30,
        "device": None,
        "gain": None,
        "ppm_error": None,
        "sample_rate": None,
        "all_protocols": False,
        # Jansite matches almost any TPMS burst, so it re-decodes other
        # makers' packets under its own name. Every one of those is a phantom
        # sensor the duplicate detector then has to clean up. Excluded by
        # default; drop it from this list to get the decoder back.
        "exclude_protocols": ["Jansite"],
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
        # Wheel sets appear to be programmed with near-consecutive IDs, so two
        # sensors from one decoder with close IDs are likely one vehicle.
        # Measure it against your own capture with `tpms ids` before trusting
        # it; the widest genuine pair seen so far was 10042 apart.
        "id_adjacency": True,
        "id_max_distance": 65536,
        # A sensor audible this share of the time, over at least this long, is
        # parked in range rather than driving past. It co-occurs with all
        # passing traffic, so it is not allowed to seed a single-pass grouping.
        "resident_duty_cycle": 0.5,
        "resident_min_span_seconds": 3600,
        "single_pass_rssi_spread": 10.0,
        "auto_interval_seconds": 300,
    },
    "direction": {
        # Which wheels were heard says which side of the vehicle faced the
        # receiver. Only you know which way traffic on that side is going, so
        # name the two sides here and the log will use the names. Left unset,
        # the UI reports "left side" / "right side" and claims nothing more.
        "left": None,
        "right": None,
        # When both sides are audible the near one is the louder one -- but a
        # single reading's level swings a few dB on nothing, so a call needs
        # this much of a gap before it is worth making.
        "rssi_margin": 6.0,
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8080,
    },
    "retention": {
        # Forgetting the raw JSON of old readings costs nothing -- every line
        # is also on disk under raw/ -- and it is about two thirds of the
        # database. On by default for that reason.
        "raw_days": 7,
        # Deleting readings outright is not, so it is off until you ask. The
        # sightings they roll up into are kept whatever this says: the
        # appearance log is the history, and it is a fraction of the size.
        "readings_days": None,
        "archive_gzip_days": 7,
        "archive_delete_days": None,
        # Reclaim the freed pages afterwards. A VACUUM rewrites the file, so
        # it needs room for a second copy and a moment of exclusive access.
        "vacuum": True,
        # Run the above once a day inside the service.
        "run_daily": True,
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
    #: Decoder names (case-insensitive substrings) to leave out of -R.
    exclude_protocols: list[str] = field(default_factory=lambda: ["Jansite"])
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
    #: Treat same-decoder sensors with IDs this close as candidates for one
    #: vehicle. Never creates an edge on its own -- the pair must still have
    #: been heard together.
    id_adjacency: bool = True
    id_max_distance: int = 65536
    #: Share of its observed window a sensor must be audible to count as
    #: resident, and the shortest window over which that means anything.
    resident_duty_cycle: float = 0.5
    resident_min_span_seconds: float = 3600
    #: Widest spread of mean RSSI, in dB, tolerated within one such group.
    single_pass_rssi_spread: float = 10.0
    auto_interval_seconds: float = 300


@dataclass
class DirectionConfig:
    #: What to call a pass whose left / right side faced the receiver --
    #: "northbound", "towards town". None means report the side itself.
    left: str | None = None
    right: str | None = None
    #: dB one side must beat the other by before the louder is called nearer.
    rssi_margin: float = 6.0

    @property
    def names(self) -> dict[str, str | None]:
        return {"left": self.left, "right": self.right}


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class RetentionConfig:
    """How long each kind of data is kept.

    Sightings and per-sensor totals are never pruned. They are the summary of
    everything here at a fraction of the size, so a database trimmed to a
    fortnight of readings still knows every vehicle it has ever heard.
    """

    #: Drop the archived JSON text of readings older than this. Recoverable
    #: from raw/ with `tpms replay`, so this is nearly free.
    raw_days: float | None = 7
    #: Delete readings older than this outright. None keeps them forever.
    readings_days: float | None = None
    #: Compress raw/rtl433-*.jsonl files older than this (~10:1 on JSON).
    archive_gzip_days: float | None = 7
    #: Delete those archives entirely past this age.
    archive_delete_days: float | None = None
    #: VACUUM after a run that deleted rows. SQLite does not shrink otherwise.
    vacuum: bool = True
    #: Have the service do this daily, rather than only on `tpms prune`.
    run_daily: bool = True


@dataclass
class Config:
    database: str = "tpms.db"
    #: IANA name -- "America/Chicago", "UTC", "Europe/London".
    timezone: str = "America/Chicago"
    radio: RadioConfig = field(default_factory=RadioConfig)
    sessions: SessionConfig = field(default_factory=SessionConfig)
    aliases: AliasConfig = field(default_factory=AliasConfig)
    clustering: ClusterConfig = field(default_factory=ClusterConfig)
    direction: DirectionConfig = field(default_factory=DirectionConfig)
    web: WebConfig = field(default_factory=WebConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    #: Directory the config file was loaded from; relative paths resolve here.
    base_dir: Path = field(default_factory=Path.cwd)
    #: The file this was read from, if any. The Settings page writes back to
    #: it. None when the program was started with no --config at all, in which
    #: case a save creates `config.yaml` in base_dir and says so.
    source_path: Path | None = None

    @property
    def write_path(self) -> Path:
        return self.source_path or (self.base_dir / "config.yaml")

    def __post_init__(self) -> None:
        # Building a config is what puts its zone into effect: the CLI, the
        # service and the tests all construct one directly, and a stamp
        # rendered before anyone called a setter would be in the wrong zone.
        set_display_timezone(self.timezone)

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
    config_path: Path | None = None

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
        timezone=data["timezone"],
        radio=RadioConfig(**data["radio"]),
        sessions=SessionConfig(**data["sessions"]),
        aliases=AliasConfig(**data["aliases"]),
        clustering=ClusterConfig(**data["clustering"]),
        direction=DirectionConfig(**data["direction"]),
        web=WebConfig(**data["web"]),
        retention=RetentionConfig(**data["retention"]),
        base_dir=base_dir,
        source_path=config_path if path is not None else None,
    )


# -- editing it from the UI -------------------------------------------------
#
# The Settings page rewrites config.yaml in place. That file is the one an
# operator hand-edits, so a rewrite that dropped its comments would strip the
# thing that makes it readable -- every knob here has a reason, and the reason
# is worth more than the number. The prose therefore lives beside the defaults
# rather than only in the file, and `dump` re-emits it on every save, so a
# file written by the UI reads like one written by hand.

#: Why each section exists. Emitted above the section in the written file, and
#: shown under its heading on the Settings page.
SECTION_HELP: dict[str, str] = {
    "radio": (
        "The receiver itself. Changes here need the receiver restarted before\n"
        "they take effect -- the Settings page offers that when you save one."
    ),
    "sessions": (
        "What counts as one continuous sighting. A sensor heard again within\n"
        "the gap extends its sighting; heard after it, it starts a new one."
    ),
    "aliases": (
        "Several rtl_433 decoders can match one RF burst, so one transmitter\n"
        "shows up under several protocol names. These tolerances decide when\n"
        "two decodes are the same instant of the same signal."
    ),
    "clustering": (
        "How sensors are grouped into vehicles. Every one of these trades a\n"
        "missed grouping against a wrong one; `tpms diagnose` shows what the\n"
        "current settings are doing to your own capture."
    ),
    "direction": (
        "Which wheels were heard says which side of the vehicle faced the\n"
        "receiver. Naming the two sides turns that into a direction -- only\n"
        "you know which way traffic on each side is going, so the program\n"
        "never guesses it. Unnamed, the log reports the side and stops."
    ),
    "web": "Where the web UI listens.",
    "retention": (
        "Housekeeping. Sightings are never pruned whatever these say: the\n"
        "traffic log is the history, and it costs a fraction of what it\n"
        "summarises."
    ),
}

#: Why each individual setting exists, by dotted path. Anything absent here
#: renders bare, which is the honest signal that it explains itself.
FIELD_HELP: dict[str, str] = {
    "database": "SQLite file. Relative paths resolve beside this config file.",
    "timezone": (
        "IANA name. Every stamp on screen, in the charts and in the CSV is "
        "written in this zone; readings are stored as epoch seconds either way, "
        "so changing it re-reads history rather than rewriting it."
    ),
    "radio.binary": "rtl_433 executable, found on PATH unless given a full path.",
    "radio.frequencies": (
        "Bands to tune. North American factory TPMS is on 315M; EU and most "
        "aftermarket sensors are on 433.92M. More than one hops between them, "
        "which halves the dwell time per band and drops a real share of passes."
    ),
    "radio.hop_seconds": "Seconds on each band before moving to the next.",
    "radio.device": "Dongle index or serial. Empty picks the first one found.",
    "radio.gain": (
        "Tuner gain in dB. Empty is automatic, which usually loses to a fixed "
        "30-40 for bursts as short as TPMS."
    ),
    "radio.ppm_error": "Crystal correction, in parts per million.",
    "radio.sample_rate": "Sample rate, e.g. 250k. Empty uses the rtl_433 default.",
    "radio.all_protocols": (
        "Enable every decoder rtl_433 has, not just the TPMS ones. Much more "
        "CPU, and a great deal of traffic that is not a vehicle."
    ),
    "radio.exclude_protocols": (
        "Decoders to leave off by name. Jansite matches almost any TPMS burst "
        "and re-decodes other makers' packets under its own name, so every one "
        "is a phantom sensor the duplicate detector then has to clean up."
    ),
    "radio.extra_args": "Extra rtl_433 arguments, one per entry.",
    "radio.raw_archive_dir": (
        "Where the raw JSON lines are archived. Empty turns archiving off, "
        "which makes a normalization bug unrecoverable."
    ),
    "radio.restart_min_delay": "Seconds before the first restart attempt.",
    "radio.restart_max_delay": "Longest backoff between restart attempts.",
    "sessions.gap_seconds": (
        "Silence that ends a sighting. TPMS sensors transmit every 30-60s "
        "while rolling, so anything under about 90 splits one pass in two."
    ),
    "sessions.sweep_interval_seconds": "How often open sightings are checked for having ended.",
    "aliases.time_tolerance": "Seconds apart two decodes may be and still be one burst.",
    "aliases.rssi_tolerance": "dB apart, likewise. The same burst arrives at one level.",
    "aliases.snr_tolerance": "Signal-to-noise apart, likewise.",
    "aliases.require_different_decoder": (
        "Only fold together decodes from different protocols. Two readings "
        "from one decoder at one instant are two transmitters, not one."
    ),
    "aliases.min_shared_bursts": "Coincidences before two sensors are called aliases.",
    "aliases.min_share_ratio": "Share of the rarer sensor's readings that must coincide.",
    "aliases.auto_interval_seconds": "How often to re-scan. 0 turns the automatic scan off.",
    "clustering.window_seconds": "How close in time two readings count as heard together.",
    "clustering.min_cooccurrences": "Separate passes two sensors must share before grouping.",
    "clustering.min_support": (
        "Share of the rarer sensor's sightings the two must share. This is the "
        "number that separates wheels on one car from two cars that happened "
        "to pass together."
    ),
    "clustering.max_cluster_size": (
        "Sensors past which a cluster is flagged for review rather than "
        "trusted. Most vehicles carry four."
    ),
    "clustering.single_pass": (
        "Let one pass seed a provisional grouping, when the sensors share a "
        "decoder and a similar level. Off, a vehicle needs several passes "
        "before it exists at all."
    ),
    "clustering.id_adjacency": (
        "Treat near-consecutive IDs from one decoder as evidence of one wheel "
        "set. Measure it against your own capture with `tpms ids` first."
    ),
    "clustering.id_max_distance": "How far apart two IDs may be and still count as adjacent.",
    "clustering.resident_duty_cycle": (
        "Audible this share of the time and a sensor is parked in range rather "
        "than driving past, so it is not allowed to seed a one-pass grouping."
    ),
    "clustering.resident_min_span_seconds": "Shortest window over which that share means anything.",
    "clustering.single_pass_rssi_spread": "Widest dB spread tolerated within a one-pass grouping.",
    "clustering.auto_interval_seconds": "How often to re-cluster. 0 turns the automatic run off.",
    "direction.left": (
        'What to call a pass whose left side faced the receiver -- "northbound", '
        '"towards town". Empty reports "left side" instead.'
    ),
    "direction.right": "The same for the right side.",
    "direction.rssi_margin": (
        "When both sides are audible the near one is the louder one, but a "
        "single reading's level swings a few dB on nothing at all. One side "
        "must beat the other by this much before the guess is worth making."
    ),
    "web.host": "Interface to bind. 0.0.0.0 is every interface on the machine.",
    "web.port": "Port to listen on.",
    "retention.raw_days": (
        "Forget the raw JSON of readings older than this. It is about two "
        "thirds of the database and every line is also on disk under raw/, so "
        "this costs nothing. Empty keeps it forever."
    ),
    "retention.readings_days": (
        "Delete readings outright past this age. Empty keeps them forever. The "
        "sightings they roll up into are kept whatever this says."
    ),
    "retention.archive_gzip_days": "Compress raw archive files older than this. Empty leaves them.",
    "retention.archive_delete_days": "Delete raw archive files older than this. Empty keeps them.",
    "retention.vacuum": "VACUUM after a run that deleted rows. SQLite does not shrink otherwise.",
    "retention.run_daily": "Run the above daily inside the service, not only on `tpms prune`.",
}

#: Fields of Config that describe where the config came from rather than what
#: it says. They are not settings, are not written to the file, and must not
#: appear on the Settings page -- a Path in the dump also cannot be
#: represented as YAML, so leaking one turns every save into a 500.
NOT_SETTINGS: frozenset[str] = frozenset({"base_dir", "source_path"})

#: Settings the running process cannot adopt without being restarted.
#: `database` is the whole of it: every component was handed a connection to
#: the old file at startup, so changing this changes where the *next* run
#: looks and nothing about this one. Shown, never editable -- a box that
#: silently does nothing until a restart is worse than no box.
READ_ONLY: frozenset[str] = frozenset({"database"})

#: Sections whose values the receiver only reads when it starts.
NEEDS_RADIO_RESTART: frozenset[str] = frozenset({"radio"})


@dataclass(frozen=True)
class Setting:
    """One editable value, described well enough to render and to parse."""

    path: str            # "radio.gain"
    section: str | None  # "radio", or None for a top-level key
    name: str            # "gain"
    kind: str            # bool | int | float | str | list
    optional: bool       # may be left empty, meaning None
    value: Any
    default: Any
    help: str = ""
    read_only: bool = False

    @property
    def label(self) -> str:
        return self.name.replace("_", " ")


def _kind_of(annotation: Any) -> tuple[str, bool]:
    """Map a dataclass annotation onto a form control.

    Annotations arrive as strings under `from __future__ import annotations`,
    so this reads them as text rather than resolving them -- the set in play
    is small and closed, and importing typing machinery to parse six shapes
    would be more code than the six shapes.
    """
    text = annotation if isinstance(annotation, str) else getattr(
        annotation, "__name__", str(annotation)
    )
    optional = "None" in text
    if text.startswith("list"):
        return "list", optional
    for name in ("bool", "int", "float", "str"):
        # bool before int: `bool` is a subclass and would match the wrong one.
        if text.startswith(name):
            return name, optional
    return "str", optional


def settings(config: Config) -> list[Setting]:
    """Every editable value, in the order the file declares them.

    Generated from the dataclasses rather than listed by hand, so a key added
    to the config appears on the Settings page without anyone remembering to
    put it there -- and cannot be silently absent from it either.
    """
    import dataclasses

    out: list[Setting] = []
    for field_ in dataclasses.fields(config):
        if field_.name in NOT_SETTINGS:
            continue  # where the file was found, not a setting in it
        value = getattr(config, field_.name)
        if dataclasses.is_dataclass(value):
            for sub in dataclasses.fields(value):
                path = f"{field_.name}.{sub.name}"
                kind, optional = _kind_of(sub.type)
                out.append(
                    Setting(
                        path=path,
                        section=field_.name,
                        name=sub.name,
                        kind=kind,
                        optional=optional,
                        value=getattr(value, sub.name),
                        default=_default_at(path),
                        help=FIELD_HELP.get(path, ""),
                        read_only=path in READ_ONLY,
                    )
                )
        else:
            kind, optional = _kind_of(field_.type)
            out.append(
                Setting(
                    path=field_.name,
                    section=None,
                    name=field_.name,
                    kind=kind,
                    optional=optional,
                    value=value,
                    default=_default_at(field_.name),
                    help=FIELD_HELP.get(field_.name, ""),
                    read_only=field_.name in READ_ONLY,
                )
            )
    return out


def _default_at(path: str) -> Any:
    node: Any = DEFAULTS
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


class SettingError(ValueError):
    """A value the config will not take, named by the box that carried it."""

    def __init__(self, path: str, message: str):
        super().__init__(message)
        self.path = path


def coerce(setting: Setting, raw: Any) -> Any:
    """Turn one submitted form value into what the dataclass wants.

    A refusal names the field, because the Settings page has forty boxes on it
    and "invalid literal for int()" identifies none of them.
    """
    if setting.kind == "bool":
        return raw in (True, "1", "true", "on", "yes")

    if setting.kind == "list":
        if isinstance(raw, str):
            items = [part.strip() for part in raw.replace("\n", ",").split(",")]
        else:
            items = [str(part).strip() for part in (raw or [])]
        items = [item for item in items if item]
        # A list of ints stays a list of ints: extra_args is text, but
        # nothing else that is a list wants to become one by round-tripping.
        if items and all(item.lstrip("-").isdigit() for item in items) and (
            setting.default and all(isinstance(d, int) for d in setting.default)
        ):
            return [int(item) for item in items]
        return items

    text = "" if raw is None else str(raw).strip()
    if text == "":
        if setting.optional:
            return None
        raise SettingError(setting.path, f"{setting.label} cannot be empty")

    if setting.kind == "int":
        try:
            return int(float(text))
        except ValueError:
            raise SettingError(setting.path, f"{setting.label} must be a number") from None
    if setting.kind == "float":
        try:
            return float(text)
        except ValueError:
            raise SettingError(setting.path, f"{setting.label} must be a number") from None
    return text


def apply(config: Config, values: dict[str, Any]) -> list[str]:
    """Write ``{path: value}`` into a live Config, in place.

    In place, and never by replacing a sub-config: the ingestor, the clusterer,
    the alias detector and the radio were each handed a reference to one of
    these objects when they were built, so rebinding `config.radio` would
    leave every one of them reading the old settings while the page showed the
    new ones. Mutating the object they all hold is what makes a save take
    effect without a restart.

    Returns the paths that actually changed.
    """
    changed: list[str] = []
    for setting in settings(config):
        if setting.read_only or setting.path not in values:
            continue
        target = config if setting.section is None else getattr(config, setting.section)
        if getattr(target, setting.name) == values[setting.path]:
            continue
        setattr(target, setting.name, values[setting.path])
        changed.append(setting.path)

    if "timezone" in changed:
        # The zone is global state, set when a Config is built. Nothing
        # rebuilds one here, so a save has to put it into effect itself --
        # otherwise every stamp on the page stays in the old zone until the
        # process restarts, which is exactly the bug this page invites.
        set_display_timezone(config.timezone)
    return changed


def to_dict(config: Config) -> dict[str, Any]:
    """The whole config as plain data, shaped like the file."""
    import dataclasses

    out: dict[str, Any] = {}
    for field_ in dataclasses.fields(config):
        if field_.name in NOT_SETTINGS:
            continue
        value = getattr(config, field_.name)
        if dataclasses.is_dataclass(value):
            out[field_.name] = {
                sub.name: getattr(value, sub.name)
                for sub in dataclasses.fields(value)
            }
        else:
            out[field_.name] = value
    return out


def _wrap(text: str, width: int = 74) -> list[str]:
    import textwrap

    lines: list[str] = []
    for paragraph in text.split("\n"):
        lines.extend(textwrap.wrap(paragraph, width) or [""])
    return lines


def dump(data: dict[str, Any]) -> str:
    """Render the config as YAML, with its commentary put back.

    The Settings page rewrites config.yaml in place, and a rewrite that
    emitted bare values would strip the reason from every knob in it -- the
    part of that file worth reading. So the prose is regenerated here from
    FIELD_HELP on every save, and a file written by the UI comes out looking
    like one written by hand.
    """
    lines = [
        "# TPMS watch configuration.",
        "#",
        "# Written by the Settings page. Hand edits are kept -- values are read",
        "# back before every save -- but comments in this file are regenerated,",
        "# so notes of your own belong beside the key they explain, not here.",
    ]
    for key, value in data.items():
        lines.append("")
        if key in SECTION_HELP:
            lines.extend(f"# {line}" if line else "#" for line in _wrap(SECTION_HELP[key]))
        elif key in FIELD_HELP:
            lines.extend(f"# {line}" if line else "#" for line in _wrap(FIELD_HELP[key]))

        if not isinstance(value, dict):
            lines.append(_yaml_line(key, value))
            continue

        lines.append(f"{key}:")
        for sub_key, sub_value in value.items():
            help_text = FIELD_HELP.get(f"{key}.{sub_key}")
            if help_text:
                lines.extend(
                    f"  # {line}" if line else "  #" for line in _wrap(help_text, 72)
                )
            lines.append("  " + _yaml_line(sub_key, sub_value))
    return "\n".join(lines) + "\n"


def _yaml_line(key: str, value: Any) -> str:
    # yaml.safe_dump of a one-key mapping, so quoting, escaping and the
    # null/true/false spellings are the library's problem rather than a set of
    # special cases written out here.
    if isinstance(value, list):
        # Inline, so a two-band radio is one line rather than three. An empty
        # list has to be written out: safe_dump gives "key: []" already.
        inline = yaml.safe_dump(value, default_flow_style=True).strip()
        return f"{key}: {inline}"
    return yaml.safe_dump(
        {key: value}, default_flow_style=False, sort_keys=False
    ).strip()
