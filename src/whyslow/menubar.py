"""macOS menu-bar app (rumps): a live glance plus a few controls.

It runs as its own process and talks to the sampler through the same files the
CLI uses: the read-only database for the glance, the lock/pid file for "is it
running", and the pause marker for pause/resume. Quitting the menu bar leaves
the sampler running.

`build_glance` holds all the logic and touches nothing external, so the
interesting part is testable without a GUI. rumps and AppKit are imported
lazily inside run() so the rest of whyslow works without them.
"""

from __future__ import annotations

import logging
import time
import webbrowser
from dataclasses import dataclass, field

from whyslow import daemon, paths, storage
from whyslow.config import Config
from whyslow.text import safe

log = logging.getLogger("whyslow")

REFRESH_SECONDS = 3.0
TITLE_MAX = 26
SPIKE_LOOKBACK_SECONDS = 3600.0
TOP_PROCESSES = 3
BUSY_PROCESS_PERCENT = 5.0  # below this, show system CPU instead of a "top" process

METRIC_LABELS = {
    "cpu": "CPU", "memory": "Memory", "disk_read": "Disk read", "disk_write": "Disk write",
    "net_recv": "Net in", "net_sent": "Net out",
}


def _fmt_bytes(n: float | None) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "?"


def _fmt_metric(metric: str, value: float) -> str:
    return f"{value:.0f}%" if metric in ("cpu", "memory") else f"{_fmt_bytes(value)}/s"


def _fmt_hours(seconds: float | None) -> str:
    if not seconds:
        return ""
    hours, minutes = int(seconds // 3600), int(seconds % 3600 // 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


@dataclass
class Glance:
    title: str
    lines: list[str] = field(default_factory=list)
    running: bool = True
    paused: bool = False


def build_glance(latest: dict | None, spikes: list[dict], *, running: bool,
                 paused: bool, stale: bool) -> Glance:
    """Everything the menu shows, from the same data the dashboard uses."""
    if not running:
        return Glance("whyslow ⏹", ["Sampler is stopped"], running=False, paused=paused)
    if paused:
        return Glance("whyslow ⏸", ["Sampling is paused"], paused=True)
    if latest is None:
        return Glance("whyslow …", ["Waiting for the first samples…"])

    system = latest["system"]
    procs = [p for p in latest["processes"] if p.get("cpu_percent")]
    lines = [f"CPU {system['cpu_percent']:.0f}%   Memory {system['mem_percent']:.0f}%"]
    for p in procs[:TOP_PROCESSES]:
        lines.append(f"   {safe(p['app'], 28)}  {p['cpu_percent']:.0f}%")
    if not procs:
        lines.append("   nothing busy")

    if system["battery_percent"] is not None:
        state = "on AC" if system["power_plugged"] else "on battery"
        left = _fmt_hours(system["battery_secs_left"])
        lines.append(f"Battery {system['battery_percent']:.0f}%   {state}" + (f", ~{left} left" if left else ""))

    ongoing = next((s for s in spikes if s["ended_at"] is None), None)
    recent = spikes[0] if spikes else None
    if ongoing:
        culprit = ongoing["culprits"][0]["app"] if ongoing["culprits"] else "no visible process"
        lines.append(f"▲ {METRIC_LABELS.get(ongoing['metric'], ongoing['metric'])} spike now: {safe(culprit, 24)}")
    elif recent:
        when = time.strftime("%H:%M", time.localtime(recent["started_at"]))
        lines.append(f"Last spike {when}: {METRIC_LABELS.get(recent['metric'], recent['metric'])} "
                     f"{_fmt_metric(recent['metric'], recent['peak_value'])}")
    else:
        lines.append("No spikes in the last hour")

    if stale:
        return Glance("whyslow …", ["Sampler isn't reporting"] + lines)
    if ongoing:
        title = f"▲ {METRIC_LABELS.get(ongoing['metric'], ongoing['metric'])} " \
                f"{_fmt_metric(ongoing['metric'], ongoing['peak_value'])}"
    elif procs and procs[0]["cpu_percent"] >= BUSY_PROCESS_PERCENT:
        title = f"{safe(procs[0]['app'], 16)} {procs[0]['cpu_percent']:.0f}%"
    else:
        title = f"whyslow {system['cpu_percent']:.0f}%"
    return Glance(safe(title, TITLE_MAX), lines)


def read_state(cfg: Config) -> Glance:
    """Read the current state from disk; never raises."""
    running = daemon.running_pid() is not None
    paused = daemon.is_paused()
    latest, spikes, stale = None, [], True
    try:
        conn = storage.open_readonly(paths.db_path()) if paths.db_path().exists() else None
        if conn is not None:
            try:
                latest = storage.latest(conn)
                spikes = storage.recent_spikes(conn, time.time() - SPIKE_LOOKBACK_SECONDS, 5)
            finally:
                conn.close()
    except Exception:
        log.exception("menu bar could not read the database")
    if latest is not None:
        stale = time.time() - latest["system"]["ts"] > max(5.0, 3 * cfg.sampling.interval_seconds)
    return build_glance(latest, spikes, running=running, paused=paused, stale=stale)


def run(cfg: Config, self_test: float | None = None) -> int:
    """Run the menu-bar app (blocks until Quit).

    `self_test` runs it for that many seconds, prints what AppKit actually put
    in the menu bar, and quits - so the GUI path can be checked from a script.
    """
    try:
        import rumps
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    except ImportError:
        print("The menu bar needs rumps: pip install -r requirements.txt")
        return 1

    # Menu-bar only: no Dock icon, no app switcher entry (rumps doesn't set this itself).
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    app = rumps.App("whyslow", title="whyslow …", quit_button=None)
    info = [rumps.MenuItem("") for _ in range(6)]          # filled by refresh(); no callback = greyed out
    dashboard_item = rumps.MenuItem("Open dashboard")
    pause_item = rumps.MenuItem("Pause sampling")
    sampler_item = rumps.MenuItem("Start sampler")
    quit_item = rumps.MenuItem("Quit menu bar (sampler keeps running)")

    def refresh(_timer=None) -> None:
        glance = read_state(cfg)
        app.title = glance.title
        for item, text in zip(info, glance.lines + [""] * len(info)):
            item.title = text
            (item.show if text else item.hide)()
        pause_item.title = "Resume sampling" if glance.paused else "Pause sampling"
        (pause_item.show if glance.running else pause_item.hide)()
        sampler_item.title = "Stop sampler" if glance.running else "Start sampler"
        (dashboard_item.show if glance.running and cfg.dashboard.enabled else dashboard_item.hide)()

    def on_dashboard(_item) -> None:
        url = daemon.dashboard_login_url(cfg)
        if url:
            webbrowser.open(url)
        else:
            rumps.alert("whyslow", "The dashboard isn't running. Start the sampler first.")

    def on_pause(_item) -> None:
        daemon.set_paused(not daemon.is_paused())
        refresh()

    def on_sampler(_item) -> None:
        if daemon.running_pid():
            try:
                daemon.stop_background()
            except TimeoutError as exc:
                rumps.alert("whyslow", str(exc))
        elif daemon.start_background(None) is None:
            rumps.alert("whyslow", f"Couldn't start the sampler. See {paths.log_dir() / 'whyslow.log'}")
        refresh()

    def on_quit(_item) -> None:
        rumps.quit_application()

    dashboard_item.set_callback(on_dashboard)
    pause_item.set_callback(on_pause)
    sampler_item.set_callback(on_sampler)
    quit_item.set_callback(on_quit)
    app.menu = [*info, None, dashboard_item, None, pause_item, sampler_item, None, quit_item]

    refresh()
    rumps.Timer(refresh, REFRESH_SECONDS).start()

    if self_test:
        def report(_timer) -> None:
            status_item = getattr(app._nsapp, "nsstatusitem", None)
            if status_item is None:
                print("SELF-TEST FAILED: no status item in the menu bar", flush=True)
            else:
                titles = [str(i.title()) for i in status_item.menu().itemArray()]
                print(f"menu bar title: {status_item.title()!r}", flush=True)
                print("menu items: " + " | ".join(t for t in titles if t), flush=True)
            # NSApp.terminate_ exits without flushing Python's buffers, hence flush=True above.
            rumps.quit_application()

        rumps.Timer(report, self_test).start()

    app.run()
    return 0


LAUNCH_AGENT_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{arguments}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <!-- Restart if it crashes, but not after `whyslow stop` or Quit. -->
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>Nice</key>
    <integer>5</integer>
    <key>LowPriorityIO</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{log}</string>
{environment}</dict>
</plist>
"""


def launch_agent_plist(menubar: bool, executable: str, home_override: str | None = None) -> str:
    """A LaunchAgent that starts the sampler (or the menu bar) at login."""
    args = [executable, "menubar"] if menubar else [executable, "run"]
    label = "local.whyslow.menubar" if menubar else "local.whyslow.sampler"
    environment = ""
    if home_override:
        environment = ("    <key>EnvironmentVariables</key>\n    <dict>\n"
                       "        <key>WHYSLOW_HOME</key>\n"
                       f"        <string>{home_override}</string>\n    </dict>\n")
    return LAUNCH_AGENT_TEMPLATE.format(
        label=label,
        arguments="\n".join(f"        <string>{a}</string>" for a in args),
        log=str(paths.log_dir() / "launchd.log"),
        environment=environment,
    )
