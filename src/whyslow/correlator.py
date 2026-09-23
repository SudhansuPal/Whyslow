"""Correlation engine: when a metric spikes, decide which processes caused it.

Attribution depends on what macOS lets us measure per process:

* cpu     DIRECT: processes ranked by CPU at the moment (value = core-%,
          share = fraction of all CPU in use). The CPU no visible process
          accounts for (kernel_task, sub-second processes) is recorded on the
          spike as `unattributed`.
* memory  DIRECT: processes ranked by RSS growth over the last ~minute
          (value = bytes grown, share = fraction of total growth). Processes
          that appeared in that minute count with their whole RSS.
* disk_*, net_*   CORRELATED: macOS gives no per-process disk/network numbers
          without sudo, so we rank processes whose CPU jumped above their own
          recent norm when the spike began (value = core-% above normal).
          This is a lead, not proof, and it's labelled as such everywhere.
          With the sudo-gated powermetrics helper on, this upgrades to direct
          attribution from the measured bytes (see whyslow/helpers.py).

Culprits are captured when a spike starts and refreshed when it hits a new peak.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Callable

from whyslow.config import Config
from whyslow.detector import MetricDetector, SpikeEvent
from whyslow.sampler import ProcInfo, Sample, SystemSample

log = logging.getLogger("whyslow")

MB = 1024 * 1024
_CPU_EWMA_SECONDS = 60.0     # time constant of each process's "usual CPU"
_RSS_SNAPSHOT_EVERY_S = 10.0
_RSS_SNAPSHOTS = 7           # ~60 s of memory history
_MIN_CORRELATED_JUMP = 2.0   # core-%: ignore processes that barely moved


@dataclass(frozen=True)
class MetricSpec:
    name: str
    value: Callable[[SystemSample], float | None]
    min_delta: float
    kind: str  # "cpu" | "memory" | "io"
    ceiling: float | None = None  # bounded metrics (percentages)
    # Per-process field with the same meaning, when the sudo helper supplies it.
    process_field: str | None = None


def metric_specs(cfg: Config) -> list[MetricSpec]:
    d = cfg.detector
    return [
        MetricSpec("cpu", lambda s: s.cpu_percent, d.cpu_min_delta_percent, "cpu", ceiling=100.0),
        MetricSpec("memory", lambda s: s.mem_percent, d.memory_min_delta_percent, "memory", ceiling=100.0),
        MetricSpec("disk_read", lambda s: s.disk_read_bps, d.disk_min_delta_mb_per_s * MB, "io",
                   process_field="disk_read_bytes"),
        MetricSpec("disk_write", lambda s: s.disk_write_bps, d.disk_min_delta_mb_per_s * MB, "io",
                   process_field="disk_write_bytes"),
        MetricSpec("net_recv", lambda s: s.net_recv_bps, d.net_min_delta_mb_per_s * MB, "io",
                   process_field="net_recv_bytes"),
        MetricSpec("net_sent", lambda s: s.net_sent_bps, d.net_min_delta_mb_per_s * MB, "io",
                   process_field="net_sent_bytes"),
    ]


@dataclass(frozen=True)
class Culprit:
    info: ProcInfo
    value: float
    share: float | None
    attribution: str  # "direct" | "correlated"


class Correlator:
    """Keeps a little per-process history so blame can go to what *changed*."""

    def __init__(self, interval: float) -> None:
        self._alpha = min(1.0, interval / _CPU_EWMA_SECONDS)
        self._cpu_norm: dict[ProcInfo, float] = {}
        self._rss_snaps: deque[tuple[float, dict[ProcInfo, int]]] = deque(maxlen=_RSS_SNAPSHOTS)

    def update(self, sample: Sample) -> None:
        """Fold this tick into the history. Call *after* computing culprits for it."""
        for info in sample.ended:
            self._cpu_norm.pop(info, None)
        for p in sample.processes:
            if p.cpu_percent is None:
                continue
            prev = self._cpu_norm.get(p.info)
            self._cpu_norm[p.info] = p.cpu_percent if prev is None else prev + self._alpha * (p.cpu_percent - prev)
        ts = sample.system.ts
        if not self._rss_snaps or ts - self._rss_snaps[-1][0] >= _RSS_SNAPSHOT_EVERY_S:
            self._rss_snaps.append((ts, {p.info: p.rss for p in sample.processes if p.rss is not None}))

    def culprits(self, spec: MetricSpec, sample: Sample, top_n: int) -> list[Culprit]:
        if spec.kind == "cpu":
            return self._cpu(sample, top_n)
        if spec.kind == "memory":
            return self._memory(sample, top_n)
        # With the sudo helper on, disk/network become measured rather than guessed.
        measured = self._measured_io(spec, sample, top_n)
        return measured if measured else self._correlated(sample, top_n)

    def _measured_io(self, spec: MetricSpec, sample: Sample, top_n: int) -> list[Culprit]:
        if not spec.process_field:
            return []
        rows = [(p.info, getattr(p, spec.process_field) or 0.0) for p in sample.processes]
        rows = [(info, value) for info, value in rows if value > 0]
        if not rows:
            return []
        total = sum(value for _, value in rows)
        rows.sort(key=lambda r: r[1], reverse=True)
        return [Culprit(info, value, value / total if total else None, "direct")
                for info, value in rows[:top_n]]

    def _cpu(self, sample: Sample, top_n: int) -> list[Culprit]:
        total = sample.system.cpu_percent * sample.system.cpu_count  # core-% in use
        procs = sorted((p for p in sample.processes if p.cpu_percent), key=lambda p: p.cpu_percent, reverse=True)
        return [Culprit(p.info, p.cpu_percent, p.cpu_percent / total if total > 0 else None, "direct")
                for p in procs[:top_n]]

    def _memory(self, sample: Sample, top_n: int) -> list[Culprit]:
        before = self._rss_snaps[0][1] if self._rss_snaps else {}
        growth = [(p.info, p.rss - before.get(p.info, 0)) for p in sample.processes if p.rss is not None]
        growth = [(info, g) for info, g in growth if g > 0]
        total = sum(g for _, g in growth)
        growth.sort(key=lambda x: x[1], reverse=True)
        return [Culprit(info, float(g), g / total if total else None, "direct") for info, g in growth[:top_n]]

    def _correlated(self, sample: Sample, top_n: int) -> list[Culprit]:
        jumps = []
        for p in sample.processes:
            if p.cpu_percent is None:
                continue
            jump = p.cpu_percent - self._cpu_norm.get(p.info, 0.0)
            if jump >= _MIN_CORRELATED_JUMP:
                jumps.append((p.info, jump))
        jumps.sort(key=lambda x: x[1], reverse=True)
        return [Culprit(info, jump, None, "correlated") for info, jump in jumps[:top_n]]


class SpikeMonitor:
    """Feeds every tick through the detectors and records spikes + culprits."""

    def __init__(self, cfg: Config, store) -> None:  # store: whyslow.storage.Store
        d = cfg.detector
        window = max(2, round(d.baseline_window_seconds / cfg.sampling.interval_seconds))
        self.specs = metric_specs(cfg)
        self.detectors = {
            s.name: MetricDetector(s.name, window, d.threshold_k, d.sustain_samples, s.min_delta, s.ceiling)
            for s in self.specs
        }
        self.correlator = Correlator(cfg.sampling.interval_seconds)
        self.store = store
        self.top_n = d.culprits_top_n
        self.k = d.threshold_k
        self.sustain = d.sustain_samples
        self._open: dict[str, int] = {}  # metric -> spike id
        self._last_ts: float | None = None

    def process(self, sample: Sample) -> list[SpikeEvent]:
        events: list[SpikeEvent] = []
        if not sample.complete:
            # First tick or a gap (sleep): nothing measurable; close open spikes at the last good sample.
            if self._last_ts is not None:
                for det in self.detectors.values():
                    events += det.interrupt(self._last_ts)
            self._handle(events, sample)
            self.correlator.update(sample)
            return events
        for spec in self.specs:
            value = spec.value(sample.system)
            if value is None:
                continue
            events += self.detectors[spec.name].observe(sample.system.ts, value)
        self._handle(events, sample)
        self.correlator.update(sample)
        self._last_ts = sample.system.ts
        return events

    def close(self) -> None:
        """Shutting down: end any open spike at the last sample we saw."""
        if self._last_ts is None:
            return
        for det in self.detectors.values():
            for ev in det.interrupt(self._last_ts):
                if ev.metric in self._open:
                    self.store.end_spike(self._open.pop(ev.metric), ev.ended_at, ev.peak, ev.peak_at)

    def _handle(self, events: list[SpikeEvent], sample: Sample) -> None:
        by_name = {s.name: s for s in self.specs}
        for ev in events:
            spec = by_name[ev.metric]
            unattributed = sample.unattributed_cpu_percent if spec.kind == "cpu" else None
            if ev.kind == "start":
                spike_id = self.store.insert_spike(
                    ev.metric, ev.started_at, ev.peak, ev.peak_at, ev.baseline.median, ev.baseline.spread,
                    self.k, self.sustain, unattributed,
                )
                self._open[ev.metric] = spike_id
                culprits = self.correlator.culprits(spec, sample, self.top_n)
                self.store.replace_culprits(spike_id, culprits, sample.system.ts)
                top = ", ".join(c.info.app for c in culprits[:3]) or "no visible process"
                log.info("spike start: %s %.1f (baseline %.1f) - %s: %s", ev.metric, ev.value,
                         ev.baseline.median, culprits[0].attribution if culprits else "direct", top)
            elif ev.kind == "peak" and ev.metric in self._open:
                spike_id = self._open[ev.metric]
                self.store.update_spike(spike_id, ev.peak, ev.peak_at, unattributed)
                self.store.replace_culprits(spike_id, self.correlator.culprits(spec, sample, self.top_n),
                                            sample.system.ts)
            elif ev.kind == "end" and ev.metric in self._open:
                self.store.end_spike(self._open.pop(ev.metric), ev.ended_at, ev.peak, ev.peak_at)
                log.info("spike end (%s): %s peak %.1f", ev.reason, ev.metric, ev.peak)
