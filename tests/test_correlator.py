import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from whyslow import storage
from whyslow.config import Config, DetectorConfig
from whyslow.correlator import Correlator, SpikeMonitor, metric_specs
from whyslow.sampler import Sample

from helpers import proc, sample, system

SPECS = {s.name: s for s in metric_specs(Config())}


class TestCorrelator(unittest.TestCase):
    def test_cpu_culprits_are_direct_and_ranked(self):
        a, b, c = proc(1, "A"), proc(2, "B"), proc(3, "C")
        s = sample(100.0, [(a, 50.0, 1), (b, 300.0, 1), (c, 0.0, 1)], cpu=50.0)  # 8 cores -> 400 core-%
        culprits = Correlator(1.0).culprits(SPECS["cpu"], s, top_n=5)
        self.assertEqual([x.info for x in culprits], [b, a])  # idle process excluded
        self.assertEqual(culprits[0].attribution, "direct")
        self.assertAlmostEqual(culprits[0].share, 300 / 400)

    def test_memory_culprits_rank_growth_not_size(self):
        big, grower, newcomer = proc(1, "Big"), proc(2, "Grower"), proc(3, "New")
        corr = Correlator(1.0)
        corr.update(sample(100.0, [(big, 0, 4000), (grower, 0, 100)]))
        s = sample(160.0, [(big, 0, 4000), (grower, 0, 900), (newcomer, 0, 300)])
        culprits = corr.culprits(SPECS["memory"], s, top_n=5)
        self.assertEqual([(x.info.app, x.value) for x in culprits], [("Grower", 800.0), ("New", 300.0)])
        self.assertAlmostEqual(culprits[0].share, 800 / 1100)

    def test_io_culprits_are_correlated_cpu_jumps(self):
        steady, jumper = proc(1, "Steady"), proc(2, "Jumper")
        corr = Correlator(1.0)
        for ts in range(100):
            corr.update(sample(float(ts), [(steady, 80.0, 1), (jumper, 1.0, 1)]))
        s = sample(100.0, [(steady, 80.0, 1), (jumper, 40.0, 1)])
        culprits = corr.culprits(SPECS["disk_read"], s, top_n=5)
        # The busier-but-steady process is not blamed; the one that changed is.
        self.assertEqual([x.info.app for x in culprits], ["Jumper"])
        self.assertEqual(culprits[0].attribution, "correlated")
        self.assertIsNone(culprits[0].share)
        self.assertAlmostEqual(culprits[0].value, 39.0, delta=1.0)

    def test_io_culprits_become_measured_when_the_helper_supplies_bytes(self):
        noisy, hog = proc(1, "Noisy"), proc(2, "Backup")
        s = sample(100.0, [(noisy, 90.0, 1), (hog, 1.0, 1)])
        # Without helper data: correlated guess from CPU jumps.
        self.assertEqual(Correlator(1.0).culprits(SPECS["disk_write"], s, 5)[0].attribution, "correlated")
        # With it: the quiet process actually writing the bytes is named, measured.
        s.processes[1].disk_write_bytes = 50_000_000.0
        s.processes[0].disk_write_bytes = 1_000.0
        culprits = Correlator(1.0).culprits(SPECS["disk_write"], s, 5)
        self.assertEqual([c.info.app for c in culprits], ["Backup", "Noisy"])
        self.assertTrue(all(c.attribution == "direct" for c in culprits))
        self.assertAlmostEqual(culprits[0].share, 50_000_000 / 50_001_000)

    def test_measured_io_only_applies_to_its_own_metric(self):
        a = proc(1, "A")
        s = sample(100.0, [(a, 50.0, 1)])
        s.processes[0].net_recv_bytes = 10_000.0
        self.assertEqual(Correlator(1.0).culprits(SPECS["net_recv"], s, 5)[0].attribution, "direct")
        # Nothing measured for disk, so that one still falls back to correlation.
        self.assertEqual(Correlator(1.0).culprits(SPECS["disk_read"], s, 5)[0].attribution, "correlated")


class TestSpikeMonitor(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        cfg = replace(Config(), detector=DetectorConfig(baseline_window_seconds=100, sustain_samples=3))
        self.store = storage.Store(Path(tmp.name) / "w.sqlite3", cfg)
        self.addCleanup(self.store.conn.close)
        self.mon = SpikeMonitor(cfg, self.store)

    def q(self, sql, *args):
        return self.store.conn.execute(sql, args).fetchall()

    def test_cpu_spike_recorded_with_culprits_and_closed(self):
        idle, hog = proc(1, "Idle"), proc(2, "Hog")
        ts = 0.0
        for _ in range(100):
            self.mon.process(sample(ts, [(idle, 5.0, 1), (hog, 1.0, 1)], cpu=10.0))
            ts += 1
        spike_ts = ts
        for _ in range(5):
            self.mon.process(sample(ts, [(idle, 5.0, 1), (hog, 600.0, 1)], cpu=80.0))
            ts += 1
        for _ in range(5):
            self.mon.process(sample(ts, [(idle, 5.0, 1), (hog, 1.0, 1)], cpu=10.0))
            ts += 1

        (spike,) = self.q("SELECT metric, started_at, ended_at, peak_value, baseline FROM spikes")
        self.assertEqual(spike, ("cpu", spike_ts, spike_ts + 4, 80.0, 10.0))
        culprits = self.q("""SELECT c.rank, p.pid, c.attribution FROM spike_culprits c
                             JOIN processes p ON p.id = c.process_id ORDER BY c.rank""")
        self.assertEqual(culprits, [(1, 2, "direct"), (2, 1, "direct")])

    def test_gap_closes_open_spike(self):
        ts = 0.0
        for _ in range(100):
            self.mon.process(sample(ts, [], cpu=10.0))
            ts += 1
        for _ in range(4):
            self.mon.process(sample(ts, [], cpu=90.0))
            ts += 1
        self.mon.process(Sample(system(ts + 3600), [], [], None, False))  # laptop slept
        (ended_at,) = self.q("SELECT ended_at FROM spikes")[0]
        self.assertEqual(ended_at, ts - 1)

    def test_shutdown_closes_open_spike(self):
        ts = 0.0
        for v in [10.0] * 100 + [90.0] * 4:
            self.mon.process(sample(ts, [], cpu=v))
            ts += 1
        self.mon.close()
        self.assertEqual(self.q("SELECT ended_at FROM spikes")[0][0], ts - 1)

    def test_recent_spikes_query(self):
        hog = proc(2, "Hog")
        ts = 0.0
        for v in [10.0] * 100 + [90.0] * 4:
            self.mon.process(sample(ts, [(hog, v * 8, 1)], cpu=v))
            ts += 1
        self.store.conn.row_factory = sqlite3.Row
        (spike,) = storage.recent_spikes(self.store.conn, since=0, limit=10)
        self.assertEqual(spike["metric"], "cpu")
        self.assertIsNone(spike["ended_at"])
        self.assertEqual(spike["culprits"][0]["app"], "Hog")


class TestMigration(unittest.TestCase):
    def test_v1_database_is_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "w.sqlite3"
            conn = sqlite3.connect(path)
            conn.executescript(storage.SCHEMA_V1)
            conn.execute("INSERT INTO spikes (metric, started_at, peak_value, baseline, spread, threshold_k, "
                         "sustain_n) VALUES ('cpu', 1, 2, 3, 4, 5, 6)")
            conn.commit()
            conn.close()
            store = storage.Store(path, Config())
            try:
                self.assertEqual(store.conn.execute("PRAGMA user_version").fetchone()[0], storage.SCHEMA_VERSION)
                row = store.conn.execute("SELECT metric, peak_at, unattributed FROM spikes").fetchone()
                self.assertEqual(row, ("cpu", None, None))
            finally:
                store.conn.close()


if __name__ == "__main__":
    unittest.main()
