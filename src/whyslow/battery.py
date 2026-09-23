"""Battery drain attribution — an estimate, and labelled as one everywhere.

macOS reports true per-process energy only through `powermetrics`, which needs
sudo and is off by default (see whyslow/helpers.py). Without it we infer, and
we are careful not to pretend otherwise.

The honest part is that *most* battery drain isn't any process's fault: the
display, Wi-Fi, Bluetooth and the kernel draw power no matter what runs. So we
don't divide 100% of the drain among apps. Instead we fit, from your own data,

    drain (%/hour) = baseline + k * system CPU (percentage points)

Each data point is one **1% drop** of the battery reading: the time between
drops gives the rate, and the mean CPU over that stretch gives the x value.
macOS reports whole-number battery percentages, so the drops themselves are the
precise events; fixed-width buckets would mostly measure rounding error (an
earlier version did, and fitted its own quantization noise). Then:

* baseline x hours  -> "not attributable to any process" (screen, radios, idle)
* k * CPU x hours   -> the CPU-driven part, which IS split among apps

Apps split that CPU-driven part by an energy score:

    score = CPU seconds on battery + WAKEUP_COST_SECONDS * wakeups on battery

When the sudo-gated `powermetrics` helper is on, that guess is replaced by
macOS's own per-process **energy impact** (the number Activity Monitor shows),
and the ranking is marked "measured" instead of "estimated".

Wakeups matter because pulling the CPU out of an idle state costs energy even
when the work is tiny - that's why a mostly-idle background process can still
drain a battery. WAKEUP_COST_SECONDS charges each wakeup as a small slice of
CPU time; it's a rough constant, not a measurement.

If the fit is poor (too little data, or drain that simply doesn't track CPU -
a bright screen on an idle Mac) we say so and fall back to ranking by score
alone, with no percentages. Reporting "can't tell" is part of the job.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MIN_POINTS = 6             # fewer 1%-drops than this and the fit means nothing
MIN_R_SQUARED = 0.3        # below this, drain isn't tracking CPU; say so instead of guessing
MIN_STEP_SECONDS = 30.0    # a faster "drop" is a jumpy reading, not real drain
MAX_STEP_SECONDS = 7200.0  # a slower one spans sleep or idle gaps
MIN_SESSION_SECONDS = 300.0
WAKEUP_COST_SECONDS = 2e-5  # ~20 µs of CPU-equivalent energy per wakeup
MAX_GAP_SECONDS = 120.0     # bigger gap = sleep/sampler off: split the session


@dataclass
class Session:
    start: float
    end: float
    from_percent: float
    to_percent: float

    @property
    def seconds(self) -> float:
        return self.end - self.start

    @property
    def drained(self) -> float:
        return max(0.0, self.from_percent - self.to_percent)

    @property
    def per_hour(self) -> float | None:
        return self.drained / (self.seconds / 3600) if self.seconds >= MIN_SESSION_SECONDS else None


@dataclass
class Model:
    baseline_per_hour: float = 0.0     # drain with the CPU idle
    per_cpu_point_per_hour: float = 0.0
    r_squared: float = 0.0
    points: int = 0
    usable: bool = False
    note: str = "not enough time on battery yet"


@dataclass
class AppEstimate:
    app: str
    cpu_seconds: float
    wakeups: int
    score: float
    share: float                      # of the CPU-driven part
    percent: float | None = None      # estimated battery % this app cost
    measured: bool = False            # True when the score is powermetrics energy impact


@dataclass
class Estimate:
    model: Model
    hours_on_battery: float = 0.0
    drained_percent: float = 0.0
    baseline_percent: float = 0.0     # display, radios, kernel: not attributable
    cpu_percent_of_drain: float = 0.0  # the part apps are ranked within
    apps: list[AppEstimate] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)
    basis: str = "cpu"                # "cpu" (inferred) or "energy" (powermetrics)


def sessions(samples: list[tuple]) -> list[Session]:
    """Split (ts, battery%, cpu%, plugged) rows into unplugged stretches.

    A stretch ends when the Mac is plugged in, when the battery % rises
    (charging), or when sampling stops for a while (sleep).
    """
    out: list[Session] = []
    current: Session | None = None
    previous_ts = None
    for ts, percent, _cpu, plugged in samples:
        gap = previous_ts is not None and ts - previous_ts > MAX_GAP_SECONDS
        previous_ts = ts
        if plugged:
            current = None
            continue
        if current is None or gap or percent > current.to_percent:
            current = Session(ts, ts, percent, percent)
            out.append(current)
        else:
            current.end = ts
            current.to_percent = percent
    return [s for s in out if s.seconds >= MIN_SESSION_SECONDS and s.drained > 0]


def _drain_points(samples: list[tuple]) -> list[tuple[float, float]]:
    """(mean CPU %, drain %/hour), one point per 1% drop of the battery reading."""
    out: list[tuple[float, float]] = []
    level: float | None = None
    since_ts: float | None = None
    cpus: list[float] = []
    for ts, percent, cpu, plugged in samples:
        if plugged:
            level, since_ts, cpus = None, None, []
            continue
        if level is None or percent > level:  # first sample, or charging/replaced battery
            level, since_ts, cpus = percent, ts, []
            continue
        cpus.append(cpu)
        if percent < level:
            elapsed = ts - since_ts
            if MIN_STEP_SECONDS <= elapsed <= MAX_STEP_SECONDS and cpus:
                out.append((sum(cpus) / len(cpus), (level - percent) / (elapsed / 3600)))
            level, since_ts, cpus = percent, ts, []
    return out


def fit(samples: list[tuple]) -> Model:
    """Least-squares fit of drain rate against system CPU."""
    points = _drain_points(samples)
    if len(points) < MIN_POINTS:
        return Model(points=len(points))
    n = len(points)
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mean_x) ** 2 for p in points)
    sxy = sum((p[0] - mean_x) * (p[1] - mean_y) for p in points)
    if sxx <= 0:
        return Model(points=n, note="CPU never varied while on battery")
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    syy = sum((p[1] - mean_y) ** 2 for p in points)
    r2 = (sxy ** 2 / (sxx * syy)) if syy > 0 else 0.0
    if slope <= 0 or r2 < MIN_R_SQUARED:
        return Model(max(0.0, intercept), 0.0, r2, n, False,
                     f"drain didn't track CPU closely enough (R²={r2:.2f}); "
                     "screen and radios probably dominated")
    # A negative intercept is physically meaningless; clamp and keep the slope.
    return Model(max(0.0, intercept), slope, r2, n, True,
                 f"fitted on {n} battery-percent drops (R²={r2:.2f})")


def estimate(samples: list[tuple], usage: list[dict], limit: int = 10) -> Estimate:
    """Rank apps by estimated battery cost.

    `usage` rows come from storage.leaderboard(): they need `app`,
    `cpu_seconds_on_battery` and `ctx_switches_on_battery`.
    """
    model = fit(samples)
    runs = sessions(samples)
    est = Estimate(model=model, sessions=runs)
    est.hours_on_battery = sum(s.seconds for s in runs) / 3600
    est.drained_percent = sum(s.drained for s in runs)

    # Real energy impact when the helper supplied it; otherwise the CPU+wakeups proxy.
    measured = any((row.get("energy_impact") or 0) > 0 for row in usage)
    est.basis = "energy" if measured else "cpu"
    scored = []
    for row in usage:
        cpu = row.get("cpu_seconds_on_battery") or 0.0
        wake = row.get("ctx_switches_on_battery") or 0
        score = (row.get("energy_impact") or 0.0) if measured else cpu + WAKEUP_COST_SECONDS * wake
        if score > 0:
            scored.append(AppEstimate(row["app"], cpu, wake, score, 0.0, measured=measured))
    total_score = sum(a.score for a in scored)
    scored.sort(key=lambda a: a.score, reverse=True)

    if model.usable and est.hours_on_battery > 0:
        est.baseline_percent = min(est.drained_percent, model.baseline_per_hour * est.hours_on_battery)
        est.cpu_percent_of_drain = max(0.0, est.drained_percent - est.baseline_percent)
    for app in scored:
        app.share = app.score / total_score if total_score else 0.0
        if model.usable:
            app.percent = est.cpu_percent_of_drain * app.share
    est.apps = scored[:limit]
    return est
