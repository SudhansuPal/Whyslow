import plistlib
import unittest
from unittest import mock

from whyslow import helpers, storage
from whyslow.config import Config, HelpersConfig
from whyslow.helpers import HelperError, ProcessMetrics, Snapshot
from whyslow.sampler import Sample, Sampler

from helpers_fixtures import make_plist  # noqa: E402  (test helper module)


class TestCommand(unittest.TestCase):
    def test_command_is_fixed_and_non_interactive(self):
        self.assertEqual(helpers.PROBE_COMMAND[:3], ("/usr/bin/sudo", "-n", "/usr/bin/powermetrics"))
        self.assertTrue(all(isinstance(a, str) for a in helpers.PROBE_COMMAND))
        # One short sample, never a stream; no output file, no shell metacharacters.
        self.assertIn("--sample-count", helpers.POWERMETRICS_ARGS)
        self.assertEqual(helpers.POWERMETRICS_ARGS[helpers.POWERMETRICS_ARGS.index("--sample-count") + 1], "1")
        self.assertNotIn("-o", helpers.POWERMETRICS_ARGS)
        self.assertFalse(any(c in a for a in helpers.PROBE_COMMAND for c in ";|&$`><*?"))

    def test_sudoers_line_matches_the_command_exactly(self):
        line = helpers.sudoers_line("alice")
        self.assertTrue(line.startswith("alice ALL=(root) NOPASSWD: /usr/bin/powermetrics "))
        # The rule must list precisely the args we run, or sudo will refuse.
        self.assertTrue(line.endswith(" ".join(helpers.POWERMETRICS_ARGS)))
        self.assertNotIn("*", line)  # no wildcards: exact match only


class TestParser(unittest.TestCase):
    def test_parses_tasks(self):
        raw = make_plist([
            {"pid": 42, "name": "Firefox", "energy_impact": 12.5, "diskio_bytesread": 1024,
             "diskio_byteswritten": 2048, "bytes_received": 4096, "bytes_sent": 512},
        ], elapsed_ns=1_000_000_000)
        snap = helpers.parse_plist(raw, ts=100.0, span_seconds=30.0)
        self.assertEqual(snap.window_seconds, 1.0)
        self.assertEqual(snap.span_seconds, 30.0)
        m = snap.by_pid[42]
        self.assertEqual((m.energy_impact, m.disk_read_bytes, m.disk_write_bytes), (12.5, 1024, 2048))
        self.assertEqual((m.net_recv_bytes, m.net_sent_bytes), (4096, 512))

    def test_accepts_alternative_key_spellings(self):
        raw = make_plist([{"pid": 7, "name": "x", "energy": 3.0, "bytes_read": 10,
                           "bytes_written": 20, "bytes_in": 30, "bytes_out": 40}])
        m = helpers.parse_plist(raw, 0.0, 30.0).by_pid[7]
        self.assertEqual((m.energy_impact, m.disk_read_bytes, m.net_recv_bytes), (3.0, 10, 30))

    def test_tasks_without_usable_numbers_are_dropped(self):
        raw = make_plist([{"pid": 1, "name": "idle"}, {"pid": 2, "name": "busy", "energy_impact": 1.0}])
        self.assertEqual(list(helpers.parse_plist(raw, 0.0, 30.0).by_pid), [2])

    def test_bad_pids_ignored(self):
        raw = make_plist([{"pid": "x", "energy_impact": 1.0}, {"name": "no pid", "energy_impact": 1.0}])
        self.assertEqual(helpers.parse_plist(raw, 0.0, 30.0).by_pid, {})

    def test_reports_keys_for_diagnostics(self):
        raw = make_plist([{"pid": 1, "name": "x", "energy_impact": 1.0, "future_metric": 9}])
        self.assertIn("future_metric", helpers.parse_plist(raw, 0.0, 30.0).task_keys)

    def test_leading_banner_before_the_plist_is_tolerated(self):
        raw = b"Machine model: MacBookAir\n" + make_plist([{"pid": 5, "energy_impact": 2.0}])
        self.assertIn(5, helpers.parse_plist(raw, 0.0, 30.0).by_pid)

    def test_garbage_raises(self):
        for raw in (b"", b"powermetrics must be invoked as the superuser\n"):
            with self.assertRaises(HelperError):
                helpers.parse_plist(raw, 0.0, 30.0)

    def test_truncated_plist_raises_not_crashes(self):
        raw = make_plist([{"pid": 1, "energy_impact": 1.0}])[:40]
        with self.assertRaises(HelperError):
            helpers.parse_plist(raw, 0.0, 30.0)


class TestProbe(unittest.TestCase):
    def run_with(self, returncode=0, stdout=b"", stderr=b""):
        result = mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)
        with mock.patch("subprocess.run", return_value=result) as runner:
            try:
                return helpers.probe(30.0), runner
            except HelperError as exc:
                return exc, runner

    def test_missing_sudoers_rule_is_flagged(self):
        exc, _ = self.run_with(returncode=1, stderr=b"sudo: a password is required\n")
        self.assertIsInstance(exc, HelperError)
        self.assertTrue(exc.needs_sudoers)

    def test_command_not_allowed_is_flagged(self):
        exc, _ = self.run_with(returncode=1, stderr=b"Sorry, user alice is not allowed to execute ...\n")
        self.assertTrue(exc.needs_sudoers)

    def test_other_failures_are_not_blamed_on_sudoers(self):
        exc, _ = self.run_with(returncode=2, stderr=b"powermetrics: unknown sampler\n")
        self.assertFalse(exc.needs_sudoers)

    def test_environment_is_scrubbed(self):
        _, runner = self.run_with(returncode=0, stdout=make_plist([{"pid": 1, "energy_impact": 1.0}]))
        env = runner.call_args.kwargs["env"]
        self.assertEqual(set(env), {"PATH", "LC_ALL"})
        self.assertEqual(runner.call_args.args[0], list(helpers.PROBE_COMMAND))
        self.assertIn("timeout", runner.call_args.kwargs)


class FakeHelper:
    """Stands in for helpers.PowerMetrics in the sampler."""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.takes = 0

    def take(self):
        self.takes += 1
        return self.snapshot if self.takes == 1 else None  # each snapshot used once


class TestSamplerIntegration(unittest.TestCase):
    def test_helper_values_attach_to_matching_processes(self):
        import os

        snapshot = Snapshot(ts=0.0, window_seconds=1.0, span_seconds=30.0, by_pid={
            os.getpid(): ProcessMetrics(os.getpid(), energy_impact=5.0, disk_read_bytes=100.0,
                                        disk_write_bytes=200.0, net_recv_bytes=300.0, net_sent_bytes=400.0),
        })
        helper = FakeHelper(snapshot)
        sampler = Sampler(Config(), helper)
        sampler.tick()
        sample = sampler.tick()
        mine = next(p for p in sample.processes if p.info.pid == os.getpid())
        self.assertEqual(sample.helper_span_seconds, 30.0)
        self.assertEqual((mine.energy_impact, mine.disk_read_bytes, mine.net_sent_bytes), (5.0, 100.0, 400.0))
        # A second tick gets no snapshot, so nothing is double-counted.
        again = sampler.tick()
        self.assertIsNone(again.helper_span_seconds)
        self.assertIsNone(next(p for p in again.processes if p.info.pid == os.getpid()).energy_impact)


class TestStorageExtrapolation(unittest.TestCase):
    def test_sampled_rates_become_extrapolated_totals(self):
        import os
        import tempfile
        from pathlib import Path

        from helpers import proc, system  # tests/helpers.py
        from whyslow.sampler import ProcSample

        with tempfile.TemporaryDirectory() as d:
            old = os.umask(0o077)
            store = storage.Store(Path(d) / "w.sqlite3", Config())
            try:
                info = proc(1, "Chrome")
                p = ProcSample(info, 0.5, 50.0, 100, 10)
                p.disk_read_bytes, p.net_sent_bytes, p.energy_impact = 1000.0, 500.0, 2.0
                store.record(Sample(system(100.0), [p], [], 0.0, True, helper_span_seconds=30.0))
                store.flush()
                row = store.conn.execute(
                    "SELECT disk_read_bytes, net_sent_bytes, energy_impact FROM app_usage_hourly").fetchone()
                self.assertEqual(row, (30_000, 15_000, 60.0))  # 1 s of every 30 s, scaled up
                sampled = store.conn.execute(
                    "SELECT disk_read_bytes, net_sent_bytes FROM process_samples").fetchone()
                self.assertEqual(sampled, (1000.0, 500.0))     # per-sample rates kept as measured
            finally:
                store.conn.close()
                os.umask(old)

    def test_io_heavy_process_is_stored_even_with_no_cpu(self):
        import os
        import tempfile
        from pathlib import Path

        from helpers import proc, system
        from whyslow.sampler import ProcSample

        with tempfile.TemporaryDirectory() as d:
            old = os.umask(0o077)
            store = storage.Store(Path(d) / "w.sqlite3", Config())
            store._ticks = 1  # skip the periodic memory snapshot
            try:
                quiet = ProcSample(proc(9, "backupd"), 0.0, 0.0, 10, None)
                quiet.disk_write_bytes = 50_000_000.0
                store.record(Sample(system(100.0), [quiet], [], 0.0, True, helper_span_seconds=30.0))
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM process_samples").fetchone()[0], 1)
            finally:
                store.conn.close()
                os.umask(old)


class TestConfig(unittest.TestCase):
    def test_helper_defaults_off(self):
        self.assertFalse(Config().helpers.powermetrics)
        self.assertEqual(Config().helpers.interval_seconds, 30.0)

    def test_interval_is_range_checked(self):
        from whyslow import config as config_mod
        with self.assertRaises(config_mod.ConfigError):
            config_mod.from_dict({"helpers": {"interval_seconds": 1}})
        self.assertEqual(config_mod.from_dict({"helpers": {"powermetrics": True}}).helpers,
                         HelpersConfig(powermetrics=True))


if __name__ == "__main__":
    unittest.main()
