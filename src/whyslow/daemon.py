"""Background sampling loop: runs as the normal user, never as root.

A single instance is enforced with an flock on whyslow.lock that the running
sampler holds for its whole life; the pid file is informational. `stop` only
signals a PID if that lock is held *and* the PID is a whyslow process.
"""

from __future__ import annotations

import fcntl
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO

import psutil

from whyslow import paths
from whyslow import helpers as helpers_mod
from whyslow.config import Config
from whyslow.correlator import SpikeMonitor
from whyslow.sampler import Sampler
from whyslow import storage
from whyslow.storage import Store

log = logging.getLogger("whyslow")


class _Paused(Exception):
    """Internal: this tick was skipped because sampling is paused."""

_PRUNE_EVERY_S = 3600.0
_LOG_MAX_BYTES = 1_000_000
_LOG_BACKUPS = 3


def setup_logging(foreground: bool) -> None:
    """Rotating log capped at ~4 MB total. Never log command lines or secrets."""
    log_dir = paths.ensure_private_dir(paths.log_dir())
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "whyslow.log", maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8"
    )
    paths.make_private(log_dir / "whyslow.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    if foreground:
        log.addHandler(logging.StreamHandler(sys.stderr))
    log.setLevel(logging.INFO)


def _try_lock() -> IO[str] | None:
    paths.ensure_private_dir(paths.home())
    fh = open(paths.lock_path(), "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def running_pid() -> int | None:
    """PID of the running sampler, or None. Verifies the lock and the process."""
    probe = _try_lock()
    if probe is not None:  # nobody holds the lock -> not running
        probe.close()
        return None
    try:
        pid = int(paths.pid_path().read_text().strip())
        cmd = psutil.Process(pid).cmdline()
    except (OSError, ValueError, psutil.Error):
        return None
    return pid if is_sampler_cmdline(cmd) else None


def is_sampler_cmdline(argv: list[str]) -> bool:
    """True only for `python -m whyslow run ...` or `<venv>/bin/whyslow run ...`.

    Deliberately strict: merely containing "whyslow" isn't enough (an editor
    whose workspace is named whyslow has that in its argv too).
    """
    for i, part in enumerate(argv[:4]):
        is_module = part == "whyslow" and i > 0 and argv[i - 1] == "-m"
        is_script = part == "whyslow" or part.endswith("/whyslow")
        if (is_module or is_script) and "run" in argv[i + 1:]:
            return True
    return False


def run(cfg: Config, foreground: bool) -> int:
    lock = _try_lock()
    if lock is None:
        print("whyslow is already running (see `whyslow status`).", file=sys.stderr)
        return 1
    setup_logging(foreground)
    pid_file = paths.pid_path()

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())

    interval = cfg.sampling.interval_seconds
    helper = None
    if cfg.helpers.powermetrics:
        helper = helpers_mod.PowerMetrics(cfg.helpers.interval_seconds)
        helper.start()
        log.info("powermetrics helper enabled (one %ss sample every %.0fs, sudo -n)",
                 helpers_mod.SAMPLE_WINDOW_MS / 1000, helper.interval)
    sampler = Sampler(cfg, helper)
    store = Store(paths.db_path(), cfg)
    _write_helper_state(store, cfg, helper)
    if closed := store.close_dangling_spikes():
        log.info("closed %d spike(s) left open by a previous run", closed)
    spikes = SpikeMonitor(cfg, store)
    dashboard = _start_dashboard(cfg)
    # Written last: `whyslow start` treats the pid file as "fully up" (dashboard included).
    pid_file.write_text(f"{os.getpid()}\n")
    log.info("sampler started pid=%d interval=%.2fs cmdline_mode=%s system_processes=%s",
             os.getpid(), interval, cfg.privacy.cmdline, cfg.sampling.system_process_visibility)
    last_prune = 0.0
    overhead_ms = 0.0
    ticks = 0
    was_paused = False
    helper_reported = helper.available if helper is not None else None
    next_tick = time.monotonic()
    try:
        while not stop.is_set():
            try:
                # Pausing keeps the process (and its dashboard) alive but stops
                # collecting. Resuming looks like a data gap, which already resets
                # baselines and closes open spikes.
                paused = paths.pause_path().exists()
                if paused != was_paused:
                    log.info("sampling %s", "paused" if paused else "resumed")
                    was_paused = paused
                if paused:
                    raise _Paused
                sample = sampler.tick()
                store.record(sample)
                spikes.process(sample)
                overhead_ms += sample.overhead_cpu_ms
                ticks += 1
                if helper is not None and helper.available is not helper_reported:
                    helper_reported = helper.available
                    _write_helper_state(store, cfg, helper)
                if time.monotonic() - last_prune > _PRUNE_EVERY_S:
                    deleted = store.prune(cfg.storage.retention_days, cfg.storage.rollup_retention_days)
                    log.info("retention prune: %s", deleted)
                    last_prune = time.monotonic()
                if ticks % 600 == 0:
                    log.info("overhead: %.1f ms CPU/tick (%.2f%% of one core)",
                             overhead_ms / 600, overhead_ms / 600 / (interval * 10))
                    overhead_ms = 0.0
            except _Paused:
                pass
            except Exception:
                # Keep sampling; tracebacks don't include locals, so no cmdlines leak here.
                log.exception("tick failed")
            next_tick += interval
            delay = next_tick - time.monotonic()
            if delay < 0:  # fell behind (or woke from sleep): don't try to catch up
                next_tick = time.monotonic()
                delay = 0
            stop.wait(delay)
    finally:
        if helper is not None:
            helper.stop()
        try:
            spikes.close()
        except Exception:
            log.exception("closing open spikes failed")
        store.close()
        if dashboard is not None:
            dashboard.stop()
        paths.token_path().unlink(missing_ok=True)
        pid_file.unlink(missing_ok=True)
        log.info("sampler stopped")
        lock.close()
    return 0


def _write_helper_state(store: Store, cfg: Config, helper) -> None:
    """Record helper status in the database so the CLI and dashboard can show it."""
    state = {"enabled": cfg.helpers.powermetrics}
    if helper is not None:
        state.update(available=helper.available, error=helper.error,
                     needs_sudoers=helper.needs_sudoers, probes=helper.probes,
                     interval_seconds=helper.interval)
    try:
        storage.set_meta(store.conn, storage.HELPER_META_KEY, json.dumps(state))
    except Exception:
        log.exception("could not record helper state")


def _start_dashboard(cfg: Config):
    """Start the localhost dashboard thread; the sampler keeps running if this fails."""
    if not cfg.dashboard.enabled:
        return None
    from whyslow.web import auth
    from whyslow.web.app import DashboardServer, HOST

    token = auth.new_token()
    auth.write_token(paths.token_path(), token)
    try:
        server = DashboardServer(cfg, token)
        if server.start():
            log.info("dashboard listening on http://%s:%d/", HOST, cfg.dashboard.port)
            return server
    except Exception:
        log.exception("dashboard failed to start")
        return None
    log.error("dashboard could not start on port %d (in use?); sampling continues without it",
              cfg.dashboard.port)
    paths.token_path().unlink(missing_ok=True)
    return None


def dashboard_login_url(cfg: Config) -> str | None:
    """URL with a fresh single-use login code, if the running sampler serves a dashboard."""
    from whyslow.web import auth
    from whyslow.web.app import HOST

    token = auth.read_token(paths.token_path())
    if token is None or running_pid() is None:
        return None
    return f"http://{HOST}:{cfg.dashboard.port}/#login={auth.mint_code(token)}"


def is_paused() -> bool:
    return paths.pause_path().exists()


def set_paused(paused: bool) -> None:
    if paused:
        paths.ensure_private_dir(paths.home())
        paths.pause_path().touch()
    else:
        paths.pause_path().unlink(missing_ok=True)


def start_background(config_file: Path | None) -> int | None:
    """Spawn `python -m whyslow run` detached; return its PID once it's up."""
    args = [sys.executable, "-m", "whyslow"]
    if config_file:
        args += ["--config", str(config_file)]
    args += ["run", "--background"]
    proc = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None
        pid = running_pid()
        if pid == proc.pid:
            return pid
        time.sleep(0.1)
    return None


def stop_background(timeout: float = 10.0) -> bool:
    pid = running_pid()
    if pid is None:
        return False
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if running_pid() is None:
            return True
        time.sleep(0.1)
    raise TimeoutError(f"whyslow (pid {pid}) did not stop within {timeout:.0f}s")
