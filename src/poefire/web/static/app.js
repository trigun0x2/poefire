"use strict";

const $ = (id) => document.getElementById(id);
let status = null;
let statusAt = 0; // performance.now() when status arrived (for the ticking timer)
let runsData = null;
let selectedRun = null; // run id pinned by the user; null = follow the latest run
let lastRunKey = "";

// --- formatting ---

const nf0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function duration(sec) {
  if (sec == null) return "—";
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}
function chaos(v) {
  if (v == null) return '<span class="dim">—</span>';
  const cls = v < 0 ? "neg" : "chaos";
  const txt = Math.abs(v) >= 100 ? nf0.format(v) : nf1.format(v);
  return `<span class="${cls}">${txt}c</span>`;
}
function divine(v) {
  return v == null ? '<span class="dim">—</span>' : `<span class="divine">${nf2.format(v)}d</span>`;
}
function timeOf(iso) {
  return iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—";
}
function ago(unix) {
  if (!unix) return "never";
  const min = Math.round((Date.now() / 1000 - unix) / 60);
  return min < 1 ? "just now" : min < 60 ? `${min} min ago` : `${Math.round(min / 60)} h ago`;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

// --- api ---

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", "X-Poefire": "1", ...(opts.headers || {}) },
  });
  if (!r.ok) {
    const detail = await r.json().then((b) => b.detail).catch(() => null);
    throw new Error(typeof detail === "string" ? detail : `${r.status} ${path}`);
  }
  return r.json();
}

// --- rendering ---

function renderStatus() {
  const s = status;
  const p = s.prices;
  const pd = $("prices-pill").querySelector(".dot");
  pd.className = "dot " + (p.refreshing ? "busy" : !p.fetched_at ? "bad" : p.error ? "warn" : "ok");
  $("prices-text").textContent = p.refreshing
    ? "Updating prices…"
    : !p.fetched_at
      ? "No prices yet"
      : `Prices${p.league !== s.league ? ` (${p.league})` : ""} ${ago(p.fetched_at)}` +
        (p.chaos_per_divine ? ` · 1d = ${nf0.format(p.chaos_per_divine)}c` : "");
  $("prices-pill").title = p.error ? `Last error: ${p.error}` : "Refresh prices from poe.ninja";

  const l = s.loot;
  const expired = l.session?.expired;
  const ld = $("loot-pill").querySelector(".dot");
  ld.className = "dot " + (!l.enabled || expired ? "bad" : l.last && !l.last.complete ? "warn" : "ok");
  $("loot-text").textContent = expired
    ? "POESESSID expired · update it"
    : !l.enabled
      ? "Loot tracking off"
      : l.last ? `Snapshot ${timeOf(l.last.at)}${l.last.complete ? "" : " failed"}` : "Loot tracking on";
  $("loot-pill").title = expired
    ? "The website rejected your POESESSID. Click to paste a new one in Settings."
    : !l.enabled ? `${l.reason} (click to open Settings)` : l.last?.error || "Take a snapshot now";
  renderSessionBanner(l.session);
  renderUpdate(s.update);
  $("last-empty").innerHTML = s.log_path
    ? "No map finished yet. Stats appear when you're back in your hideout."
    : 'Client.txt not found. Set its location in <a href="#settings">Settings</a>.';

  // Current run: map, elapsed time, level and deaths, in the sidebar.
  const run = s.run;
  $("side-now").hidden = !run;
  if (run) {
    $("now-name").textContent = run.map_name;
    $("side-now").classList.toggle("paused", run.status !== "active");
    $("now-meta").textContent = `Level ${run.area_level} · ${run.deaths} death${run.deaths === 1 ? "" : "s"}` +
      (run.status === "active" ? "" : " · in hideout");
    $("now-dot").className = `live-dot${run.status === "active" ? "" : " paused"}`;
    $("side-now").title = run.status === "active" ? "In map" : "Paused: in hideout";
  }
  const v = s.value;
  $("side-value").innerHTML = !v ? '<span class="dim">—</span>'
    : v.divine != null ? `<span class="divine">${nf2.format(v.divine)}d</span>` : chaos(v.chaos);
  $("side-value").title = v ? `${nf0.format(v.chaos)}c at ${timeOf(v.at)}` : "No snapshot yet";
  tickTimer();
}

function tickTimer() {
  const run = status?.run;
  if (!run) return;
  const extra = run.status === "active" ? (performance.now() - statusAt) / 1000 : 0;
  $("now-timer").textContent = duration(run.active_seconds + extra);
}

function since(unix) {
  if (!unix) return "never";
  const s = Date.now() / 1000 - unix;
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
function span(seconds) {
  if (seconds == null) return "—";
  if (seconds < 5400) return `${Math.max(1, Math.round(seconds / 60))} min`;
  if (seconds < 172800) return `${(seconds / 3600).toFixed(1)} hours`;
  return `${(seconds / 86400).toFixed(1)} days`;
}
function dateOf(unix) {
  return new Date(unix * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// --- updates ---

function dismissedUpdate() {
  try { return localStorage.getItem("poefire.update.dismissed"); } catch { return null; }
}

function renderUpdate(u) {
  const a = u?.available;
  $("update-row").hidden = !a;
  if (a) $("update-text").textContent = `Update: v${a.version}${a.prerelease ? " (beta)" : ""}`;
  const b = $("update-banner");
  const busy = u && (u.stage === "downloading" || u.stage === "installing");
  const show = a && (busy || u.stage === "failed" || dismissedUpdate() !== a.version);
  b.hidden = !show;
  if (!show) return;
  const key = `${a.version}:${u.stage}:${u.progress[0]}:${u.error}`;
  if (b.dataset.key === key) return; // don't rebuild (keeps the notes open) unless it changed
  b.dataset.key = key;
  let actions;
  if (u.stage === "downloading" || u.stage === "installing") {
    const [done, total] = u.progress;
    const pct = total ? Math.round((done / total) * 100) : 0;
    actions = u.stage === "installing"
      ? "<span>Installing… poefire will restart</span>"
      : `<span class="progress"><i style="width:${pct}%"></i></span><span>${pct}%</span>`;
  } else if (u.can_install) {
    actions = '<button type="button" class="btn primary" data-act="install">Install and restart</button>' +
      '<button type="button" class="btn" data-act="later">Later</button>';
  } else {
    actions = `<a class="btn primary" href="${esc(a.page_url)}" target="_blank" rel="noopener">Download</a>` +
      '<button type="button" class="btn" data-act="later">Later</button>';
  }
  const failed = u.stage === "failed" ? `<div class="bad">Update failed: ${esc(u.error || "unknown error")}</div>` : "";
  const why = !u.can_install && u.cannot_install_reason ? `<div class="hint">${esc(u.cannot_install_reason)}</div>` : "";
  const notes = a.notes ? `<details><summary>What's new</summary><pre>${esc(a.notes)}</pre></details>` : "";
  b.innerHTML = `<div><b>poefire v${esc(a.version)} is available</b> <span class="muted">(you have v${esc(u.current)})</span>` +
    `${failed}${why}${notes}</div><div class="actions">${actions}</div>`;
}

$("update-banner").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  if (btn.dataset.act === "later") {
    try { localStorage.setItem("poefire.update.dismissed", status.update.available.version); } catch { /* ignore */ }
    $("update-banner").hidden = true;
    return;
  }
  btn.disabled = true;
  await api("/api/update/install", { method: "POST" }).catch((err) => alert(err.message));
  pollStatus();
});
$("update-row").addEventListener("click", () => {
  try { localStorage.removeItem("poefire.update.dismissed"); } catch { /* ignore */ }
  $("update-banner").dataset.key = "";
  renderUpdate(status?.update);
});

function renderSessionBanner(h) {
  const b = $("session-banner");
  const show = h?.expired && location.hash !== "#settings";
  b.hidden = !show;
  if (!show) return;
  const lasted = h.worked_seconds ? ` It worked for ${span(h.worked_seconds)} after you saved it.` : "";
  b.innerHTML = `<span><b>Your POESESSID expired ${since(h.expired_at)}.</b> Loot tracking is paused until you paste a new one.${lasted}</span>` +
    '<a class="btn primary" href="#settings">Update in Settings</a>';
}

function renderSidebarStats() {
  const d = currentRuns || runsData;
  const m = d.last_map, r = d.recent;
  $("side-last-title").textContent = m ? `Last map · ${m.map_name}` : "Last map";
  $("side-last-loot").innerHTML = m && m.loot_tracked ? chaos(m.chaos) : '<span class="dim">—</span>';
  $("side-avg").innerHTML = r.maps ? chaos(r.avg_chaos) : '<span class="dim">—</span>';
  $("side-maps").textContent = d.session.runs ?? 0;
}

function renderLastMap() {
  const m = runsData.last_map;
  $("last-empty").hidden = !!m;
  $("last-body").hidden = !m;
  if (!m) return;
  $("last-name").textContent = m.map_name;
  $("last-meta").textContent = `Level ${m.area_level} · ` +
    (m.status === "finished" ? `finished ${timeOf(m.ended_at)}` : "just now · back in hideout");
  $("last-value").innerHTML = m.loot_tracked
    ? `<span class="big">${chaos(m.chaos)}</span><span class="sub">${divine(m.divine)}</span>`
    : '<span class="dim">No loot data for this map</span>';
  $("last-time").textContent = duration(m.active_seconds);
  $("last-deaths").innerHTML = m.deaths || '<span class="dim">0</span>';
  const r = runsData.recent;
  if (m.loot_tracked && r.maps) {
    const d = m.chaos - r.avg_chaos;
    $("last-vs").innerHTML = `<span class="${d >= 0 ? "good" : "neg"}">${d >= 0 ? "+" : "−"}${nf1.format(Math.abs(d))}c</span>`;
  } else {
    $("last-vs").innerHTML = '<span class="dim">—</span>';
  }
  $("last-drops").innerHTML = (m.top_items || []).map((i) => `
    <li>
      ${i.icon ? `<img src="${esc(i.icon)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'noicon'}))">` : '<span class="noicon"></span>'}
      <span class="name" title="${esc(i.name)}">${esc(i.name)}</span>
      <span class="qty">+${i.quantity}</span>
      <span class="val">${chaos(i.chaos_total)}</span>
    </li>`).join("");
}

function tile(label, value, sub = "", hero = false) {
  return `<div class="tile${hero ? " hero" : ""}"><div class="label">${label}</div>` +
    `<div class="value">${value}</div><div class="sub">${sub}</div></div>`;
}

function renderSession() {
  const s = runsData.session;
  const r = runsData.recent;
  const past = viewSession != null;
  $("session-title").textContent = past ? "Past session" : "This session";
  $("session-new").hidden = past;
  $("session-back").hidden = !past;
  $("session-since").textContent = !s.started_at ? ""
    : past ? `· ${dateTimeOf(s.started_at)} – ${timeOf(s.ended_at)}` : `· since ${dateTimeOf(s.started_at)}`;
  if (!s.runs) {
    $("tiles").innerHTML = tile("Maps", "0", "No maps yet");
    return;
  }
  const tracked = s.loot_tracked_runs;
  const lootSub = tracked < s.runs ? `${tracked} of ${s.runs} runs with loot data` : "net loot value";
  const recentSub = !r.maps ? "no finished maps yet"
    : (r.avg_divine == null ? "" : `${nf2.format(r.avg_divine)}d · `) +
      (r.maps < r.of || past ? `over ${r.maps} map${r.maps === 1 ? "" : "s"}` : `last ${r.of} maps`);
  $("tiles").innerHTML = [
    tile("Avg chaos / map", r.maps ? `${nf1.format(r.avg_chaos)}c` : "—", recentSub, true),
    tile("Avg map time", r.maps ? duration(r.avg_seconds) : "—", r.maps ? `${nf1.format(r.avg_deaths)} deaths / map` : ""),
    tile("Total loot", s.chaos == null ? "—" : `${nf0.format(s.chaos)}c`,
      s.divine == null ? lootSub : `${nf2.format(s.divine)}d · ${lootSub}`),
    tile("Best run", s.best_run ? `${nf0.format(s.best_run.chaos)}c` : "—", s.best_run ? esc(s.best_run.map_name) : ""),
    tile("Runs", s.runs, `avg ${duration(s.avg_run_seconds)}`),
    tile("Map time", duration(s.active_seconds), `${duration(s.wall_seconds)} played`),
    tile("Deaths", s.deaths, s.runs ? `${nf1.format(s.deaths / s.runs)} per run` : ""),
    tile("Prices", status?.prices.chaos_per_divine ? `${nf0.format(status.prices.chaos_per_divine)}c` : "—", "per divine (poe.ninja)"),
  ].join("");
}

// --- sortable tables ---

function loadSort(name, fallback) {
  try {
    const v = JSON.parse(localStorage.getItem(`poefire.sort.${name}`) || "null");
    if (v && typeof v.key === "string" && (v.dir === 1 || v.dir === -1)) return v;
  } catch { /* storage unavailable */ }
  return fallback;
}
function saveSort(name, state) {
  try { localStorage.setItem(`poefire.sort.${name}`, JSON.stringify(state)); } catch { /* ignore */ }
}

/** Sorted copy: strings A→Z / numbers ascending for dir 1; missing values always last. */
function sortRows(rows, key, dir) {
  return rows.slice().sort((a, b) => {
    const x = a[key], y = b[key];
    const xm = x == null, ym = y == null;
    if (xm || ym) return xm === ym ? 0 : xm ? 1 : -1;
    const c = typeof x === "string" && typeof y === "string"
      ? x.localeCompare(y, undefined, { sensitivity: "base", numeric: true })
      : x - y;
    return c * dir;
  });
}

/** Clickable headers: th[data-sort]; first click sorts text A→Z and numbers high→low. */
function makeSortable(tableId, name, state, rerender) {
  const table = $(tableId);
  const paint = () => table.querySelectorAll("th[data-sort]").forEach((th) => {
    th.setAttribute("aria-sort", th.dataset.sort !== state.key ? "none" : state.dir === 1 ? "ascending" : "descending");
  });
  table.querySelector("thead").addEventListener("click", (e) => {
    const th = e.target.closest("th[data-sort]");
    if (!th) return;
    if (state.key === th.dataset.sort) state.dir = -state.dir;
    else {
      state.key = th.dataset.sort;
      state.dir = th.classList.contains("num") ? -1 : 1;
    }
    saveSort(name, state);
    paint();
    rerender();
  });
  paint();
}

const runsSort = loadSort("runs", { key: "started_at", dir: -1 });
const itemsSort = loadSort("items", { key: "chaos_total", dir: -1 });

function renderRuns() {
  const inSession = new Set(runsData.session_run_ids);
  const rows = sortRows(runsData.runs, runsSort.key, runsSort.dir);
  $("runs-empty").hidden = rows.length > 0;
  const current = selectedRun ?? runsData.runs[0]?.id; // newest run, whatever the sort
  $("runs-body").innerHTML = rows.map((r) => {
    const live = r.status !== "finished"
      ? `<span class="live-dot ${r.status === "paused" ? "paused" : ""}" title="${r.status}"></span>` : "";
    const cls = [r.id === current ? "selected" : "", inSession.has(r.id) ? "" : "out-of-session"].join(" ");
    return `<tr data-id="${esc(r.id)}" class="${cls}">
      <td>${timeOf(r.started_at)}</td>
      <td>${live}${esc(r.map_name)}</td>
      <td class="num">${r.area_level}</td>
      <td class="num">${duration(r.active_seconds)}</td>
      <td class="num">${r.deaths || '<span class="dim">0</span>'}</td>
      <td class="num">${chaos(r.chaos)}</td>
      <td class="num">${divine(r.divine)}</td>
    </tr>`;
  }).join("");
}

function itemList(items, limit = 40) {
  if (!items.length) return '<div class="empty">Nothing changed.</div>';
  const shown = items.slice(0, limit);
  const more = items.length - shown.length;
  return `<ul class="items">${shown.map((i) => `
    <li>
      ${i.icon ? `<img src="${esc(i.icon)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'noicon'}))">` : '<span class="noicon"></span>'}
      <span class="name" title="${esc(i.name)}">${esc(i.name)}</span>
      <span class="qty">${i.quantity > 0 ? "+" : ""}${i.quantity}</span>
      <span class="val">${i.chaos_total == null ? '<span class="dim">no price</span>' : chaos(i.chaos_total)}</span>
    </li>`).join("")}</ul>` + (more > 0 ? `<div class="note">+${more} more</div>` : "");
}

async function renderDetail() {
  const id = selectedRun ?? runsData?.runs[0]?.id;
  if (!id) {
    $("detail-title").textContent = "Run loot";
    $("detail").innerHTML = '<div class="empty">Select a run to see what dropped.</div>';
    return;
  }
  const d = await api(`/api/runs/${encodeURIComponent(id)}`);
  $("detail-title").textContent = `Run loot · ${d.map_name}`;
  if (!d.loot_tracked) {
    $("detail").innerHTML = `<div class="empty">No loot data for this run. ${
      status?.loot.enabled ? "A snapshot is taken when you return to your hideout." : esc(status?.loot.reason || "")}</div>`;
    return;
  }
  const before = d.before.items.length
    ? `<div class="section-label">Before the run (hideout: map used, trades, crafting) · ${chaos(d.before.chaos)}</div>${itemList(d.before.items, 10)}`
    : "";
  $("detail").innerHTML = `
    <div class="detail-head">
      <div class="big">${chaos(d.loot.chaos)} <span class="muted">·</span> ${divine(d.loot.divine)}</div>
      <div class="muted">${duration(d.active_seconds)} · ${d.deaths} death${d.deaths === 1 ? "" : "s"}</div>
    </div>
    ${itemList(d.loot.items)}
    ${d.loot.hidden ? `<div class="note">${d.loot.hidden} item kind${d.loot.hidden === 1 ? "" : "s"} with no price or worth under 0.05c hidden.</div>` : ""}
    ${before}`;
}

// --- loops ---

async function pollStatus() {
  try {
    status = await api("/api/status");
    statusAt = performance.now();
    renderStatus();
    const key = (status.run ? `${status.run.id}:${status.run.status}:${status.run.deaths}:${status.run.chaos}` : "") +
      `:${status.loot.last?.at}`;
    if (key !== lastRunKey) {
      lastRunKey = key;
      await pollRuns();
      if (!$("overview-view").hidden) loadOverview().catch(() => {}); // a new snapshot
    }
  } catch (e) {
    console.error(e);
    if (e instanceof TypeError) $("prices-text").textContent = "Disconnected"; // fetch failed
  }
}

let viewSession = null; // a past session's id; null = the current session
let currentRuns = null; // the current session (the sidebar always shows it)
let sessionsData = null;

async function pollRuns() {
  currentRuns = await api("/api/runs?limit=200");
  runsData = viewSession == null ? currentRuns : await api(`/api/runs?session=${viewSession}&limit=500`);
  renderSession();
  renderLastMap();
  renderSidebarStats();
  renderRuns();
  await renderDetail();
  loadSessions().catch(() => {});
}

async function loadSessions() {
  sessionsData = await api("/api/sessions");
  renderSessions();
}

const sessionsSort = loadSort("sessions", { key: "started_at", dir: -1 });

function renderSessions() {
  const rows = sortRows(sessionsData.sessions, sessionsSort.key, sessionsSort.dir);
  const shown = runsData?.session?.id;
  $("sessions-empty").hidden = rows.length > 0;
  $("sessions-body").innerHTML = rows.map((s) => {
    const live = s.current ? '<span class="live-dot" title="current session"></span>' : "";
    const avg = s.avg_chaos == null ? '<span class="dim">—</span>'
      : `${chaos(s.avg_chaos)}${s.avg_divine == null ? "" : ` <span class="dim">· ${nf2.format(s.avg_divine)}d</span>`}`;
    const total = s.chaos == null ? '<span class="dim">—</span>'
      : `${chaos(s.chaos)}${s.divine == null ? "" : ` <span class="dim">· ${nf2.format(s.divine)}d</span>`}`;
    const del = s.current ? "" : `<button type="button" class="del" data-del="${s.id}" title="Delete this session and its maps">✕</button>`;
    return `<tr data-session="${s.id}" class="${s.id === shown ? "selected" : ""}">
      <td>${live}${s.current ? "Current · " : ""}${s.started_at ? dateTimeOf(s.started_at) : "—"}</td>
      <td class="num">${duration(s.wall_seconds)}</td>
      <td class="num">${s.maps}</td>
      <td class="num">${s.avg_map_seconds == null ? "—" : duration(s.avg_map_seconds)}</td>
      <td class="num">${avg}</td>
      <td class="num">${total}</td>
      <td class="num">${s.deaths || '<span class="dim">0</span>'}</td>
      <td class="num">${del}</td>
    </tr>`;
  }).join("");
}

async function showSession(id) {
  viewSession = id;
  selectedRun = null;
  await pollRuns();
}

$("sessions-body").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]");
  if (del) {
    e.stopPropagation();
    const s = sessionsData.sessions.find((x) => String(x.id) === del.dataset.del);
    if (!confirm(`Delete the session from ${dateTimeOf(s?.started_at)} and its ${s?.maps ?? 0} maps? This can't be undone.`)) return;
    try {
      await api(`/api/sessions/${del.dataset.del}/delete`, { method: "POST" });
      if (String(viewSession) === del.dataset.del) viewSession = null;
      await pollRuns();
    } catch (err) { alert(err.message); }
    return;
  }
  const tr = e.target.closest("tr[data-session]");
  if (!tr) return;
  const s = sessionsData.sessions.find((x) => String(x.id) === tr.dataset.session);
  await showSession(s?.current ? null : Number(tr.dataset.session));
});
$("session-back").addEventListener("click", () => showSession(null));
$("session-new").addEventListener("click", async () => {
  if (!confirm("Start a new session? Stats start again from zero; this session moves to the Sessions history.")) return;
  await api("/api/sessions/new", { method: "POST" });
  viewSession = null;
  selectedRun = null;
  await pollRuns();
});

async function loadLeagues() {
  const l = await api("/api/leagues");
  $("league").innerHTML = l.options.map((o) => `<option${o === l.current ? " selected" : ""}>${esc(o)}</option>`).join("") +
    '<option value="__other__">Other league…</option>';
}

$("league").addEventListener("change", async (e) => {
  let league = e.target.value;
  if (league === "__other__") {
    league = (prompt("League name (private leagues look like \"My League (PL12345)\"):") || "").trim();
    if (!league) { await loadLeagues(); return; }
  }
  await api("/api/league", { method: "POST", body: JSON.stringify({ league }) });
  await loadLeagues();
  await pollStatus();
  await pollRuns();
});
$("prices-pill").addEventListener("click", () => api("/api/prices/refresh", { method: "POST" }).then(pollStatus));
$("loot-pill").addEventListener("click", () => {
  if (status?.loot.enabled && !status.loot.session?.expired) api("/api/snapshot", { method: "POST" }).catch(() => {});
  else location.hash = "#settings";
});
$("runs-body").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-id]");
  if (!tr) return;
  selectedRun = tr.dataset.id === runsData.runs[0]?.id ? null : tr.dataset.id;
  renderRuns();
  renderDetail();
});

// --- settings page ---

let settings = null;
let account = null; // result of the last connection check: characters + tabs

function setStatus(id, text, cls = "") {
  const el = $(id);
  el.className = `hint ${cls}`;
  el.textContent = text;
}

async function loadSettings() {
  settings = await api("/api/settings");
  const s = settings;
  $("set-log").value = s.log_path;
  $("set-log").placeholder = s.log_path_detected || s.log_path_candidates[0];
  if (s.log_path_exists) setStatus("set-log-status", `✓ Reading ${s.log_path_effective}`, "ok");
  else if (s.log_path_effective) setStatus("set-log-status", `✗ ${s.log_path_effective} not found`, "bad");
  else setStatus("set-log-status", "✗ Client.txt not found in the usual install folders; enter its location", "bad");
  $("set-log-detected").hidden = !s.log_path_detected || s.log_path === s.log_path_detected;
  $("set-log-detected-path").textContent = s.log_path_detected || "";

  $("set-account").value = s.account_name;
  const chip = $("set-session-chip");
  const h = s.session_health;
  chip.className = `chip${h.expired ? " bad" : s.session_saved ? " ok" : ""}`;
  chip.textContent = s.session_from_env ? "set by POE_SESSID variable" : h.expired ? "expired" : s.session_saved ? "saved" : "not set";
  const lines = [];
  if (h.saved_at) {
    lines.push(h.expired
      ? `Rejected by the website ${since(h.expired_at)} (${dateOf(h.expired_at)}), ${h.last_ok ? `after working for ${span(h.worked_seconds)}` : "without ever working: check the account name"}. Paste a new one.`
      : `Saved ${dateOf(h.saved_at)}${h.last_ok ? ` · last worked ${since(h.last_ok)} · working for ${span(h.worked_seconds)}` : " · not used yet"}.`);
  }
  const past = h.history.filter((x) => x.lasted_seconds != null);
  if (past.length) {
    lines.push(`Previous cookies lasted: ${past.map((x) => `${span(x.lasted_seconds)}${x.expired ? "" : " (replaced while still working)"}`).join(", ")}.`);
  }
  $("set-session-health").className = `hint${h.expired ? " bad" : ""}`;
  $("set-session-health").textContent = lines.join(" ");
  $("set-session-clear").disabled = !s.session_saved || s.session_from_env;

  const leagues = (await api("/api/leagues")).options;
  $("set-league").value = s.league;
  $("set-league-options").innerHTML = leagues.map((l) => `<option value="${esc(l)}"></option>`).join("");
  $("set-price-league").value = s.price_league;
  $("set-price-league").placeholder = `Automatic: ${s.price_league_effective}`;
  $("set-price-options").innerHTML = s.ninja_leagues.map((l) => `<option value="${esc(l)}"></option>`).join("");
  renderLeagueHints();
  $("set-run-start").checked = s.snapshot_on_run_start;
  $("set-version").textContent = `v${s.version}`;
  $("set-check-updates").checked = s.check_updates;
  $("set-update-channel").value = s.update_channel;
  $("set-delay").value = s.snapshot_delay;
  $("set-deaths").value = s.characters.join(", ");
  renderDeathNames();
  renderAccountPickers();
  setStatus("set-save-status", s.loot.enabled ? "Loot tracking is on." : `Loot tracking is off: ${s.loot.reason}`, s.loot.enabled ? "ok" : "warn");
}

const PRIVATE_RE = /\(PL\d+\)\s*$/;

function renderLeagueHints() {
  const league = $("set-league").value.trim();
  const priv = PRIVATE_RE.test(league);
  const chars = account?.characters || [];
  const current = chars.find((c) => c.current);
  let hint = priv ? "Private league: stash and inventory are read from it as usual. " : "";
  if (current && current.league && current.league !== league) {
    hint += `Your current character ${esc(current.name)} is in <a href="#" id="set-league-use">${esc(current.league)}</a>.`;
  } else if (!priv) {
    hint += 'For a private league, enter its full name, e.g. <code>My League (PL12345)</code>.';
  }
  $("set-league-hint").innerHTML = hint;
  $("set-league-use")?.addEventListener("click", (e) => {
    e.preventDefault();
    $("set-league").value = current.league;
    renderLeagueHints();
    renderAccountPickers();
  });
  $("set-price-hint").innerHTML = priv
    ? "poe.ninja has no prices for private leagues. Leave empty to use the current challenge league, " +
      "or pick the league yours is based on (e.g. the hardcore one for a hardcore private league)."
    : "Leave empty to price from the league you play.";
}

function renderDeathNames() {
  const seen = settings.log_characters || [];
  if (!seen.length) {
    $("set-deaths-seen").innerHTML = '<span class="hint">No deaths or level-ups in the log yet. Character names show up here once they appear.</span>';
    return;
  }
  const typed = $("set-deaths").value.split(",").map((x) => x.trim().toLowerCase()).filter(Boolean);
  $("set-deaths-seen").innerHTML = '<span class="hint">Seen in your log (click to add):</span>' + seen.map((c) => {
    const counted = !typed.length || typed.includes(c.name.toLowerCase());
    return `<button type="button" class="name-chip ${counted ? "on" : "off"}" data-name="${esc(c.name)}" title="${counted ? "Deaths counted" : "Deaths ignored: not in the list"}">` +
      `${esc(c.name)}<span>${c.deaths} death${c.deaths === 1 ? "" : "s"} · ${counted ? "counted" : "ignored"}</span></button>`;
  }).join("");
}

function renderAccountPickers() {
  const s = settings;
  const chars = account?.characters || [];
  const opts = [`<option value="">Current character (auto)</option>`]
    .concat(chars.filter((c) => c.league === $("set-league").value || c.name === s.character).map((c) =>
      `<option value="${esc(c.name)}"${c.name === s.character ? " selected" : ""}>${esc(c.name)} · ${esc(c.class || "")} ${c.level ?? ""}</option>`));
  if (s.character && !chars.some((c) => c.name === s.character)) {
    opts.push(`<option value="${esc(s.character)}" selected>${esc(s.character)}</option>`);
  }
  $("set-character").innerHTML = opts.join("");

  const chosen = new Set(s.stash_tabs.map((t) => t.toLowerCase()));
  const tabs = account?.tabs || [];
  const known = new Set();
  let html = tabs.map((t) => {
    const on = chosen.has(String(t.name).toLowerCase()) || chosen.has(String(t.id).toLowerCase());
    if (on) { known.add(String(t.name).toLowerCase()); known.add(String(t.id).toLowerCase()); }
    return `<label><input type="checkbox" value="${esc(t.name)}"${on ? " checked" : ""}>${esc(t.name)}<span class="type">${esc(String(t.type || "").replace(/Stash$/, ""))}</span></label>`;
  }).join("");
  // tabs saved earlier but not in the loaded list (or nothing loaded yet)
  html += s.stash_tabs.filter((t) => !known.has(t.toLowerCase())).map((t) =>
    `<label><input type="checkbox" value="${esc(t)}" checked>${esc(t)}</label>`).join("");
  $("set-tabs").innerHTML = html || '<span class="hint">Check the connection above to load your stash tabs.</span>';
}

async function checkAccount() {
  setStatus("set-check-result", "Checking…");
  $("set-check").disabled = true;
  try {
    const r = await api("/api/settings/check-account", { method: "POST" });
    if (!r.ok) { setStatus("set-check-result", `✗ ${r.error}`, "bad"); return; }
    account = r;
    const inLeague = r.characters.filter((c) => c.league === $("set-league").value).length;
    setStatus("set-check-result", `✓ Connected: ${r.characters.length} characters (${inLeague} in ${$("set-league").value}), ${r.tabs.length} stash tabs`, "ok");
    const leagues = (await api("/api/leagues")).options; // now includes your characters' leagues
    $("set-league-options").innerHTML = leagues.map((l) => `<option value="${esc(l)}"></option>`).join("");
    renderLeagueHints();
    renderAccountPickers();
  } catch (e) {
    setStatus("set-check-result", `✗ ${e.message}`, "bad");
  } finally {
    $("set-check").disabled = false;
  }
}

function showWarnings(r, okText) {
  const w = r.warnings || [];
  if (w.length) setStatus("set-save-status", `Saved. ${w.join(" ")}`, "warn");
  else setStatus("set-save-status", r.loot.enabled ? okText : `${okText} Loot tracking is off: ${r.loot.reason}`, r.loot.enabled ? "ok" : "warn");
}

$("set-log-use").addEventListener("click", () => { $("set-log").value = settings.log_path_detected; });
$("set-league").addEventListener("input", () => { renderLeagueHints(); renderAccountPickers(); });

$("set-session-save").addEventListener("click", async () => {
  const value = $("set-session").value.trim();
  if (!value) { setStatus("set-check-result", "Paste the cookie value first.", "warn"); return; }
  try {
    // save the account name too: the cookie is checked against it
    await api("/api/settings", { method: "POST", body: JSON.stringify({ account_name: $("set-account").value }) });
    const r = await api("/api/settings/session", { method: "POST", body: JSON.stringify({ poesessid: value }) });
    $("set-session").value = "";
    await loadSettings();
    showWarnings(r, "Cookie saved.");
    if (r.account_name) await checkAccount();
  } catch (e) {
    setStatus("set-check-result", `✗ ${e.message}`, "bad");
  }
});

$("set-session-clear").addEventListener("click", async () => {
  if (!confirm("Remove the saved POESESSID from this PC?")) return;
  await api("/api/settings/session/clear", { method: "POST" });
  account = null;
  await loadSettings();
  setStatus("set-check-result", "Cookie removed.");
});

$("set-check").addEventListener("click", checkAccount);
$("set-update-check").addEventListener("click", async () => {
  setStatus("set-update-result", "Checking…");
  try {
    const u = await api("/api/update/check", { method: "POST" });
    if (u.error) setStatus("set-update-result", `✗ ${u.error}`, "bad");
    else if (u.available) setStatus("set-update-result", `v${u.available.version} is available, see the banner at the top.`, "ok");
    else setStatus("set-update-result", "You're on the latest version.", "ok");
    try { localStorage.removeItem("poefire.update.dismissed"); } catch { /* ignore */ }
    if (status) { status.update = u; renderUpdate(u); }
  } catch (e) {
    setStatus("set-update-result", `✗ ${e.message}`, "bad");
  }
});
$("set-deaths").addEventListener("input", renderDeathNames);
$("set-deaths-seen").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-name]");
  if (!b) return;
  const names = $("set-deaths").value.split(",").map((x) => x.trim()).filter(Boolean);
  if (!names.some((n) => n.toLowerCase() === b.dataset.name.toLowerCase())) names.push(b.dataset.name);
  $("set-deaths").value = names.join(", ");
  renderDeathNames();
});

$("settings-view").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    log_path: $("set-log").value,
    account_name: $("set-account").value,
    league: $("set-league").value.trim(),
    price_league: $("set-price-league").value.trim(),
    character: $("set-character").value,
    characters: $("set-deaths").value.split(",").map((x) => x.trim()).filter(Boolean),
    stash_tabs: [...$("set-tabs").querySelectorAll("input:checked")].map((i) => i.value),
    snapshot_on_run_start: $("set-run-start").checked,
    snapshot_delay: Number($("set-delay").value || 0),
    check_updates: $("set-check-updates").checked,
    update_channel: $("set-update-channel").value,
  };
  $("set-save").disabled = true;
  try {
    const leagueChanged = settings && body.league !== settings.league;
    const r = await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
    await loadSettings();
    if (leagueChanged && account) await checkAccount(); // stash tabs are per league
    await loadLeagues().catch(() => {});
    showWarnings(r, "Saved.");
  } catch (err) {
    setStatus("set-save-status", `✗ ${err.message}`, "bad");
  } finally {
    $("set-save").disabled = false;
  }
});

// --- routing: #settings shows the settings page, anything else the dashboard ---

// --- overview ---

let ovUnit = "divine";
let ovRange = "7d";
const ovTabs = new Set(); // empty = all tabs
let ovHistory = null;
let ovItems = null;

function money(chaosValue, divineValue, digits) {
  // one value in the selected unit, as text (callers escape nothing: numbers only)
  if (ovUnit === "divine") {
    if (divineValue == null) return "—";
    return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: digits ?? (Math.abs(divineValue) >= 100 ? 0 : 2) }).format(divineValue)}d`;
  }
  if (chaosValue == null) return "—";
  return `${(Math.abs(chaosValue) >= 100 ? nf0 : nf1).format(chaosValue)}c`;
}
function pointValue(p) { return ovUnit === "divine" ? p.divine : p.chaos; }

async function loadOverview() {
  const tabs = [...ovTabs].map(encodeURIComponent).join(",");
  [ovHistory, ovItems] = await Promise.all([
    api(`/api/overview/history?range=${ovRange}`),
    api(`/api/overview/items?tabs=${tabs}`),
  ]);
  renderOverview();
}

function renderOverview() {
  if (!ovHistory || !ovItems) return;
  const pts = ovHistory.points.filter((p) => pointValue(p) != null);
  // headline: the latest recorded value, and its change over the range
  if (pts.length) {
    const last = pts[pts.length - 1], first = pts[0];
    $("ov-total").textContent = money(last.chaos, last.divine);
    const d = pointValue(last) - pointValue(first);
    const sign = d > 0 ? "+" : d < 0 ? "−" : "±";
    const abs = Math.abs(d);
    $("ov-change").className = `ov-change ${d > 0 ? "good" : d < 0 ? "neg" : "dim"}`;
    $("ov-change").textContent = pts.length > 1
      ? `${sign}${ovUnit === "divine" ? money(null, abs) : money(abs, null)} in ${ovRange === "all" ? "total" : ovRange}`
      : "";
  } else {
    $("ov-total").textContent = ovTabs.size === 0 && ovItems.total_chaos != null ? money(ovItems.total_chaos, ovItems.total_divine) : "—";
    $("ov-change").textContent = "";
  }
  renderChart(pts);
  renderDataTable(pts);
  renderHoldings();
}

function niceStep(max, count) {
  const raw = max / count, mag = 10 ** Math.floor(Math.log10(raw)), f = raw / mag;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * mag;
}

function renderChart(pts) {
  const box = $("ov-chart");
  if (!pts.length) {
    const why = !status?.loot.enabled
      ? 'Loot tracking is off. Set up your account in <a href="#settings">Settings</a> to record values.'
      : ovHistory.points.length && ovUnit === "divine"
        ? "No divine price yet for these snapshots. Switch to chaos, or wait for poe.ninja prices."
        : "No values recorded in this range yet. A value is recorded every time you return to your hideout from a map, or when you press Snapshot now.";
    box.innerHTML = `<div class="chart-empty">${why}</div>`;
    return;
  }
  const W = box.clientWidth || 800, H = box.clientHeight || 280;
  const m = { l: 56, r: 72, t: 12, b: 26 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const ts = pts.map((p) => new Date(p.t).getTime());
  let t0 = Math.min(...ts), t1 = Math.max(...ts);
  if (t1 - t0 < 3600e3) { t0 -= 1800e3; t1 += 1800e3; }
  const vmax = Math.max(...pts.map(pointValue), 0);
  const step = niceStep(vmax || 1, 4);
  const ymax = Math.ceil((vmax || 1) / step) * step;
  const x = (t) => m.l + ((t - t0) / (t1 - t0)) * iw;
  const y = (v) => m.t + ih - (v / ymax) * ih;
  const fmtTick = (v) => ovUnit === "divine"
    ? new Intl.NumberFormat(undefined, { maximumFractionDigits: step < 1 ? 1 : 0, notation: v >= 10000 ? "compact" : "standard" }).format(v)
    : new Intl.NumberFormat(undefined, { maximumFractionDigits: 0, notation: v >= 10000 ? "compact" : "standard" }).format(v);
  let grid = "", axis = "";
  for (let v = 0; v <= ymax + step / 2; v += step) {
    grid += `<line x1="${m.l}" x2="${m.l + iw}" y1="${y(v)}" y2="${y(v)}"/>`;
    axis += `<text x="${m.l - 8}" y="${y(v) + 4}" text-anchor="end">${fmtTick(v)}</text>`;
  }
  const span = t1 - t0, maxTicks = Math.max(2, Math.min(7, Math.floor(iw / 100)));
  const ticks = [];
  if (span <= 36 * 3600e3) {
    // times, on whole hours
    const stepH = [1, 2, 3, 4, 6, 12].find((h) => span / (h * 3600e3) <= maxTicks) || 24;
    const d = new Date(t0); d.setMinutes(0, 0, 0);
    for (let t = d.getTime(); t <= t1; t += stepH * 3600e3) {
      if (t >= t0) ticks.push([t, new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })]);
    }
  } else {
    // dates, at midnight, every N days
    const stepD = Math.max(1, Math.ceil(span / 86400e3 / maxTicks));
    const d = new Date(t0); d.setHours(24, 0, 0, 0);
    for (; d.getTime() <= t1; d.setDate(d.getDate() + stepD)) {
      ticks.push([d.getTime(), d.toLocaleDateString([], { month: "short", day: "numeric" })]);
    }
  }
  for (const [t, label] of ticks) {
    axis += `<text x="${x(t)}" y="${H - 6}" text-anchor="middle">${label}</text>`;
  }
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(ts[i]).toFixed(1)},${y(pointValue(p)).toFixed(1)}`).join("");
  const area = `${line}L${x(ts[ts.length - 1]).toFixed(1)},${y(0)}L${x(ts[0]).toFixed(1)},${y(0)}Z`;
  const last = pts[pts.length - 1];
  const lx = x(ts[ts.length - 1]), ly = y(pointValue(last));
  box.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Total value over time, ${pts.length} snapshots">
      <g class="grid">${grid}</g>
      <path class="series-area" d="${area}"/>
      <path class="series-line" d="${line}"/>
      <circle class="marker" cx="${lx}" cy="${ly}" r="4"/>
      <text class="end-label" x="${lx + 8}" y="${ly + 4}">${money(last.chaos, last.divine)}</text>
      <g class="axis">${axis}</g>
      <line class="crosshair" id="ov-cross" y1="${m.t}" y2="${m.t + ih}" visibility="hidden"/>
      <circle class="marker" id="ov-dot" r="4" visibility="hidden"/>
      <rect id="ov-hit" x="${m.l}" y="0" width="${iw}" height="${H}" fill="transparent"/>
    </svg>
    <div class="chart-tip" id="ov-tip" hidden></div>`;
  const hit = $("ov-hit"), cross = $("ov-cross"), dot = $("ov-dot"), tip = $("ov-tip");
  const hide = () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true; };
  hit.addEventListener("pointermove", (e) => {
    const r = hit.ownerSVGElement.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    let best = 0;
    for (let i = 1; i < ts.length; i++) if (Math.abs(x(ts[i]) - px) < Math.abs(x(ts[best]) - px)) best = i;
    const p = pts[best], cx = x(ts[best]), cy = y(pointValue(p));
    cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", cx); dot.setAttribute("cy", cy); dot.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.style.left = `${Math.min(Math.max(cx, 80), W - 80)}px`;
    tip.style.top = `${cy - 10}px`;
    tip.textContent = "";
    const b = document.createElement("b");
    const key = document.createElement("span"); key.className = "key";
    b.append(key, document.createTextNode(money(p.chaos, p.divine)));
    const when = new Date(p.t).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    tip.append(b, document.createTextNode(ovUnit === "divine" && p.chaos != null ? `${nf0.format(p.chaos)}c · ${when}` : when));
  });
  hit.addEventListener("pointerleave", hide);
}

function renderDataTable(pts) {
  const rows = pts.slice().reverse().map((p) =>
    `<tr><td>${new Date(p.t).toLocaleString()}</td><td class="num">${money(p.chaos, p.divine)}</td></tr>`).join("");
  $("ov-data").innerHTML = `<thead><tr><th>Snapshot</th><th class="num">Value</th></tr></thead><tbody>${rows}</tbody>`;
}

function renderHoldings() {
  const d = ovItems;
  const cpd = d.chaos_per_divine;
  const hiddenNote = d.hidden ? ` · ${d.hidden} with no price or ~0c hidden` : "";
  $("ov-asof").textContent = d.taken_at ? `· as of ${new Date(d.taken_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}, current prices${hiddenNote}` : "";
  const chips = [`<button type="button" data-tab=""${ovTabs.size === 0 ? ' class="on"' : ""}>All tabs</button>`]
    .concat(d.tabs.map((t) => `<button type="button" data-tab="${esc(t.id)}"${ovTabs.has(t.id) ? ' class="on"' : ""}>${esc(t.name)}<span class="v">${money(t.chaos, cpd ? t.chaos / cpd : null)}</span></button>`));
  $("ov-tabs").innerHTML = d.tabs.length ? chips.join("") : "";
  $("ov-items-empty").hidden = d.items.length > 0;
  $("ov-items-empty").innerHTML = !d.taken_at
    ? (d.loot_enabled ? "No snapshot yet. Press Snapshot now, or finish a map." : 'Loot tracking is off. Set up your account in <a href="#settings">Settings</a>.')
    : "No items in the selected tabs.";
  $("ov-items-body").innerHTML = sortRows(d.items, itemsSort.key, itemsSort.dir).map((i) => {
    const icon = i.icon
      ? `<img src="${esc(i.icon)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'noicon'}))">`
      : '<span class="noicon"></span>';
    const each = i.chaos_each == null ? null : i.chaos_each;
    const pct = Math.round(i.share * 1000) / 10;
    return `<tr>
      <td><div class="item">${icon}<div><div>${esc(i.name)}</div><div class="where">${esc(i.tabs.join(", "))}</div></div></div></td>
      <td class="num">${nf0.format(i.quantity)}</td>
      <td class="num">${each == null ? '<span class="dim">no price</span>' : money(each, cpd ? each / cpd : null, 3)}</td>
      <td class="num">${i.chaos_total == null ? '<span class="dim">—</span>' : money(i.chaos_total, i.divine_total)}</td>
      <td class="num"><span class="share"><span class="bar"><i style="width:${Math.min(100, pct)}%"></i></span>${pct ? `${pct}%` : ""}</span></td>
    </tr>`;
  }).join("");
}

$("ov-unit").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-unit]");
  if (!b) return;
  ovUnit = b.dataset.unit;
  $("ov-unit").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  renderOverview();
});
$("ov-range").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-range]");
  if (!b) return;
  ovRange = b.dataset.range;
  $("ov-range").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  loadOverview().catch(() => {});
});
$("ov-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (!b) return;
  const id = b.dataset.tab;
  if (!id) ovTabs.clear();
  else if (ovTabs.has(id)) ovTabs.delete(id);
  else ovTabs.add(id);
  loadOverview().catch(() => {});
});
async function requestSnapshot(button) {
  if (!status?.loot.enabled) { location.hash = "#settings"; return; }
  button.disabled = true;
  button.textContent = "Snapshot requested…";
  await api("/api/snapshot", { method: "POST" }).catch(() => {});
  setTimeout(() => { button.disabled = false; button.textContent = "Snapshot now"; }, 4000);
}
$("ov-snapshot").addEventListener("click", () => requestSnapshot($("ov-snapshot")));
$("side-snapshot").addEventListener("click", () => requestSnapshot($("side-snapshot")));
new ResizeObserver(() => { if (!$("overview-view").hidden) renderOverview(); }).observe($("ov-chart"));

// --- native window (frameless): title bar buttons, double-click to maximize, edge resizing ---

const MAX_ICON = '<rect x="1.5" y="1.5" width="7" height="7"/>';
const RESTORE_ICON = '<path d="M3 3V1.5h5.5V7H7"/><rect x="1.5" y="3" width="5.5" height="5.5"/>';

function initWindowControls() {
  const win = window.pywebview?.api;
  if (!win || !win.toggle_maximize || $("titlebar").classList.contains("native")) return;
  $("titlebar").classList.add("native");
  $("win-controls").hidden = false;
  document.querySelectorAll(".resize-handle").forEach((h) => { h.hidden = false; });
  const setMax = (on) => {
    $("win-max").querySelector("svg").innerHTML = on ? RESTORE_ICON : MAX_ICON;
    $("win-max").title = on ? "Restore" : "Maximize";
  };
  $("win-min").addEventListener("click", () => win.minimize());
  $("win-max").addEventListener("click", async () => setMax(await win.toggle_maximize()));
  $("win-close").addEventListener("click", () => win.close());
  $("titlebar").addEventListener("dblclick", (e) => {
    if (e.target.classList.contains("pywebview-drag-region")) $("win-max").click();
  });
  document.querySelectorAll(".resize-handle").forEach((h) => {
    h.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      h.setPointerCapture(e.pointerId);
      const start = { x: e.screenX, y: e.screenY, w: window.innerWidth, h: window.innerHeight };
      let pending = null, busy = false;
      const flush = async () => {
        if (busy || !pending) return;
        busy = true;
        const [w, hh] = pending;
        pending = null;
        await win.resize(w, hh);
        busy = false;
        flush();
      };
      const move = (ev) => {
        const edge = h.dataset.edge;
        pending = [
          edge.includes("e") ? start.w + ev.screenX - start.x : start.w,
          edge.includes("s") ? start.h + ev.screenY - start.y : start.h,
        ];
        flush();
      };
      const up = () => {
        h.removeEventListener("pointermove", move);
        h.removeEventListener("pointerup", up);
        setMax(false);
      };
      h.addEventListener("pointermove", move);
      h.addEventListener("pointerup", up);
    });
  });
}
window.addEventListener("pywebviewready", initWindowControls);

// --- campaign ---

let cpData = null; // /api/campaign
let cpFetchedAt = 0; // performance.now() when cpData arrived
let cpSelected = null; // a past run's id; null = the run in progress
let cpDetail = null; // /api/campaign/runs/<cpSelected>
let cpNotesFor = null; // run id whose notes are in the textarea
let cpNotesTimer = 0;
const cpSort = loadSort("campaign", { key: "started_at", dir: -1 });

function dateTimeOf(iso) {
  return iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
}
function signed(sec) {
  if (sec == null) return '<span class="dim">—</span>';
  const cls = sec < 0 ? "ahead" : sec > 0 ? "behind" : "";
  return `<span class="${cls}">${sec < 0 ? "−" : "+"}${duration(Math.abs(sec))}</span>`;
}
function classLabel(r) {
  if (!r.class) return "—";
  return r.base_class && r.base_class !== r.class ? `${r.class} <span class="muted">(${esc(r.base_class)})</span>` : esc(r.class);
}
function cpShown() {
  return cpSelected != null ? cpDetail : cpData?.active ?? null;
}
function cpLive(r) {
  // running runs tick locally between polls
  const extra = r && r.status === "running" ? (performance.now() - cpFetchedAt) / 1000 : 0;
  return r ? { rta: r.rta + extra, igt: r.igt + extra } : null;
}

async function loadCampaign() {
  cpData = await api("/api/campaign");
  cpFetchedAt = performance.now();
  if (cpSelected != null && !cpData.runs.some((r) => r.id === cpSelected)) { cpSelected = null; cpDetail = null; }
  if (cpSelected != null && cpData.active?.id === cpSelected) { cpSelected = null; cpDetail = null; }
  renderCampaign();
}

async function selectCampaignRun(id) {
  if (cpData?.active?.id === id) id = null;
  cpSelected = id;
  cpDetail = id == null ? null : await api(`/api/campaign/runs/${id}`);
  renderCampaign();
}

function renderCampaign() {
  if (!cpData) return;
  const r = cpShown();
  const active = cpData.active;
  $("cp-follow").hidden = cpSelected == null;
  $("cp-run-title").textContent = cpSelected != null
    ? `Run · ${dateTimeOf(r?.started_at)}${r?.status === "abandoned" ? " · stopped" : r?.status === "finished" ? " · finished" : ""}`
    : active ? "Current run" : "Campaign run";
  $("cp-finish").hidden = $("cp-stop").hidden = !active || cpSelected != null;
  $("cp-start").textContent = active ? "Restart" : "Start new run";
  $("cp-character").hidden = cpSelected != null;
  $("cp-start").hidden = cpSelected != null;
  $("cp-hint").hidden = !!active || cpSelected != null;
  $("cp-past-actions").hidden = cpSelected == null;
  $("cp-notes-field").hidden = !r;
  $("cp-timer").className = "cp-timer" + (r?.status === "running" ? " live" : r?.status === "abandoned" ? " abandoned" : "");
  $("cp-char").textContent = r?.character || "—";
  $("cp-class").textContent = r?.class || "—";
  $("cp-class").title = r?.base_class && r.base_class !== r.class ? r.base_class : "";
  $("cp-level").textContent = r?.level ?? "—";
  $("cp-act").textContent = r?.act ? (r.act > 10 ? "Epilogue" : r.act) : "—";
  $("cp-deaths").textContent = r ? r.deaths : "—";
  $("cp-loads").textContent = r ? `· ${duration(r.load_seconds)} loading` : "";
  if (r && cpNotesFor !== r.id && document.activeElement !== $("cp-notes")) {
    $("cp-notes").value = r.notes || "";
    cpNotesFor = r.id;
    $("cp-notes-status").textContent = "";
  }
  tickCampaign();
  renderCampaignSplits(r);
  renderCampaignZones(r);
  renderCampaignRuns();
}

function tickCampaign() {
  const t = cpLive(cpShown());
  $("cp-timer").textContent = t ? duration(t.rta) : "—";
  $("cp-igt").textContent = t ? duration(t.igt) : "—";
  const now = $("cp-acts-body").querySelector("tr.now");
  if (now && t) {
    now.querySelector(".t").textContent = duration(t.rta);
    now.querySelector(".i").textContent = duration(t.igt);
    const pb = now.dataset.pb === "" ? null : Number(now.dataset.pb);
    now.querySelector(".d").innerHTML = pb == null ? '<span class="dim">—</span>' : signed(t.rta - pb);
  }
}

function renderCampaignSplits(r) {
  const pbActs = new Map((r?.pb_acts || []).map((a) => [a.name, a]));
  const acts = new Map((r?.acts || []).map((a) => [a.name, a]));
  $("cp-pb-label").textContent = !r ? "" : r.pb_id ? "· compared with your best finished run" : cpData.pb && cpData.pb.id === r.id ? "· this is your best run" : "· no finished run to compare with yet";
  let prev = 0, prevPb = 0;
  const rows = [];
  for (let n = 1; n <= 10; n++) {
    const name = `Act ${n}`, s = acts.get(name), pb = pbActs.get(name);
    const isNow = r && r.status === "running" && !s && (r.act ?? 1) === n;
    const seg = s ? s.rta - prev : null;
    const pbSeg = pb ? pb.rta - prevPb : null;
    const segCls = seg != null && pbSeg != null && seg < pbSeg ? "ahead" : "";
    rows.push(`<tr class="${isNow ? "now" : s ? "" : "todo"}" data-pb="${pb ? pb.rta : ""}">
      <td>${name}</td>
      <td class="num t">${s ? duration(s.rta) : isNow ? "" : "—"}</td>
      <td class="num i">${s ? duration(s.igt) : isNow ? "" : "—"}</td>
      <td class="num d">${s ? signed(s.delta) : isNow ? "" : '<span class="dim">—</span>'}</td>
      <td class="num"><span class="${segCls}">${seg != null ? duration(seg) : "—"}</span></td>
      <td class="num">${pb ? duration(pb.rta) : '<span class="dim">—</span>'}</td>
    </tr>`);
    if (s) prev = s.rta;
    if (pb) prevPb = pb.rta;
  }
  $("cp-acts-body").innerHTML = rows.join("");
  tickCampaign();
}

function renderCampaignZones(r) {
  const entries = [
    ...(r?.zones || []).map((z) => ({ ...z, cls: "" })),
    ...(r?.levels || []).map((z) => ({ ...z, cls: "lvl" })),
    ...(r?.acts || []).map((z) => ({ ...z, name: `${z.name} done`, cls: "actdone" })),
  ].sort((a, b) => b.rta - a.rta || (a.cls === "actdone" ? -1 : 1));
  $("cp-zones-empty").hidden = entries.length > 0;
  $("cp-zones").innerHTML = entries.map((z) => `<li class="${z.cls}">
    <span class="t">${duration(z.rta)}</span><span class="act">${z.act ? `A${z.act}` : ""}</span><span>${esc(z.name)}</span></li>`).join("");
}

function renderCampaignRuns() {
  const rows = sortRows(cpData.runs, cpSort.key, cpSort.dir);
  const shownId = cpShown()?.id;
  $("cp-runs-empty").hidden = rows.length > 0;
  $("cp-runs-body").innerHTML = rows.map((r) => {
    const mark = r.status === "running" ? '<span class="live-dot" title="in progress"></span>'
      : r.status === "abandoned" ? '<span class="dim" title="stopped">✕ </span>' : "";
    const best = cpData.pb && cpData.pb.id === r.id ? ' <span class="chip ok">PB</span>' : "";
    const t = r.status === "running" ? cpLive(r) : r;
    return `<tr data-id="${r.id}" class="${r.id === shownId ? "selected" : ""}">
      <td>${mark}${dateTimeOf(r.started_at)}</td>
      <td>${esc(r.character || "—")}</td>
      <td>${classLabel(r)}</td>
      <td class="num">${r.level ?? "—"}</td>
      <td class="num">${r.act ? (r.act > 10 ? "E" : r.act) : "—"}</td>
      <td class="num">${duration(t.rta)}${best}</td>
      <td class="num">${duration(t.igt)}</td>
      <td class="num">${r.deaths || '<span class="dim">0</span>'}</td>
      <td class="notes" title="${esc(r.notes)}">${esc(r.notes)}</td>
    </tr>`;
  }).join("");
}

async function cpAction(path, body) {
  try {
    cpData = await api(path, { method: "POST", body: JSON.stringify(body ?? {}) });
    cpFetchedAt = performance.now();
    cpSelected = null; cpDetail = null;
    renderCampaign();
  } catch (e) {
    alert(e.message);
  }
}

$("cp-start").addEventListener("click", () => {
  if (cpData?.active && !confirm("Stop the current run and start a new one?")) return;
  cpAction("/api/campaign/start", { character: $("cp-character").value.trim() || null });
});
$("cp-finish").addEventListener("click", () => cpAction("/api/campaign/finish"));
$("cp-stop").addEventListener("click", () => {
  if (confirm("Stop this run? It stays in the list, marked as stopped.")) cpAction("/api/campaign/stop");
});
$("cp-follow").addEventListener("click", () => selectCampaignRun(null));
$("cp-delete").addEventListener("click", async () => {
  if (cpSelected == null || !confirm("Delete this run and its notes? This can't be undone.")) return;
  await api(`/api/campaign/runs/${cpSelected}/delete`, { method: "POST" }).catch((e) => alert(e.message));
  cpSelected = null; cpDetail = null;
  await loadCampaign();
});
$("cp-runs-body").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-id]");
  if (tr) selectCampaignRun(Number(tr.dataset.id)).catch(() => {});
});
$("cp-notes").addEventListener("input", () => {
  const id = cpNotesFor;
  if (id == null) return;
  $("cp-notes-status").textContent = "· editing…";
  clearTimeout(cpNotesTimer);
  cpNotesTimer = setTimeout(async () => {
    const notes = $("cp-notes").value;
    try {
      await api(`/api/campaign/runs/${id}/notes`, { method: "POST", body: JSON.stringify({ notes }) });
      $("cp-notes-status").textContent = "· saved";
      for (const r of [cpDetail, cpData?.active, ...(cpData?.runs || [])]) if (r && r.id === id) r.notes = notes;
      renderCampaignRuns();
    } catch (e) {
      $("cp-notes-status").textContent = `· not saved: ${e.message}`;
    }
  }, 700);
});
makeSortable("cp-runs-table", "campaign", cpSort, () => { if (cpData) renderCampaignRuns(); });

// --- routing: #session (default), #overview, #campaign, #settings ---

const VIEWS = { session: "dashboard", overview: "overview-view", campaign: "campaign-view", settings: "settings-view" };

function route() {
  const name = Object.hasOwn(VIEWS, location.hash.slice(1)) ? location.hash.slice(1) : "session";
  for (const [key, id] of Object.entries(VIEWS)) {
    $(id).hidden = key !== name;
    $(`nav-${key}`).classList.toggle("active", key === name);
  }
  $("content").scrollTop = 0;
  renderSessionBanner(status?.loot.session);
  if (name === "settings") loadSettings().catch((e) => setStatus("set-save-status", `✗ ${e.message}`, "bad"));
  if (name === "overview") loadOverview().catch(() => {});
  if (name === "campaign") loadCampaign().catch(() => {});
}
window.addEventListener("hashchange", route);

makeSortable("runs-table", "runs", runsSort, () => { if (runsData) renderRuns(); });
makeSortable("sessions-table", "sessions", sessionsSort, () => { if (sessionsData) renderSessions(); });
makeSortable("ov-items-table", "items", itemsSort, () => { if (ovItems) renderHoldings(); });

(async function init() {
  route();
  initWindowControls();
  await loadLeagues().catch(() => {});
  await pollStatus();
  if (!runsData) await pollRuns();
  setInterval(pollStatus, 1000);
  setInterval(pollRuns, 15000);
  setInterval(tickTimer, 250);
  setInterval(() => { if (!$("campaign-view").hidden) tickCampaign(); }, 250);
  setInterval(() => { if (!$("campaign-view").hidden) loadCampaign().catch(() => {}); }, 2000);
  setInterval(() => { if (!$("overview-view").hidden) loadOverview().catch(() => {}); }, 60000);
})();
