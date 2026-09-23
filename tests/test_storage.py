import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from whyslow import macos, storage
from whyslow.config import Config
from whyslow.sampler import ProcSample, Sample

from helpers import proc, system


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "data" / "w.sqlite3"
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        self.store = storage.Store(self.db, Config())
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        self.store.conn.close()

    def q(self, sql, *args):
        return self.store.conn.execute(sql, args).fetchall()

    def test_file_permissions(self):
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.db.parent.stat().st_mode), 0o700)
        self.assertEqual(self.q("PRAGMA journal_mode")[0][0], "wal")
        self.assertEqual(self.q("PRAGMA user_version")[0][0], storage.SCHEMA_VERSION)

    def test_record_and_rollup(self):
        a, b = proc(1, "Chrome"), proc(2, "Chrome")
        for ts in (100.0, 101.0):
            self.store.record(Sample(system(ts), [ProcSample(a, 0.5, 50.0, 100, 10),
                                                   ProcSample(b, 0.25, 25.0, 200, None)], [], 0.0, True))
        self.store.flush()
        self.assertEqual(self.q("SELECT COUNT(*) FROM system_samples")[0][0], 2)
        self.assertEqual(self.q("SELECT COUNT(*) FROM process_samples")[0][0], 4)
        # Same pid+create_time -> one process row across ticks.
        self.assertEqual(self.q("SELECT COUNT(*) FROM processes")[0][0], 2)
        cpu, batt, ctx, rss = self.q(
            "SELECT cpu_seconds, cpu_seconds_on_battery, ctx_switches, peak_rss_bytes FROM app_usage_hourly")[0]
        self.assertAlmostEqual(cpu, 1.5)
        self.assertAlmostEqual(batt, 1.5)
        self.assertEqual(ctx, 20)
        self.assertEqual(rss, 300)

    def test_pid_reuse_creates_new_identity(self):
        self.store.record(Sample(system(100.0), [ProcSample(proc(7, "A", 1.0), 1.0, 100.0, 1, 1)], [], 0.0, True))
        self.store.record(Sample(system(101.0), [ProcSample(proc(7, "B", 2.0), 1.0, 100.0, 1, 1)], [], 0.0, True))
        self.assertEqual(self.q("SELECT COUNT(*) FROM processes WHERE pid = 7")[0][0], 2)

    def test_incomplete_sample_not_stored(self):
        self.store.record(Sample(system(100.0), [], [], None, False))
        self.assertEqual(self.q("SELECT COUNT(*) FROM system_samples")[0][0], 0)

    def test_idle_processes_not_stored_per_tick(self):
        self.store._ticks = 1  # skip the periodic memory snapshot
        busy, idle = proc(1, "A"), proc(2, "B")
        self.store.record(Sample(system(100.0), [ProcSample(busy, 0.5, 50.0, 1, 1),
                                                  ProcSample(idle, 0.0, 0.0, 1, 0)], [], 0.0, True))
        self.assertEqual([r[0] for r in self.q("SELECT pid FROM processes")], [1])

    def test_ended_process_marked(self):
        a = proc(1, "A")
        self.store.record(Sample(system(100.0), [ProcSample(a, 0.5, 50.0, 1, 1)], [], 0.0, True))
        self.store.record(Sample(system(101.0), [], [a], 0.0, True))
        self.assertEqual(self.q("SELECT ended, last_seen FROM processes")[0], (1, 100.0))

    def test_prune(self):
        now = 10 * 86400.0
        old, new = now - 8 * 86400, now - 60
        self.store.record(Sample(system(old), [ProcSample(proc(1, "Old"), 1.0, 100.0, 1, 1)], [], 0.0, True))
        self.store.record(Sample(system(new), [ProcSample(proc(2, "New"), 1.0, 100.0, 1, 1)], [], 0.0, True))
        self.store.flush()
        deleted = self.store.prune(retention_days=7, rollup_retention_days=180, now=now)
        self.assertEqual(deleted["system_samples"], 1)
        self.assertEqual(deleted["process_samples"], 1)
        self.assertEqual([r[0] for r in self.q("SELECT pid FROM processes")], [2])
        # Hourly rollups outlive raw samples.
        self.assertEqual(self.q("SELECT COUNT(*) FROM app_usage_hourly")[0][0], 2)

    def test_hostile_names_are_stored_verbatim_as_data(self):
        evil = proc(3, "x'); DROP TABLE apps; --")
        evil.name = "<img src=x onerror=alert(1)>"
        self.store.record(Sample(system(100.0), [ProcSample(evil, 1.0, 100.0, 1, 1)], [], 0.0, True))
        self.assertEqual(self.q("SELECT name FROM apps")[0][0], "x'); DROP TABLE apps; --")
        self.assertEqual(self.q("SELECT name FROM processes")[0][0], "<img src=x onerror=alert(1)>")


class TestLeaderboard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        self.store = storage.Store(Path(self.tmp.name) / "w.sqlite3", Config())
        self.addCleanup(self.store.conn.close)

    def record(self, ts, entries, plugged=False):
        self.store.record(Sample(system(ts, plugged=plugged),
                                 [ProcSample(info, cpu, cpu * 100, rss, ctx) for info, cpu, rss, ctx in entries],
                                 [], 0.0, True))

    def test_totals_group_by_app_and_split_battery_time(self):
        chrome_a, chrome_b, daemon = proc(1, "Chrome"), proc(2, "Chrome"), proc(3, "backupd")
        self.record(100.0, [(chrome_a, 0.5, 100, 10), (chrome_b, 0.25, 200, 5), (daemon, 0.1, 50, None)], plugged=True)
        self.record(101.0, [(chrome_a, 0.5, 300, 10), (daemon, 0.1, 50, None)], plugged=False)
        self.store.flush()
        self.store.conn.row_factory = sqlite3.Row
        rows = {r["app"]: r for r in storage.leaderboard(self.store.conn, 0, 10)}
        self.assertAlmostEqual(rows["Chrome"]["cpu_seconds"], 1.25)
        self.assertAlmostEqual(rows["Chrome"]["cpu_seconds_on_battery"], 0.5)  # only the unplugged tick
        self.assertEqual(rows["Chrome"]["ctx_switches"], 25)
        self.assertEqual(rows["Chrome"]["ctx_switches_on_battery"], 10)
        self.assertEqual(rows["Chrome"]["peak_rss_bytes"], 300)  # summed per tick, max over time
        self.assertIsNone(rows["Chrome"]["disk_read_bytes"])     # helper-only, still unknown
        self.assertEqual(rows["backupd"]["ctx_switches"], 0)     # macOS hides these

    def test_spike_involvement_counted_per_app(self):
        hog = proc(1, "Chrome")
        self.record(100.0, [(hog, 1.0, 10, 1)])
        self.store.flush()
        spike = self.store.insert_spike("cpu", 100.0, 90.0, 100.0, 10.0, 1.0, 4.0, 3, None)
        other = self.store.insert_spike("disk_read", 100.0, 9e6, 100.0, 1e3, 1.0, 4.0, 3, None)
        Culprit = type("C", (), {})
        direct, correlated = Culprit(), Culprit()
        direct.info, direct.value, direct.share, direct.attribution = hog, 90.0, 0.9, "direct"
        correlated.info, correlated.value, correlated.share, correlated.attribution = hog, 5.0, None, "correlated"
        self.store.replace_culprits(spike, [direct], 100.0)
        self.store.replace_culprits(other, [correlated], 100.0)
        self.store.conn.row_factory = sqlite3.Row
        row = storage.leaderboard(self.store.conn, 0, 10)[0]
        self.assertEqual((row["spikes_caused"], row["spikes_suspected"]), (1, 1))


class TestMigrationV3(unittest.TestCase):
    def test_v2_database_gains_battery_wakeups_column(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "w.sqlite3"
            conn = sqlite3.connect(path)
            conn.executescript(storage.SCHEMA_V1)
            conn.executescript(storage.MIGRATIONS[0][1])
            conn.execute("INSERT INTO apps (id, name) VALUES (1, 'Chrome')")
            conn.execute("""INSERT INTO app_usage_hourly (hour, app_id, cpu_seconds, ctx_switches)
                            VALUES (3600, 1, 42.0, 7)""")
            conn.commit()
            conn.close()
            store = storage.Store(path, Config())
            self.addCleanup(store.conn.close)
            self.assertEqual(store.conn.execute("PRAGMA user_version").fetchone()[0], storage.SCHEMA_VERSION)
            row = store.conn.execute(
                "SELECT cpu_seconds, ctx_switches, ctx_switches_on_battery FROM app_usage_hourly").fetchone()
            self.assertEqual(row, (42.0, 7, 0))  # existing data kept, new column defaults to 0


class TestPlatformHelpers(unittest.TestCase):
    def test_parse_ps_time(self):
        self.assertAlmostEqual(macos.parse_ps_time("886:50.30"), 886 * 60 + 50.30)
        self.assertAlmostEqual(macos.parse_ps_time("0:00.47"), 0.47)
        self.assertAlmostEqual(macos.parse_ps_time("1:02:03"), 3723)
        self.assertAlmostEqual(macos.parse_ps_time("2-01:00:00"), 2 * 86400 + 3600)
        self.assertIsNone(macos.parse_ps_time("garbage"))

    def test_app_name(self):
        exe = ("/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/"
               "Helpers/Google Chrome Helper (Renderer).app/Contents/MacOS/Google Chrome Helper (Renderer)")
        self.assertEqual(macos.app_name(exe, "Google Chrome Helper (Renderer)"), "Google Chrome")
        self.assertEqual(macos.app_name("/usr/sbin/mDNSResponder", "mDNSResponder"), "mDNSResponder")
        self.assertEqual(macos.app_name(None, "kernel_task"), "kernel_task")


if __name__ == "__main__":
    unittest.main()
