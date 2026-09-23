"""Optional, sudo-gated per-process metrics. Off by default.

macOS hides per-process disk I/O, network and energy from unprivileged code.
Exactly one tool can supply all three: `powermetrics`, which is root-only.

**What whyslow does and doesn't do**

* One fixed command, built from constants - no shell, no user input, absolute
  paths, a timeout, and a scrubbed environment.
* `sudo -n`: never prompts, never touches your password. If no sudoers rule
  allows the command, the probe fails, the helper switches itself off for this
  run and says exactly what to add. (Retrying would spam the system's auth log.)
* **Short-lived probes, never a root daemon.** Each probe collects ONE
  1-second sample and exits; whyslow runs one every `interval_seconds`
  (default 30) on a background thread, so sampling is never blocked.
* `fs_usage` is deliberately *not* used, even though the brief allowed it: it
  needs a continuous root process streaming every filesystem syscall on the
  machine, including file paths - a privacy firehose and real overhead, for
  data `powermetrics --show-process-io` already gives in aggregate.
* `nettop` is not needed either: `--show-process-netstats` covers it.

**What the numbers mean.** A probe measures one second. Rates (bytes/s, energy
impact) are real for that second; long-run totals are extrapolated from them
(one second in every thirty), so the leaderboard marks them approximate.
"""

from __future__ import annotations

import logging
import plistlib
import subprocess
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("whyslow")

SUDO = "/usr/bin/sudo"
POWERMETRICS = "/usr/bin/powermetrics"
SAMPLE_WINDOW_MS = 1000
PROBE_TIMEOUT_S = 20.0

# Fixed argument list. The sudoers rule matches it exactly, so nothing here may
# depend on config or user input - adding or reordering an argument means the
# rule no longer matches (and the documented rule must be updated in lockstep).
POWERMETRICS_ARGS = (
    "--samplers", "tasks",
    "--show-process-energy",
    "--show-process-io",
    "--show-process-netstats",
    "--sample-count", "1",
    "--sample-rate", str(SAMPLE_WINDOW_MS),
    "--format", "plist",
)

PROBE_COMMAND = (SUDO, "-n", POWERMETRICS, *POWERMETRICS_ARGS)
_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}

# powermetrics key names vary a little by macOS release; accept any of these.
_KEYS = {
    "energy_impact": ("energy_impact", "energy", "task_energy_impact"),
    "disk_read_bytes": ("diskio_bytesread", "bytes_read", "diskio_bytes_read"),
    "disk_write_bytes": ("diskio_byteswritten", "bytes_written", "diskio_bytes_written"),
    "net_recv_bytes": ("bytes_received", "bytes_in", "net_bytes_received"),
    "net_sent_bytes": ("bytes_sent", "bytes_out", "net_bytes_sent"),
}


@dataclass(frozen=True)
class ProcessMetrics:
    pid: int
    energy_impact: float | None = None
    disk_read_bytes: float | None = None
    disk_write_bytes: float | None = None
    net_recv_bytes: float | None = None
    net_sent_bytes: float | None = None

    def is_empty(self) -> bool:
        return all(getattr(self, f) is None for f in
                   ("energy_impact", "disk_read_bytes", "disk_write_bytes",
                    "net_recv_bytes", "net_sent_bytes"))


@dataclass(frozen=True)
class Snapshot:
    ts: float
    window_seconds: float          # what the numbers below cover (~1 s)
    span_seconds: float            # what this snapshot stands in for (the probe interval)
    by_pid: dict[int, ProcessMetrics] = field(default_factory=dict)
    task_keys: tuple[str, ...] = ()  # keys seen in the output, for `whyslow helpers --check`


class HelperError(Exception):
    def __init__(self, message: str, needs_sudoers: bool = False) -> None:
        super().__init__(message)
        self.needs_sudoers = needs_sudoers


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _pick(task: dict, field_name: str) -> float | None:
    for key in _KEYS[field_name]:
        if key in task:
            value = _number(task[key])
            if value is not None:
                return value
    return None


def parse_plist(raw: bytes, ts: float, span_seconds: float) -> Snapshot:
    """Parse one powermetrics plist sample into per-process metrics."""
    start = min((i for i in (raw.find(b"<?xml"), raw.find(b"bplist00")) if i >= 0), default=-1)
    if start < 0:
        raise HelperError("powermetrics produced no plist output")
    end = raw.rfind(b"</plist>")
    payload = raw[start:end + len(b"</plist>")] if end > 0 else raw[start:]
    try:
        data = plistlib.loads(payload)
    except Exception as exc:
        raise HelperError(f"could not parse powermetrics output: {exc}") from None
    if not isinstance(data, dict):
        raise HelperError("unexpected powermetrics output")

    window = _number(data.get("elapsed_ns"))
    window_seconds = window / 1e9 if window else SAMPLE_WINDOW_MS / 1000
    tasks = data.get("tasks") or []
    by_pid: dict[int, ProcessMetrics] = {}
    seen_keys: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        seen_keys.update(task.keys())
        pid = task.get("pid")
        if not isinstance(pid, int) or pid < 0:
            continue
        metrics = ProcessMetrics(
            pid=pid,
            energy_impact=_pick(task, "energy_impact"),
            disk_read_bytes=_pick(task, "disk_read_bytes"),
            disk_write_bytes=_pick(task, "disk_write_bytes"),
            net_recv_bytes=_pick(task, "net_recv_bytes"),
            net_sent_bytes=_pick(task, "net_sent_bytes"),
        )
        if not metrics.is_empty():
            by_pid[pid] = metrics
    return Snapshot(ts, window_seconds, span_seconds, by_pid, tuple(sorted(seen_keys)))


def probe(span_seconds: float, timeout: float = PROBE_TIMEOUT_S) -> Snapshot:
    """Run one short powermetrics sample. Raises HelperError on any failure."""
    try:
        result = subprocess.run(list(PROBE_COMMAND), capture_output=True, timeout=timeout,
                                check=False, env=dict(_ENV))
    except subprocess.TimeoutExpired:
        raise HelperError(f"powermetrics did not finish within {timeout:.0f}s") from None
    except OSError as exc:
        raise HelperError(f"could not run {SUDO}: {exc}") from None
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip().splitlines()
        message = stderr[-1] if stderr else f"exit status {result.returncode}"
        needs_rule = any(t in message.lower() for t in
                         ("password is required", "not allowed", "not permitted", "may not run"))
        raise HelperError(message, needs_sudoers=needs_rule)
    return parse_plist(result.stdout, time.time(), span_seconds)


def sudoers_line(user: str) -> str:
    return f"{user} ALL=(root) NOPASSWD: {POWERMETRICS} {' '.join(POWERMETRICS_ARGS)}"


class PowerMetrics:
    """Runs probes on a background thread and hands out the latest snapshot.

    The sampler never blocks on this: a probe takes about a second, and if the
    helper is unavailable it disables itself after the first refusal.
    """

    def __init__(self, interval_seconds: float) -> None:
        self.interval = max(5.0, interval_seconds)
        self.available: bool | None = None   # None until the first probe
        self.error: str | None = None
        self.needs_sudoers = False
        self.probes = 0
        self._snapshot: Snapshot | None = None
        self._taken = False                  # each snapshot is attributed to exactly one tick
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="powermetrics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=PROBE_TIMEOUT_S + 2)

    def _loop(self) -> None:
        last = None
        while not self._stop.is_set():
            started = time.monotonic()
            span = self.interval if last is None else started - last
            try:
                snapshot = probe(span)
            except HelperError as exc:
                self.available = False
                self.error = str(exc)
                self.needs_sudoers = exc.needs_sudoers
                log.error("powermetrics helper disabled: %s", exc)
                if exc.needs_sudoers:
                    log.error("run `whyslow helpers --sudoers` for the rule to install")
                return  # don't retry: repeated sudo refusals spam the auth log
            last = started
            self.probes += 1
            self.available = True
            self.error = None
            with self._lock:
                self._snapshot = snapshot
                self._taken = False
            self._stop.wait(max(1.0, self.interval - (time.monotonic() - started)))

    def take(self) -> Snapshot | None:
        """The newest unused snapshot, or None. Each one is returned only once."""
        with self._lock:
            if self._snapshot is None or self._taken:
                return None
            self._taken = True
            return self._snapshot
