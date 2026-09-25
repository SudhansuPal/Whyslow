# whyslow — "Why Is This So Slow?"

A local, real-time system detective for macOS. whyslow checks CPU, memory,
disk, network and battery once a second. When something spikes, it tells you
which processes caused it. Over days and weeks it also keeps a leaderboard of
the apps that use the most, including the background process that quietly
drains your battery overnight.

It runs as your normal user, keeps everything in one local SQLite file, and
makes no network connections.

## Inspiration

Every Mac user knows the moment: the fans spin up, the cursor stutters, the
battery that was at 80% an hour ago is at 30%, and you ask *why is this so
slow?* Activity Monitor only shows what is happening **right now**. By the time
you open it, the culprit has often finished. And it never answers the slower
question: *what has been draining my battery all week?*

whyslow is the tool I wanted for those moments. It records quietly in the
background, notices when something unusual happens, and keeps the evidence so
you can look later and get a name instead of a guess.

## What it does

- **Live view.** `whyslow top` shows the current top consumers, including
  system processes such as WindowServer and mds_stores that macOS normally
  hides from non-admin tools.
- **Spike detection.** It learns a rolling baseline for each metric and flags
  sustained jumps above it. Each spike records the processes behind it, marked
  as either *measured* or *correlated* (a lead, not proof).
- **Offenders leaderboard.** Totals CPU time, time on battery, wakeups and peak
  memory per app (all of Chrome's helpers count as "Chrome") over days or weeks.
- **Battery drain estimate.** Separates the drain no app is responsible for
  (screen, radios, kernel) from the CPU-driven part, and splits only the
  CPU-driven part among apps. It is always labelled as an estimate.
- **Dashboard.** A localhost web page with live charts, a spike timeline, top
  processes, offenders and battery drain. It follows light/dark mode.
- **Menu bar app.** Shows the busiest app or the current spike at a glance.
- **Optional sudo helper.** Off by default. A tightly scoped `powermetrics`
  probe that adds per-process disk, network and energy impact.

## Install

Requires macOS (Apple Silicon or Intel) and Python 3.11+.

```sh
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .              # add ".[menubar]" for the menu-bar app
```

## Quick start

```sh
whyslow top --watch      # live view of top consumers
whyslow start            # start the background sampler and open the dashboard
whyslow spikes           # spikes from the last day and who caused them
whyslow offenders        # biggest resource users over the last week
whyslow battery          # what drained the battery (estimate)
whyslow menubar          # menu-bar app
whyslow status           # is it running, and what can it see?
whyslow stop             # stop the sampler
whyslow wipe             # delete all collected data (asks first)
```

`CPU%` is per core, as in Activity Monitor, so totals can exceed 100%.

## What it can and can't see

Without sudo, whyslow sees system-wide CPU, memory, disk, network and battery,
plus per-process CPU and memory for **every** process. For processes owned by
other users it uses Apple's `/bin/ps`, so it never needs elevated privileges
itself. macOS does not expose per-process disk, network or energy data without
root. The optional helper fills that gap. CPU that no visible process accounts
for (mostly `kernel_task`) is reported as **unattributed**, which is itself a
useful clue.

## Privacy and security

- Runs as you, never as root. It only observes and never signals other
  processes.
- Local only: no telemetry, no network calls. Chart.js is vendored.
- The dashboard is bound to `127.0.0.1` and protected by a token, a one-time
  sign-in link, a Host allowlist and a strict CSP.
- Command lines are redacted (passwords, API keys, tokens) before they are
  stored or displayed. Environment variables are never read.
- Data lives in `~/Library/Application Support/whyslow/` with 0600
  permissions. Raw samples are kept for 7 days and hourly totals for 180 days.
- Overhead is about 1.5–2% of one core and about 21 MB of RAM.

## Learn more

- **[MANUAL.md](MANUAL.md)** is the full user guide: every command, reading
  the output, the dashboard, how spike detection and the battery model work,
  the sudo helper, configuration, data storage, the security model and
  troubleshooting.
- **[config.example.toml](config.example.toml)** lists every setting with its
  default.

## Tests

```sh
python -m unittest discover -s tests
```
