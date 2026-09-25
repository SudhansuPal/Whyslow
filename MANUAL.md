# whyslow — user manual

A local, real-time system detective for macOS. It samples CPU, memory, disk,
network and battery once a second, detects spikes, and names the processes
behind them. Everything stays on this Mac.

This manual is the practical guide: every command, how to read the output, how
to turn on the optional sudo feature, what whyslow **cannot** see, and what
parts have not been verified end to end (and why). The [README](README.md) is
the short overview.

---

## Contents

1. [Install and first run](#1-install-and-first-run)
2. [Command reference](#2-command-reference)
3. [Reading the output](#3-reading-the-output)
4. [The dashboard](#4-the-dashboard)
5. [The menu bar](#5-the-menu-bar)
6. [Start at login](#6-start-at-login)
7. [Optional: per-process disk, network and energy (sudo)](#7-optional-per-process-disk-network-and-energy-sudo)
8. [What macOS lets whyslow see](#8-what-macos-lets-whyslow-see)
9. [How spike detection works](#9-how-spike-detection-works)
10. [How the battery estimate works](#10-how-the-battery-estimate-works)
11. [Configuration reference](#11-configuration-reference)
12. [Your data: where it lives, how to erase it](#12-your-data-where-it-lives-how-to-erase-it)
13. [Security model](#13-security-model)
14. [Troubleshooting](#14-troubleshooting)
15. [Known limits and unverified paths](#15-known-limits-and-unverified-paths)
16. [Development notes](#16-development-notes)

---

## 1. Install and first run

Requires macOS (Apple Silicon or Intel) and Python 3.11+.

```sh
cd ~/whyslow
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .            # requirements.txt already includes the menu-bar deps
```

Then:

```sh
whyslow start     # background sampler + dashboard, opens your browser
whyslow top       # or just look at right now, without storing anything
```

`whyslow start` prints the dashboard URL and where data is stored. Stop it with
`whyslow stop`.

**Give it time.** Spike detection needs a 60-sample warm-up (about a minute),
the leaderboard gets more useful over days, and the battery estimate needs a
few hours of unplugged use before it can say anything confident.

---

## 2. Command reference

### Looking at right now

| Command | What it does |
|---|---|
| `whyslow top` | One-shot list of the current top consumers. Measures over 1 second. |
| `whyslow top --watch` | Refreshes continuously until Ctrl-C. |
| `whyslow top --sort mem` | Sort by memory instead of CPU. |
| `whyslow top -n 30` | Show more rows (default 15). |
| `whyslow top --interval 3` | Use a 3-second measurement window. |
| `whyslow top --cmd` | Also show each process's redacted command line. |

`top` works whether or not the sampler is running and stores nothing.

### Investigating

| Command | What it does |
|---|---|
| `whyslow spikes` | Spikes in the last day, each with its culprit processes. |
| `whyslow spikes --since 2h` | Any window: `90s`, `30m`, `6h`, `7d`. |
| `whyslow spikes -n 50 --cmd` | More spikes, with redacted command lines. |
| `whyslow offenders` | Cumulative use per app over the last 7 days. |
| `whyslow offenders --sort battery` | Sort by CPU time spent on battery (also `cpu`, `memory`, `wakeups`). |
| `whyslow offenders --since 30d -n 25` | Longer window, more apps. |
| `whyslow battery` | Estimate of what drained the battery. |
| `whyslow battery --since 3d -n 15` | Narrower window, more apps. |

### Running the sampler

| Command | What it does |
|---|---|
| `whyslow start` | Starts the background sampler and dashboard; opens the browser. |
| `whyslow stop` | Stops it. |
| `whyslow pause` / `whyslow resume` | Stops/starts **collecting** while leaving the sampler and dashboard running. |
| `whyslow status` | Running? paused? what's visible? how much data is stored? |
| `whyslow run` | Runs the sampler in the foreground (Ctrl-C to stop) — useful for debugging. |

### Interfaces and setup

| Command | What it does |
|---|---|
| `whyslow dashboard` | Opens the dashboard with a fresh one-time sign-in link. |
| `whyslow dashboard --print` | Prints that link instead of opening a browser. |
| `whyslow menubar` | Runs the menu-bar app (starts the sampler too, unless `--no-sampler`). |
| `whyslow launchagent` | Prints a launchd plist so the sampler starts at login. |
| `whyslow launchagent --menubar` | Same, but starts the menu-bar app. |
| `whyslow helpers` | Status of the optional sudo helper. |
| `whyslow helpers --sudoers` | Prints the exact sudoers rule to install. |
| `whyslow helpers --check` | Runs one probe now. Never prompts for a password. |

### Data and configuration

| Command | What it does |
|---|---|
| `whyslow config` | Prints the effective configuration and where it came from. |
| `whyslow wipe` | Deletes **all** collected data and logs (asks first; `--yes` skips). |
| `whyslow --version` | Version. |
| `whyslow --config PATH <command>` | Use a specific config file. |

---

## 3. Reading the output

**CPU% is per core, like Activity Monitor.** 100% means one core fully busy, so
on an 8-core Mac the total can reach 800%. System CPU in the header is 0–100%
across all cores.

**"Unattributed" CPU** is system CPU minus everything whyslow can see. It's
mostly `kernel_task` — which is invisible to every unprivileged tool — plus
processes too short-lived to sample. A large unattributed share is itself a
clue: heavy I/O, thermal throttling, or a storm of tiny short-lived processes.

**`SRC: ps`** marks a process owned by another user (root, `_windowserver`, …).
whyslow reads its CPU and memory through Apple's `/bin/ps`; macOS hides its
command line.

**Culprits are labelled by evidence:**

| Label | Meaning |
|---|---|
| **measured** / *direct* | The number is that process's own metric. |
| **correlated** | A lead, not proof: this process's CPU jumped when the spike began. Used for disk and network unless the sudo helper is on. |

**`~` means approximate.** Disk and network totals in the leaderboard are
extrapolated from sampled seconds (see [section 7](#7-optional-per-process-disk-network-and-energy-sudo)).

**`–` means "not visible", not "zero".** Wakeup counts for system processes are
hidden by macOS.

---

## 4. The dashboard

`whyslow start` serves it at `http://127.0.0.1:8765/` and opens it;
`whyslow dashboard` reopens it later.

**Sections**

- **Tiles** — current CPU, memory, disk, network and battery. A tile shows
  "▲ Spike" while that resource is spiking.
- **Charts** — one per resource, 5 minutes to 24 hours. Hovering gives a
  crosshair and a readout of every series at that moment. Gaps (Mac asleep,
  sampler stopped) appear as breaks rather than straight lines.
- **Spikes** — shaded bands on the charts and a list below. Click a band or a
  row to see the culprits, their share, and whether each is measured or
  correlated.
- **Offenders** — the leaderboard, sortable.
- **Battery drain** — the estimate, with the non-attributable baseline shown
  separately from the part split across apps.
- **Top processes right now.**

**Zoomed-out ranges keep peaks.** Above ~1500 points, each plotted point is the
**peak** of its bucket for CPU, disk and network (so short spikes don't vanish)
and the average for memory and battery. The chart says so when it's bucketing.

**Signing in.** The server listens only on `127.0.0.1`, but every account on
the Mac shares loopback, so the API needs a token:

1. Each sampler start makes a random session token, stored in a 0600 file.
2. `whyslow start` / `whyslow dashboard` open a URL containing a **one-time
   code** that expires in 2 minutes.
3. The page trades that code for the token and keeps it in the tab's
   `sessionStorage`.

Why the extra step? Opening a URL passes it through the argv of `open`, and on
macOS **any local user can read any process's arguments** (`/bin/ps` is setuid
root). A code that's already used and short-lived leaks nothing.

Restarting the sampler rotates the token, so open tabs will ask you to run
`whyslow dashboard` again. The dashboard follows your light/dark setting, works
at phone width, and pauses polling while its tab is hidden.

---

## 5. The menu bar

```sh
whyslow menubar            # also starts the sampler if needed
whyslow menubar --no-sampler
```

The title shows whatever matters most:

| Title | Meaning |
|---|---|
| `Firefox 82%` | busiest app and its CPU |
| `▲ CPU 90%` | a spike is happening now |
| `whyslow 4%` | nothing busy — system CPU |
| `whyslow ⏸` / `⏹` / `…` | paused / stopped / not reporting |

The menu lists system CPU and memory, the top three apps, battery state and the
current or most recent spike, plus **Open dashboard**, **Pause/Resume**,
**Start/Stop sampler** and **Quit**.

It's a separate process from the sampler, so **quitting the menu bar leaves
collection running**. It's menu-bar only: no Dock icon, no app-switcher entry.
There's deliberately no signed `.app` bundle — unnecessary friction for a
personal tool.

---

## 6. Start at login

```sh
whyslow launchagent --menubar > ~/Library/LaunchAgents/local.whyslow.menubar.plist
launchctl load ~/Library/LaunchAgents/local.whyslow.menubar.plist
```

Drop `--menubar` for the sampler alone. To stop auto-starting:

```sh
launchctl unload ~/Library/LaunchAgents/local.whyslow.menubar.plist
rm ~/Library/LaunchAgents/local.whyslow.menubar.plist
```

The plist runs as **you** (a LaunchAgent, never a root LaunchDaemon), restarts
only after a crash — a clean `whyslow stop` stays stopped — runs at slightly
lowered priority, and carries over `WHYSLOW_HOME` if you set one. Instructions
print to stderr, so redirecting stdout gives you a clean file.

---

## 7. Optional: per-process disk, network and energy (sudo)

**Everything above works without this.** It is off by default.

macOS hides three things from unprivileged code: per-process **disk I/O**,
**network traffic**, and **energy impact** (the number Activity Monitor shows).
Exactly one tool reports all three — `powermetrics` — and it requires root.

### Turning it on

```sh
whyslow helpers --sudoers          # prints the exact rule
sudo visudo -f /etc/sudoers.d/whyslow
whyslow helpers --check            # test it; never prompts for a password
```

Then in your config file:

```toml
[helpers]
powermetrics = true
interval_seconds = 30.0
```

Restart the sampler (`whyslow stop && whyslow start`).

### What changes

| | Without it | With it |
|---|---|---|
| Disk/network spikes | culprits **correlated** (whose CPU jumped) | culprits **measured**, with each one's share of the bytes |
| Battery ranking | CPU time on battery + a charge per wakeup | macOS **energy impact** per app |
| Leaderboard | no per-app disk/network | `~Disk` and `~Net` columns |

### How it's kept narrow

- **One fixed command**, built from constants: absolute paths, no shell, a
  scrubbed environment, a timeout. The sudoers rule pins every argument, so
  sudo refuses anything else — including an added `-o outfile`.
- **`sudo -n` only.** whyslow never prompts for, sees or stores your password.
- **Short-lived probes, never a root daemon.** Each probe takes one 1-second
  sample and exits; one runs every `interval_seconds` on a background thread,
  so sampling never waits on it.
- **It gives up instead of nagging.** If the rule is missing, it reports what
  to add and switches off for that run — repeated `sudo -n` refusals would
  spam the system auth log.
- **`fs_usage` is deliberately not used.** It would need a continuous root
  process streaming every filesystem syscall on the machine, **including file
  paths** — a privacy firehose with real overhead — for data
  `--show-process-io` already aggregates. `nettop` isn't needed either;
  `--show-process-netstats` covers it.

### Accuracy, honestly

A probe measures **one second in every thirty**. Rates are real for that
second; long-run totals are extrapolated from them, which is why they're marked
`~` and called approximate. Lower `interval_seconds` for better accuracy at
more CPU cost; raise it for the reverse.

### What the rule actually grants

Anyone who can run commands as you can run that one `powermetrics` invocation
as root. It only reads metrics and writes to stdout, but it is a real (small)
privilege grant. That's the trade, and it's why this is off by default.

### Turning it off

Set `powermetrics = false` (or delete the `[helpers]` section) and restart the
sampler. To revoke the privilege entirely:

```sh
sudo rm /etc/sudoers.d/whyslow
```

---

## 8. What macOS lets whyslow see

| Metric | Without sudo | How |
|---|---|---|
| System CPU, memory, swap, load | ✅ | psutil |
| System disk and network totals | ✅ | psutil (loopback excluded) |
| Battery %, charging, time left | ✅ | psutil / IOKit |
| Your processes: CPU, memory, command line, wakeups | ✅ | psutil |
| Other users' processes (root, WindowServer, `mds_stores`, `backupd`): CPU and memory | ✅ | Apple's setuid `/bin/ps` |
| Other users' command lines | ❌ | Hidden by macOS |
| `kernel_task` (PID 0) | ❌ | Invisible even to `ps`; shows as "unattributed" |
| Processes living less than one sample | ⚠️ | Counted if born between samples, else "unattributed" |
| Per-process disk I/O, network, energy | ❌ | Optional `powermetrics` helper ([section 7](#7-optional-per-process-disk-network-and-energy-sudo)) |

On a typical Mac, psutil is denied CPU and memory for roughly **40% of
processes** — everything owned by root or system users, which includes many of
the usual culprits. `/bin/ps` ships with macOS, is signed by Apple and is
setuid root, so whyslow asks it for those. whyslow itself never gains
privileges. Turn this off with `system_process_visibility = false`; those
processes then count as unattributed.

---

## 9. How spike detection works

Six metrics are watched: CPU, memory, disk read, disk write, network in and
network out. Each keeps a rolling baseline over the last
`baseline_window_seconds` (default 5 minutes): the **median** and the **MAD**
(median absolute deviation). A spike is a value above

```
median + max(k × spread, min_delta)
```

for `sustain_samples` samples in a row (defaults: k = 4, 3 samples).

- **`min_delta`** is the smallest jump worth caring about. Without it, a flat
  metric like an idle disk (spread = 0) would "spike" on every blip.
- **Bounded metrics (CPU %, memory %)** cap that noise margin at half the
  remaining headroom, so a jump halfway from normal to fully saturated always
  counts. Without this, a busy machine pushed the CPU threshold above 100% and
  real load became undetectable.
- **The threshold freezes** when a spike starts. The spike ends when values
  fall back under it, or when the load has run so long (about half the window)
  that it has become the new normal.
- **Spike values are clipped before entering the baseline**, so a spike can't
  raise its own bar.
- Sleep/wake and pausing close any open spike.

**Culprits** are captured when a spike starts and refreshed at each new peak:
CPU spikes rank processes by CPU; memory spikes rank by memory **growth** over
the last minute (not by size); disk and network spikes are correlated guesses
unless the sudo helper is on.

**Tuning.** Lower `threshold_k` or the `*_min_delta_*` values for more
sensitivity. For scale: on a busy 8-core Mac, two fully busy cores (+17 points
of system CPU) did **not** qualify at k = 4; six busy cores did. Long-running
single-process hogs are the leaderboard's job, not spike detection's.

---

## 10. How the battery estimate works

It is an **estimate**, labelled as one everywhere.

Most drain isn't any process's fault — the display, Wi-Fi, Bluetooth and the
kernel draw power regardless. So whyslow doesn't split 100% of the drain among
apps. It fits, from your own data:

```
drain (%/hour) = baseline + k × system CPU
```

Each data point is one **1% drop** of the battery reading; the time between
drops gives the rate. (macOS reports whole-number percentages, so the drops are
the precise events — fixed-width buckets mostly measure rounding error.)

- `baseline × hours` is reported as **not attributable to any process**.
- `k × CPU × hours` is the CPU-driven part, and only that gets split among
  apps, by `CPU seconds on battery + a small charge per wakeup`.

Wakeups count because pulling the CPU out of idle costs energy even when the
work is trivial — exactly how a quiet background process drains a battery
overnight.

**When it can't tell, it says so.** Too little time unplugged, or drain that
doesn't track CPU (a bright screen on an idle Mac), and you get the ranking
without percentages. With the sudo helper on, the ranking switches to macOS's
measured energy impact.

---

## 11. Configuration reference

Optional file at `~/Library/Application Support/whyslow/config.toml`
(copy [`config.example.toml`](config.example.toml)). Unknown keys, wrong types
and out-of-range values are rejected at startup with a clear message. Nothing
in it is ever executed. `whyslow config` prints what's in effect.

```toml
[sampling]
interval_seconds = 1.0            # 0.25 – 60
process_top_n = 10                # process rows stored per tick (top by CPU)
min_process_cpu_percent = 1.0     # ...only above this
memory_snapshot_every = 30        # also store top-N by memory every N ticks
system_process_visibility = true  # read other users' processes via /bin/ps

[storage]
retention_days = 7                # raw per-second samples
rollup_retention_days = 180       # hourly per-app totals and spikes

[privacy]
cmdline = "redact"                # or "name_only" to store no arguments at all

[detector]
baseline_window_seconds = 300     # 30 – 3600
threshold_k = 4.0
sustain_samples = 3
cpu_min_delta_percent = 15.0
memory_min_delta_percent = 5.0
disk_min_delta_mb_per_s = 20.0
net_min_delta_mb_per_s = 1.0
culprits_top_n = 5

[dashboard]
enabled = true
port = 8765                       # host is always 127.0.0.1, not configurable
open_browser = true

[helpers]
powermetrics = false              # see section 7
interval_seconds = 30.0           # 5 – 3600
```

Environment overrides: `WHYSLOW_HOME` (all data, config, logs in one directory)
and `WHYSLOW_CONFIG` (config file path), plus `--config`.

---

## 12. Your data: where it lives, how to erase it

| What | Where | Permissions |
|---|---|---|
| Database | `~/Library/Application Support/whyslow/whyslow.sqlite3` (+ `-wal`, `-shm`) | 0600 |
| Config | `~/Library/Application Support/whyslow/config.toml` | yours |
| Logs | `~/Library/Logs/whyslow/whyslow.log` (rotated at 1 MB, 3 kept) | 0600 |
| PID/lock, dashboard token, pause marker | `~/Library/Application Support/whyslow/` | 0600 |

**Retention.** Raw per-second samples are kept 7 days; hourly per-app totals
and spikes 180 days. Pruning runs at startup and hourly.

**Erase everything:**

```sh
whyslow wipe          # stops the sampler if needed, deletes database + logs
```

Your config file is kept. To remove every trace, delete the directories above.

**What's never stored:** environment variables, and command-line arguments if
you set `cmdline = "name_only"`. Command lines that are stored go through
redaction first — masking passwords, API keys, tokens, URL credentials,
connection strings, Bearer/Basic auth, well-known token formats (GitHub, AWS,
Slack, Stripe, Google, OpenAI/Anthropic, JWTs, PEM keys) and long hex/base64
blobs. The rules deliberately over-mask; on a real machine they alter about 5%
of arguments.

---

## 13. Security model

- **Runs as you, never root.** Core features need no privileges. The one
  optional sudo feature is described in [section 7](#7-optional-per-process-disk-network-and-energy-sudo).
- **No network, no telemetry, no cloud.** Chart.js is bundled in the repo, not
  loaded from a CDN, and the page's content-security policy forbids outside
  connections.
- **Dashboard:** bound to `127.0.0.1` (hard-coded); rejects requests whose Host
  header isn't localhost (blocking DNS-rebinding attacks from web pages);
  token-authenticated; read-only API; strict CSP with no inline code; no
  API docs endpoints; no access log.
- **Read-only.** whyslow observes. It never kills, stops or changes another
  process. The only signal it sends is to its own sampler, after checking both
  the lock file and that the target's arguments are exactly `… whyslow run`.
- **Untrusted text stays text.** Process names and arguments have control
  characters stripped before terminal output (no escape-sequence injection),
  and reach the web page only as text nodes, never as HTML.
- **Parameterized SQL only**, private files (umask 077, 0600), and logs that
  never contain command lines and are capped at about 4 MB.

---

## 14. Troubleshooting

**"No data yet."** The sampler hasn't run. `whyslow start`.

**"database is schema vN, expected vM."** Your database predates an upgrade.
Run `whyslow start` once; it migrates in place, keeping your data.

**The dashboard says "Session ended" or "Not signed in."** The token rotates
each time the sampler starts. Run `whyslow dashboard` for a fresh link.

**`whyslow start` says the dashboard failed.** Usually the port is in use.
Change `[dashboard] port`, or set `enabled = false` to run without it. The
sampler keeps working either way; check `~/Library/Logs/whyslow/whyslow.log`.

**No spikes are being detected.** Detection needs a 60-sample warm-up, and the
defaults are deliberately conservative on a noisy machine. See
[section 9](#9-how-spike-detection-works) for tuning.

**Gaps in the charts.** Normal: the Mac slept or the sampler was stopped or
paused. whyslow marks gaps rather than drawing a misleading straight line.

**Wakeups show `–`.** macOS hides context switches for other users' processes.
That's "unknown", not zero.

**A big "unattributed" share.** `kernel_task` (thermal management, interrupts,
heavy I/O) plus very short-lived processes. Nothing unprivileged can attribute
it.

**`whyslow helpers --check` says "a password is required."** The sudoers rule
isn't installed or doesn't match exactly. Re-run `whyslow helpers --sudoers`
and install it with `sudo visudo -f /etc/sudoers.d/whyslow`.

**The menu bar doesn't appear.** It needs `rumps`, which `requirements.txt`
installs. If you installed the package alone, add the extra:
`pip install -e ".[menubar]"`.

**Stopping seems slow.** `whyslow stop` waits up to 10 seconds for a clean
shutdown so the last data is flushed.

---

## 15. Known limits and unverified paths

Things that are genuinely uncertain, rather than polished over:

### The sudo helper has never run against real root output

I (Claude) built and tested everything in this project on this Mac, but **I
cannot run `sudo`** — it needs your password, and I won't ask for or handle it.
So the `powermetrics` code paths that require root have never executed against
real output. What that means concretely:

- The **command construction, sudoers rule, failure handling and refusal path
  are verified.** I ran `whyslow helpers --check` without a rule installed: it
  fails cleanly, never prompts, prints the exact rule, and exits 1.
- The **plist parsing is not verified against real output.** It's written
  defensively — it accepts several spellings of each field name, tolerates a
  text banner before the plist, and ignores malformed entries — and it's tested
  against synthetic plists. But the exact key names `powermetrics` emits could
  differ on your macOS version.

**To close this gap**, install the rule and run:

```sh
whyslow helpers --check
```

It prints the parsed values *and the full list of keys `powermetrics`
returned*. If a metric shows `-` while a key in that list obviously holds it,
the fix is a one-line addition to `_KEYS` in `src/whyslow/helpers.py`.

### Other limits

- **`kernel_task` can never be attributed** without root, and even root gives
  only coarse insight. It's reported as "unattributed" rather than guessed at.
- **Processes shorter than one sample are missed** unless they were born
  between two ticks. A build spawning thousands of brief compilers shows up as
  system CPU and unattributed, not as per-process rows.
- **Per-app disk and network totals are extrapolated** from one sampled second
  in every thirty, hence `~`.
- **The battery model is a model.** It can only separate baseline from
  CPU-driven drain when CPU actually varies while unplugged; otherwise it
  declines to give percentages.
- **The wakeup cost constant** in the battery score (20 µs of CPU-equivalent
  per wakeup) is a reasoned guess, not a measurement.
- **Storage is bounded by sampling choices**: only the top processes by CPU
  (above 1%) are stored each second, plus the top by memory every 30 ticks.
  Hourly per-app rollups cover *every* process, so long-run totals stay
  accurate even though per-second detail doesn't.

---

## 16. Development notes

```sh
python -m unittest discover -s tests      # 136 tests
```

Tests cover redaction (leak cases and must-not-mangle cases), spike detection
(noise, blips, plateaus, sleep gaps, the regressions below), culprit
attribution, the battery model, storage (permissions, identity, rollups,
retention, migrations, hostile names), the dashboard server (bind address,
auth, headers, methods, traversal, validation), the menu-bar glance, and the
sudo helper's command, parser and failure handling.

**Layout:** `sampler` / `storage` / `detector` / `correlator` / `battery` /
`helpers` / `web` / `menubar`, with all macOS quirks isolated in `macos.py` and
all secret-masking in `redact.py`.

**Bugs that live testing found** (each now has a regression test):

- Spike values inflated their own baseline's spread, so the bar raced upward
  and a spike "ended" 18 seconds into a load that was still running.
- On a busy machine the CPU threshold could exceed 100%, making real load
  undetectable — fixed by capping the noise margin for bounded metrics.
- The battery model was fitting its own rounding error until it keyed off
  battery-percent drops instead of fixed buckets.
- A dashboard opened in a background tab stayed empty until focused.
- A helper snapshot was consumed on warm-up/post-sleep ticks and discarded.

**Overhead** on an M2 MacBook Air, ~450 processes at 1 Hz: about **1.4% of one
core** for sampling, ~1.8% with spike detection and the dashboard server
running, ~21 MB of RAM. Half of the sampling cost is the `/bin/ps` call;
`system_process_visibility = false` halves it. Every `whyslow top` prints the
cost of its own sample.
