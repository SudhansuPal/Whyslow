"""Where whyslow keeps its files.

Everything lives under the current user's ~/Library. The CLI sets umask 077 at
startup so every file and directory we create is private to the user; the
database and log files are additionally chmod'ed to 0600 when opened.

Overrides (mainly for development and tests):
  WHYSLOW_HOME    use this directory for data, config, logs and the pid file
  WHYSLOW_CONFIG  read the config file from this path
"""

from __future__ import annotations

import os
from pathlib import Path

APP = "whyslow"


def home() -> Path:
    override = os.environ.get("WHYSLOW_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / APP


def db_path() -> Path:
    return home() / "whyslow.sqlite3"


def config_path() -> Path:
    override = os.environ.get("WHYSLOW_CONFIG")
    return Path(override).expanduser() if override else home() / "config.toml"


def log_dir() -> Path:
    if os.environ.get("WHYSLOW_HOME"):
        return home() / "logs"
    return Path.home() / "Library" / "Logs" / APP


def pid_path() -> Path:
    return home() / "whyslow.pid"


def lock_path() -> Path:
    return home() / "whyslow.lock"


def pause_path() -> Path:
    """Marker file: while it exists the running sampler skips its ticks."""
    return home() / "paused"


def token_path() -> Path:
    """Dashboard session token for the running sampler (0600, removed on stop)."""
    return home() / "dashboard.token"


def ensure_private_dir(path: Path) -> Path:
    if not path.exists():
        path.mkdir(mode=0o700, parents=True)
    return path


def make_private(path: Path) -> None:
    """Tighten one of *our* files to 0600 (it may predate a umask change)."""
    if path.exists():
        os.chmod(path, 0o600)
