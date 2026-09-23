import unittest

from whyslow import battery
from whyslow.battery import MIN_R_SQUARED, WAKEUP_COST_SECONDS


def samples(minutes, cpu, drain_per_hour, start=0.0, start_percent=100.0, plugged=0, step=5.0):
    """Synthetic (ts, battery%, cpu%, plugged) rows draining at a steady rate."""
    rows = []
    for i in range(int(minutes * 60 / step)):
        ts = start + i * step
        percent = start_percent - drain_per_hour * (ts - start) / 3600
        rows.append((ts, round(percent), cpu, plugged))  # macOS reports whole percents
    return rows


class TestSessions(unittest.TestCase):
    def test_single_discharge_session(self):
        runs = battery.sessions(samples(60, cpu=20, drain_per_hour=10))
        self.assertEqual(len(runs), 1)
        self.assertAlmostEqual(runs[0].drained, 10, delta=1)
        self.assertAlmostEqual(runs[0].per_hour, 10, delta=1)

    def test_plugged_time_is_excluded(self):
        rows = samples(30, 20, 10) + samples(30, 20, 0, start=2000, start_percent=90, plugged=1)
        runs = battery.sessions(rows)
        self.assertEqual(len(runs), 1)
        self.assertLess(runs[0].end, 2000)

    def test_charging_starts_a_new_session(self):
        rows = samples(20, 20, 12) + samples(20, 20, 12, start=1300, start_percent=99)
        self.assertEqual(len(battery.sessions(rows)), 2)

    def test_sleep_gap_splits_sessions(self):
        rows = samples(20, 20, 12) + samples(20, 20, 12, start=9000, start_percent=90)
        runs = battery.sessions(rows)
        self.assertEqual(len(runs), 2)

    def test_short_or_flat_runs_ignored(self):
        self.assertEqual(battery.sessions(samples(2, 20, 12)), [])       # too short
        self.assertEqual(battery.sessions(samples(60, 20, 0)), [])       # nothing drained


class TestFit(unittest.TestCase):
    def build(self, baseline, k, cpus, minutes_each=10):
        """Rows whose drain rate really is baseline + k * cpu."""
        return self.build_pairs([(cpu, baseline + k * cpu) for cpu in cpus], minutes_each)

    def build_pairs(self, pairs, minutes_each=10):
        """Rows with an explicit (cpu, drain-rate) per stretch."""
        rows, start, percent = [], 0.0, 100.0
        for cpu, rate in pairs:
            chunk = samples(minutes_each, cpu, rate, start=start, start_percent=percent)
            rows += chunk
            start = chunk[-1][0] + 5
            percent -= rate * (minutes_each / 60)
        return rows

    def test_recovers_baseline_and_slope(self):
        model = battery.fit(self.build(baseline=6.0, k=0.2, cpus=[5, 40, 10, 70, 20, 90, 30, 60]))
        self.assertTrue(model.usable)
        self.assertAlmostEqual(model.baseline_per_hour, 6.0, delta=1.5)
        self.assertAlmostEqual(model.per_cpu_point_per_hour, 0.2, delta=0.08)
        self.assertGreater(model.r_squared, 0.8)

    def test_not_enough_data(self):
        model = battery.fit(samples(10, 20, 10))
        self.assertFalse(model.usable)
        self.assertIn("not enough time", model.note)

    def test_constant_cpu_cannot_be_separated(self):
        model = battery.fit(self.build(baseline=6.0, k=0.0, cpus=[20] * 8))
        self.assertFalse(model.usable)

    def test_drain_unrelated_to_cpu_is_reported_not_invented(self):
        # Screen/radios dominate: drain is flat regardless of CPU.
        model = battery.fit(self.build(baseline=9.0, k=0.0, cpus=[5, 50, 10, 80, 20, 95, 35, 65]))
        self.assertFalse(model.usable)
        self.assertIn("didn't track CPU", model.note)

    def test_plugged_buckets_are_ignored(self):
        rows = self.build(baseline=6.0, k=0.2, cpus=[5, 40, 10, 70, 20, 90, 30, 60])
        rows += [(t, 100, 95, 1) for t in range(20000, 26000, 5)]  # plugged in, busy
        self.assertTrue(battery.fit(rows).usable)

    def test_quantization_does_not_wreck_the_fit(self):
        # Battery % is whole numbers; the fit must key off the 1% drops themselves.
        model = battery.fit(self.build(baseline=6.0, k=0.2, cpus=[5, 40, 10, 70, 20, 90, 30, 60]))
        self.assertGreater(model.r_squared, 0.9)

    def test_jumpy_readings_and_huge_gaps_are_skipped(self):
        instant = [(i * 5.0, 100 - i, 20, 0) for i in range(10)]        # 1% every 5s: implausible
        self.assertEqual(battery.fit(instant).points, 0)
        slow = [(i * 9000.0, 100 - i, 20, 0) for i in range(10)]        # 1% every 2.5h: spans sleep
        self.assertEqual(battery.fit(slow).points, 0)


class TestEstimate(unittest.TestCase):
    def usage(self):
        return [
            {"app": "Chrome", "cpu_seconds_on_battery": 600.0, "ctx_switches_on_battery": 100_000},
            {"app": "Idle Daemon", "cpu_seconds_on_battery": 5.0, "ctx_switches_on_battery": 5_000_000},
            {"app": "Quiet", "cpu_seconds_on_battery": 0.0, "ctx_switches_on_battery": 0},
        ]

    def test_splits_only_the_cpu_driven_part(self):
        rows = TestFit().build(baseline=6.0, k=0.2, cpus=[5, 40, 10, 70, 20, 90, 30, 60])
        est = battery.estimate(rows, self.usage())
        self.assertTrue(est.model.usable)
        self.assertGreater(est.baseline_percent, 0)
        self.assertAlmostEqual(est.baseline_percent + est.cpu_percent_of_drain, est.drained_percent, delta=0.01)
        self.assertAlmostEqual(sum(a.percent for a in est.apps), est.cpu_percent_of_drain, delta=0.01)
        self.assertNotIn("Quiet", [a.app for a in est.apps])  # nothing to blame it for

    def test_wakeups_count_toward_the_score(self):
        est = battery.estimate(TestFit().build(6.0, 0.2, [5, 40, 10, 70, 20, 90, 30, 60]), self.usage())
        daemon = next(a for a in est.apps if a.app == "Idle Daemon")
        self.assertAlmostEqual(daemon.score, 5.0 + 5_000_000 * WAKEUP_COST_SECONDS)
        self.assertGreater(daemon.score, 100)  # a mostly-idle waker still registers

    def test_no_percentages_without_a_usable_model(self):
        est = battery.estimate(samples(20, 20, 10), self.usage())
        self.assertFalse(est.model.usable)
        self.assertTrue(all(a.percent is None for a in est.apps))
        self.assertTrue(all(a.share > 0 for a in est.apps))  # still ranked

    def test_measured_energy_replaces_the_proxy_score(self):
        usage = [
            {"app": "Chrome", "cpu_seconds_on_battery": 600.0, "ctx_switches_on_battery": 100_000,
             "energy_impact": 20.0},
            {"app": "Idle Daemon", "cpu_seconds_on_battery": 5.0, "ctx_switches_on_battery": 5_000_000,
             "energy_impact": 80.0},  # macOS says this one really is the expensive one
        ]
        est = battery.estimate(TestFit().build(6.0, 0.2, [5, 40, 10, 70, 20, 90, 30, 60]), usage)
        self.assertEqual(est.basis, "energy")
        self.assertEqual([a.app for a in est.apps], ["Idle Daemon", "Chrome"])
        self.assertTrue(all(a.measured for a in est.apps))
        self.assertAlmostEqual(est.apps[0].share, 0.8)

    def test_falls_back_to_the_proxy_without_energy_data(self):
        est = battery.estimate(TestFit().build(6.0, 0.2, [5, 40, 10, 70, 20, 90, 30, 60]), self.usage())
        self.assertEqual(est.basis, "cpu")
        self.assertFalse(any(a.measured for a in est.apps))

    def test_no_battery_data(self):
        est = battery.estimate([], self.usage())
        self.assertEqual(est.sessions, [])
        self.assertEqual(est.drained_percent, 0)

    def test_weak_correlation_yields_ranking_but_no_percentages(self):
        # CPU climbs steadily, drain alternates for unrelated reasons (screen brightness).
        noisy = [(cpu, rate) for cpu, rate in zip(range(10, 90, 10), [8, 20, 6, 18, 7, 19, 9, 21])]
        est = battery.estimate(TestFit().build_pairs(noisy), self.usage())
        self.assertLess(est.model.r_squared, MIN_R_SQUARED)
        self.assertFalse(est.model.usable)
        self.assertTrue(all(a.percent is None for a in est.apps))
        self.assertEqual([a.app for a in est.apps][:2], ["Chrome", "Idle Daemon"])  # still ranked


if __name__ == "__main__":
    unittest.main()
