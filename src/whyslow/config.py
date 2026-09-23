"""Configuration: a TOML file parsed with stdlib tomllib and strictly validated.

Unknown sections/keys, wrong types and out-of-range values are rejected with a
clear message. Nothing in the file is ever evaluated or imported.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


def _range(lo: float, hi: float) -> dict[str, Any]:
    return {"min": lo, "max": hi}


@dataclass(frozen=True)
class SamplingConfig:
    interval_seconds: float = field(default=1.0, metadata=_range(0.25, 60))
    # Per-tick process rows stored: the top N by CPU above min_process_cpu_percent...
    process_top_n: int = field(default=10, metadata=_range(1, 100))
    min_process_cpu_percent: float = field(default=1.0, metadata=_range(0, 100))
    # ...plus the top N by memory every this many ticks.
    memory_snapshot_every: int = field(default=30, metadata=_range(1, 3600))
    # Use Apple's /bin/ps (setuid, ships with macOS) to see CPU/RSS of processes
    # owned by other users (root, _windowserver, ...). No sudo involved.
    system_process_visibility: bool = True


@dataclass(frozen=True)
class StorageConfig:
    # Raw per-second samples.
    retention_days: float = field(default=7, metadata=_range(0.05, 365))
    # Hourly per-app rollups, spikes and their culprits (small; kept longer).
    rollup_retention_days: float = field(default=180, metadata=_range(1, 3650))


@dataclass(frozen=True)
class PrivacyConfig:
    # "redact": store/display command lines with secrets masked.
    # "name_only": never store or display command-line arguments at all.
    cmdline: str = field(default="redact", metadata={"choices": ("redact", "name_only")})


@dataclass(frozen=True)
class DetectorConfig:
    baseline_window_seconds: int = field(default=300, metadata=_range(30, 3600))
    # spike = value > rolling median + max(k * spread, min_delta), sustained for N samples
    threshold_k: float = field(default=4.0, metadata=_range(1, 50))
    sustain_samples: int = field(default=3, metadata=_range(1, 600))
    # Smallest jump that counts as a spike, per metric (stops idle metrics "spiking" on blips).
    cpu_min_delta_percent: float = field(default=15.0, metadata=_range(1, 100))
    memory_min_delta_percent: float = field(default=5.0, metadata=_range(0.5, 100))
    disk_min_delta_mb_per_s: float = field(default=20.0, metadata=_range(0.1, 100000))
    net_min_delta_mb_per_s: float = field(default=1.0, metadata=_range(0.01, 100000))
    culprits_top_n: int = field(default=5, metadata=_range(1, 20))


@dataclass(frozen=True)
class DashboardConfig:  # host is deliberately not configurable (always 127.0.0.1)
    enabled: bool = True
    port: int = field(default=8765, metadata=_range(1024, 65535))
    open_browser: bool = True  # open it when `whyslow start` runs


@dataclass(frozen=True)
class HelpersConfig:
    # Per-process disk I/O, network and energy via `sudo -n powermetrics`.
    # Off by default; needs a sudoers rule (`whyslow helpers --sudoers`).
    # nettop isn't needed (powermetrics covers it) and fs_usage is deliberately
    # not used at all - see whyslow/helpers.py.
    powermetrics: bool = False
    interval_seconds: float = field(default=30.0, metadata=_range(5, 3600))


@dataclass(frozen=True)
class Config:
    sampling: SamplingConfig = SamplingConfig()
    storage: StorageConfig = StorageConfig()
    privacy: PrivacyConfig = PrivacyConfig()
    detector: DetectorConfig = DetectorConfig()
    dashboard: DashboardConfig = DashboardConfig()
    helpers: HelpersConfig = HelpersConfig()


def _check_value(section: str, f: Any, value: Any) -> Any:
    where = f"[{section}] {f.name}"
    expected = f.type if isinstance(f.type, type) else {"float": float, "int": int, "bool": bool, "str": str}[f.type]
    # bool is a subclass of int in Python; never accept true/false for numbers.
    if expected is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true/false, got {value!r}")
    elif expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}: expected an integer, got {value!r}")
    elif expected is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: expected a number, got {value!r}")
        value = float(value)
    elif expected is str:
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a string, got {value!r}")
    meta = f.metadata
    if "min" in meta and not (meta["min"] <= value <= meta["max"]):
        raise ConfigError(f"{where}: {value!r} is outside the allowed range {meta['min']}..{meta['max']}")
    if "choices" in meta and value not in meta["choices"]:
        raise ConfigError(f"{where}: {value!r} is not one of {', '.join(meta['choices'])}")
    return value


def from_dict(data: dict[str, Any]) -> Config:
    sections = {f.name: f for f in fields(Config)}
    built: dict[str, Any] = {}
    for name, raw in data.items():
        if name not in sections:
            raise ConfigError(f"unknown section [{name}] (allowed: {', '.join(sections)})")
        if not isinstance(raw, dict):
            raise ConfigError(f"[{name}] must be a table")
        default = sections[name].default
        allowed = {f.name: f for f in fields(default)}
        values = {}
        for key, value in raw.items():
            if key not in allowed:
                raise ConfigError(f"[{name}] unknown key {key!r} (allowed: {', '.join(allowed)})")
            values[key] = _check_value(name, allowed[key], value)
        built[name] = replace(default, **values)
    return Config(**built)


def load(path: Path) -> Config:
    """Load and validate the config file; a missing file means all defaults."""
    if not path.exists():
        return Config()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None
    try:
        return from_dict(data)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def as_toml(cfg: Config) -> str:
    """Render the effective config (used by `whyslow config`)."""
    out = []
    for section in fields(cfg):
        out.append(f"[{section.name}]")
        for f in fields(getattr(cfg, section.name)):
            value = getattr(getattr(cfg, section.name), f.name)
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, str):
                rendered = f'"{value}"'
            else:
                rendered = repr(value)
            out.append(f"{f.name} = {rendered}")
        out.append("")
    return "\n".join(out)
