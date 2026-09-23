"""SQLite storage: schema, writes, retention and wipe.

Rules: WAL mode, parameterized queries only (no SQL is ever built from data),
database file is 0600 inside a 0700 directory.

The disk/network/energy columns are filled only when the optional sudo-gated
powermetrics helper is enabled. (The frozen v1 schema text below credits
fs_usage/nettop for them; in the end one powermetrics probe supplies all three
and neither of those tools is used - see whyslow/helpers.py.)

Volume control: a row per process per second would be ~40M rows/day, so each
tick stores only the top-N processes by CPU (above a floor) plus a periodic
top-N-by-memory snapshot. Nothing is lost for the leaderboard because every
process's CPU is also accumulated into hourly per-app rollups in memory and
flushed once a minute.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from whyslow import paths
from whyslow.config import Config
from whyslow.sampler import ProcInfo, ProcSample, Sample

SCHEMA_VERSION = 3

# Version 1, as shipped in M1. Never edit: later changes go in MIGRATIONS.
SCHEMA_V1 = """
BEGIN;
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

-- Grouping key for the leaderboard: the outermost .app bundle, else the process name.
CREATE TABLE IF NOT EXISTS apps (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
) STRICT;

-- One row per process *instance*. PIDs are reused, so identity is (pid, create_time).
-- Rows are created lazily, only for processes that appear in a stored sample or spike.
CREATE TABLE IF NOT EXISTS processes (
    id          INTEGER PRIMARY KEY,
    pid         INTEGER NOT NULL,
    create_time REAL    NOT NULL,           -- epoch seconds
    app_id      INTEGER NOT NULL REFERENCES apps(id),
    name        TEXT    NOT NULL,
    exe         TEXT,
    cmdline     TEXT,                       -- REDACTED; NULL in name_only mode or if unreadable
    username    TEXT,
    visibility  TEXT    NOT NULL CHECK (visibility IN ('full', 'ps', 'hidden')),
    first_seen  REAL    NOT NULL,
    last_seen   REAL    NOT NULL,
    ended       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (pid, create_time)
) STRICT;
CREATE INDEX IF NOT EXISTS processes_app ON processes(app_id);
CREATE INDEX IF NOT EXISTS processes_last_seen ON processes(last_seen);

-- One row per tick. Rates are per second; NULL when not measurable.
CREATE TABLE IF NOT EXISTS system_samples (
    ts                REAL PRIMARY KEY,
    interval_s        REAL    NOT NULL,
    cpu_percent       REAL    NOT NULL,     -- 0-100 across all cores
    unattributed_cpu  REAL,                 -- core-% not explained by visible processes
    load1             REAL,
    mem_used          INTEGER,
    mem_available     INTEGER,
    mem_percent       REAL,
    swap_used         INTEGER,
    disk_read_bps     REAL,
    disk_write_bps    REAL,
    net_sent_bps      REAL,
    net_recv_bps      REAL,
    battery_percent   REAL,
    power_plugged     INTEGER,
    battery_secs_left INTEGER
) STRICT, WITHOUT ROWID;

-- Per-process detail for the processes that mattered in each tick.
CREATE TABLE IF NOT EXISTS process_samples (
    ts               REAL    NOT NULL,
    process_id       INTEGER NOT NULL REFERENCES processes(id) ON DELETE CASCADE,
    cpu_percent      REAL,                  -- 100 = one core
    cpu_seconds      REAL,
    rss_bytes        INTEGER,
    ctx_switches     INTEGER,               -- wakeup proxy; own processes only
    disk_read_bytes  INTEGER,               -- NULL unless the sudo fs_usage helper is on (M6)
    disk_write_bytes INTEGER,
    net_sent_bytes   INTEGER,               -- NULL unless the sudo nettop helper is on (M6)
    net_recv_bytes   INTEGER,
    energy_impact    REAL,                  -- NULL unless the sudo powermetrics helper is on (M6)
    PRIMARY KEY (ts, process_id)
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS process_samples_proc ON process_samples(process_id, ts);

-- Hourly per-app totals over *all* processes. Feeds the offenders leaderboard (M4).
CREATE TABLE IF NOT EXISTS app_usage_hourly (
    hour                   INTEGER NOT NULL, -- epoch seconds, floored to the hour
    app_id                 INTEGER NOT NULL REFERENCES apps(id),
    cpu_seconds            REAL    NOT NULL DEFAULT 0,
    cpu_seconds_on_battery REAL    NOT NULL DEFAULT 0,
    ctx_switches           INTEGER NOT NULL DEFAULT 0,
    peak_rss_bytes         INTEGER NOT NULL DEFAULT 0,  -- summed over the app's processes
    disk_read_bytes        INTEGER,          -- helper-only (M6)
    disk_write_bytes       INTEGER,
    net_sent_bytes         INTEGER,
    net_recv_bytes         INTEGER,
    energy_impact          REAL,
    PRIMARY KEY (hour, app_id)
) STRICT, WITHOUT ROWID;

-- Detected anomalies (M2).
CREATE TABLE IF NOT EXISTS spikes (
    id          INTEGER PRIMARY KEY,
    metric      TEXT    NOT NULL,           -- cpu | memory | disk_read | disk_write | net_sent | net_recv | battery_drain
    started_at  REAL    NOT NULL,
    ended_at    REAL,                       -- NULL while ongoing
    peak_value  REAL    NOT NULL,
    baseline    REAL    NOT NULL,           -- rolling median at trigger time
    spread      REAL    NOT NULL,           -- rolling scaled MAD at trigger time
    threshold_k REAL    NOT NULL,
    sustain_n   INTEGER NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS spikes_started ON spikes(started_at);

-- Which processes were responsible for a spike (M2).
CREATE TABLE IF NOT EXISTS spike_culprits (
    spike_id    INTEGER NOT NULL REFERENCES spikes(id) ON DELETE CASCADE,
    rank        INTEGER NOT NULL,
    process_id  INTEGER NOT NULL REFERENCES processes(id),
    value       REAL    NOT NULL,           -- in the spike metric's unit
    share       REAL,                       -- fraction of the system total, when meaningful
    attribution TEXT    NOT NULL CHECK (attribution IN ('direct', 'correlated')),
    PRIMARY KEY (spike_id, rank)
) STRICT, WITHOUT ROWID;
PRAGMA user_version = 1;
COMMIT;
"""

# (target version, script). Each script is one transaction and bumps user_version itself.
MIGRATIONS = [
    (2, """
BEGIN;
-- When the peak happened, and (cpu spikes) core-% no visible process accounted for.
ALTER TABLE spikes ADD COLUMN peak_at REAL;
ALTER TABLE spikes ADD COLUMN unattributed REAL;
-- spike_culprits.value units: cpu = core-%, memory = bytes grown,
-- correlated = core-% above the process's own recent norm.
PRAGMA user_version = 2;
COMMIT;
"""),
    (3, """
BEGIN;
-- Wakeups while unplugged, for the battery-drain estimate (M4).
ALTER TABLE app_usage_hourly ADD COLUMN ctx_switches_on_battery INTEGER NOT NULL DEFAULT 0;
PRAGMA user_version = 3;
COMMIT;
"""),
]

_FLUSH_EVERY_S = 60.0


@dataclass
class _Rollup:
    cpu: float = 0.0
    cpu_batt: float = 0.0
    ctx: int = 0
    ctx_batt: int = 0
    peak_rss: int = 0
    # Helper-only, extrapolated from sampled seconds - approximate by nature.
    disk_read: float = 0.0
    disk_write: float = 0.0
    net_sent: float = 0.0
    net_recv: float = 0.0
    energy: float = 0.0
    helper_seconds: float = 0.0


@dataclass
class _Pending:
    rollups: dict[tuple[int, str], _Rollup] = field(default_factory=dict)
    last_seen: dict[int, float] = field(default_factory=dict)  # process db id -> ts


def connect(path: Path) -> sqlite3.Connection:
    paths.ensure_private_dir(path.parent)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    for suffix in ("", "-wal", "-shm"):  # umask 077 already covers new files; this fixes old ones
        paths.make_private(Path(str(path) + suffix))
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"database schema v{version} is newer than this whyslow (v{SCHEMA_VERSION})")
    if version == 0:
        conn.executescript(SCHEMA_V1)
        version = 1
    for target, script in MIGRATIONS:
        if version < target:
            conn.executescript(script)
            version = target


class Store:
    def __init__(self, path: Path, cfg: Config) -> None:
        self.conn = connect(path)
        migrate(self.conn)
        s = cfg.sampling
        self._top_n = s.process_top_n
        self._min_cpu = s.min_process_cpu_percent
        self._mem_every = s.memory_snapshot_every
        self._app_ids: dict[str, int] = {}
        self._pending = _Pending()
        self._ticks = 0
        self._last_flush = time.monotonic()

    # --- ids ---

    def _app_id(self, name: str) -> int:
        app_id = self._app_ids.get(name)
        if app_id is None:
            self.conn.execute("INSERT INTO apps(name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
            (app_id,) = self.conn.execute("SELECT id FROM apps WHERE name = ?", (name,)).fetchone()
            self._app_ids[name] = app_id
        return app_id

    def process_id(self, info: ProcInfo, ts: float) -> int:
        if info.db_id is None:
            self.conn.execute(
                """INSERT INTO processes
                   (pid, create_time, app_id, name, exe, cmdline, username, visibility, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(pid, create_time) DO UPDATE SET last_seen = excluded.last_seen, ended = 0""",
                (info.pid, info.create_time, self._app_id(info.app), info.name, info.exe,
                 info.cmdline, info.username, info.visibility, ts, ts),
            )
            (info.db_id,) = self.conn.execute(
                "SELECT id FROM processes WHERE pid = ? AND create_time = ?", (info.pid, info.create_time)
            ).fetchone()
        return info.db_id

    # --- writes ---

    def _select(self, procs: list[ProcSample]) -> list[ProcSample]:
        busy = sorted(
            (p for p in procs if p.cpu_percent is not None and p.cpu_percent >= self._min_cpu),
            key=lambda p: p.cpu_percent, reverse=True,
        )[: self._top_n]
        chosen = {id(p): p for p in busy}
        if self._ticks % self._mem_every == 0:
            for p in sorted((p for p in procs if p.rss), key=lambda p: p.rss, reverse=True)[: self._top_n]:
                chosen.setdefault(id(p), p)
        # A process can move a lot of disk or network with almost no CPU, so when
        # helper data is present rank by that too.
        def traffic(p: ProcSample) -> float:
            return sum(v or 0 for v in (p.disk_read_bytes, p.disk_write_bytes,
                                        p.net_recv_bytes, p.net_sent_bytes))
        busiest_io = sorted((p for p in procs if traffic(p) > 0), key=traffic, reverse=True)
        for p in busiest_io[: self._top_n]:
            chosen.setdefault(id(p), p)
        return list(chosen.values())

    def record(self, sample: Sample) -> None:
        """Persist one complete tick in a single transaction."""
        if not sample.complete:
            return
        sys_ = sample.system
        ts = sys_.ts
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO system_samples VALUES
                   (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, sys_.interval, sys_.cpu_percent, sample.unattributed_cpu_percent, sys_.load1,
                 sys_.mem_used, sys_.mem_available, sys_.mem_percent, sys_.swap_used,
                 sys_.disk_read_bps, sys_.disk_write_bps, sys_.net_sent_bps, sys_.net_recv_bps,
                 sys_.battery_percent,
                 None if sys_.power_plugged is None else int(sys_.power_plugged),
                 sys_.battery_secs_left),
            )
            rows = [
                (ts, self.process_id(p.info, ts), p.cpu_percent, p.cpu_seconds, p.rss, p.ctx_switches,
                 p.disk_read_bytes, p.disk_write_bytes, p.net_sent_bytes, p.net_recv_bytes, p.energy_impact)
                for p in self._select(sample.processes)
            ]
            self.conn.executemany(
                """INSERT OR REPLACE INTO process_samples
                   (ts, process_id, cpu_percent, cpu_seconds, rss_bytes, ctx_switches,
                    disk_read_bytes, disk_write_bytes, net_sent_bytes, net_recv_bytes, energy_impact)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        self._accumulate(sample)
        self._ticks += 1
        if time.monotonic() - self._last_flush >= _FLUSH_EVERY_S:
            self.flush()

    def _accumulate(self, sample: Sample) -> None:
        hour = int(sample.system.ts // 3600 * 3600)
        on_battery = sample.system.power_plugged is False
        app_rss: dict[str, int] = {}
        for p in sample.processes:
            r = self._pending.rollups.setdefault((hour, p.info.app), _Rollup())
            if p.cpu_seconds:
                r.cpu += p.cpu_seconds
                if on_battery:
                    r.cpu_batt += p.cpu_seconds
            if p.ctx_switches:
                r.ctx += p.ctx_switches
                if on_battery:
                    r.ctx_batt += p.ctx_switches
            if p.rss:
                app_rss[p.info.app] = app_rss.get(p.info.app, 0) + p.rss
            # Helper rates cover ~1 s; scale to the period this snapshot stands for.
            span = sample.helper_span_seconds
            if span:
                r.disk_read += (p.disk_read_bytes or 0) * span
                r.disk_write += (p.disk_write_bytes or 0) * span
                r.net_recv += (p.net_recv_bytes or 0) * span
                r.net_sent += (p.net_sent_bytes or 0) * span
                r.energy += (p.energy_impact or 0) * span
                r.helper_seconds += span
            if p.info.db_id is not None:
                self._pending.last_seen[p.info.db_id] = sample.system.ts
        for app, rss in app_rss.items():
            r = self._pending.rollups[(hour, app)]
            r.peak_rss = max(r.peak_rss, rss)
        ended = [
            (self._pending.last_seen.pop(info.db_id, 0.0), info.db_id)
            for info in sample.ended if info.db_id is not None
        ]
        if ended:
            with self.conn:
                self.conn.executemany(
                    "UPDATE processes SET ended = 1, last_seen = MAX(last_seen, ?) WHERE id = ?", ended
                )

    def flush(self) -> None:
        pending, self._pending = self._pending, _Pending()
        self._last_flush = time.monotonic()
        rollups = [
            (hour, self._app_id(app), r.cpu, r.cpu_batt, r.ctx, r.ctx_batt, r.peak_rss,
             round(r.disk_read) or None, round(r.disk_write) or None,
             round(r.net_sent) or None, round(r.net_recv) or None, r.energy or None)
            for (hour, app), r in pending.rollups.items()
            if r.cpu or r.ctx or r.peak_rss
        ]
        with self.conn:
            self.conn.executemany(
                """INSERT INTO app_usage_hourly
                   (hour, app_id, cpu_seconds, cpu_seconds_on_battery, ctx_switches,
                    ctx_switches_on_battery, peak_rss_bytes,
                    disk_read_bytes, disk_write_bytes, net_sent_bytes, net_recv_bytes, energy_impact)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(hour, app_id) DO UPDATE SET
                       cpu_seconds = cpu_seconds + excluded.cpu_seconds,
                       cpu_seconds_on_battery = cpu_seconds_on_battery + excluded.cpu_seconds_on_battery,
                       ctx_switches = ctx_switches + excluded.ctx_switches,
                       ctx_switches_on_battery = ctx_switches_on_battery + excluded.ctx_switches_on_battery,
                       peak_rss_bytes = MAX(peak_rss_bytes, excluded.peak_rss_bytes),
                       disk_read_bytes = COALESCE(disk_read_bytes, 0) + COALESCE(excluded.disk_read_bytes, 0),
                       disk_write_bytes = COALESCE(disk_write_bytes, 0) + COALESCE(excluded.disk_write_bytes, 0),
                       net_sent_bytes = COALESCE(net_sent_bytes, 0) + COALESCE(excluded.net_sent_bytes, 0),
                       net_recv_bytes = COALESCE(net_recv_bytes, 0) + COALESCE(excluded.net_recv_bytes, 0),
                       energy_impact = COALESCE(energy_impact, 0) + COALESCE(excluded.energy_impact, 0)""",
                rollups,
            )
            self.conn.executemany(
                "UPDATE processes SET last_seen = MAX(last_seen, ?) WHERE id = ?",
                [(ts, pid) for pid, ts in pending.last_seen.items()],
            )

    # --- spikes ---

    def insert_spike(self, metric: str, started_at: float, peak: float, peak_at: float, baseline: float,
                     spread: float, k: float, sustain: int, unattributed: float | None) -> int:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO spikes (metric, started_at, peak_value, peak_at, baseline, spread,
                                       threshold_k, sustain_n, unattributed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (metric, started_at, peak, peak_at, baseline, spread, k, sustain, unattributed),
            )
        return cur.lastrowid

    def update_spike(self, spike_id: int, peak: float, peak_at: float, unattributed: float | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE spikes SET peak_value = ?, peak_at = ?, unattributed = COALESCE(?, unattributed) WHERE id = ?",
                (peak, peak_at, unattributed, spike_id),
            )

    def end_spike(self, spike_id: int, ended_at: float, peak: float, peak_at: float) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE spikes SET ended_at = ?, peak_value = MAX(peak_value, ?), "
                "peak_at = CASE WHEN ? > peak_value THEN ? ELSE peak_at END WHERE id = ?",
                (ended_at, peak, peak, peak_at, spike_id),
            )

    def replace_culprits(self, spike_id: int, culprits: list, ts: float) -> None:
        """Store the culprit list (whyslow.correlator.Culprit) for a spike, replacing any earlier one."""
        with self.conn:
            rows = [
                (spike_id, rank, self.process_id(c.info, ts), c.value, c.share, c.attribution)
                for rank, c in enumerate(culprits, start=1)
            ]
            self.conn.execute("DELETE FROM spike_culprits WHERE spike_id = ?", (spike_id,))
            self.conn.executemany(
                """INSERT INTO spike_culprits (spike_id, rank, process_id, value, share, attribution)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )

    def close_dangling_spikes(self) -> int:
        """Spikes left open by a crash or kill -9: end them at their last known moment."""
        with self.conn:
            return self.conn.execute(
                "UPDATE spikes SET ended_at = COALESCE(peak_at, started_at) WHERE ended_at IS NULL"
            ).rowcount

    # --- retention ---

    def prune(self, retention_days: float, rollup_retention_days: float, now: float | None = None) -> dict[str, int]:
        now = time.time() if now is None else now
        raw_cutoff = now - retention_days * 86400
        long_cutoff = now - rollup_retention_days * 86400
        deleted = {}
        with self.conn:
            for table, sql, cutoff in (
                ("system_samples", "DELETE FROM system_samples WHERE ts < ?", raw_cutoff),
                ("process_samples", "DELETE FROM process_samples WHERE ts < ?", raw_cutoff),
                ("spikes", "DELETE FROM spikes WHERE started_at < ?", long_cutoff),
                ("app_usage_hourly", "DELETE FROM app_usage_hourly WHERE hour < ?", long_cutoff),
            ):
                deleted[table] = self.conn.execute(sql, (cutoff,)).rowcount
            cur = self.conn.execute(
                """DELETE FROM processes
                   WHERE last_seen < ?
                     AND NOT EXISTS (SELECT 1 FROM process_samples s WHERE s.process_id = processes.id)
                     AND NOT EXISTS (SELECT 1 FROM spike_culprits c WHERE c.process_id = processes.id)""",
                (raw_cutoff,),
            )
            deleted["processes"] = cur.rowcount
            cur = self.conn.execute(
                """DELETE FROM apps
                   WHERE NOT EXISTS (SELECT 1 FROM processes p WHERE p.app_id = apps.id)
                     AND NOT EXISTS (SELECT 1 FROM app_usage_hourly u WHERE u.app_id = apps.id)"""
            )
            deleted["apps"] = cur.rowcount
        self._app_ids.clear()
        return deleted

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self.conn.close()


def _ro_uri(path: Path) -> str:
    return "file:" + quote(str(path.resolve())) + "?mode=ro"


def open_readonly(path: Path) -> sqlite3.Connection | None:
    """Read-only connection for reporting (CLI, dashboard). None if there's no data yet."""
    if not path.exists():
        return None
    conn = sqlite3.connect(_ro_uri(path), uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    if version != SCHEMA_VERSION:  # old DB not yet migrated by the sampler, or a newer one
        conn.close()
        raise RuntimeError(f"database is schema v{version}, expected v{SCHEMA_VERSION}; "
                           "run `whyslow start` once to migrate it")
    return conn


def recent_spikes(conn: sqlite3.Connection, since: float, limit: int) -> list[dict]:
    spikes = [dict(r) for r in conn.execute(
        """SELECT id, metric, started_at, ended_at, peak_value, peak_at, baseline, spread,
                  threshold_k, sustain_n, unattributed
           FROM spikes WHERE ended_at IS NULL OR ended_at >= ?  -- overlapping the period, not just started in it
           ORDER BY started_at DESC LIMIT ?""",
        (since, limit),
    )]
    for s in spikes:
        s["culprits"] = spike_culprits(conn, s["id"])
    return spikes


def spike_culprits(conn: sqlite3.Connection, spike_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT c.rank, c.value, c.share, c.attribution,
                  p.pid, p.name, p.cmdline, p.visibility, a.name AS app
           FROM spike_culprits c
           JOIN processes p ON p.id = c.process_id
           JOIN apps a ON a.id = p.app_id
           WHERE c.spike_id = ? ORDER BY c.rank""",
        (spike_id,),
    )]


SERIES_COLUMNS = ("ts", "cpu", "memory", "disk_read", "disk_write", "net_recv", "net_sent",
                  "battery", "plugged", "unattributed")


def system_series(conn: sqlite3.Connection, since: float, until: float, bucket: float) -> dict[str, list]:
    """System metrics between since and until, column-oriented for charting.

    With bucket > 0 each point summarises `bucket` seconds: the PEAK for CPU,
    disk and network (so spikes survive zooming out) and the MEAN for memory and
    battery. power_plugged is 0 if any sample in the bucket was on battery.
    """
    if bucket <= 0:
        rows = conn.execute(
            """SELECT ts, cpu_percent, mem_percent, disk_read_bps, disk_write_bps, net_recv_bps,
                      net_sent_bps, battery_percent, power_plugged, unattributed_cpu
               FROM system_samples WHERE ts >= ? AND ts <= ? ORDER BY ts""",
            (since, until),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT CAST(ts / ? AS INTEGER) * ? AS b, MAX(cpu_percent), AVG(mem_percent),
                      MAX(disk_read_bps), MAX(disk_write_bps), MAX(net_recv_bps), MAX(net_sent_bps),
                      AVG(battery_percent), MIN(power_plugged), MAX(unattributed_cpu)
               FROM system_samples WHERE ts >= ? AND ts <= ? GROUP BY b ORDER BY b""",
            (bucket, bucket, since, until),
        ).fetchall()
    return {name: [row[i] for row in rows] for i, name in enumerate(SERIES_COLUMNS)}


def latest(conn: sqlite3.Connection) -> dict | None:
    """The most recent system sample plus the processes stored for that moment."""
    row = conn.execute("SELECT * FROM system_samples ORDER BY ts DESC LIMIT 1").fetchone()
    if row is None:
        return None
    procs = [dict(r) for r in conn.execute(
        """SELECT ps.cpu_percent, ps.rss_bytes, p.pid, p.name, p.cmdline, p.visibility, a.name AS app
           FROM process_samples ps
           JOIN processes p ON p.id = ps.process_id
           JOIN apps a ON a.id = p.app_id
           WHERE ps.ts = (SELECT MAX(ts) FROM process_samples WHERE ts >= ?)
           ORDER BY ps.cpu_percent DESC""",
        (row["ts"] - 5,),
    )]
    return {"system": dict(row), "processes": procs}


HELPER_META_KEY = "helpers"


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def helper_state(path: Path) -> dict:
    """What the running sampler last reported about the sudo helper."""
    try:
        conn = open_readonly(path)
    except (RuntimeError, sqlite3.Error):
        return {}
    if conn is None:
        return {}
    try:
        raw = get_meta(conn, HELPER_META_KEY)
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def leaderboard(conn: sqlite3.Connection, since: float, limit: int) -> list[dict]:
    """Cumulative per-app usage since `since`, from the hourly rollups.

    Rollups cover *every* process (not just the ones stored per tick), so these
    totals are complete. Disk/network columns stay NULL until the sudo helpers
    helper is enabled.
    """
    rows = [dict(r) for r in conn.execute(
        """SELECT a.name AS app,
                  SUM(u.cpu_seconds) AS cpu_seconds,
                  SUM(u.cpu_seconds_on_battery) AS cpu_seconds_on_battery,
                  SUM(u.ctx_switches) AS ctx_switches,
                  SUM(u.ctx_switches_on_battery) AS ctx_switches_on_battery,
                  MAX(u.peak_rss_bytes) AS peak_rss_bytes,
                  SUM(u.disk_read_bytes) AS disk_read_bytes,
                  SUM(u.disk_write_bytes) AS disk_write_bytes,
                  SUM(u.net_sent_bytes) AS net_sent_bytes,
                  SUM(u.net_recv_bytes) AS net_recv_bytes
           FROM app_usage_hourly u JOIN apps a ON a.id = u.app_id
           WHERE u.hour >= ?
           GROUP BY a.name ORDER BY cpu_seconds DESC LIMIT ?""",
        (since, limit),
    )]
    blamed = {r["app"]: (r["caused"], r["suspected"]) for r in conn.execute(
        """SELECT a.name AS app,
                  COUNT(DISTINCT CASE WHEN c.attribution = 'direct' THEN c.spike_id END) AS caused,
                  COUNT(DISTINCT CASE WHEN c.attribution = 'correlated' THEN c.spike_id END) AS suspected
           FROM spike_culprits c
           JOIN spikes s ON s.id = c.spike_id
           JOIN processes p ON p.id = c.process_id
           JOIN apps a ON a.id = p.app_id
           WHERE s.started_at >= ? GROUP BY a.name""",
        (since,),
    )}
    for row in rows:
        row["spikes_caused"], row["spikes_suspected"] = blamed.get(row["app"], (0, 0))
    return rows


def battery_samples(conn: sqlite3.Connection, since: float) -> list[tuple]:
    """(ts, battery_percent, cpu_percent, power_plugged) while a battery reading exists."""
    return conn.execute(
        """SELECT ts, battery_percent, cpu_percent, power_plugged
           FROM system_samples WHERE ts >= ? AND battery_percent IS NOT NULL ORDER BY ts""",
        (since,),
    ).fetchall()


def stats(path: Path) -> dict[str, object] | None:
    """Read-only summary for `whyslow status`."""
    if not path.exists():
        return None
    conn = sqlite3.connect(_ro_uri(path), uri=True, timeout=5.0)
    try:
        out: dict[str, object] = {}
        for table, sql in (
            ("system_samples", "SELECT COUNT(*) FROM system_samples"),
            ("process_samples", "SELECT COUNT(*) FROM process_samples"),
            ("processes", "SELECT COUNT(*) FROM processes"),
            ("apps", "SELECT COUNT(*) FROM apps"),
            ("app_usage_hourly", "SELECT COUNT(*) FROM app_usage_hourly"),
            ("spikes", "SELECT COUNT(*) FROM spikes"),
        ):
            (out[table],) = conn.execute(sql).fetchone()
        out["oldest"], out["newest"] = conn.execute("SELECT MIN(ts), MAX(ts) FROM system_samples").fetchone()
        return out
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def wipe_files() -> list[Path]:
    """Delete the database (and WAL/SHM) and logs. The config file is kept."""
    removed = []
    db = paths.db_path()
    for p in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        if p.exists():
            p.unlink()
            removed.append(p)
    log_dir = paths.log_dir()
    if log_dir.exists():
        for p in log_dir.glob("whyslow.log*"):
            p.unlink()
            removed.append(p)
    return removed
