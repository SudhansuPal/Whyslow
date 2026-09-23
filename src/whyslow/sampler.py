"""Sampler: one tick = one consistent snapshot of system + per-process metrics.

Per-process CPU is computed from deltas of cumulative CPU time between ticks
(not psutil's cpu_percent), so it works identically for processes we read via
psutil and those we read via /bin/ps. 100% = one fully busy core.

Process identity is (pid, create_time) because PIDs get reused. We enumerate
with psutil.process_iter() every tick; psutil drops exited PIDs from its cache
on each call, so a reused PID always gets a fresh Process object (and a fresh
create_time) unless the PID space wraps within a single tick.
"""

from __future__ import annotations

import os
import resource
import time
from dataclasses import dataclass, field

import psutil

from whyslow import macos
from whyslow.config import Config
from whyslow.redact import redact_cmdline

FULL = "full"      # readable via psutil (our own processes)
PS = "ps"          # other users' processes; CPU/RSS via /bin/ps, no cmdline
HIDDEN = "hidden"  # other users' processes with system_process_visibility off


@dataclass(slots=True, eq=False)
class ProcInfo:
    """Identity and static metadata of one process, cached for its lifetime."""
    pid: int
    create_time: float
    name: str
    exe: str | None
    app: str
    cmdline: str | None  # already redacted; None in name_only mode or when unreadable
    username: str | None
    visibility: str
    last_cpu: float | None = None
    last_ctx: int | None = None
    db_id: int | None = None  # set lazily by storage when first persisted


@dataclass(slots=True)
class ProcSample:
    info: ProcInfo
    cpu_seconds: float | None  # CPU consumed during this tick
    cpu_percent: float | None  # 100 = one full core
    rss: int | None
    ctx_switches: int | None   # context switches during this tick (own processes only)
    # Below: only when the sudo-gated powermetrics helper is on. Each value
    # covers the helper's ~1 s sample window, so they read as per-second rates.
    disk_read_bytes: float | None = None
    disk_write_bytes: float | None = None
    net_recv_bytes: float | None = None
    net_sent_bytes: float | None = None
    energy_impact: float | None = None


@dataclass(slots=True)
class SystemSample:
    ts: float
    interval: float | None
    cpu_percent: float          # 0-100 across all cores
    cpu_count: int
    load1: float
    mem_total: int
    mem_used: int
    mem_available: int
    mem_percent: float
    swap_used: int
    disk_read_bps: float | None
    disk_write_bps: float | None
    net_sent_bps: float | None
    net_recv_bps: float | None
    battery_percent: float | None
    power_plugged: bool | None
    battery_secs_left: int | None


@dataclass(slots=True)
class Sample:
    system: SystemSample
    processes: list[ProcSample]
    ended: list[ProcInfo]
    # Core-% of system CPU not explained by any visible process
    # (kernel_task, processes that lived < 1 tick, hidden processes).
    unattributed_cpu_percent: float | None
    # False on the first tick and after a gap (sleep, SIGSTOP): rates/deltas are None.
    complete: bool
    # Set when this tick carries fresh helper data: the period that data stands
    # in for, used to extrapolate sampled rates into hourly totals.
    helper_span_seconds: float | None = None
    overhead_cpu_ms: float = 0.0  # our own CPU cost for this tick, including /bin/ps
    counts: dict[str, int] = field(default_factory=dict)


def _own_cpu_seconds() -> float:
    s = resource.getrusage(resource.RUSAGE_SELF)
    c = resource.getrusage(resource.RUSAGE_CHILDREN)
    return s.ru_utime + s.ru_stime + c.ru_utime + c.ru_stime


def _rate(cur: tuple[int, int] | None, prev: tuple[int, int] | None, dt: float) -> tuple[float | None, float | None]:
    if cur is None or prev is None or dt <= 0:
        return None, None
    # Counters can reset (interface down, disk detached): clamp instead of going negative.
    return max(0, cur[0] - prev[0]) / dt, max(0, cur[1] - prev[1]) / dt


class Sampler:
    def __init__(self, cfg: Config, helper=None) -> None:  # helper: whyslow.helpers.PowerMetrics
        self._helper = helper
        self._interval = cfg.sampling.interval_seconds
        self._use_ps = cfg.sampling.system_process_visibility
        self._cmd_mode = cfg.privacy.cmdline
        self._cpu_count = psutil.cpu_count() or 1
        self._procs: dict[tuple[int, float], ProcInfo] = {}
        self._last_mono: float | None = None
        self._last_wall: float | None = None
        self._last_disk: tuple[int, int] | None = None
        self._last_net: tuple[int, int] | None = None
        psutil.cpu_percent(None)  # prime: the first call always returns 0.0

    def _describe(self, p: psutil.Process, create_time: float) -> ProcInfo | None:
        try:
            with p.oneshot():
                name = p.name()
                try:
                    exe = p.exe() or None
                except psutil.AccessDenied:
                    exe = None
                try:
                    username = p.username()
                except (psutil.AccessDenied, KeyError):
                    username = None
                try:
                    cmdline = redact_cmdline(p.cmdline(), self._cmd_mode)
                except psutil.AccessDenied:
                    cmdline = None
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return None
        except psutil.AccessDenied:
            name, exe, username, cmdline = f"pid {p.pid}", None, None, None
        return ProcInfo(
            pid=p.pid, create_time=create_time, name=name, exe=exe,
            app=macos.app_name(exe, name), cmdline=cmdline, username=username,
            visibility=FULL,  # optimistic; downgraded on the first AccessDenied
        )

    def tick(self) -> Sample:
        cost_start = _own_cpu_seconds()
        wall = time.time()
        mono = time.monotonic()
        dt = mono - self._last_mono if self._last_mono is not None else 0.0
        # time.monotonic() stops during system sleep on macOS; wall time doesn't.
        # A big wall-clock jump means a gap: reset baselines instead of reporting bogus rates.
        complete = (
            self._last_wall is not None and dt > 0
            and (wall - self._last_wall) < 3 * self._interval + 5
        )

        # --- processes ---
        seen: dict[tuple[int, float], ProcInfo] = {}
        readings: dict[tuple[int, float], tuple[float, int, int | None]] = {}
        need_ps: list[tuple[tuple[int, float], ProcInfo]] = []
        for p in psutil.process_iter():
            try:
                key = (p.pid, p.create_time())
            except psutil.Error:
                continue
            info = self._procs.get(key) or self._describe(p, key[1])
            if info is None:
                continue
            seen[key] = info
            if info.visibility == FULL:
                try:
                    with p.oneshot():  # one proc_pidinfo call for all three
                        ct = p.cpu_times()
                        rss = p.memory_info().rss
                        ctx = p.num_ctx_switches()
                    readings[key] = (ct.user + ct.system, rss, ctx.voluntary + ctx.involuntary)
                    continue
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    seen.pop(key)
                    continue
                except psutil.AccessDenied:
                    info.visibility = PS if self._use_ps else HIDDEN
            if info.visibility == PS:
                need_ps.append((key, info))
        if need_ps:
            ps_data = macos.ps_cpu_rss([info.pid for _, info in need_ps])
            for key, info in need_ps:
                if info.pid in ps_data:
                    cpu, rss = ps_data[info.pid]
                    readings[key] = (cpu, rss, None)

        procs: list[ProcSample] = []
        visible_pct = 0.0
        for key, (cpu, rss, ctx) in readings.items():
            info = seen[key]
            born_this_tick = self._last_wall is not None and info.create_time >= self._last_wall
            if info.last_cpu is not None:
                d_cpu = cpu - info.last_cpu if cpu >= info.last_cpu else None
            else:
                d_cpu = cpu if born_this_tick else None  # all of its CPU happened this tick
            if ctx is not None and info.last_ctx is not None:
                d_ctx = ctx - info.last_ctx if ctx >= info.last_ctx else None
            else:
                d_ctx = ctx if (ctx is not None and born_this_tick) else None
            info.last_cpu, info.last_ctx = cpu, ctx
            if not complete:
                d_cpu = d_ctx = None
            pct = d_cpu / dt * 100 if d_cpu is not None else None
            if pct is not None:
                visible_pct += pct
            procs.append(ProcSample(info, d_cpu, pct, rss, d_ctx))

        # Per-process disk/network/energy, if the sudo helper is enabled and working.
        # Only on a complete tick: an incomplete one (warm-up, or just after sleep)
        # is discarded, and taking a snapshot there would waste it.
        helper_span = None
        snapshot = self._helper.take() if (self._helper is not None and complete) else None
        if snapshot is not None:
            helper_span = snapshot.span_seconds
            for p in procs:
                metrics = snapshot.by_pid.get(p.info.pid)
                if metrics is not None:
                    p.disk_read_bytes = metrics.disk_read_bytes
                    p.disk_write_bytes = metrics.disk_write_bytes
                    p.net_recv_bytes = metrics.net_recv_bytes
                    p.net_sent_bytes = metrics.net_sent_bytes
                    p.energy_impact = metrics.energy_impact

        ended = [info for key, info in self._procs.items() if key not in seen]
        self._procs = seen

        # --- system ---
        cpu_pct = psutil.cpu_percent(None)
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = macos.disk_bytes()
        net = macos.net_bytes()
        batt = macos.read_battery()
        disk_r, disk_w = _rate(disk, self._last_disk, dt) if complete else (None, None)
        net_s, net_r = _rate(net, self._last_net, dt) if complete else (None, None)
        self._last_disk, self._last_net = disk, net
        self._last_mono, self._last_wall = mono, wall

        system = SystemSample(
            ts=wall, interval=dt if complete else None,
            cpu_percent=cpu_pct, cpu_count=self._cpu_count, load1=os.getloadavg()[0],
            mem_total=vm.total, mem_used=vm.used, mem_available=vm.available,
            mem_percent=vm.percent, swap_used=swap.used,
            disk_read_bps=disk_r, disk_write_bps=disk_w,
            net_sent_bps=net_s, net_recv_bps=net_r,
            battery_percent=batt.percent if batt else None,
            power_plugged=batt.plugged if batt else None,
            battery_secs_left=batt.secs_left if batt else None,
        )
        unattributed = max(0.0, cpu_pct * self._cpu_count - visible_pct) if complete else None
        counts = {FULL: 0, PS: 0, HIDDEN: 0}
        for info in seen.values():
            counts[info.visibility] += 1
        return Sample(
            system=system, processes=procs, ended=ended,
            unattributed_cpu_percent=unattributed, complete=complete,
            helper_span_seconds=helper_span,
            overhead_cpu_ms=(_own_cpu_seconds() - cost_start) * 1000, counts=counts,
        )
