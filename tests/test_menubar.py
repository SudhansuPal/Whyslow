import plistlib
import unittest

from whyslow import menubar


def latest(cpu=30.0, mem=60.0, procs=(("Firefox", 82.0),), battery=55.0, plugged=0, secs_left=17000):
    return {
        "system": {"ts": 1000.0, "cpu_percent": cpu, "mem_percent": mem, "battery_percent": battery,
                   "power_plugged": plugged, "battery_secs_left": secs_left},
        "processes": [{"app": app, "name": app, "pid": 1, "cpu_percent": pct, "rss_bytes": 1,
                       "cmdline": None, "visibility": "full"} for app, pct in procs],
    }


def spike(metric="cpu", peak=90.0, ended=2000.0, culprit="Firefox"):
    return {"id": 1, "metric": metric, "started_at": 1000.0, "ended_at": ended, "peak_value": peak,
            "culprits": [{"app": culprit, "rank": 1}] if culprit else []}


def glance(latest_data=None, spikes=(), running=True, paused=False, stale=False):
    return menubar.build_glance(latest_data, list(spikes), running=running, paused=paused, stale=stale)


class TestGlance(unittest.TestCase):
    def test_busy_process_becomes_the_title(self):
        g = glance(latest())
        self.assertEqual(g.title, "Firefox 82%")
        self.assertIn("CPU 30%   Memory 60%", g.lines[0])

    def test_quiet_machine_shows_system_cpu(self):
        g = glance(latest(cpu=4.0, procs=(("Finder", 1.0),)))
        self.assertEqual(g.title, "whyslow 4%")

    def test_ongoing_spike_takes_over_the_title(self):
        g = glance(latest(), [spike(ended=None)])
        self.assertEqual(g.title, "▲ CPU 90%")
        self.assertIn("▲ CPU spike now: Firefox", g.lines)

    def test_ongoing_rate_spike_formats_bytes(self):
        g = glance(latest(), [spike(metric="disk_read", peak=288 * 1024 * 1024, ended=None)])
        self.assertEqual(g.title, "▲ Disk read 288.0 MB/s")

    def test_recent_spike_listed_when_finished(self):
        g = glance(latest(), [spike()])
        self.assertTrue(any(line.startswith("Last spike") and "CPU 90%" in line for line in g.lines))

    def test_no_spikes_says_so(self):
        self.assertIn("No spikes in the last hour", glance(latest()).lines)

    def test_states(self):
        self.assertEqual(glance(latest(), running=False).title, "whyslow ⏹")
        self.assertEqual(glance(latest(), paused=True).title, "whyslow ⏸")
        self.assertEqual(glance(None).title, "whyslow …")
        stale = glance(latest(), stale=True)
        self.assertEqual(stale.title, "whyslow …")
        self.assertEqual(stale.lines[0], "Sampler isn't reporting")

    def test_battery_line(self):
        self.assertIn("Battery 55%   on battery, ~4h43m left", glance(latest()).lines)
        self.assertIn("Battery 55%   on AC", glance(latest(plugged=1, secs_left=None)).lines)
        self.assertFalse(any("Battery" in line for line in glance(latest(battery=None)).lines))

    def test_untrusted_names_are_sanitised_and_capped(self):
        evil = "Ev\x1b[31mil" + "X" * 60
        g = glance(latest(procs=((evil, 99.0),)), [spike(ended=None, culprit=evil)])
        self.assertNotIn("\x1b", g.title)
        self.assertLessEqual(len(g.title), menubar.TITLE_MAX)
        self.assertTrue(all("\x1b" not in line for line in g.lines))

    def test_idle_machine_with_no_busy_processes(self):
        g = glance(latest(cpu=2.0, procs=()))
        self.assertIn("   nothing busy", g.lines)
        self.assertEqual(g.title, "whyslow 2%")


class TestLaunchAgent(unittest.TestCase):
    def test_sampler_plist(self):
        data = plistlib.loads(menubar.launch_agent_plist(False, "/opt/whyslow").encode())
        self.assertEqual(data["Label"], "local.whyslow.sampler")
        self.assertEqual(data["ProgramArguments"], ["/opt/whyslow", "run"])
        self.assertTrue(data["RunAtLoad"])
        self.assertFalse(data["KeepAlive"]["SuccessfulExit"])  # a clean `whyslow stop` stays stopped
        self.assertNotIn("EnvironmentVariables", data)

    def test_menubar_plist_with_home_override(self):
        data = plistlib.loads(menubar.launch_agent_plist(True, "/opt/whyslow", "/tmp/data").encode())
        self.assertEqual(data["Label"], "local.whyslow.menubar")
        self.assertEqual(data["ProgramArguments"], ["/opt/whyslow", "menubar"])
        self.assertEqual(data["EnvironmentVariables"]["WHYSLOW_HOME"], "/tmp/data")


if __name__ == "__main__":
    unittest.main()
