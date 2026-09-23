"""`whyslow` command-line interface."""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from whyslow import __version__, battery, config, daemon, helpers, macos, paths, storage
from whyslow.text import safe
from whyslow.sampler import PS, Sample, Sampler


# --- formatting --------------------------------------------------------------

def fmt_bytes(n: float | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "-"


def fmt_rate(n: float | None) -> str:
    return "-" if n is None else fmt_bytes(n) + "/s"


def fmt_duration(secs: float | None) -> str:
    if secs is None:
        return "?"
    secs = int(secs)
    h, m = secs // 3600, secs % 3600 // 60
    return f"{h}h{m:02d}m" if h else f"{m}m"


def render_top(sample: Sample, n: int, sort: str, show_cmd: bool, caps: macos.Capabilities) -> str:
    s = sample.system
    lines = [
        f"whyslow {__version__} — {datetime.fromtimestamp(s.ts):%H:%M:%S}   mode: {caps.mode}",
        (f"CPU {s.cpu_percent:5.1f}% ({s.cpu_count} cores, load {s.load1:.2f})   "
         f"MEM {fmt_bytes(s.mem_used)} / {fmt_bytes(s.mem_total)} ({s.mem_percent:.0f}%)  swap {fmt_bytes(s.swap_used)}"),
        (f"DISK read {fmt_rate(s.disk_read_bps)}  write {fmt_rate(s.disk_write_bps)}   "
         f"NET ↓ {fmt_rate(s.net_recv_bps)}  ↑ {fmt_rate(s.net_sent_bps)}"),
    ]
    if s.battery_percent is not None:
        state = "on AC" if s.power_plugged else f"on battery, ~{fmt_duration(s.battery_secs_left)} left"
        lines.append(f"BATTERY {s.battery_percent:.0f}% ({state})")
    lines.append("")

    key = (lambda p: p.cpu_percent or 0.0) if sort == "cpu" else (lambda p: p.rss or 0)
    rows = sorted(sample.processes, key=key, reverse=True)[:n]
    lines.append(f"{'PID':>6}  {'APP':<22} {'PROCESS':<24} {'CPU%':>6} {'MEM':>9} {'CTX/s':>7}  SRC")
    for p in rows:
        ctx = "-" if p.ctx_switches is None or not s.interval else f"{p.ctx_switches / s.interval:.0f}"
        cpu = "-" if p.cpu_percent is None else f"{p.cpu_percent:.1f}"
        lines.append(
            f"{p.info.pid:>6}  {safe(p.info.app, 22):<22} {safe(p.info.name, 24):<24} "
            f"{cpu:>6} {fmt_bytes(p.rss):>9} {ctx:>7}  {'ps' if p.info.visibility == PS else ''}"
        )
        if show_cmd and p.info.cmdline:
            lines.append(f"{'':>8}{safe(p.info.cmdline, 120)}")
    if sample.unattributed_cpu_percent is not None:
        lines.append(f"{'':>6}  {'(unattributed)':<22} {'kernel_task, <1s procs':<24} "
                     f"{sample.unattributed_cpu_percent:>6.1f}")
    lines.append("")
    lines.append(f"CPU% is per core (100 = one full core). SRC 'ps' = system process read via /bin/ps "
                 f"(no command line). Processes: {sample.counts.get('full', 0)} own, "
                 f"{sample.counts.get('ps', 0)} system, {sample.counts.get('hidden', 0)} hidden.")
    for note in caps.notes():
        lines.append(f"note: {note}")
    lines.append(f"whyslow overhead this tick: {sample.overhead_cpu_ms:.1f} ms CPU")
    return "\n".join(lines)


_RATE_METRICS = {"disk_read", "disk_write", "net_recv", "net_sent"}
_METRIC_LABELS = {"cpu": "CPU", "memory": "MEMORY", "disk_read": "DISK READ", "disk_write": "DISK WRITE",
                  "net_recv": "NET IN", "net_sent": "NET OUT"}


def fmt_metric(metric: str, value: float | None) -> str:
    if value is None:
        return "-"
    return fmt_rate(value) if metric in _RATE_METRICS else f"{value:.1f}%"


def fmt_culprit(metric: str, c: dict) -> str:
    if c["attribution"] == "correlated":
        what = f"+{c['value']:.1f}% CPU vs its usual"
    elif metric == "memory":
        what = f"+{fmt_bytes(c['value'])} RSS"
    else:
        what = f"{c['value']:.1f}% CPU"
    share = f" ({c['share']:.0%})" if c["share"] is not None else ""
    return what + share


def render_spikes(spikes: list[dict], show_cmd: bool) -> str:
    if not spikes:
        return "No spikes recorded in this period."
    lines = []
    for s in spikes:
        start = datetime.fromtimestamp(s["started_at"])
        if s["ended_at"] is None:
            duration = "ongoing"
        else:
            duration = f"{max(1, round(s['ended_at'] - s['started_at']))}s"
        m = s["metric"]
        lines.append(
            f"{start:%Y-%m-%d %H:%M:%S}  {_METRIC_LABELS.get(m, m):<10} peak {fmt_metric(m, s['peak_value'])}"
            f"  (normal {fmt_metric(m, s['baseline'])})  {duration}"
        )
        if s["culprits"] and s["culprits"][0]["attribution"] == "correlated":
            lines.append("    culprits are CORRELATED, not measured: processes whose CPU jumped when this began")
        for c in s["culprits"]:
            name = safe(c["app"], 24)
            proc = "" if c["name"] == c["app"] else f" / {safe(c['name'], 30)}"
            lines.append(f"    {c['rank']}. {name}{proc} (pid {c['pid']})  {fmt_culprit(m, c)}")
            if show_cmd and c["cmdline"]:
                lines.append(f"         {safe(c['cmdline'], 110)}")
        if not s["culprits"]:
            lines.append("    no visible process stood out")
        if m == "cpu" and s["unattributed"]:
            lines.append(f"    + {s['unattributed']:.1f}% CPU unattributed (kernel_task / short-lived processes)")
        lines.append("")
    return "\n".join(lines).rstrip()


_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)([smhd])$")


def parse_duration(text: str) -> float:
    m = _DURATION_RE.match(text.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError("use a duration like 90s, 30m, 6h or 7d")
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def fmt_wakeups(n: int) -> str:
    # macOS doesn't report context switches for other users' processes, so 0 here
    # means "not visible", not "never woke up".
    return f"{n:,}" if n else "–"


def fmt_cpu_time(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} h"


def render_leaderboard(rows: list[dict], since_label: str, sort: str) -> str:
    if not rows:
        return "No usage recorded yet. Run `whyslow start` and let it collect for a while."
    lines = [f"Offenders over the {since_label}, grouped by app (every process counted).", ""]
    has_io = any(r["disk_read_bytes"] or r["disk_write_bytes"] or
                 r["net_recv_bytes"] or r["net_sent_bytes"] for r in rows)
    io_head = f" {'~DISK R/W':>17} {'~NET IN/OUT':>17}" if has_io else ""
    lines.append(f"{'APP':<26} {'CPU TIME':>9} {'ON BATT':>9} {'WAKEUPS':>10} {'PEAK MEM':>9}{io_head}  SPIKES")
    for r in rows:
        spikes = []
        if r["spikes_caused"]:
            spikes.append(f"{r['spikes_caused']} caused")
        if r["spikes_suspected"]:
            spikes.append(f"{r['spikes_suspected']} suspected")
        io = ""
        if has_io:
            io = (f" {fmt_bytes(r['disk_read_bytes']) + '/' + fmt_bytes(r['disk_write_bytes']):>17}"
                  f" {fmt_bytes(r['net_recv_bytes']) + '/' + fmt_bytes(r['net_sent_bytes']):>17}")
        lines.append(
            f"{safe(r['app'], 26):<26} {fmt_cpu_time(r['cpu_seconds']):>9} "
            f"{fmt_cpu_time(r['cpu_seconds_on_battery']):>9} {fmt_wakeups(r['ctx_switches']):>10} "
            f"{fmt_bytes(r['peak_rss_bytes']):>9}{io}  {', '.join(spikes) or '-'}"
        )
    lines += ["", "CPU TIME is CPU-seconds consumed (a process pinning one core for a minute = 60s).",
              "WAKEUPS are context switches, a proxy for how often a process stirs the CPU;",
              "'–' means macOS hides them (system processes) or none were recorded yet."]
    lines.append("~DISK and ~NET are extrapolated from 1-second powermetrics samples: approximate."
                 if has_io else
                 "Per-app disk and network totals need the powermetrics helper (`whyslow helpers`).")
    lines.append(f"Sorted by {sort}.")
    return "\n".join(lines)


def render_battery(est: battery.Estimate, since_label: str) -> str:
    m = est.model
    measured = est.basis == "energy"
    headline = ("Battery drain over the %s — ranking from measured energy impact (powermetrics); "
                "the split below is still an estimate." if measured
                else "Battery drain over the %s — ESTIMATE, not a measurement.")
    lines = [headline % since_label, ""]
    if not est.sessions:
        return lines[0] + "\n\nNo time on battery recorded yet."
    lines.append(f"On battery for {est.hours_on_battery:.1f} h across {len(est.sessions)} session(s); "
                 f"{est.drained_percent:.0f}% of charge used "
                 f"({est.drained_percent / max(est.hours_on_battery, 0.01):.1f}%/hour).")
    if not m.usable:
        lines += ["", f"Can't separate baseline drain from CPU-driven drain: {m.note}.",
                  "Ranking by energy score only (CPU time on battery + wakeup cost):", ""]
    else:
        lines += ["",
                  f"  {est.baseline_percent:5.1f}%  screen, radios, kernel and idle draw "
                  f"({m.baseline_per_hour:.1f}%/hour baseline) — not attributable to any process",
                  f"  {est.cpu_percent_of_drain:5.1f}%  CPU-driven, split below "
                  f"({m.per_cpu_point_per_hour:.2f}%/hour per point of CPU; {m.note})", ""]
    lines.append(f"{'APP':<26} {'EST. BATTERY':>13} {'SHARE':>7} {'CPU ON BATT':>12} {'WAKEUPS':>12}"
                 + ("  ENERGY" if measured else ""))
    for a in est.apps:
        percent = f"{a.percent:.1f}%" if a.percent is not None else "-"
        lines.append(f"{safe(a.app, 26):<26} {percent:>13} {a.share:>6.0%} "
                     f"{fmt_cpu_time(a.cpu_seconds):>12} {fmt_wakeups(a.wakeups):>12}"
                     + (f"  {a.score:.0f}" if a.measured else ""))
    lines += ["", "How this works: whyslow fits drain-rate = baseline + k x CPU from your own",
              "battery-percent drops, then splits only the CPU-driven part between apps."]
    lines.append("Apps are ranked by macOS energy impact (powermetrics)." if measured
                 else "Apps are ranked by CPU time on battery plus a small charge per wakeup; "
                      "enable the\npowermetrics helper (`whyslow helpers`) for measured energy instead.")
    return "\n".join(lines)


# --- commands ----------------------------------------------------------------

def _capabilities(cfg: config.Config) -> macos.Capabilities:
    state = storage.helper_state(paths.db_path())
    state.setdefault("enabled", cfg.helpers.powermetrics)
    return macos.capabilities(cfg.sampling.system_process_visibility, state)


def cmd_top(cfg: config.Config, args: argparse.Namespace) -> int:
    caps = _capabilities(cfg)
    sampler = Sampler(cfg)
    sampler.tick()  # baseline: CPU and I/O are deltas, so the first tick has no rates
    try:
        while True:
            time.sleep(args.interval)
            out = render_top(sampler.tick(), args.n, args.sort, args.cmd, caps)
            if args.watch:
                sys.stdout.write("\033[H\033[2J")
            print(out, flush=True)
            if not args.watch:
                return 0
    except KeyboardInterrupt:
        return 0


def cmd_spikes(cfg: config.Config, args: argparse.Namespace) -> int:
    conn = _read_only()
    if conn is None:
        return 0
    try:
        spikes = storage.recent_spikes(conn, time.time() - args.since, args.n)
    finally:
        conn.close()
    print(render_spikes(spikes, args.cmd))
    return 0


def _read_only():
    """Open the database read-only. None means "no data yet"; a real problem exits 1."""
    if not paths.db_path().exists():
        print("No data yet. Start the sampler with `whyslow start`.")
        return None
    try:
        return storage.open_readonly(paths.db_path())
    except RuntimeError as exc:
        print(f"whyslow: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


def cmd_offenders(cfg: config.Config, args: argparse.Namespace) -> int:
    conn = _read_only()
    if conn is None:
        return 0
    try:
        rows = storage.leaderboard(conn, time.time() - args.since, 200)
    finally:
        conn.close()
    key = {"cpu": lambda r: r["cpu_seconds"], "battery": lambda r: r["cpu_seconds_on_battery"],
           "memory": lambda r: r["peak_rss_bytes"], "wakeups": lambda r: r["ctx_switches"]}[args.sort]
    rows.sort(key=key, reverse=True)
    print(render_leaderboard(rows[: args.n], args.since_label, args.sort))
    return 0


def cmd_battery(cfg: config.Config, args: argparse.Namespace) -> int:
    conn = _read_only()
    if conn is None:
        return 0
    since = time.time() - args.since
    try:
        samples = storage.battery_samples(conn, since)
        usage = storage.leaderboard(conn, since, 200)
    finally:
        conn.close()
    if not samples:
        print("No battery readings recorded (no battery, or no data yet).")
        return 0
    print(render_battery(battery.estimate(samples, usage, args.n), args.since_label))
    return 0


def cmd_helpers(cfg: config.Config, args: argparse.Namespace) -> int:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "<your-user>"
    if args.sudoers:
        print(helpers.sudoers_line(user))
        print("\n# Install it with visudo (never edit sudoers with a plain editor):",
              "#   sudo visudo -f /etc/sudoers.d/whyslow",
              "# Then enable it in your whyslow config:",
              "#   [helpers]",
              "#   powermetrics = true",
              sep="\n", file=sys.stderr)
        return 0

    state = storage.helper_state(paths.db_path())
    print(f"powermetrics helper: {'enabled' if cfg.helpers.powermetrics else 'disabled'} in config"
          f" ([helpers] powermetrics), one {helpers.SAMPLE_WINDOW_MS / 1000:g}s sample every"
          f" {cfg.helpers.interval_seconds:g}s")
    print("command:  " + " ".join(helpers.PROBE_COMMAND))
    if state:
        available = state.get("available")
        status = {True: "working", False: "not working", None: "starting"}.get(available, "unknown")
        print(f"sampler reports: {status}" + (f" — {state['error']}" if state.get("error") else ""))
        if state.get("probes"):
            print(f"probes so far: {state['probes']}")
    if not args.check:
        print("\nRun `whyslow helpers --check` to test it now, or `--sudoers` for the rule to install.")
        return 0

    print("\nProbing (sudo -n: it will never ask for your password)…")
    try:
        snapshot = helpers.probe(span_seconds=cfg.helpers.interval_seconds)
    except helpers.HelperError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        if exc.needs_sudoers:
            print("\nAdd the sudoers rule first:\n  " + helpers.sudoers_line(user), file=sys.stderr)
            print("  (install with: sudo visudo -f /etc/sudoers.d/whyslow)", file=sys.stderr)
        return 1
    print(f"ok: {len(snapshot.by_pid)} processes reported over {snapshot.window_seconds:.2f}s")
    ranked = sorted(snapshot.by_pid.values(),
                    key=lambda m: (m.energy_impact or 0, (m.disk_read_bytes or 0) + (m.disk_write_bytes or 0)),
                    reverse=True)[:5]
    for m in ranked:
        print(f"  pid {m.pid:>6}  energy {m.energy_impact if m.energy_impact is not None else '-'}"
              f"  disk r/w {fmt_bytes(m.disk_read_bytes)}/{fmt_bytes(m.disk_write_bytes)}"
              f"  net in/out {fmt_bytes(m.net_recv_bytes)}/{fmt_bytes(m.net_sent_bytes)}")
    unknown = [k for k in snapshot.task_keys if k not in
               {key for keys in helpers._KEYS.values() for key in keys} | {"pid", "name"}]
    print(f"\nkeys powermetrics reported: {', '.join(snapshot.task_keys) or 'none'}")
    if unknown:
        print(f"(not used by whyslow: {', '.join(unknown)})")
    return 0


def cmd_menubar(cfg: config.Config, args: argparse.Namespace) -> int:
    from whyslow import menubar

    if not args.no_sampler and daemon.running_pid() is None:
        if daemon.start_background(args.config) is None:
            print(f"whyslow: the sampler failed to start; see {paths.log_dir() / 'whyslow.log'}", file=sys.stderr)
        else:
            print("whyslow sampler started.")
    if not args.self_test:
        print("whyslow is in your menu bar. Quit it from that menu (the sampler keeps running).")
    return menubar.run(cfg, args.self_test)


def cmd_launchagent(cfg: config.Config, args: argparse.Namespace) -> int:
    from whyslow import menubar

    executable = str(Path(sys.argv[0]).resolve()) if Path(sys.argv[0]).name == "whyslow" else "whyslow"
    plist = menubar.launch_agent_plist(args.menubar, executable, os.environ.get("WHYSLOW_HOME"))
    name = "local.whyslow.menubar" if args.menubar else "local.whyslow.sampler"
    print(plist)
    print(f"# Save as ~/Library/LaunchAgents/{name}.plist, then:", file=sys.stderr)
    print(f"#   launchctl load ~/Library/LaunchAgents/{name}.plist", file=sys.stderr)
    print(f"# Stop auto-start: launchctl unload ~/Library/LaunchAgents/{name}.plist", file=sys.stderr)
    return 0


def cmd_run(cfg: config.Config, args: argparse.Namespace) -> int:
    return daemon.run(cfg, foreground=not args.background)


def cmd_start(cfg: config.Config, args: argparse.Namespace) -> int:
    pid = daemon.running_pid()
    if pid:
        print(f"whyslow is already running (pid {pid}).")
        return 0
    pid = daemon.start_background(args.config)
    if pid is None:
        print(f"whyslow failed to start; see {paths.log_dir() / 'whyslow.log'}", file=sys.stderr)
        return 1
    print(f"whyslow sampler started (pid {pid}, every {cfg.sampling.interval_seconds:g}s).")
    print(f"data: {paths.db_path()}")
    if cfg.dashboard.enabled:
        url = daemon.dashboard_login_url(cfg)
        if url is None:
            print(f"dashboard failed to start; see {paths.log_dir() / 'whyslow.log'}", file=sys.stderr)
        else:
            print(f"dashboard: http://127.0.0.1:{cfg.dashboard.port}/  (open with `whyslow dashboard`)")
            if cfg.dashboard.open_browser:
                webbrowser.open(url)
    return 0


def cmd_dashboard(cfg: config.Config, args: argparse.Namespace) -> int:
    if not cfg.dashboard.enabled:
        print("The dashboard is disabled ([dashboard] enabled = false).", file=sys.stderr)
        return 1
    url = daemon.dashboard_login_url(cfg)
    if url is None:
        print("The sampler isn't running (or its dashboard failed to start). Run `whyslow start`.",
              file=sys.stderr)
        return 1
    if args.print:
        print(url)
        print("This login link works once, within 2 minutes.", file=sys.stderr)
    else:
        webbrowser.open(url)
        print(f"Opened http://127.0.0.1:{cfg.dashboard.port}/ in your browser.")
    return 0


def cmd_stop(cfg: config.Config, args: argparse.Namespace) -> int:
    try:
        stopped = daemon.stop_background()
    except TimeoutError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("whyslow stopped." if stopped else "whyslow is not running.")
    return 0


def cmd_pause(cfg: config.Config, args: argparse.Namespace) -> int:
    daemon.set_paused(True)
    where = "sampling paused" if daemon.running_pid() else "sampling will start paused"
    print(f"{where}. Resume with `whyslow resume`.")
    return 0


def cmd_resume(cfg: config.Config, args: argparse.Namespace) -> int:
    daemon.set_paused(False)
    print("sampling resumed." if daemon.running_pid() else "pause cleared (the sampler isn't running).")
    return 0


def cmd_status(cfg: config.Config, args: argparse.Namespace) -> int:
    pid = daemon.running_pid()
    caps = _capabilities(cfg)
    paused = " (paused)" if daemon.is_paused() else ""
    print(f"sampler:  {'running (pid %d)' % pid if pid else 'stopped'}{paused}")
    if pid and cfg.dashboard.enabled and paths.token_path().exists():
        print(f"dashboard: http://127.0.0.1:{cfg.dashboard.port}/  (open with `whyslow dashboard`)")
    print(f"mode:     {caps.mode}")
    for note in caps.notes():
        print(f"          - {note}")
    cfg_path = args.config or paths.config_path()
    print(f"config:   {cfg_path}{'' if cfg_path.exists() else ' (not present; using defaults)'}")
    db = paths.db_path()
    st = storage.stats(db)
    if st is None:
        print(f"data:     {db} (none yet)")
        return 0
    size = sum(p.stat().st_size for p in db.parent.glob(db.name + "*") if p.is_file())
    print(f"data:     {db} ({fmt_bytes(size)})")
    if st["oldest"]:
        span = fmt_duration(st["newest"] - st["oldest"])
        print(f"range:    {datetime.fromtimestamp(st['oldest']):%Y-%m-%d %H:%M} → "
              f"{datetime.fromtimestamp(st['newest']):%Y-%m-%d %H:%M:%S} ({span})")
    print("rows:     " + ", ".join(f"{k}={v}" for k, v in st.items() if k not in ("oldest", "newest")))
    print(f"logs:     {paths.log_dir()}")
    return 0


def cmd_wipe(cfg: config.Config, args: argparse.Namespace) -> int:
    print(f"This permanently deletes all collected data:\n  {paths.db_path()} (+ -wal/-shm)\n"
          f"  {paths.log_dir()}/whyslow.log*\nYour config file is kept.")
    if not args.yes:
        if not sys.stdin.isatty():
            print("Refusing to wipe without --yes when not interactive.", file=sys.stderr)
            return 1
        if input("Type 'wipe' to confirm: ").strip() != "wipe":
            print("Aborted.")
            return 1
    was_running = daemon.stop_background() if daemon.running_pid() else False
    removed = storage.wipe_files()
    print(f"Deleted {len(removed)} file(s).")
    if was_running:
        print("The sampler was stopped for the wipe; run `whyslow start` to resume.")
    return 0


def cmd_config(cfg: config.Config, args: argparse.Namespace) -> int:
    path = args.config or paths.config_path()
    print(f"# effective configuration ({path}{'' if path.exists() else ': not present, defaults'})\n")
    print(config.as_toml(cfg))
    return 0


# --- entry point -------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="whyslow", description="Why Is This Slow? — a local system detective for macOS.")
    parser.add_argument("--version", action="version", version=f"whyslow {__version__}")
    parser.add_argument("--config", type=Path, help="config file (default: %(default)s)", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("top", help="print the current top resource consumers")
    p.add_argument("-n", type=int, default=15, help="rows to show (default 15)")
    p.add_argument("--sort", choices=("cpu", "mem"), default="cpu")
    p.add_argument("--interval", type=float, default=1.0, help="measurement window in seconds (default 1)")
    p.add_argument("--watch", action="store_true", help="refresh continuously until Ctrl-C")
    p.add_argument("--cmd", action="store_true", help="also show (redacted) command lines")
    p.set_defaults(func=cmd_top)

    p = sub.add_parser("spikes", help="list recent spikes and the processes behind them")
    p.add_argument("--since", type=parse_duration, default=86400.0, help="look back this far, e.g. 30m, 6h, 7d (default 1d)")
    p.add_argument("-n", type=int, default=20, help="max spikes to show (default 20)")
    p.add_argument("--cmd", action="store_true", help="also show (redacted) command lines")
    p.set_defaults(func=cmd_spikes)

    p = sub.add_parser("offenders", help="cumulative per-app resource use (the leaderboard)")
    p.add_argument("--since", type=parse_duration, default=7 * 86400.0, help="look back this far (default 7d)")
    p.add_argument("-n", type=int, default=15, help="apps to show (default 15)")
    p.add_argument("--sort", choices=("cpu", "battery", "memory", "wakeups"), default="cpu")
    p.set_defaults(func=cmd_offenders)

    p = sub.add_parser("battery", help="estimate which apps drained the battery")
    p.add_argument("--since", type=parse_duration, default=7 * 86400.0, help="look back this far (default 7d)")
    p.add_argument("-n", type=int, default=10, help="apps to show (default 10)")
    p.set_defaults(func=cmd_battery)

    p = sub.add_parser("start", help="start the background sampler")
    p.set_defaults(func=cmd_start)
    p = sub.add_parser("dashboard", help="open the localhost dashboard in your browser")
    p.add_argument("--print", action="store_true", help="print a one-time login URL instead of opening it")
    p.set_defaults(func=cmd_dashboard)
    p = sub.add_parser("helpers", help="status of the optional sudo-gated powermetrics helper")
    p.add_argument("--sudoers", action="store_true", help="print the sudoers rule to install")
    p.add_argument("--check", action="store_true", help="run one probe now (never prompts for a password)")
    p.set_defaults(func=cmd_helpers)

    p = sub.add_parser("menubar", help="run the macOS menu-bar app (Ctrl-C or its Quit item to exit)")
    p.add_argument("--no-sampler", action="store_true", help="don't start the sampler if it isn't running")
    p.add_argument("--self-test", type=float, default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_menubar)

    p = sub.add_parser("launchagent", help="print a launchd plist to start whyslow at login")
    p.add_argument("--menubar", action="store_true", help="start the menu-bar app instead of just the sampler")
    p.set_defaults(func=cmd_launchagent)

    p = sub.add_parser("pause", help="pause sampling without stopping the sampler")
    p.set_defaults(func=cmd_pause)
    p = sub.add_parser("resume", help="resume sampling after `whyslow pause`")
    p.set_defaults(func=cmd_resume)
    p = sub.add_parser("stop", help="stop the background sampler")
    p.set_defaults(func=cmd_stop)
    p = sub.add_parser("status", help="show sampler state, capabilities and stored data")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("run", help="run the sampler in the foreground (Ctrl-C to stop)")
    p.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("wipe", help="delete ALL collected data and logs")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(func=cmd_wipe)
    p = sub.add_parser("config", help="print the effective configuration")
    p.set_defaults(func=cmd_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)  # everything we create is private to this user
    macos.check_supported()
    args = build_parser().parse_args(argv)
    if args.config:
        os.environ["WHYSLOW_CONFIG"] = str(args.config)
    if hasattr(args, "since"):
        hours = args.since / 3600
        args.since_label = f"last {hours / 24:.0f} days" if hours >= 48 else (
            f"last {hours:.0f} hours" if hours >= 2 else f"last {args.since / 60:.0f} minutes")
    if getattr(args, "interval", 1.0) <= 0 or getattr(args, "n", 1) <= 0:
        print("whyslow: -n and --interval must be positive", file=sys.stderr)
        return 2
    try:
        cfg = config.load(paths.config_path())
    except config.ConfigError as exc:
        print(f"whyslow: config error: {exc}", file=sys.stderr)
        return 2
    return args.func(cfg, args)
