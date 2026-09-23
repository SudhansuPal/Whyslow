/* whyslow dashboard.
 *
 * Security: every string from the API (process names, command lines, app
 * names) is untrusted. It only ever reaches the DOM via textContent or an
 * attribute (title), never innerHTML. Chart.js draws labels on a canvas.
 */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);

  function el(tag, opts = {}, children = []) {
    const node = document.createElement(tag);
    if (opts.cls) node.className = opts.cls;
    if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
    if (opts.title) node.title = opts.title;
    for (const [k, v] of Object.entries(opts.attrs || {})) node.setAttribute(k, v);
    for (const child of children) if (child) node.append(child);
    return node;
  }

  // ---------- formatting ----------
  const UNITS = ["B", "KB", "MB", "GB", "TB"];
  function fmtBytes(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return "–";
    let i = 0;
    while (Math.abs(n) >= 1024 && i < UNITS.length - 1) { n /= 1024; i++; }
    return i === 0 ? `${Math.round(n)} B` : `${n.toFixed(n < 10 ? 1 : 0)} ${UNITS[i]}`;
  }
  const fmtRate = (n) => (n === null || n === undefined ? "–" : `${fmtBytes(n)}/s`);
  const fmtPct = (n) => (n === null || n === undefined ? "–" : `${n.toFixed(n < 10 ? 1 : 0)}%`);
  const pad = (n) => String(n).padStart(2, "0");
  function fmtClock(ts, seconds = true) {
    const d = new Date(ts * 1000);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}${seconds ? ":" + pad(d.getSeconds()) : ""}`;
  }
  function fmtWhen(ts) {
    const d = new Date(ts * 1000);
    const today = new Date();
    const sameDay = d.toDateString() === today.toDateString();
    return sameDay ? fmtClock(ts) : `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${fmtClock(ts, false)}`;
  }
  function fmtDuration(s) {
    if (s === null || s === undefined) return "–";
    s = Math.max(1, Math.round(s));
    if (s < 90) return `${s}s`;
    if (s < 5400) return `${Math.round(s / 60)} min`;
    return `${(s / 3600).toFixed(1)} h`;
  }

  const METRICS = {
    cpu: { label: "CPU", fmt: fmtPct, chart: "cpu" },
    memory: { label: "Memory", fmt: fmtPct, chart: "memory" },
    disk_read: { label: "Disk read", fmt: fmtRate, chart: "disk" },
    disk_write: { label: "Disk write", fmt: fmtRate, chart: "disk" },
    net_recv: { label: "Network in", fmt: fmtRate, chart: "net" },
    net_sent: { label: "Network out", fmt: fmtRate, chart: "net" },
  };

  function fmtCpuTime(seconds) {
    if (seconds === null || seconds === undefined) return "–";
    if (seconds < 90) return `${seconds.toFixed(0)}s`;
    if (seconds < 5400) return `${(seconds / 60).toFixed(1)} min`;
    return `${(seconds / 3600).toFixed(1)} h`;
  }
  // macOS hides context switches for other users' processes, so 0 means
  // "not visible", not "never woke up".
  const fmtWakeups = (n) => (n ? n.toLocaleString() : "–");

  function fmtCulpritValue(metric, c) {
    if (c.attribution === "correlated") return `+${c.value.toFixed(1)}% CPU vs usual`;
    if (metric === "memory") return `+${fmtBytes(c.value)}`;
    return `${c.value.toFixed(1)}% CPU`;
  }

  // ---------- auth ----------
  const TOKEN_KEY = "whyslow.session";
  let token = null;
  try { token = sessionStorage.getItem(TOKEN_KEY); } catch (_) { /* storage blocked */ }

  async function login() {
    const match = location.hash.match(/^#login=([A-Za-z0-9._-]{10,200})$/);
    if (location.hash) history.replaceState(null, "", location.pathname);  // never leave the code visible
    if (!match) return token !== null;
    const res = await fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: match[1] }),
    });
    if (!res.ok) return token !== null;
    token = (await res.json()).token;
    try { sessionStorage.setItem(TOKEN_KEY, token); } catch (_) { /* works for this page load only */ }
    return true;
  }

  class Unauthorized extends Error {}
  async function api(path) {
    const res = await fetch(path, { headers: { Authorization: `Bearer ${token}` }, cache: "no-store" });
    if (res.status === 401) throw new Unauthorized();
    if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
    return res.json();
  }

  function showGate(expired) {
    $("main").hidden = true;
    $("gate").hidden = false;
    if (expired) {
      $("gate-title").textContent = "Session ended";
      $("gate-text").textContent = "whyslow was restarted or stopped. Run `whyslow dashboard` to open a fresh session.";
    }
    setLive("critical", expired ? "Signed out" : "Not signed in");
  }

  // Byte-rate axes: ticks at round *binary* steps (1/2/5 x KB/MB/GB) so labels read
  // "512 KB/s, 1.0 MB/s" rather than "488 KB/s, 977 KB/s".
  function binaryStep(max, targetTicks = 4) {
    const unit = 1024 ** Math.max(0, Math.floor(Math.log(Math.max(max, 1)) / Math.log(1024)));
    const raw = max / unit / targetTicks;
    const mag = 10 ** Math.floor(Math.log10(raw || 1));
    return [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) * unit;
  }
  const rateAxisHooks = {
    afterDataLimits: (axis) => {
      const step = binaryStep(axis.max);
      axis.min = 0;
      axis.max = Math.ceil(axis.max / step) * step;
    },
    afterBuildTicks: (axis) => {
      const step = binaryStep(axis.max);
      const ticks = [];
      for (let v = 0; v <= axis.max + step / 1000; v += step) ticks.push({ value: v });
      axis.ticks = ticks;
    },
  };

  // ---------- theme ----------
  function tokens() {
    const cs = getComputedStyle(document.documentElement);
    const v = (name) => cs.getPropertyValue(name).trim();
    return {
      surface: v("--surface"), text1: v("--text-1"), text2: v("--text-2"), muted: v("--muted"),
      grid: v("--grid"), axis: v("--axis"), border: v("--border"),
      series: [v("--series-1"), v("--series-2")],
      band: v("--spike-band"), bandStrong: v("--spike-band-strong"),
    };
  }
  let theme = null;

  // ---------- state ----------
  const state = {
    meta: null,
    seconds: 900,
    series: null,
    board: [],
    boardSort: "cpu",
    spikes: [],
    selected: null,   // spike id
    stopped: false,
  };

  // ---------- charts ----------
  const CHARTS = [
    { key: "cpu", title: "CPU", unit: "pct", max: 100, series: [{ field: "cpu", label: "CPU in use" }],
      sub: "All cores, 0–100%" },
    { key: "memory", title: "Memory", unit: "pct", max: 100, series: [{ field: "memory", label: "Memory in use" }],
      sub: "Share of RAM in use" },
    { key: "disk", title: "Disk", unit: "rate", series: [{ field: "disk_read", label: "Read" }, { field: "disk_write", label: "Write" }],
      sub: "System-wide; per-process disk I/O needs sudo (not enabled)" },
    { key: "net", title: "Network", unit: "rate", series: [{ field: "net_recv", label: "In" }, { field: "net_sent", label: "Out" }],
      sub: "System-wide, excluding loopback; per-process needs sudo (not enabled)" },
    { key: "battery", title: "Battery", unit: "pct", max: 100, series: [{ field: "battery", label: "Charge" }],
      sub: "Charge level" },
  ];
  const charts = {};

  const spikeBands = {
    id: "spikeBands",
    beforeDatasetsDraw(chart, _args, opts) {
      const { ctx, chartArea: area, scales: { x } } = chart;
      for (const s of opts.spikes || []) {
        const x0 = x.getPixelForValue(s.started_at);
        const x1 = x.getPixelForValue(s.ended_at ?? opts.until);
        const left = Math.max(area.left, x0);
        const right = Math.min(area.right, Math.max(x1, x0 + 4));
        if (right <= area.left || left >= area.right) continue;
        ctx.save();
        ctx.fillStyle = s.id === opts.selected ? opts.strong : opts.fill;
        ctx.fillRect(left, area.top, right - left, area.bottom - area.top);
        ctx.restore();
      }
    },
  };

  const crosshair = {
    id: "crosshair",
    afterDatasetsDraw(chart, _args, opts) {
      const active = chart.tooltip && chart.tooltip.getActiveElements();
      if (!active || !active.length) return;
      const { ctx, chartArea: area } = chart;
      const x = active[0].element.x;
      ctx.save();
      ctx.strokeStyle = opts.color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, area.top);
      ctx.lineTo(Math.round(x) + 0.5, area.bottom);
      ctx.stroke();
      ctx.restore();
    },
  };

  function spikesFor(spec) {
    return state.spikes.filter((s) => METRICS[s.metric] && METRICS[s.metric].chart === spec.key);
  }

  function spikeAt(spec, ts) {
    const slack = (state.series ? state.series.bucket : 1) * 1.5;
    return spikesFor(spec).find((s) => ts >= s.started_at - slack && ts <= (s.ended_at ?? Date.now() / 1000) + slack);
  }

  function buildChart(spec) {
    const card = el("article", { cls: "card", attrs: { "data-chart": spec.key } }, [
      el("header", { cls: "card-head" }, [el("h2", { text: spec.title }), el("p", { cls: "sub", text: spec.sub })]),
    ]);
    const canvas = el("canvas", { attrs: { role: "img", "aria-label": `${spec.title} chart` } });
    card.append(el("div", { cls: "plot" }, [canvas]));
    $("charts").append(card);

    const fmt = spec.unit === "pct" ? fmtPct : fmtRate;
    const chart = new Chart(canvas, {
      type: "line",
      data: { datasets: spec.series.map((s) => ({ label: s.label, data: [], parsing: false })) },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        normalized: true,
        spanGaps: false,
        interaction: { mode: "index", intersect: false },
        elements: { point: { radius: 0, hoverRadius: 4, hitRadius: 12 }, line: { borderWidth: 2, tension: 0 } },
        scales: {
          x: { type: "linear", ticks: { maxTicksLimit: 6, maxRotation: 0, callback: (v) => fmtClock(v, state.seconds <= 900) } },
          y: {
            beginAtZero: true, max: spec.max, suggestedMax: spec.unit === "rate" ? 1024 * 1024 : undefined,
            ticks: { maxTicksLimit: 5, callback: (v) => fmt(v) },
            ...(spec.unit === "rate" ? rateAxisHooks : {}),
          },
        },
        plugins: {
          legend: {
            display: spec.series.length > 1, align: "end",
            labels: { usePointStyle: true, pointStyle: "line", boxWidth: 16, font: { size: 12 } },
          },
          tooltip: {
            usePointStyle: true,
            callbacks: {
              title: (items) => {
                if (!items.length) return "";
                const ts = items[0].parsed.x;
                const agg = state.series && state.series.aggregated;
                const kind = spec.unit === "rate" || spec.key === "cpu" ? "peak" : "average";
                return agg ? `${fmtClock(ts)} · ${kind} of ${state.series.bucket}s` : fmtClock(ts);
              },
              label: (item) => `${fmt(item.parsed.y)}  ${item.dataset.label}`,
              labelPointStyle: () => ({ pointStyle: "line", rotation: 0 }),
              afterBody: (items) => {
                const lines = [];
                if (!items.length) return lines;
                const ts = items[0].parsed.x;
                if (spec.key === "battery") {
                  const i = state.series.ts.indexOf(ts);
                  const plugged = i >= 0 ? state.series.plugged[i] : null;
                  if (plugged !== null) lines.push(plugged ? "On AC power" : "On battery");
                }
                if (spikeAt(spec, ts)) lines.push("▲ Spike: click for culprits");
                return lines;
              },
            },
          },
          spikeBands: { spikes: [], selected: null },
          crosshair: {},
        },
        onClick: (_evt, _els, ch) => {
          const pos = ch.tooltip && ch.tooltip.getActiveElements();
          if (!pos || !pos.length) return;
          const ts = ch.data.datasets[pos[0].datasetIndex].data[pos[0].index].x;
          const hit = spikeAt(spec, ts);
          if (hit) selectSpike(hit.id, true);
        },
        onHover: (evt, _els, ch) => {
          const pos = ch.tooltip && ch.tooltip.getActiveElements();
          let over = false;
          if (pos && pos.length) {
            const ts = ch.data.datasets[pos[0].datasetIndex].data[pos[0].index].x;
            over = Boolean(spikeAt(spec, ts));
          }
          evt.native.target.style.cursor = over ? "pointer" : "default";
        },
      },
      plugins: [spikeBands, crosshair],
    });
    charts[spec.key] = { chart, spec, card, canvas };
    applyTheme(charts[spec.key]);
  }

  function applyTheme(entry) {
    const t = theme;
    const o = entry.chart.options;
    entry.chart.data.datasets.forEach((ds, i) => {
      ds.borderColor = t.series[i];
      ds.backgroundColor = t.series[i];
      ds.pointHoverBorderColor = t.surface;
      ds.pointHoverBorderWidth = 2;
    });
    for (const axis of [o.scales.x, o.scales.y]) {
      axis.grid = { color: t.grid, drawTicks: false };
      axis.border = { color: t.axis };
      axis.ticks.color = t.muted;
      axis.ticks.font = { size: 11 };
      axis.ticks.padding = 6;
    }
    o.scales.x.grid.display = false;
    o.plugins.legend.labels.color = t.text2;
    Object.assign(o.plugins.tooltip, {
      backgroundColor: t.surface, borderColor: t.border, borderWidth: 1,
      titleColor: t.text2, bodyColor: t.text1, footerColor: t.text2, padding: 10,
      titleFont: { size: 12, weight: "normal" }, bodyFont: { size: 12, weight: "600" },
    });
    o.plugins.spikeBands.fill = t.band;
    o.plugins.spikeBands.strong = t.bandStrong;
    o.plugins.crosshair.color = t.axis;
  }

  function toPoints(field) {
    const s = state.series;
    const gap = Math.max(5, s.bucket * 3);
    const pts = [];
    for (let i = 0; i < s.ts.length; i++) {
      if (i > 0 && s.ts[i] - s.ts[i - 1] > gap) pts.push({ x: s.ts[i - 1] + s.bucket, y: null }); // sampler was off
      pts.push({ x: s.ts[i], y: s[field][i] });
    }
    return pts;
  }

  function renderCharts() {
    const s = state.series;
    if (!s) return;
    for (const { chart, spec, canvas } of Object.values(charts)) {
      spec.series.forEach((ser, i) => { chart.data.datasets[i].data = toPoints(ser.field); });
      chart.options.scales.x.min = s.since;
      chart.options.scales.x.max = s.until;
      Object.assign(chart.options.plugins.spikeBands, { spikes: spikesFor(spec), selected: state.selected, until: s.until });
      chart.update("none");
      const vals = s[spec.series[0].field].filter((v) => v !== null);
      const fmt = spec.unit === "pct" ? fmtPct : fmtRate;
      const label = vals.length
        ? `${spec.title}, ${$("range-note").dataset.label}: now ${fmt(vals[vals.length - 1])}, highest ${fmt(Math.max(...vals))}`
        : `${spec.title}: no data`;
      canvas.setAttribute("aria-label", label);
    }
    const agg = s.aggregated ? `Each point covers ${s.bucket}s (peak for CPU, disk and network; average for memory and battery).` : "";
    $("range-note").textContent = agg;
  }

  // ---------- tiles ----------
  function setTile(id, value, sub, spiking) {
    const tile = $(id);
    tile.querySelector(".tile-value").textContent = value;
    tile.querySelector(".tile-sub").textContent = sub;
    const label = tile.querySelector(".tile-label");
    const existing = label.querySelector(".badge");
    if (spiking && !existing) label.append(el("span", { cls: "badge", text: "▲ Spike" }));
    if (!spiking && existing) existing.remove();
  }

  function renderTiles(latest) {
    if (!latest) return;
    const s = latest.system;
    const open = new Set(state.spikes.filter((x) => x.ended_at === null).map((x) => x.metric));
    setTile("tile-cpu", fmtPct(s.cpu_percent),
      s.unattributed_cpu ? `${s.unattributed_cpu.toFixed(0)}% of a core unattributed` : "all cores", open.has("cpu"));
    setTile("tile-memory", fmtPct(s.mem_percent), `${fmtBytes(s.mem_used)} used · ${fmtBytes(s.mem_available)} free`,
      open.has("memory"));
    setTile("tile-disk", fmtRate((s.disk_read_bps || 0) + (s.disk_write_bps || 0)),
      `read ${fmtRate(s.disk_read_bps)} · write ${fmtRate(s.disk_write_bps)}`, open.has("disk_read") || open.has("disk_write"));
    setTile("tile-net", fmtRate((s.net_recv_bps || 0) + (s.net_sent_bps || 0)),
      `in ${fmtRate(s.net_recv_bps)} · out ${fmtRate(s.net_sent_bps)}`, open.has("net_recv") || open.has("net_sent"));
    if (s.battery_percent === null) {
      setTile("tile-battery", "–", "no battery", false);
    } else {
      let sub = "on AC power";
      if (!s.power_plugged) {
        sub = "on battery";
        if (s.battery_secs_left) sub += `, ~${fmtDuration(s.battery_secs_left)} left`;
      }
      setTile("tile-battery", fmtPct(s.battery_percent), sub, false);
    }
  }

  // ---------- tables ----------
  function processCell(app, name, cmdline, appPrefix) {
    const cell = el("td", { cls: "cell-name" });
    const label = appPrefix && appPrefix !== name ? `${appPrefix} / ${name}` : name;
    cell.append(el("span", { cls: "cell-main", text: label, title: label }));
    if (cmdline && cmdline !== name) cell.append(el("span", { cls: "cell-cmd", text: cmdline, title: cmdline }));
    return cell;
  }

  function renderProcesses(latest) {
    const body = $("proc-table").querySelector("tbody");
    const rows = latest ? latest.processes : [];
    body.replaceChildren(...rows.map((p) => el("tr", {}, [
      el("td", { cls: "cell-name", text: p.app, title: p.app }),
      processCell(p.app, p.name, p.cmdline),
      el("td", { cls: "num", text: p.pid }),
      el("td", { cls: "num", text: p.cpu_percent === null ? "–" : `${p.cpu_percent.toFixed(1)}%` }),
      el("td", { cls: "num", text: fmtBytes(p.rss_bytes) }),
      el("td", {}, [p.visibility === "ps"
        ? el("span", { cls: "tag", text: "system", title: "Owned by another user; CPU and memory via /bin/ps, command line hidden by macOS" })
        : el("span", { cls: "muted", text: "yours" })]),
    ])));
    $("proc-empty").hidden = rows.length > 0;
  }

  function renderSpikeList() {
    const body = $("spike-table").querySelector("tbody");
    const rows = state.spikes.map((s) => {
      const m = METRICS[s.metric] || { label: s.metric, fmt: (v) => v.toFixed(1) };
      const top = s.culprits[0];
      const cause = el("td");
      if (top) {
        cause.append(el("span", { cls: "cell-main", text: top.app }));
        if (top.attribution === "correlated") cause.append(" ", el("span", { cls: "tag", text: "correlated" }));
      } else {
        cause.append(el("span", { cls: "muted", text: "no visible process" }));
      }
      const lasted = s.ended_at === null
        ? el("span", { cls: "tag tag-strong", text: "▲ ongoing" })
        : document.createTextNode(fmtDuration(s.ended_at - s.started_at));
      const tr = el("tr", {
        cls: "clickable",
        attrs: { tabindex: "0", "aria-selected": String(s.id === state.selected), "data-id": String(s.id) },
      }, [
        el("td", { text: fmtWhen(s.started_at) }),
        el("td", { text: m.label }),
        el("td", { cls: "num", text: m.fmt(s.peak_value) }),
        el("td", { cls: "num muted", text: m.fmt(s.baseline) }),
        el("td", { cls: "num" }, [lasted]),
        cause,
      ]);
      tr.addEventListener("click", () => selectSpike(s.id, false));
      tr.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectSpike(s.id, false); }
      });
      return tr;
    });
    body.replaceChildren(...rows);
    $("spike-empty").hidden = rows.length > 0;
  }

  function renderDetail() {
    const s = state.spikes.find((x) => x.id === state.selected);
    const bodyEl = $("detail-body");
    if (!s) {
      $("detail-title").textContent = "Spike details";
      $("detail-sub").textContent = "";
      bodyEl.replaceChildren(el("p", { cls: "empty", text: "Select a spike in the list, or click a shaded band on a chart." }));
      return;
    }
    const m = METRICS[s.metric] || { label: s.metric, fmt: (v) => v.toFixed(1) };
    $("detail-title").textContent = `${m.label} spike`;
    const end = s.ended_at === null ? "ongoing" : fmtClock(s.ended_at);
    $("detail-sub").textContent = `${fmtWhen(s.started_at)} → ${end}`;

    const fact = (label, value) => el("span", {}, [document.createTextNode(`${label} `), el("b", { text: value })]);
    const facts = el("p", { cls: "facts" }, [
      fact("Peak", m.fmt(s.peak_value)),
      fact("Normal", m.fmt(s.baseline)),
      fact("Lasted", s.ended_at === null ? "ongoing" : fmtDuration(s.ended_at - s.started_at)),
      s.peak_at ? fact("Peak at", fmtClock(s.peak_at)) : null,
    ]);
    const parts = [facts];
    const correlated = s.culprits.some((c) => c.attribution === "correlated");
    if (correlated) {
      parts.push(el("p", {
        cls: "callout",
        text: "Correlated, not measured: macOS doesn't report per-process disk or network use without sudo. " +
              "These processes' CPU jumped above their own normal when the spike began. Treat them as leads.",
      }));
    }
    if (s.culprits.length) {
      const table = el("table", { cls: "table" }, [
        el("thead", {}, [el("tr", {}, [
          el("th", { text: "#" }), el("th", { text: "Process" }), el("th", { cls: "num", text: "PID" }),
          el("th", { cls: "num", text: correlated ? "CPU jump" : "Use" }), el("th", { cls: "num", text: "Share" }),
          el("th", { text: "Evidence" }),
        ])]),
        el("tbody", {}, s.culprits.map((c) => el("tr", {}, [
          el("td", { cls: "muted", text: c.rank }),
          processCell(c.app, c.name, c.cmdline, c.app),
          el("td", { cls: "num", text: c.pid }),
          el("td", { cls: "num", text: fmtCulpritValue(s.metric, c) }),
          el("td", { cls: "num", text: c.share === null ? "–" : `${Math.round(c.share * 100)}%` }),
          el("td", {}, [el("span", { cls: "tag", text: c.attribution === "direct" ? "measured" : "correlated" })]),
        ]))),
      ]);
      parts.push(el("div", { cls: "table-wrap" }, [table]));
    } else {
      parts.push(el("p", { cls: "empty", text: "No visible process stood out." }));
    }
    if (s.metric === "cpu" && s.unattributed) {
      parts.push(el("p", {
        cls: "sub",
        text: `Plus ${s.unattributed.toFixed(0)}% of a core unattributed: kernel_task (invisible without root) and processes too short-lived to sample.`,
      }));
    }
    bodyEl.replaceChildren(...parts);
  }

  function selectSpike(id, scroll) {
    state.selected = state.selected === id ? null : id;
    renderSpikeList();
    renderDetail();
    renderCharts();
    if (scroll && state.selected !== null) $("detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // ---------- leaderboard & battery ----------
  const BOARD_KEYS = {
    cpu: (r) => r.cpu_seconds, battery: (r) => r.cpu_seconds_on_battery,
    wakeups: (r) => r.ctx_switches, memory: (r) => r.peak_rss_bytes,
  };

  function renderLeaderboard() {
    const rows = [...state.board].sort((a, b) => BOARD_KEYS[state.boardSort](b) - BOARD_KEYS[state.boardSort](a));
    // Disk/network columns only exist when the sudo helper supplied them.
    const hasIo = rows.some((r) => r.disk_read_bytes || r.disk_write_bytes || r.net_recv_bytes || r.net_sent_bytes);
    const head = $("board-table").querySelector("thead tr");
    const headings = ["App", "CPU time", "On battery", "Wakeups", "Peak memory",
      ...(hasIo ? ["~Disk r/w", "~Net in/out"] : []), "Spikes"];
    head.replaceChildren(...headings.map((h, i) => el("th", { cls: i > 0 && i < headings.length - 1 ? "num" : "", text: h })));
    const body = $("board-table").querySelector("tbody");
    body.replaceChildren(...rows.map((r) => {
      const spikes = el("td");
      if (r.spikes_caused) spikes.append(el("span", { cls: "tag tag-strong", text: `${r.spikes_caused} caused` }));
      if (r.spikes_caused && r.spikes_suspected) spikes.append(" ");
      if (r.spikes_suspected) spikes.append(el("span", { cls: "tag", text: `${r.spikes_suspected} suspected` }));
      if (!r.spikes_caused && !r.spikes_suspected) spikes.append(el("span", { cls: "muted", text: "–" }));
      const io = hasIo ? [
        el("td", { cls: "num", text: `${fmtBytes(r.disk_read_bytes || 0)} / ${fmtBytes(r.disk_write_bytes || 0)}` }),
        el("td", { cls: "num", text: `${fmtBytes(r.net_recv_bytes || 0)} / ${fmtBytes(r.net_sent_bytes || 0)}` }),
      ] : [];
      return el("tr", {}, [
        el("td", { cls: "cell-name", text: r.app, title: r.app }),
        el("td", { cls: "num", text: fmtCpuTime(r.cpu_seconds) }),
        el("td", { cls: "num", text: fmtCpuTime(r.cpu_seconds_on_battery) }),
        el("td", { cls: "num", text: fmtWakeups(r.ctx_switches) }),
        el("td", { cls: "num", text: fmtBytes(r.peak_rss_bytes) }),
        ...io,
        spikes,
      ]);
    }));
    $("board-empty").hidden = rows.length > 0;
  }

  function bar(label, value, fraction, baseline) {
    const fill = el("div", { cls: `bar-fill${baseline ? " baseline" : ""}` });
    // Set via CSSOM, not a style attribute: the CSP has no 'unsafe-inline'.
    fill.style.width = `${Math.max(1, Math.min(100, fraction * 100)).toFixed(1)}%`;
    return el("div", { cls: "bar-row" }, [
      el("span", { cls: "bar-label", text: label, title: label }),
      el("span", { cls: "bar-value", text: value }),
      el("div", { cls: "bar-track" }, [fill]),
    ]);
  }

  function renderBattery(data) {
    const body = $("battery-body");
    if (!data || !data.available) {
      $("battery-sub").textContent = "";
      body.replaceChildren(el("p", { cls: "empty", text: "No battery readings recorded yet." }));
      return;
    }
    const perHour = data.hours_on_battery > 0 ? data.drained_percent / data.hours_on_battery : 0;
    $("battery-sub").textContent =
      `${data.hours_on_battery.toFixed(1)} h on battery across ${data.sessions} session(s) · ` +
      `${data.drained_percent.toFixed(0)}% used (${perHour.toFixed(1)}%/hour)`;
    const parts = [];
    if (data.model.usable) {
      const total = Math.max(data.drained_percent, 0.01);
      parts.push(bar("Screen, radios, kernel — not any process",
        `${data.baseline_percent.toFixed(1)}%`, data.baseline_percent / total, true));
      parts.push(bar("CPU-driven — split below",
        `${data.cpu_percent_of_drain.toFixed(1)}%`, data.cpu_percent_of_drain / total, false));
      parts.push(el("p", { cls: "sub", text: `Model: ${data.model.note}.` }));
    } else {
      parts.push(el("p", { cls: "callout", text: `Can't split baseline from CPU-driven drain: ${data.model.note}. Ranking by energy score only.` }));
    }
    if (data.apps.length) {
      const maxShare = Math.max(...data.apps.map((a) => a.share), 0.01);
      for (const a of data.apps) {
        const value = a.percent !== null ? `${a.percent.toFixed(1)}%` : `${Math.round(a.share * 100)}% of score`;
        parts.push(bar(a.app, value, a.share / maxShare, false));
      }
      parts.push(el("p", { cls: "sub", text: data.basis === "energy"
        ? "Apps are ranked by macOS energy impact, measured by the powermetrics helper. The baseline/CPU split above is still a model."
        : "Estimate: apps are ranked by CPU time on battery plus a small charge per wakeup. Real per-process energy needs the powermetrics helper (sudo, not enabled)." }));
    } else {
      parts.push(el("p", { cls: "empty", text: "No per-app activity recorded on battery yet." }));
    }
    body.replaceChildren(...parts);
  }

  // ---------- live status ----------
  function setLive(level, text) {
    $("live").dataset.state = level;
    $("live-text").textContent = text;
  }

  // ---------- data loops ----------
  async function loadSeries() {
    state.series = await api(`/api/series?seconds=${state.seconds}`);
    renderCharts();
  }
  async function loadSpikes() {
    const { spikes } = await api(`/api/spikes?seconds=${state.seconds}&limit=500`);
    state.spikes = spikes;
    if (state.selected !== null && !spikes.some((s) => s.id === state.selected)) state.selected = null;
    renderSpikeList();
    renderDetail();
    renderCharts();
  }
  async function loadBoard() {
    const [board, batt] = await Promise.all([
      api(`/api/leaderboard?seconds=${Math.max(3600, state.seconds)}&limit=15`),
      api(`/api/battery?seconds=${Math.max(3600, state.seconds)}`),
    ]);
    state.board = board.apps;
    renderLeaderboard();
    renderBattery(batt);
    $("board-sub").textContent =
      `Cumulative per app over the ${state.seconds >= 86400 ? "last 24 hours" : "selected range"} ` +
      "(every process counted). Per-app disk and network need sudo helpers.";
  }

  async function loadNow() {
    const { latest, stale } = await api("/api/now");
    renderTiles(latest);
    renderProcesses(latest);
    if (!latest) setLive("warning", "Waiting for first samples…");
    else if (stale) setLive("warning", `No new samples since ${fmtClock(latest.system.ts)}`);
    else setLive("good", `Live · ${fmtClock(latest.system.ts)}`);
  }

  // Polls fn; the first run always happens, repeats pause while the tab is hidden (saves CPU).
  function every(fn, seconds) {
    let first = true;
    const run = async () => {
      if (state.stopped) return;
      if (first || !document.hidden) {
        first = false;
        try {
          await fn();
        } catch (err) {
          if (err instanceof Unauthorized) { stopAll(); showGate(true); return; }
          setLive("critical", "Can't reach whyslow");
        }
      }
      setTimeout(run, seconds() * 1000);
    };
    run();
  }
  function stopAll() { state.stopped = true; }

  function seriesCadence() {
    const s = state.series;
    const interval = state.meta ? state.meta.interval : 1;
    return Math.max(interval, s && s.aggregated ? s.bucket : interval, 1);
  }

  function setRange(seconds, label) {
    state.seconds = seconds;
    $("range-note").dataset.label = label.toLowerCase();
    for (const b of $("range").querySelectorAll("button")) b.setAttribute("aria-pressed", String(Number(b.dataset.seconds) === seconds));
    $("charts").classList.add("loading");   // keep the old frame, dimmed, while refetching
    Promise.all([loadSeries(), loadSpikes(), loadBoard()]).catch(() => {})
      .finally(() => $("charts").classList.remove("loading"));
  }

  // ---------- boot ----------
  async function main() {
    const ok = await login().catch(() => false);
    if (!ok) { showGate(false); return; }
    try {
      state.meta = await api("/api/meta");
    } catch (err) {
      if (err instanceof Unauthorized) { try { sessionStorage.removeItem(TOKEN_KEY); } catch (_) {} showGate(true); }
      else setLive("critical", "Can't reach whyslow");
      return;
    }
    $("gate").hidden = true;
    $("main").hidden = false;
    $("mode").textContent = `Mode: ${state.meta.mode}`;
    $("notes-list").replaceChildren(...state.meta.notes.map((n) => el("li", { text: n })));

    theme = tokens();
    CHARTS.forEach(buildChart);
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      theme = tokens();
      Object.values(charts).forEach((c) => { applyTheme(c); c.chart.update("none"); });
    });

    for (const b of $("range").querySelectorAll("button")) {
      b.addEventListener("click", () => setRange(Number(b.dataset.seconds), b.textContent));
    }
    $("range-note").dataset.label = "last 15 min";

    for (const b of $("board-sort").querySelectorAll("button")) {
      b.addEventListener("click", () => {
        state.boardSort = b.dataset.sort;
        for (const other of $("board-sort").querySelectorAll("button")) {
          other.setAttribute("aria-pressed", String(other === b));
        }
        renderLeaderboard();
      });
    }

    every(loadSeries, seriesCadence);
    every(loadBoard, () => 30);
    every(loadSpikes, () => 5);
    every(loadNow, () => Math.max(2, state.meta.interval));
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) Promise.all([loadSeries(), loadSpikes(), loadNow(), loadBoard()]).catch(() => {});
    });
  }

  document.addEventListener("DOMContentLoaded", main);
})();
