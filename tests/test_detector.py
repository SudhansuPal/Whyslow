import math
import random
import unittest

from whyslow.detector import MAD_SCALE, MIN_WARMUP_SAMPLES, MetricDetector, robust_baseline


def feed(det: MetricDetector, values, start_ts: float = 0.0):
    """Feed values at 1 Hz; return (events, next_ts)."""
    events = []
    ts = start_ts
    for v in values:
        events += det.observe(ts, v)
        ts += 1
    return events, ts


def noisy(n: int, level: float = 20.0, amp: float = 2.0, seed: int = 1):
    rnd = random.Random(seed)
    return [level + rnd.uniform(-amp, amp) for _ in range(n)]


def make(window=100, k=4.0, sustain=3, min_delta=10.0) -> MetricDetector:
    return MetricDetector("cpu", window=window, k=k, sustain=sustain, min_delta=min_delta)


class TestBaseline(unittest.TestCase):
    def test_median_and_mad(self):
        b = robust_baseline([1, 2, 3, 4, 100], k=4, min_delta=0)
        self.assertEqual(b.median, 3)
        self.assertAlmostEqual(b.spread, 1 * MAD_SCALE)
        self.assertAlmostEqual(b.threshold, 3 + 4 * MAD_SCALE)

    def test_outlier_does_not_move_baseline(self):
        b = robust_baseline([10.0] * 50 + [1000.0] * 5, k=4, min_delta=0)
        self.assertEqual(b.median, 10.0)
        self.assertEqual(b.spread, 0.0)

    def test_bounded_metric_threshold_stays_reachable(self):
        noisy_busy = [34 + d for d in (-20, -12, -6, 0, 0, 6, 12, 20, 25, -15)] * 6  # median 34, big MAD
        unbounded = robust_baseline(noisy_busy, k=4, min_delta=15)
        bounded = robust_baseline(noisy_busy, k=4, min_delta=15, ceiling=100)
        self.assertGreater(unbounded.threshold, 100)
        self.assertAlmostEqual(bounded.threshold, 34 + (100 - 34) / 2)

    def test_ceiling_does_not_change_quiet_baselines(self):
        quiet = noisy(300)
        self.assertEqual(robust_baseline(quiet, 4, 15), robust_baseline(quiet, 4, 15, ceiling=100))

    def test_min_delta_floors_a_flat_metric(self):
        b = robust_baseline([0.0] * 100, k=4, min_delta=20.0)
        self.assertEqual(b.threshold, 20.0)


class TestDetector(unittest.TestCase):
    def test_noise_never_spikes(self):
        det = make()
        events, _ = feed(det, noisy(2000))
        self.assertEqual(events, [])

    def test_sustained_step_is_a_spike(self):
        det = make()
        events, ts = feed(det, noisy(100))
        spike_start_ts = ts
        events, ts = feed(det, [80.0] * 5, ts)
        self.assertEqual([e.kind for e in events], ["start"])
        start = events[0]
        self.assertEqual(start.started_at, spike_start_ts)  # first sample of the run, not the Nth
        self.assertEqual(start.ts, spike_start_ts + 2)       # confirmed on the 3rd sample
        self.assertAlmostEqual(start.baseline.median, 20.0, delta=1.0)
        last_over = ts - 1
        events, ts = feed(det, noisy(5, seed=2), ts)
        self.assertEqual([e.kind for e in events], ["end"])
        self.assertEqual(events[0].ended_at, last_over)
        self.assertEqual(events[0].peak, 80.0)
        self.assertIsNone(det.active)

    def test_short_blip_is_not_a_spike(self):
        det = make(sustain=3)
        _, ts = feed(det, noisy(100))
        events, _ = feed(det, [90.0, 90.0, 20.0, 90.0, 90.0, 20.0, 20.0], ts)
        self.assertEqual(events, [])

    def test_sustain_of_one_fires_immediately(self):
        det = make(sustain=1)
        _, ts = feed(det, noisy(100))
        events, _ = feed(det, [90.0], ts)
        self.assertEqual([e.kind for e in events], ["start"])

    def test_small_change_below_min_delta_is_ignored(self):
        det = make(min_delta=10.0)
        _, ts = feed(det, [0.0] * 100)           # MAD = 0: only min_delta stops this from spiking
        events, ts = feed(det, [5.0] * 10, ts)
        self.assertEqual(events, [])
        events, _ = feed(det, [15.0] * 3, ts)
        self.assertEqual([e.kind for e in events], ["start"])

    def test_no_detection_during_warmup(self):
        det = make(window=300)
        events, _ = feed(det, [20.0] * (MIN_WARMUP_SAMPLES - 5) + [100.0] * 5)
        self.assertEqual(events, [])

    def test_long_plateau_becomes_the_new_normal(self):
        det = make(window=100)
        _, ts = feed(det, noisy(100))
        events, ts = feed(det, noisy(200, level=80.0, seed=3), ts)
        kinds = [e.kind for e in events]
        self.assertEqual(kinds[0], "start")
        self.assertIn("end", kinds)
        end = next(e for e in events if e.kind == "end")
        # Ends roughly once the plateau fills half the window.
        self.assertLess(end.ts - events[0].started_at, 60)

    def test_spike_does_not_end_early_while_load_continues(self):
        # Regression (seen live): the spike's own values inflated the MAD, the bar
        # raced upward, and the spike "ended" 18 s into a 60 s-window plateau.
        det = make(window=60, min_delta=15.0)
        _, ts = feed(det, noisy(60, level=20.0, amp=6.0))
        events, _ = feed(det, noisy(28, level=82.0, amp=5.0, seed=4), ts)
        self.assertEqual([e.kind for e in events if e.kind != "peak"], ["start"])
        self.assertIsNotNone(det.active)

    def test_long_plateau_is_one_spike_not_many(self):
        det = make(window=100)
        _, ts = feed(det, noisy(100))
        events, _ = feed(det, noisy(400, level=80.0, seed=5), ts)
        kinds = [e.kind for e in events if e.kind != "peak"]
        self.assertEqual(kinds, ["start", "end"])
        self.assertEqual(events[-1].reason, "new normal")

    def test_detector_stays_sensitive_after_a_spike(self):
        det = make(window=100)
        _, ts = feed(det, noisy(100))
        _, ts = feed(det, [90.0] * 20, ts)
        events, ts = feed(det, noisy(40, seed=6), ts)
        self.assertEqual(events[-1].reason, "dropped back")
        events, _ = feed(det, [90.0] * 5, ts)
        self.assertEqual([e.kind for e in events], ["start"])

    def test_peak_events_only_on_meaningful_growth(self):
        det = make()
        _, ts = feed(det, [20.0] * 100)
        events, _ = feed(det, [50, 50, 50, 51, 52, 60, 61, 80, 80.5], ts)
        kinds = [(e.kind, e.value) for e in events]
        self.assertEqual(kinds, [("start", 50), ("peak", 60), ("peak", 80)])

    def test_interrupt_closes_open_spike(self):
        det = make()
        _, ts = feed(det, [20.0] * 100)
        feed(det, [90.0] * 4, ts)
        events = det.interrupt(ts + 10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "end")
        self.assertEqual(events[0].ended_at, ts + 3)
        self.assertTrue(math.isnan(events[0].value))
        self.assertEqual(det.interrupt(ts + 11), [])

    def test_interrupt_resets_sustain_count(self):
        det = make(sustain=3)
        _, ts = feed(det, [20.0] * 100)
        feed(det, [90.0, 90.0], ts)
        det.interrupt(ts + 2)
        events, _ = feed(det, [90.0], ts + 50)
        self.assertEqual(events, [])

    def test_large_window_recomputes_lazily_but_still_detects(self):
        det = make(window=3600)
        _, ts = feed(det, noisy(1200))
        events, _ = feed(det, [90.0] * 5, ts)
        self.assertEqual([e.kind for e in events], ["start"])

    def test_saturating_load_on_a_busy_noisy_machine_is_a_spike(self):
        # Regression (seen live): baseline ~34% with heavy noise, then 94-100% for 20 s.
        # Without the ceiling the threshold sits above 100% and this can never fire.
        busy = [34 + d for d in (-20, -12, -6, 0, 0, 6, 12, 20, 25, -15)] * 6
        load = [94, 97, 100, 96, 99, 95, 100, 98] * 3
        for ceiling, expected in ((None, []), (100.0, ["start"])):
            with self.subTest(ceiling=ceiling):
                det = MetricDetector("cpu", window=60, k=4.0, sustain=3, min_delta=15.0, ceiling=ceiling)
                _, ts = feed(det, busy)
                events, _ = feed(det, load, ts)
                self.assertEqual([e.kind for e in events if e.kind == "start"], expected)

    def test_invalid_parameters(self):
        with self.assertRaises(ValueError):
            MetricDetector("cpu", window=1, k=4, sustain=3, min_delta=1)
        with self.assertRaises(ValueError):
            MetricDetector("cpu", window=10, k=4, sustain=0, min_delta=1)


if __name__ == "__main__":
    unittest.main()
