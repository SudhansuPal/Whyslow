"""Spike detection: a rolling robust baseline per metric.

Baseline = median of the trailing window; spread = MAD * 1.4826 (the MAD
scaled to be comparable to a standard deviation for normal data). Median/MAD
are used instead of mean/std because they aren't dragged around by the very
spikes we're trying to detect.

A spike STARTS when the value exceeds

    threshold = median + max(k * spread, min_delta)

for `sustain` consecutive samples. The threshold is then frozen for that spike,
and the spike ENDS when either
  * the value is back at or below the frozen threshold for `sustain` samples
    ("dropped back"), or
  * the live median has itself risen to the frozen threshold ("new normal"),
    which happens once a plateau fills about half the window. That's deliberate:
    "this is how it is now" is not an anomaly.
`min_delta` is the smallest change that counts as interesting: without it a
flat metric (idle disk: MAD = 0) would "spike" on every blip.

Bounded metrics (CPU %, memory %) pass `ceiling` (100). Their noise margin
k * spread is capped at half the remaining headroom, so "halfway from normal
to saturated" always counts. Without the cap, a noisy baseline on a busy
machine pushed the CPU threshold past 100% and a 35% -> 98% jump was
undetectable (observed live).

Values enter the window clipped at the current threshold (winsorized). Without
that, a spike's own values inflate the MAD much faster than they move the
median, the bar races upward, and a spike "ends" while the load is still
running (observed live before this fix).

This module is pure: no I/O, no psutil, no clock. Easy to test.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

MAD_SCALE = 1.4826
MIN_WARMUP_SAMPLES = 60
# Recompute the baseline at most every this-many-samples-per-300 (keeps big windows cheap).
_BASELINE_SAMPLES_PER_RECOMPUTE = 300


@dataclass(frozen=True)
class Baseline:
    median: float
    spread: float
    threshold: float


@dataclass(frozen=True)
class SpikeEvent:
    kind: str           # "start" | "peak" | "end"
    metric: str
    ts: float           # when the event was observed
    started_at: float   # first sample of the sustained run
    value: float        # the value that produced this event
    peak: float
    peak_at: float
    baseline: Baseline  # frozen at the start of the spike
    ended_at: float | None = None  # for "end": last sample above threshold, or when it became normal
    reason: str | None = None      # for "end": "dropped back" | "new normal" | "interrupted"


@dataclass
class _Active:
    started_at: float
    baseline: Baseline
    peak: float
    peak_at: float
    reported_peak: float
    last_over_ts: float


def _median(sorted_values: list[float]) -> float:
    n = len(sorted_values)
    mid = n // 2
    return sorted_values[mid] if n % 2 else (sorted_values[mid - 1] + sorted_values[mid]) / 2


def robust_baseline(values: list[float], k: float, min_delta: float, ceiling: float | None = None) -> Baseline:
    ordered = sorted(values)
    med = _median(ordered)
    spread = _median(sorted(abs(v - med) for v in ordered)) * MAD_SCALE
    margin = k * spread
    if ceiling is not None:
        margin = min(margin, (ceiling - med) / 2)
    return Baseline(med, spread, med + max(margin, min_delta))


class MetricDetector:
    def __init__(self, metric: str, window: int, k: float, sustain: int, min_delta: float,
                 ceiling: float | None = None, peak_step: float = 0.10) -> None:
        if window < 2 or sustain < 1:
            raise ValueError("window must be >= 2 and sustain >= 1")
        self.metric = metric
        self.k = k
        self.sustain = sustain
        self.min_delta = min_delta
        self.ceiling = ceiling  # upper bound of a bounded metric (100 for percentages)
        self.peak_step = peak_step  # report a new peak only if the excess grows by this fraction
        self._window: deque[float] = deque(maxlen=window)
        self._warmup = min(MIN_WARMUP_SAMPLES, window)
        self._recompute_every = max(1, window // _BASELINE_SAMPLES_PER_RECOMPUTE)
        self._since_recompute = 0
        self._baseline: Baseline | None = None
        self._over = 0
        self._under = 0
        self._run_start = 0.0
        self._run_peak = (float("-inf"), 0.0)
        self.active: _Active | None = None

    def baseline(self) -> Baseline | None:
        if len(self._window) < self._warmup:
            return None
        if self._baseline is None or self._since_recompute >= self._recompute_every:
            self._baseline = robust_baseline(list(self._window), self.k, self.min_delta, self.ceiling)
            self._since_recompute = 0
        return self._baseline

    def observe(self, ts: float, value: float) -> list[SpikeEvent]:
        events: list[SpikeEvent] = []
        b = self.baseline()
        if b is None:
            self._window.append(value)
        else:
            if self.active is None:
                events += self._idle(ts, value, value > b.threshold, b)
            else:
                events += self._in_spike(ts, value, b)
            self._window.append(min(value, b.threshold))
        self._since_recompute += 1
        return events

    def _idle(self, ts: float, value: float, over: bool, b: Baseline) -> list[SpikeEvent]:
        if not over:
            self._over = 0
            return []
        if self._over == 0:
            self._run_start = ts
            self._run_peak = (value, ts)
        elif value > self._run_peak[0]:
            self._run_peak = (value, ts)
        self._over += 1
        if self._over < self.sustain:
            return []
        peak, peak_at = self._run_peak
        self.active = _Active(self._run_start, b, peak, peak_at, peak, ts)
        self._under = 0
        return [SpikeEvent("start", self.metric, ts, self._run_start, value, peak, peak_at, b)]

    def _in_spike(self, ts: float, value: float, live: Baseline) -> list[SpikeEvent]:
        a = self.active
        assert a is not None
        over = value > a.baseline.threshold
        events = []
        if value > a.peak:
            a.peak, a.peak_at = value, ts
            base = a.baseline.median
            if value - base > (a.reported_peak - base) * (1 + self.peak_step):
                a.reported_peak = value
                events.append(SpikeEvent("peak", self.metric, ts, a.started_at, value, a.peak, a.peak_at, a.baseline))
        if over:
            self._under = 0
            a.last_over_ts = ts
        else:
            self._under += 1
        if self._under >= self.sustain:
            events.append(self._end(ts, value, a.last_over_ts, "dropped back"))
        elif live.median >= a.baseline.threshold:
            events.append(self._end(ts, value, ts, "new normal"))
        return events

    def _end(self, ts: float, value: float, ended_at: float, reason: str) -> SpikeEvent:
        a = self.active
        assert a is not None
        self.active = None
        self._over = self._under = 0
        return SpikeEvent("end", self.metric, ts, a.started_at, value, a.peak, a.peak_at, a.baseline,
                          ended_at=ended_at, reason=reason)

    def interrupt(self, ts: float) -> list[SpikeEvent]:
        """A gap in the data (sleep, pause): close any open spike and restart the sustain count."""
        self._over = self._under = 0
        if self.active is None:
            return []
        return [self._end(ts, float("nan"), self.active.last_over_ts, "interrupted")]
