# whyslow — "Why Is This So Slow?"

A local, real-time system detective for macOS. It samples CPU, memory, disk,
network and battery once a second, detects spikes and attributes each one to
the processes responsible, and (M4) keeps a long-term leaderboard of offenders,
such as the background process that quietly drains your battery.

It runs as your normal user, stores everything in one local SQLite file, and
makes no network connections.

> **Status: all six milestones are done** — sampler, storage, CLI, spike
> detection and attribution, localhost dashboard, offenders leaderboard,
> battery estimate, menu-bar app, and the optional sudo-gated helper.

**New here? [MANUAL.md](MANUAL.md)** is the user guide: every command, how to
read the output, the optional sudo feature, troubleshooting, and an honest list
of what isn't verified. This README covers the same ground from the design side.

## Install

Requires macOS (Apple Silicon or Intel) and Python 3.11+.

```sh
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

## Use

```sh
whyslow top              # one-shot: current top consumers (1 s measurement window)
whyslow top --watch      # refresh continuously (Ctrl-C to exit)
whyslow top --sort mem --cmd   # sort by memory, show (redacted) command lines
whyslow spikes           # spikes in the last day and the processes behind them
whyslow spikes --since 2h --cmd
whyslow offenders        # who used the most over the last week, grouped by app
whyslow offenders --sort battery --since 30d
whyslow battery          # estimate of what drained the battery
whyslow start            # start the background sampler (+ dashboard, opens your browser)
whyslow dashboard        # open the dashboard again (one-time sign-in link)
whyslow status           # running? what's visible? how much data?
whyslow pause            # stop collecting, keep the sampler and dashboard alive
whyslow resume
whyslow stop             # stop it
whyslow menubar          # run the macOS menu-bar app
whyslow launchagent      # print a launchd plist to start whyslow at login
whyslow helpers          # status of the optional sudo helper (--sudoers, --check)
whyslow run              # run the sampler in the foreground (for debugging)
whyslow config           # print the effective configuration
whyslow wipe             # delete ALL collected data and logs (asks first)
```

`CPU%` is per core, like Activity Monitor: 100% means one core fully busy, so
the total can exceed 100% on a multi-core Mac.

## What macOS lets us see (and what it doesn't)

| Metric | Without sudo | How |
|---|---|---|
| System CPU, memory, swap, load | ✅ | psutil |
| System disk read/write, network in/out | ✅ totals only | psutil (loopback excluded) |
| Battery %, charging state, time left | ✅ | psutil / IOKit |
| Per-process CPU, memory — **your** processes | ✅ + command line, context switches | psutil |
| Per-process CPU, memory — **other users'** processes (root, `_windowserver`: WindowServer, mds_stores, backupd, …) | ✅ CPU + memory only | Apple's `/bin/ps` (see below) |
| `kernel_task` (PID 0) | ❌ | Shown as **"unattributed"** CPU |
| Processes that live less than one sample | ⚠️ partial | Counted if born between samples; otherwise "unattributed" |
| Per-process disk I/O | ❌ | `psutil` has no `io_counters()` on macOS. Optional `powermetrics` helper (sudo) |
| Per-process network | ❌ | Optional `powermetrics` helper (sudo) |
| Per-process energy impact | ❌ | Optional `powermetrics` helper (sudo) |

**About `/bin/ps`.** As a normal user, psutil is denied CPU and memory for
roughly 40% of processes on a typical Mac: everything owned by root or system
users, which includes many classic culprits. `/bin/ps` ships with macOS,
is signed by Apple and is setuid root, so it can report cumulative CPU time
and RSS for every process. whyslow runs it with a fixed absolute path, a list
argv (no shell), a scrubbed environment and a timeout, only for the PIDs psutil
couldn't read. whyslow never gains privileges itself. Their command lines stay
hidden. Turn this off with `system_process_visibility = false`; those processes
then count as "unattributed".

**"Unattributed" CPU** = system CPU − the sum of every visible process. It is
mostly `kernel_task` (thermal management, interrupts, heavy I/O) plus processes
too short-lived to be sampled. A large unattributed share is itself a clue.

The header of `whyslow top` and `whyslow status` always shows the active mode
(`standard (no sudo)` today) and exactly which metrics are missing.

## Spike detection

While the sampler runs, six metrics are watched: CPU, memory, disk read,
disk write, network in and network out. For each one the sampler keeps a
rolling baseline over the last `baseline_window_seconds` (default 5 min): the
**median** and the **MAD** (median absolute deviation, scaled to be comparable
to a standard deviation). A **spike** is a value above

    median + max(k × spread, min_delta)

for `sustain_samples` samples in a row (defaults: k = 4, 3 samples). The
per-metric `min_delta` floor (CPU 15 points, memory 5 points, disk 20 MB/s,
network 1 MB/s) stops a flat metric such as an idle disk, with spread 0, from
"spiking" on every blip. Detection starts after a 60-sample warm-up.

The threshold is frozen when a spike starts. The spike ends when the metric has
been back under it for `sustain_samples` samples, or when the load has lasted
so long (about half the window) that it has become the new normal. Values are
clipped at the threshold before they enter the baseline, so a spike can't
raise its own bar. Sleep and wake, or pausing the sampler, closes any open
spike.

**Who gets blamed:**

| Spike | Attribution | Culprit value |
|---|---|---|
| CPU | **direct**: processes ranked by CPU at the moment. CPU that no visible process accounts for is shown as *unattributed* (kernel_task, short-lived processes) | % CPU, share of all CPU in use |
| Memory | **direct**: processes ranked by memory **growth** over the last ~minute, not by size | bytes grown, share of total growth |
| Disk, network | **correlated**: macOS doesn't expose per-process disk or network without sudo, so whyslow lists processes whose CPU jumped above their own recent norm when the spike began. Treat this as a lead, not proof; it's labelled as such in the output | % CPU above that process's usual |

Culprits are captured when a spike starts and refreshed at each new peak.
Tune sensitivity in `[detector]` (see `config.example.toml`). On a busy
8-core machine, two fully busy cores (+~17 points of system CPU) did **not**
count as a spike at k = 4; six busy cores did. Lower `threshold_k` or
`cpu_min_delta_percent` for more sensitivity. Long-running single-process hogs
are the job of the leaderboard (M4), not spike detection.

For CPU % and memory %, which can't exceed 100, the noise margin
`k × spread` is capped at half the remaining headroom. So a jump halfway from
normal to fully saturated always counts, even when the baseline is noisy.
(Without this cap, a busy machine pushed the CPU threshold above 100% and a
35% → 98% load went undetected.)

## Dashboard

`whyslow start` runs the dashboard inside the sampler process at
`http://127.0.0.1:8765/` and opens it. `whyslow dashboard` opens it again later.

* **Stat tiles** for current CPU, memory, disk, network and battery. A tile shows
  "▲ Spike" while one of its metrics is spiking.
* **Live charts**, one per resource: CPU %, memory %, disk read/write, network
  in/out, battery. Time ranges from 5 minutes to 24 hours. Longer ranges are
  bucketed: each point is the **peak** of its bucket for CPU, disk and network,
  so short spikes survive zooming out, and the average for memory and battery.
  Gaps (sampler off, Mac asleep) show as breaks.
* **Spike timeline**: spikes are shaded on their charts and listed below. Click
  a band or a row to see the culprits, whether each was **measured** or
  **correlated**, their share, and redacted command lines.
* **Top processes right now**, with whether each is yours or a system process
  read through `/bin/ps`.
* **Offenders** — the leaderboard above, sortable by CPU time, time on battery,
  wakeups or memory.
* **Battery drain** — the estimate above, showing the non-attributable baseline
  separately from the part split among apps.
* The header always shows the mode (`standard (no sudo)`), and "What this mode
  can't see" lists the blind spots.

It follows your system's light or dark setting, works at phone width, and
pauses polling while its tab is hidden. Set `[dashboard] enabled = false` to
run the sampler without it.

**How sign-in works.** The server listens on 127.0.0.1 only, but loopback is
shared by every account and program on the Mac, so the API also needs a token:

1. Each sampler start generates a random session token, kept in memory and in
   a 0600 file.
2. `whyslow start` / `whyslow dashboard` open `http://127.0.0.1:8765/#login=CODE`,
   where CODE is **single-use, expires in 2 minutes** and is HMAC-signed with the
   token. The page trades it for the token and keeps that in the tab's
   `sessionStorage`.
3. Why not put the token itself in the URL? Opening a URL passes it through the
   argv of `open`, and on macOS **any local user can read any process's argv**
   via the setuid `/bin/ps`. A used-up, short-lived code is worthless to them.
   The `#fragment` is never sent to the server or written to history.

Restarting the sampler rotates the token; open tabs then ask you to run
`whyslow dashboard` again.

## Offenders leaderboard

`whyslow offenders` totals usage per **app** (all of Chrome's helpers count as
"Chrome") over a window, by default the last 7 days:

* **CPU time** — CPU-seconds consumed. A process pinning one core for a minute
  is 60s. Totals come from the hourly rollups, which count **every** process,
  not just the ones stored each second.
* **On battery** — the part of that CPU time spent unplugged.
* **Wakeups** — context switches, a proxy for how often something stirs the CPU.
  `–` means macOS hides them (system processes) rather than "none".
* **Peak memory** — the app's biggest total resident size.
* **Spikes** — how many spikes the app *caused* (measured) or is *suspected* of
  (correlated).

Sort with `--sort cpu|battery|memory|wakeups`. Per-app disk and network totals
stay empty unless the `powermetrics` helper is on (see below).

## Battery drain estimate

`whyslow battery` answers "what's draining my battery?" — as an **estimate**,
labelled as one everywhere it appears.

Most drain isn't any process's fault: the display, Wi-Fi, Bluetooth and the
kernel draw power regardless. So whyslow doesn't split 100% of the drain among
apps. It fits, from your own samples,

    drain (%/hour) = baseline + k × system CPU

using each **1% drop** of the battery reading as a data point (macOS reports
whole-number percentages, so the drops are the precise events; fixed-width
buckets would mostly measure rounding error). Then:

* `baseline × hours` is reported as **not attributable to any process**;
* `k × CPU × hours` is the CPU-driven part, and only that is split among apps,
  in proportion to `CPU seconds on battery + a small charge per wakeup`.

Wakeups are in the score because pulling the CPU out of idle costs energy even
when the work is tiny — which is exactly how a mostly-idle background process
drains a battery overnight.

If there isn't enough time on battery, or drain simply doesn't track CPU (a
bright screen on an idle Mac), whyslow says so and falls back to ranking by
score with no percentages. Real per-process energy numbers need `powermetrics`
(sudo), which is the optional helper described below.

## Menu bar

`whyslow menubar` puts a live glance in the menu bar (and starts the sampler if
it isn't running; `--no-sampler` skips that). It's a separate process from the
sampler and talks to it through the same files the CLI uses, so **quitting the
menu bar leaves the sampler running**.

The title shows whichever matters most right now:

| Title | Meaning |
|---|---|
| `Firefox 82%` | the busiest app and its CPU (per core) |
| `▲ CPU 90%` / `▲ Disk read 288.0 MB/s` | a spike is happening now |
| `whyslow 4%` | nothing is busy, so system CPU |
| `whyslow ⏸` / `whyslow ⏹` / `whyslow …` | paused / stopped / not reporting |

The menu lists system CPU and memory, the top three apps, battery state and
the current or most recent spike, plus: **Open dashboard** (same one-time
sign-in link), **Pause/Resume sampling**, **Start/Stop sampler**, and **Quit
menu bar**. Process names in the menu go through the same control-character
stripping as the terminal output.

It's menu-bar only: no Dock icon and no app-switcher entry (whyslow sets
`NSApplicationActivationPolicyAccessory`, which rumps doesn't do itself). There
is deliberately **no signed `.app` bundle** — unnecessary friction for a
personal tool.

### Pause vs stop

`whyslow pause` leaves the sampler process (and the dashboard) alive but stops
collecting; a marker file in the data directory is the switch, so it also works
from the CLI while the menu bar is open. Resuming looks like a data gap, which
already resets the detector baselines and closes any open spike.

## Start at login (launchd)

`whyslow launchagent` prints a LaunchAgent plist (add `--menubar` for the menu
bar instead of just the sampler); instructions go to stderr so you can redirect
the plist straight to a file:

```sh
whyslow launchagent --menubar > ~/Library/LaunchAgents/local.whyslow.menubar.plist
launchctl load ~/Library/LaunchAgents/local.whyslow.menubar.plist
# later: launchctl unload ~/Library/LaunchAgents/local.whyslow.menubar.plist
```

It runs as **your** user (a LaunchAgent, never a root LaunchDaemon), restarts
only after a crash — a clean `whyslow stop` stays stopped — and runs at a
slightly lowered priority (`Nice 5`, `LowPriorityIO`). It carries over
`WHYSLOW_HOME` if you set one.

## Optional: per-process disk, network and energy (needs sudo)

Off by default, and everything above works without it. When enabled, whyslow
gets what macOS otherwise hides: **per-process disk I/O, network traffic and
energy impact** — the number Activity Monitor calls "Energy Impact".

One root-only command supplies all three, so there's one helper and one rule:

```sh
whyslow helpers --sudoers    # prints the exact rule
sudo visudo -f /etc/sudoers.d/whyslow
whyslow helpers --check      # test it (never prompts for a password)
```

then in your config:

```toml
[helpers]
powermetrics = true
interval_seconds = 30.0
```

**How it's kept narrow**

* **One fixed command**, built from constants: no shell, absolute paths, a
  scrubbed environment and a timeout. The sudoers rule pins every argument, so
  sudo will refuse anything else — including an added `-o outfile`.
* **`sudo -n` only.** whyslow never prompts for, sees or stores your password.
* **Short-lived probes, never a root daemon.** Each probe takes ONE 1-second
  sample and exits; whyslow runs one every `interval_seconds` on a background
  thread, so sampling never blocks on it.
* **It gives up rather than nag.** If the rule is missing, the helper reports
  what to add and switches off for that run instead of retrying — repeated
  `sudo -n` refusals would spam the system auth log.
* **`fs_usage` is deliberately not used**, even though the brief allowed it: it
  requires a continuous root process streaming every filesystem syscall on the
  machine, **including file paths** — a privacy firehose with real overhead,
  for data `powermetrics --show-process-io` already provides in aggregate.
  `nettop` isn't needed either; `--show-process-netstats` covers it.

**What changes when it's on**

| | Without the helper | With it |
|---|---|---|
| Disk/network spikes | culprits **correlated** (processes whose CPU jumped) | culprits **measured**, with each one's share of the bytes |
| Battery ranking | CPU time on battery + a charge per wakeup | macOS **energy impact** per app |
| Leaderboard | no per-app disk/network | `~Disk` and `~Net` columns |

**Accuracy, honestly.** A probe measures one second in every thirty. Rates are
real for that second; long-run totals are extrapolated from them, so they're
marked `~` and described as approximate. Raising `interval_seconds` makes them
rougher; lowering it costs more CPU.

**What the sudoers rule grants.** Anyone who can run commands as you can run
that one powermetrics invocation as root. It only reads metrics and writes to
stdout, but it is a real (small) privilege grant — that's the trade, and it's
why this is off by default. Remove `/etc/sudoers.d/whyslow` to revoke it.

## Where data lives

| What | Where | Permissions |
|---|---|---|
| Database | `~/Library/Application Support/whyslow/whyslow.sqlite3` (+ `-wal`, `-shm`) | 0600 |
| Config (optional) | `~/Library/Application Support/whyslow/config.toml` | yours |
| Logs | `~/Library/Logs/whyslow/whyslow.log` (rotated at 1 MB, 3 backups) | 0600 |
| PID/lock, dashboard token, pause marker | `~/Library/Application Support/whyslow/` | 0600 |

Override with `WHYSLOW_HOME=/some/dir` (puts everything there) or
`WHYSLOW_CONFIG=/path/config.toml` / `--config`.

**Wipe everything:** `whyslow wipe` stops the sampler if it's running and deletes
the database and logs. Your config file is kept; delete the directory above to
remove every trace.

**Retention:** raw per-second samples are kept 7 days (`retention_days`), and
hourly per-app totals and spikes 180 days (`rollup_retention_days`). Pruning
runs at startup and hourly.

## Configuration

See [`config.example.toml`](config.example.toml). The file is parsed with the
stdlib `tomllib` and strictly validated: unknown keys, wrong types and
out-of-range values are errors. Nothing in it is ever evaluated.

## Storage design

* `system_samples`: one row per tick (CPU, memory, disk/network rates, battery,
  and unattributed CPU).
* `process_samples`: per tick, only the **top 10 processes by CPU** above 1%, plus
  the top 10 by memory every 30 ticks. Storing every process every second would
  be ~40M rows/day.
* `app_usage_hourly`: CPU-seconds and wakeups (each **total and on battery**)
  plus peak memory per app per hour, accumulated from **every** process. This
  keeps the leaderboard and battery estimate accurate even though idle
  processes aren't stored per tick, and it's what survives the 7-day raw-sample
  retention.
* `processes` / `apps`: process instances keyed by `(pid, create_time)` because
  PIDs are reused, grouped into apps by their outermost `.app` bundle (so all
  "Google Chrome Helper (Renderer)" processes count as "Google Chrome").
* `spikes` / `spike_culprits`: each spike (metric, start/end, peak, frozen
  baseline, unattributed CPU) and its ranked culprits with attribution type.

`process_samples` and `app_usage_hourly` carry disk, network and energy columns
that stay NULL unless the `powermetrics` helper is on. In `process_samples`
they're the bytes measured during one 1-second probe (so, per-second rates); in
`app_usage_hourly` they're those rates extrapolated across the probe interval.

Schema changes are versioned migrations (`PRAGMA user_version`); an older
database is upgraded in place the first time the sampler starts.

Columns for per-process disk, network and energy exist but stay `NULL` unless
the `powermetrics` helper is enabled.

## Overhead

Measured on an M2 MacBook Air (~450 processes, 1 s interval): about **14 ms of
CPU per second (~1.4% of one core, ~0.2% of the machine)**, including the
`/bin/ps` call and SQLite writes. With spike detection and the dashboard server
running (no tab open) it measured ~1.8% of one core and ~21 MB of RAM. Half of that is `/bin/ps`; set
`system_process_visibility = false` to halve it. Every `whyslow top` prints the
cost of its own sample.

## Security model

* **Runs as you, never root.** The core needs no elevated privileges. The one
  optional helper (`powermetrics`) is off by default, must be enabled
  explicitly in config *and* allowed by a sudoers rule that pins every
  argument, and runs as short-lived `sudo -n` probes — never a persistent root
  process. See "Optional: per-process disk, network and energy" above.
* **Local only.** No telemetry and no network calls, including from the page:
  Chart.js is vendored, not loaded from a CDN, and the CSP allows only
  same-origin connections.
* **Dashboard hardening.**
  * Bound to `127.0.0.1` (hard-coded, not configurable).
  * A Host-header allowlist blocks DNS rebinding.
  * Token auth on every API call, via the one-time sign-in link above.
  * No CORS, and the custom auth header also blocks CSRF.
  * GET-only API (plus the sign-in POST).
  * Strict CSP (`default-src 'none'`; scripts, styles and fetches from the same
    origin only; no inline code, no eval, no frames), `nosniff`, `DENY`
    framing, `no-referrer`, `no-store`.
  * No `/docs` or OpenAPI endpoints, no server banner, no access log.
* **Secrets are redacted before storage or display.** Command lines pass through
  `whyslow/redact.py`, which masks sensitive flags (`--password x`,
  `--api-key=…`), `key=value` pairs, credentials in URLs/connection strings,
  Bearer/Basic auth, well-known token formats (GitHub, AWS, Slack, Stripe,
  Google, OpenAI/Anthropic, JWTs, PEM keys) and long hex/base64 blobs. The rules
  err toward over-masking. Set `privacy.cmdline = "name_only"` to never store
  arguments at all. **Environment variables are never read.**
* **Read-only.** whyslow observes; it never signals or kills other processes.
  The only signal it sends is `SIGTERM` to its own sampler, and only after
  checking the lock file and that the PID's argv is exactly `… whyslow run`.
* **Untrusted strings are treated as data.** Process names and arguments are
  stripped of control characters before terminal output (no escape-sequence
  injection). In the dashboard they're inserted only with `textContent`, never
  `innerHTML`, and Chart.js draws labels on a canvas. Tests feed
  `<script>`/`onerror` payloads through as process names.
* **Parameterized SQL only.** No query is built from strings.
* **Private files.** umask 077; database and logs are 0600.
* **Sane logs.** Logs never contain command lines, are capped at ~4 MB total, and
  are deleted by `whyslow wipe`.

### Supply chain

Runtime dependencies are `psutil`, `fastapi` and `uvicorn` (plain, without the
`[standard]` extras), plus `rumps` (with pyobjc) for the optional menu bar —
every other command works without it, and it's an extra in `pyproject.toml`
(`pip install -e ".[menubar]"`). Every transitive package is pinned exactly in
`requirements.txt`: 17 in total. **Chart.js 4.5.1** is vendored in
`src/whyslow/web/static/vendor/`. It came from the npm tarball, checked against
the registry's sha512 integrity hash, and was scanned for `eval`/`Function`/network
use (none). Its sha256 is recorded in `requirements.txt`. For stronger guarantees, install with hash checking
(for example `pip-compile --generate-hashes` then `pip install --require-hashes`).

## Tests

```sh
python -m unittest discover -s tests
```

Covers redaction (leak cases and must-not-mangle cases), the spike detector
(noise, blips, sustain, warm-up, min_delta, plateaus, sleep gaps, the live
regressions), culprit attribution, the spike pipeline end to end, schema
migration, config validation, storage, the leaderboard totals, the battery
model (parameter recovery, quantization, weak correlation, plugged-in and
sleep handling), the menu-bar glance (including hostile process names) and its
launchd plists, the sudo helper (fixed command and matching sudoers rule,
plist parsing including alternative key spellings and garbage input, failure
classification, snapshot attribution and extrapolation), the macOS platform
layer, and the dashboard
server: loopback-only bind, token and sign-in codes, Host allowlist, CSP and
headers, read-only methods, path traversal, input validation, hostile process
names. The web tests start a real server on a spare port.
