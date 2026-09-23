"""macOS platform layer: every OS-specific quirk lives here.

What an unprivileged user can see on macOS (measured, psutil 7.2 on macOS 26):

* Own processes: CPU time, RSS, context switches, command line - via psutil.
* Other users' processes (root, _windowserver, ...; typically ~40% of all
  processes, including WindowServer, mds_stores, backupd): psutil raises
  AccessDenied for CPU and memory. Apple's /bin/ps is setuid root and reports
  cumulative CPU time and RSS for them, so we ask it - one fork per tick, fixed
  absolute path, no shell. Their command lines stay hidden (and we don't try).
* kernel_task (PID 0): invisible even to ps. Its CPU shows up only as the gap
  between system CPU and the sum of visible processes ("unattributed").
* Per-process disk I/O: psutil has no io_counters() on macOS at all.
* Per-process network and energy: not exposed either. `powermetrics` can
  provide all three but needs sudo: an opt-in helper, off by default
  (see whyslow/helpers.py).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

import psutil

PS = "/bin/ps"
_PS_TIMEOUT_S = 2.0


def check_supported() -> None:
    if sys.platform != "darwin":
        raise SystemExit("whyslow only supports macOS.")


@dataclass(frozen=True)
class Capabilities:
    own_processes: bool
    other_users_processes: bool  # via /bin/ps
    kernel_task: bool
    per_process_disk: bool
    per_process_network: bool
    per_process_energy: bool
    battery: bool
    helper: dict = field(default_factory=dict)

    @property
    def mode(self) -> str:
        if self.per_process_disk or self.per_process_network or self.per_process_energy:
            return "powermetrics helper (sudo)"
        if self.helper.get("enabled"):
            return "standard (no sudo) — helper enabled but not working"
        return "standard (no sudo)"

    def notes(self) -> list[str]:
        out = []
        if not self.other_users_processes:
            out.append("system processes (root, WindowServer, ...) are hidden: system_process_visibility is off")
        out.append("kernel_task and sub-second processes are counted as 'unattributed' CPU")
        missing = [n for n, ok in (("disk I/O", self.per_process_disk),
                                   ("network", self.per_process_network),
                                   ("energy", self.per_process_energy)) if not ok]
        if missing:
            out.append(f"per-process {', '.join(missing)} unavailable without sudo helpers; system totals only")
        if self.helper.get("enabled") and self.helper.get("available") is not True:
            reason = self.helper.get("error") or "starting up"
            out.append(f"powermetrics helper is enabled but not running: {reason} "
                       "(see `whyslow helpers`)")
        elif self.helper.get("available") is True:
            out.append("per-process disk/network/energy come from 1-second powermetrics samples; "
                       "totals are extrapolated and approximate")
        if not self.battery:
            out.append("no battery detected")
        return out


def capabilities(system_process_visibility: bool, helper: dict | None = None) -> Capabilities:
    """Current capabilities. `helper` is the sudo helper's state (see storage.helper_state)."""
    helper = helper or {}
    elevated = bool(helper.get("enabled")) and helper.get("available") is True
    return Capabilities(
        own_processes=True,
        other_users_processes=system_process_visibility and os.access(PS, os.X_OK),
        kernel_task=False,
        per_process_disk=elevated,
        per_process_network=elevated,
        per_process_energy=elevated,
        battery=read_battery() is not None,
        helper=helper,
    )


# --- per-process -----------------------------------------------------------

_PS_TIME_RE = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$")


def parse_ps_time(text: str) -> float | None:
    """Parse ps(1) cumulative CPU time: "mm:ss.hh", "hh:mm:ss" or "d-hh:mm:ss"."""
    m = _PS_TIME_RE.match(text.strip())
    if not m:
        return None
    days, hours, minutes, seconds = m.groups()
    return int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


def ps_cpu_rss(pids: list[int]) -> dict[int, tuple[float, int]]:
    """CPU seconds and RSS bytes for other users' processes, via setuid /bin/ps.

    Missing PIDs (exited since enumeration) are simply absent from the result.
    """
    if not pids:
        return {}
    try:
        proc = subprocess.run(
            [PS, "-o", "pid=,time=,rss=", "-p", ",".join(map(str, pids))],
            capture_output=True, text=True, timeout=_PS_TIMEOUT_S, check=False,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out: dict[int, tuple[float, int]] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3 or not parts[0].isdigit() or not parts[2].isdigit():
            continue
        cpu = parse_ps_time(parts[1])
        if cpu is not None:
            out[int(parts[0])] = (cpu, int(parts[2]) * 1024)
    return out


def app_name(exe: str | None, name: str) -> str:
    """Group helpers under their application: the outermost *.app bundle in the path.

    "/Applications/Google Chrome.app/.../Google Chrome Helper (Renderer).app/..."
    -> "Google Chrome". Falls back to the process name for non-bundled binaries.
    """
    if exe:
        for part in exe.split("/"):
            if part.endswith(".app") and len(part) > 4:
                return part[:-4]
    return name or "?"


# --- system-wide -----------------------------------------------------------

@dataclass(frozen=True)
class Battery:
    percent: float
    plugged: bool
    secs_left: int | None


def read_battery() -> Battery | None:
    try:
        b = psutil.sensors_battery()
    except Exception:  # IOKit hiccups must never kill the sampler
        return None
    if b is None:
        return None
    secs = b.secsleft
    if secs in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) or secs is None or secs < 0:
        secs = None
    return Battery(float(b.percent), bool(b.power_plugged), secs)


def disk_bytes() -> tuple[int, int] | None:
    """System-wide (read, written) bytes across physical disks."""
    try:
        d = psutil.disk_io_counters()
    except Exception:
        return None
    return (d.read_bytes, d.write_bytes) if d else None


def net_bytes() -> tuple[int, int] | None:
    """System-wide (sent, received) bytes, excluding loopback (so our own dashboard doesn't count)."""
    try:
        nics = psutil.net_io_counters(pernic=True)
    except Exception:
        return None
    sent = recv = 0
    for name, c in nics.items():
        if name.startswith("lo"):
            continue
        sent += c.bytes_sent
        recv += c.bytes_recv
    return sent, recv
