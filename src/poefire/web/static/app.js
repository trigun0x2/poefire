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

// --- in-app dialogs (instead of the browser's confirm/alert/prompt boxes) ---

/** Shows the dialog; resolves to {ok, value}. `body` is HTML (escape user data with esc()). */
function modal({ title, body = "", ok = "OK", cancel = "Cancel", danger = false, input = null }) {
  const dlg = $("modal");
  $("modal-title").textContent = title;
  $("modal-body").innerHTML = body;
  const okBtn = $("modal-ok"), cancelBtn = $("modal-cancel"), field = $("modal-input");
  okBtn.textContent = ok;
  okBtn.className = `btn ${danger ? "danger-fill" : "primary"}`;
  cancelBtn.hidden = cancel == null;
  cancelBtn.textContent = cancel ?? "";
  field.hidden = !input;
  field.value = input?.value ?? "";
  field.placeholder = input?.placeholder ?? "";
  return new Promise((resolve) => {
    const done = (result) => {
      dlg.removeEventListener("close", onClose);
      cancelBtn.onclick = null;
      resolve(result);
    };
    const onClose = () => done({ ok: dlg.returnValue === "ok", value: field.value.trim() });
    dlg.addEventListener("close", onClose);
    cancelBtn.onclick = () => dlg.close("cancel");
    dlg.returnValue = "cancel"; // Esc / closing counts as cancel
    dlg.showModal();
    (input ? field : danger ? cancelBtn : okBtn).focus();
  });
}
const confirmModal = async (opts) => (await modal(opts)).ok;
const alertModal = (title, body = "") => modal({ title, body: esc(body), cancel: null });
async function promptModal(opts) {
  const r = await modal({ ...opts, input: opts.input ?? {} });
  return r.ok ? r.value : null;
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
  await api("/api/update/install", { method: "POST" }).catch((err) => alertModal("The update couldn't be installed", err.message));
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
    if (!$("settings-view").hidden) renderHotkeyStatus();
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

/** Refreshes the Session page. The session history is only fetched when it can have changed
 * (a map ended, sessions changed, the page was opened), not on the 15 s timer. */
async function pollRuns({ sessions = true } = {}) {
  currentRuns = await api("/api/runs?limit=200");
  runsData = viewSession == null ? currentRuns : await api(`/api/runs?session=${viewSession}&limit=500`);
  renderSession();
  renderLastMap();
  renderSidebarStats();
  renderRuns();
  await renderDetail();
  if (sessions || !sessionsData) loadSessions().catch(() => {});
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
    if (!await confirmModal({
      title: "Delete this session?",
      body: `The session from <b>${esc(dateTimeOf(s?.started_at))}</b> and its ${s?.maps ?? 0} maps are deleted. This can't be undone.`,
      ok: "Delete session", danger: true,
    })) return;
    try {
      await api(`/api/sessions/${del.dataset.del}/delete`, { method: "POST" });
      if (String(viewSession) === del.dataset.del) viewSession = null;
      await pollRuns();
    } catch (err) { alertModal("The session couldn't be deleted", err.message); }
    return;
  }
  const tr = e.target.closest("tr[data-session]");
  if (!tr) return;
  const s = sessionsData.sessions.find((x) => String(x.id) === tr.dataset.session);
  await showSession(s?.current ? null : Number(tr.dataset.session));
});
$("session-back").addEventListener("click", () => showSession(null));
$("session-new").addEventListener("click", async () => {
  if (!await confirmModal({
    title: "Start a new session?",
    body: "Stats start again from zero. This session moves to the <b>Sessions</b> history below.",
    ok: "Start new session",
  })) return;
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
    league = (await promptModal({
      title: "Other league",
      body: "Type the league's name exactly. Private leagues look like <b>My League (PL12345)</b>.",
      ok: "Use league", input: { placeholder: "League name" },
    })) || "";
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
  $("set-hotkey").value = s.campaign_hotkey || "";
  $("set-hotkey-sound").checked = s.campaign_hotkey_sound;
  $("set-overlay").checked = s.overlay_enabled;
  $("set-overlay-key").value = s.overlay_toggle_hotkey || "";
  $("set-overlay-move").value = s.overlay_move_hotkey || "";
  $("set-debug-logging").checked = !!s.debug_logging;
  $("set-overlay-opacity").value = s.overlay_opacity ?? 90;
  $("set-overlay-opacity-value").textContent = `${$("set-overlay-opacity").value}%`;
  $("set-build-snapshots").checked = s.build_snapshots;
  renderHotkeyStatus();
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
  if (!await confirmModal({
    title: "Remove the saved POESESSID?",
    body: "The cookie is deleted from this PC, and loot tracking stops until you add one again.",
    ok: "Remove", danger: true,
  })) return;
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
    campaign_hotkey: $("set-hotkey").value,
    campaign_hotkey_sound: $("set-hotkey-sound").checked,
    overlay_enabled: $("set-overlay").checked,
    overlay_toggle_hotkey: $("set-overlay-key").value,
    overlay_move_hotkey: $("set-overlay-move").value,
    overlay_opacity: Number($("set-overlay-opacity").value),
    debug_logging: $("set-debug-logging").checked,
    build_snapshots: $("set-build-snapshots").checked,
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

// Overlay opacity: previewed live while dragging (the overlay shows for a moment), saved on release.
let opacityTimer = null;
function sendOpacity(save) {
  const percent = Number($("set-overlay-opacity").value);
  $("set-overlay-opacity-value").textContent = `${percent}%`;
  return api("/api/overlay/opacity", { method: "POST", body: JSON.stringify({ percent, save }) })
    .then(() => { if (save && settings) settings.overlay_opacity = percent; })
    .catch((err) => setStatus("set-save-status", `✗ ${err.message}`, "bad"));
}
$("set-overlay-opacity").addEventListener("input", () => {
  $("set-overlay-opacity-value").textContent = `${$("set-overlay-opacity").value}%`;
  clearTimeout(opacityTimer);
  opacityTimer = setTimeout(() => sendOpacity(false), 60);
});
$("set-overlay-opacity").addEventListener("change", () => {
  clearTimeout(opacityTimer);
  sendOpacity(true);
});

// --- diagnostics ---

$("set-debug-logging").addEventListener("change", async (e) => {
  const on = e.target.checked;
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify({ debug_logging: on }) });
    if (settings) settings.debug_logging = on;
    $("diag-result").textContent = on ? "Verbose logging is on." : "Verbose logging is off.";
  } catch (err) {
    e.target.checked = !on;
    $("diag-result").textContent = `✗ ${err.message}`;
  }
});
$("diag-report").addEventListener("click", async () => {
  const btn = $("diag-report");
  btn.disabled = true;
  $("diag-result").textContent = "Saving…";
  try {
    const r = await api("/api/diagnostics/report", { method: "POST" });
    const shown = await api("/api/diagnostics/reveal", { method: "POST", body: JSON.stringify({ what: r.name }) })
      .catch(() => ({ opened: false }));
    $("diag-result").innerHTML = `Saved <b>${esc(r.name)}</b> in <code>${esc(r.folder)}</code>${
      shown.opened ? " (opened in Explorer)" : ""}. <a href="/api/diagnostics/report/${encodeURIComponent(r.name)}" download>Download</a>`;
  } catch (err) {
    $("diag-result").textContent = `✗ The report couldn't be saved: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
});
$("diag-logs").addEventListener("click", async () => {
  try {
    const r = await api("/api/diagnostics/reveal", { method: "POST", body: JSON.stringify({ what: "logs" }) });
    if (!r.opened) $("diag-result").innerHTML = `The logs are in <code>${esc(r.path)}</code>.`;
  } catch (err) {
    $("diag-result").textContent = `✗ ${err.message}`;
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
  const extra = r && r.status === "running" && !r.paused ? (performance.now() - cpFetchedAt) / 1000 : 0;
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
  $("cp-stop").hidden = !active || cpSelected != null;
  $("cp-start").textContent = active ? "Restart" : "Start new run";
  $("cp-character").hidden = cpSelected != null || !!active;
  $("cp-start").hidden = cpSelected != null;
  $("cp-hint").hidden = !!active || cpSelected != null;
  $("cp-past-actions").hidden = cpSelected == null;
  $("cp-notes-field").hidden = !r;
  $("cp-timer").className = "cp-timer" + (r?.paused ? " paused" : r?.status === "running" ? " live" : r?.status === "abandoned" ? " abandoned" : "");
  const live = !!active && cpSelected == null;
  $("cp-pause").hidden = !live;
  $("cp-pause").textContent = active?.paused ? "Resume" : "Pause";
  const hk = cpData.hotkey;
  $("cp-hotkey-hint").innerHTML = !hk?.key ? 'No timer hotkey set (<a href="#settings">Settings</a>).'
    : hk.error ? `<span class="neg">Hotkey ${esc(hk.key)} isn't working: ${esc(hk.error)}</span>`
    : `<b>${esc(hk.key)}</b> ${active ? (active.paused ? "resumes" : "pauses") : "starts a run"}, even while you're in the game.`;
  $("cp-char").innerHTML = r?.character
    ? `${esc(r.character)} <a href="#builds" class="cp-builds" data-char="${esc(r.character)}" title="This character's build at each level">builds</a>`
    : "—";
  $("cp-mule").hidden = !r?.mule;
  $("cp-mule").title = r?.mule ? `This run started with a Ranger mule (${r.mule}); the timer kept running and its splits were dropped.` : "";
  $("cp-class").textContent = r?.class || "—";
  $("cp-class").title = r?.base_class && r.base_class !== r.class ? r.base_class : "";
  $("cp-level").textContent = r?.level ?? "—";
  $("cp-act").textContent = r?.act ? (r.act > 10 ? "Epilogue" : r.act) : "—";
  $("cp-deaths").textContent = r ? r.deaths : "—";
  $("cp-loads").textContent = r ? `· ${duration(r.load_seconds)} loading` : "";
  if (r && document.activeElement !== $("cp-patch")) $("cp-patch").value = r.patch || "";
  if (r && cpNotesFor !== r.id && document.activeElement !== $("cp-notes")) {
    $("cp-notes").value = r.notes || "";
    cpNotesFor = r.id;
    $("cp-notes-status").textContent = "";
  }
  tickCampaign();
  renderCampaignSplits(r);
  renderCampaignWorst(r);
  renderCampaignZones(r);
  renderCampaignRuns();
}

function tickCampaign() {
  const r = cpShown();
  const t = cpLive(r);
  $("cp-timer").innerHTML = t ? duration(t.rta) + (r.paused ? '<span class="paused-tag">Paused</span>' : "") : "—";
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
  const bestActs = new Map((r?.best_acts || []).map((a) => [a.name, a]));
  const acts = new Map((r?.acts || []).map((a) => [a.name, a]));
  const cls = r?.base_class;
  const isPb = r && cls && cpData.pb_ids?.[cls] === r.id;
  const best = r?.best_runs ? ` · best times from ${r.best_runs} run${r.best_runs === 1 ? "" : "s"} past Act 1` : "";
  $("cp-pb-label").textContent = !r ? "" : !cls ? "· PB comparison starts once your class is known"
    : (r.pb_id ? `· compared with your ${cls} PB` : isPb ? `· this is your ${cls} PB`
      : `· no finished ${cls} run to compare with yet`) + best;
  let prev = 0, prevPb = 0;
  const rows = [];
  for (let n = 1; n <= 10; n++) {
    const name = `Act ${n}`, s = acts.get(name), pb = pbActs.get(name), b = bestActs.get(name);
    const isNow = r && r.status === "running" && !s && (r.act ?? 1) === n;
    const seg = s ? s.rta - prev : null;
    const pbSeg = pb ? pb.rta - prevPb : null;
    // gold: faster than you've ever done this act; green: faster than in your PB
    const segCls = seg != null && b?.segment != null && seg < b.segment ? "gold"
      : seg != null && pbSeg != null && seg < pbSeg ? "ahead" : "";
    rows.push(`<tr class="${isNow ? "now" : s ? "" : "todo"}" data-pb="${pb ? pb.rta : ""}">
      <td>${name}</td>
      <td class="num t">${s ? duration(s.rta) : isNow ? "" : "—"}</td>
      <td class="num i">${s ? duration(s.igt) : isNow ? "" : "—"}</td>
      <td class="num d">${s ? signed(s.delta) : isNow ? "" : '<span class="dim">—</span>'}</td>
      <td class="num"><span class="${segCls}">${seg != null ? duration(seg) : "—"}</span></td>
      <td class="num">${pb ? duration(pb.rta) : '<span class="dim">—</span>'}</td>
      <td class="num">${b ? `<span title="${s && s.best_delta != null ? `${s.best_delta < 0 ? "−" : "+"}${duration(Math.abs(s.best_delta))} vs your best` : ""}">${duration(b.rta)}</span>` : '<span class="dim">—</span>'}</td>
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
  // zone and act splits also show the PB's time there and the difference
  $("cp-zones").innerHTML = entries.map((z) => `<li class="${z.cls}">
    <span class="t">${duration(z.rta)}</span><span class="act">${z.act ? `A${z.act}` : ""}</span><span class="zn">${esc(z.name)}</span>${
      z.pb_rta != null ? `<span class="zpb" title="Your PB's time here">${duration(z.pb_rta)}</span><span class="zd">${signed(z.delta)}</span>` : ""}${
      z.best_delta != null && z.best_delta < 0 ? '<span class="zgold" title="Your fastest time here yet">best</span>' : ""}</li>`).join("");
}

// --- where you lost time: the worst zones, with CyclonDefinitiv's layout guides ---

const cpGuides = new Map(); // "act|zone" -> guide, null (none), or a pending promise
let cpWorstKey = "";

function cpGuideFor(act, zone) {
  const key = `${act}|${zone}`;
  if (!cpGuides.has(key)) {
    cpGuides.set(key, api(`/api/campaign/layout?act=${act}&zone=${encodeURIComponent(zone)}`)
      .then((g) => { cpGuides.set(key, g); return g; })
      .catch(() => { cpGuides.set(key, null); return null; }));
  }
  return Promise.resolve(cpGuides.get(key));
}

function guideHtml(g) {
  if (!g) return '<div class="hint">No layout guide for this zone yet.</div>';
  const pics = (g.layouts || []).map((l) => `<a href="${esc(l.src)}" target="_blank" rel="noopener" title="${esc(l.label)}">
      <img src="${esc(l.src)}" alt="${esc(l.label || g.title)}" loading="lazy">${esc(l.label)}</a>`).join("");
  const poi = (g.points_of_interest || []).map((p) => `<span class="chip">${esc(p)}</span>`).join("");
  const read = (g.zone_read || []).map((p) => `<p>${esc(p)}</p>`).join("");
  return `<div class="cp-guide">
      <div class="cp-layouts">${pics || '<div class="hint">No layout pictures yet.</div>'}</div>
      <div class="cp-read">${poi ? `<div class="poi">${poi}</div>` : ""}${read}
        <a class="cp-guide-link" href="${esc(g.url)}" target="_blank" rel="noopener">${esc(g.title || "This zone")} in Cyclon’s guide by CyclonDefinitiv →</a></div>
    </div>`;
}

function renderCampaignWorst(r) {
  const worst = r?.worst_zones || [];
  $("cp-worst-card").hidden = worst.length === 0;
  const key = JSON.stringify([r?.id, worst]);
  if (key === cpWorstKey) return; // unchanged: keep the loaded guides on screen
  cpWorstKey = key;
  const basis = worst[0]?.basis;
  $("cp-worst-sub").textContent = basis === "longest" ? "· your longest zones (nothing to compare with yet)"
    : "· against your fastest time in each zone";
  $("cp-worst").innerHTML = worst.map((w, i) => {
    const vs = w.lost == null ? `${duration(w.seconds)}`
      : `${duration(w.seconds)} <span class="behind">+${duration(w.lost)}</span> <span class="muted">vs ${w.basis === "pb" ? "your PB" : "your best"} ${duration(w.reference)}</span>`;
    return `<li data-act="${w.act}" data-zone="${esc(w.zone)}">
      <div class="cp-worst-head"><span class="rank">#${i + 1}</span><span class="zone">${esc(w.zone)}</span>
        <span class="act">Act ${w.act}</span><span class="lost">${vs}</span></div>
      <div class="cp-guide-slot hint">Loading the layout guide…</div></li>`;
  }).join("");
  for (const li of $("cp-worst").children) {
    cpGuideFor(Number(li.dataset.act), li.dataset.zone).then((g) => {
      if (li.isConnected) li.querySelector(".cp-guide-slot").outerHTML = guideHtml(g);
    });
  }
}

const RANKS = ["1st", "2nd", "3rd"];

function renderCampaignTop() {
  const top = (cpData.top_ids || []).map((id) => cpData.runs.find((r) => r.id === id)).filter(Boolean);
  $("cp-top-card").hidden = top.length === 0;
  $("cp-top").innerHTML = top.map((r, i) => `<li class="rank${i + 1}" data-id="${r.id}" tabindex="0" role="button">
      <span class="medal">${RANKS[i]}</span>
      <span class="t">${duration(r.rta)}</span>
      <span class="who">${esc(r.character || "—")} <span class="muted">${classLabel(r)}</span></span>
      <span class="when muted">${dateTimeOf(r.started_at)}${r.patch ? ` · ${esc(r.patch)}` : ""}</span>
    </li>`).join("");
}
$("cp-top").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-id]");
  if (li) selectCampaignRun(Number(li.dataset.id)).catch(() => {});
});
$("cp-top").addEventListener("keydown", (e) => {
  const li = e.target.closest("li[data-id]");
  if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); li.click(); }
});

function renderCampaignRuns() {
  renderCampaignTop();
  const rank = new Map((cpData.top_ids || []).map((id, i) => [id, i + 1]));
  const rows = sortRows(cpData.runs, cpSort.key, cpSort.dir);
  const shownId = cpShown()?.id;
  $("cp-runs-empty").hidden = rows.length > 0;
  $("cp-runs-body").innerHTML = rows.map((r) => {
    const mark = r.status === "running" ? '<span class="live-dot" title="in progress"></span>'
      : r.status === "abandoned" ? '<span class="dim" title="stopped">✕ </span>' : "";
    const best = r.base_class && cpData.pb_ids?.[r.base_class] === r.id
      ? ` <span class="chip ok" title="Best ${esc(r.base_class)} run">PB</span>` : "";
    const t = r.status === "running" ? cpLive(r) : r;
    const n = rank.get(r.id);
    const medal = n ? ` <span class="medal rank${n}" title="Your ${RANKS[n - 1]} fastest run">${RANKS[n - 1]}</span>` : "";
    return `<tr data-id="${r.id}" class="${r.id === shownId ? "selected" : ""}${n ? ` top rank${n}` : ""}">
      <td>${mark}${dateTimeOf(r.started_at)}</td>
      <td>${esc(r.character || "—")}${r.mule ? ` <span class="chip mule" title="Started with a Ranger mule (${esc(r.mule)})">muled</span>` : ""}</td>
      <td>${classLabel(r)}</td>
      <td class="num">${r.level ?? "—"}</td>
      <td class="num">${r.act ? (r.act > 10 ? "E" : r.act) : "—"}</td>
      <td class="num">${duration(t.rta)}${best}${medal}</td>
      <td class="num">${duration(t.igt)}</td>
      <td class="num">${r.deaths || '<span class="dim">0</span>'}</td>
      <td class="notes" title="${esc(r.notes)}">${r.patch ? `<span class="chip patch">${esc(r.patch)}</span> ` : ""}${esc(r.notes)}</td>
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
    alertModal("That didn't work", e.message);
  }
}

$("cp-start").addEventListener("click", async () => {
  if (cpData?.active && !await confirmModal({
    title: "Start a new run?",
    body: "The current run is stopped and kept in the list.",
    ok: "Start new run",
  })) return;
  cpAction("/api/campaign/start", { character: $("cp-character").value.trim() || null });
});
$("cp-pause").addEventListener("click", () => cpAction(cpData?.active?.paused ? "/api/campaign/resume" : "/api/campaign/pause"));
$("cp-stop").addEventListener("click", async () => {
  if (await confirmModal({
    title: "Stop this run?",
    body: "The timer ends here. The run stays in the list, marked as stopped, and can't be resumed. To take a break, use <b>Pause</b> instead.",
    ok: "Stop run", danger: true,
  })) cpAction("/api/campaign/stop");
});
$("cp-follow").addEventListener("click", () => selectCampaignRun(null));
$("cp-char").addEventListener("click", (e) => {
  const link = e.target.closest("a[data-char]");
  if (link) bdChar = link.dataset.char; // the Builds tab opens on this character
});
$("cp-delete").addEventListener("click", async () => {
  if (cpSelected == null || !await confirmModal({
    title: "Delete this run?",
    body: "Its splits and notes are deleted. This can't be undone.",
    ok: "Delete run", danger: true,
  })) return;
  await api(`/api/campaign/runs/${cpSelected}/delete`, { method: "POST" })
    .catch((e) => alertModal("The run couldn't be deleted", e.message));
  cpSelected = null; cpDetail = null;
  await loadCampaign();
});
$("cp-runs-body").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-id]");
  if (tr) selectCampaignRun(Number(tr.dataset.id)).catch(() => {});
});
$("cp-patch").addEventListener("change", async () => {
  const r = cpShown();
  if (!r) return;
  const patch = $("cp-patch").value.trim();
  try {
    await api(`/api/campaign/runs/${r.id}/patch`, { method: "POST", body: JSON.stringify({ patch }) });
    for (const x of [cpDetail, cpData?.active, ...(cpData?.runs || [])]) if (x && x.id === r.id) x.patch = patch || null;
    $("cp-notes-status").textContent = "· patch saved";
    renderCampaignRuns();
  } catch (e) {
    $("cp-notes-status").textContent = `· patch not saved: ${e.message}`;
  }
});
$("cp-patch").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); });
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

// --- hotkey field: click it, then press the combination ---

// hotkey fields: input id -> its status line and where its live status is in /api/status
const HOTKEY_FIELDS = {
  "set-hotkey": { status: "set-hotkey-status", live: () => status?.hotkey },
  "set-overlay-key": { status: "set-overlay-key-status", live: () => status?.overlay?.toggle },
  "set-overlay-move": { status: "set-overlay-move-status", live: () => status?.overlay?.move },
};

function renderHotkeyStatus() {
  for (const [id, f] of Object.entries(HOTKEY_FIELDS)) {
    const hk = f.live();
    const el = $(f.status);
    const value = $(id).value;
    if (!value) { el.className = "hint"; el.textContent = "Off."; continue; }
    if (!hk || hk.key !== value) { el.className = "hint"; el.textContent = "Save to apply."; continue; }
    el.className = `hint ${hk.error ? "bad" : hk.active ? "ok" : ""}`;
    el.textContent = hk.error ? `✗ ${hk.error}` : hk.active ? `✓ ${hk.key} is active.` : "Not active (global hotkeys need the Windows app).";
  }
  $("set-overlay-unavailable").hidden = !status?.overlay || status.overlay.available || !$("set-overlay").checked;
}
const KEY_NAMES = { " ": "Space", PageUp: "PgUp", PageDown: "PgDn", Insert: "Insert", Delete: "Delete", Home: "Home", End: "End", Pause: "Pause", ScrollLock: "ScrollLock" };
function keyName(e) {
  if (/^F\d{1,2}$/.test(e.key)) return e.key;
  if (e.code.startsWith("Numpad")) {
    const rest = e.code.slice(6);
    return /^\d$/.test(rest) ? `Num${rest}` : null;
  }
  if (/^Key[A-Z]$/.test(e.code)) return e.code.slice(3);
  if (/^Digit\d$/.test(e.code)) return e.code.slice(5);
  return KEY_NAMES[e.key] || null;
}
for (const id of Object.keys(HOTKEY_FIELDS)) {
  const input = $(id);
  input.addEventListener("focus", () => input.classList.add("capturing"));
  input.addEventListener("blur", () => input.classList.remove("capturing"));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Tab") return;
    e.preventDefault();
    if (["Control", "Alt", "Shift", "Meta"].includes(e.key)) return; // wait for the main key
    if (e.key === "Escape") { input.blur(); return; }
    const key = keyName(e);
    if (!key) return;
    const mods = [e.ctrlKey && "Ctrl", e.altKey && "Alt", e.shiftKey && "Shift", e.metaKey && "Win"].filter(Boolean);
    input.value = [...mods, key].join("+");
    renderHotkeyStatus();
  });
  $(`${id}-clear`).addEventListener("click", () => { input.value = ""; renderHotkeyStatus(); });
}

// --- builds ---

let bdData = null; // /api/builds
let bdChar = null; // selected character
let bdLevels = []; // its snapshots, by level
let bdIndex = 0;
const bdCache = new Map(); // snapshot id -> detail

function bdStatus() {
  const d = bdData;
  const el = $("bd-status");
  let text = "", cls = "hint";
  if (!d.enabled) { text = "Build snapshots are off (Settings → Campaign)."; cls += " warn"; }
  else if (!d.connected) { text = `Not connected to your account${d.reason ? `: ${d.reason}` : ""}. Snapshots use the connection set up for loot in Settings.`; cls += " warn"; }
  else if (d.pending.length) text = `Waiting for a zone change to capture: ${d.pending.map((p) => `${p.character} level ${p.level}`).join(", ")}.`;
  else if (d.last_error) { text = `Last snapshot failed: ${d.last_error}`; cls += " bad"; }
  else text = "Snapshots are taken at levels 5 and 10, then every level from 15, at your next zone change after the level-up.";
  el.className = cls;
  el.textContent = text;
}

async function loadBuilds() {
  bdData = await api("/api/builds");
  const chars = bdData.characters;
  if (!bdChar || !chars.some((c) => c.character === bdChar)) bdChar = chars[0]?.character ?? null;
  $("bd-character").innerHTML = chars.map((c) =>
    `<option value="${esc(c.character)}"${c.character === bdChar ? " selected" : ""}>${esc(c.character)} — ${esc(c.class || "?")} ${c.level}</option>`).join("");
  $("bd-character").disabled = !chars.length;
  $("bd-now").disabled = !bdChar || !bdData.connected;
  bdStatus();
  await loadBuildLevels();
}

async function loadBuildLevels(keepLevel = true) {
  if (!bdChar) { bdLevels = []; renderBuild(); return; }
  const prev = bdLevels[bdIndex]?.id;
  const atEnd = bdIndex === bdLevels.length - 1;
  bdLevels = (await api(`/api/builds/character/${encodeURIComponent(bdChar)}`)).snapshots;
  const keep = keepLevel && !atEnd ? bdLevels.findIndex((l) => l.id === prev) : -1;
  bdIndex = keep >= 0 ? keep : bdLevels.length - 1; // follow the newest unless you picked one
  const slider = $("bd-slider");
  slider.max = Math.max(bdLevels.length - 1, 0);
  slider.value = bdIndex;
  slider.disabled = bdLevels.length < 2;
  const n = bdLevels.length;
  // label every level when they fit, else every 5th; never closer than ~4% apart
  const every = n > 24 ? 5 : 1;
  const pos = (i) => (n > 1 ? (i / (n - 1)) * 100 : 0);
  const shown = [];
  bdLevels.forEach((l, i) => {
    if (!(i === 0 || i === n - 1 || l.level % every === 0 || !l.ok)) return;
    if (shown.length && pos(i) - pos(shown.at(-1)) < 4) {
      if (i !== n - 1) return;
      shown.pop(); // keep the newest level's label
    }
    shown.push(i);
  });
  $("bd-ticks").innerHTML = shown.map((i) =>
    `<span class="${bdLevels[i].ok ? "" : "miss"}" style="left:${pos(i)}%" title="${bdLevels[i].ok ? "" : "missed"}">${bdLevels[i].level}</span>`).join("");
  await renderBuild();
}

async function renderBuild() {
  const snap = bdLevels[bdIndex];
  $("bd-empty").hidden = !!snap;
  $("bd-body").hidden = !snap;
  $("bd-level").textContent = snap ? snap.level : "";
  $("bd-prev").disabled = !snap || bdIndex === 0;
  $("bd-next").disabled = !snap || bdIndex >= bdLevels.length - 1;
  if (!snap) return;
  let d = bdCache.get(snap.id);
  if (!d) { d = await api(`/api/builds/snapshot/${snap.id}`); bdCache.set(snap.id, d); }
  if (bdLevels[bdIndex]?.id !== snap.id) return; // moved on meanwhile
  $("bd-title").textContent = `${d.character} · level ${d.level}${d.class ? ` · ${d.class}` : ""}`;
  $("bd-meta").textContent = `${dateTimeOf(d.taken_at)}${d.league ? ` · ${d.league}` : ""}`;
  $("bd-pob").hidden = !d.ok;
  $("bd-missed").hidden = d.ok;
  $("bd-missed").textContent = d.ok ? "" : `This level was missed: ${d.error || "the build couldn't be fetched"}.`;
  if (!d.ok) { $("bd-stats").innerHTML = ""; $("bd-gear").innerHTML = ""; return; }
  const p = d.passives;
  const gems = d.gear.reduce((n, g) => n + g.gems.flat().length, 0);
  $("bd-stats").innerHTML = [
    ["Passives", p.allocated], ["Masteries", p.masteries], ["Jewels", p.jewels],
    ["Gems", gems], ["Bandit", p.bandit || "None"],
  ].map(([k, v]) => `<div><span class="label">${k}</span><span>${esc(v)}</span></div>`).join("");
  $("bd-gear").innerHTML = d.gear.map((g) => `
    <div class="bd-item">
      ${g.icon ? `<img src="${esc(g.icon)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'noicon'}))">` : '<span class="noicon"></span>'}
      <div>
        <div class="slot">${esc(g.slot)}${g.links ? ` · <span class="ilinks">${esc(g.links)}</span>` : ""}</div>
        <div class="iname r-${esc(g.rarity)}" title="${esc(g.name || g.base)}">${esc(g.name || g.base)}</div>
        ${g.name ? `<div class="ibase">${esc(g.base)}</div>` : ""}
        ${g.gems.length ? `<div class="gem-groups">${g.gems.map((grp) => `<div class="gem-group">${grp.map((x) =>
          `<span class="gem${x.support ? " support" : ""}" title="Level ${x.level ?? "?"}${x.quality ? `, ${x.quality}% quality` : ""}">${esc(x.name.replace(/ Support$/, ""))} ${x.level ?? ""}</span>`).join("")}</div>`).join("")}</div>` : ""}
      </div>
    </div>`).join("");
}

$("bd-character").addEventListener("change", (e) => { bdChar = e.target.value; loadBuildLevels(false).catch(() => {}); });
$("bd-slider").addEventListener("input", (e) => { bdIndex = Number(e.target.value); renderBuild().catch(() => {}); });
$("bd-prev").addEventListener("click", () => { if (bdIndex > 0) { bdIndex--; $("bd-slider").value = bdIndex; renderBuild().catch(() => {}); } });
$("bd-next").addEventListener("click", () => { if (bdIndex < bdLevels.length - 1) { bdIndex++; $("bd-slider").value = bdIndex; renderBuild().catch(() => {}); } });
$("bd-now").addEventListener("click", async () => {
  if (!bdChar) return;
  try {
    await api(`/api/builds/character/${encodeURIComponent(bdChar)}/snapshot`, { method: "POST" });
    $("bd-status").className = "hint";
    $("bd-status").textContent = `Fetching ${bdChar}'s build…`;
    setTimeout(() => loadBuilds().catch(() => {}), 5000);
  } catch (e) { alertModal("Couldn't take a snapshot", e.message); }
});
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const t = Object.assign(document.createElement("textarea"), { value: text });
    document.body.append(t);
    t.select();
    const ok = document.execCommand("copy");
    t.remove();
    return ok;
  }
}

$("bd-pob").addEventListener("click", async () => {
  const snap = bdLevels[bdIndex];
  if (!snap) return;
  const btn = $("bd-pob");
  btn.disabled = true;
  try {
    const { code } = await api(`/api/builds/snapshot/${snap.id}/pob`);
    if (await copyText(code)) {
      btn.textContent = "Copied ✓";
      setTimeout(() => { btn.textContent = "Copy PoB code"; }, 2000);
    } else {
      await modal({
        title: "Path of Building code",
        body: `Copy this code, then in Path of Building: <b>Import/Export Build</b> → paste it under "Import from code".<textarea class="modal-code" readonly onclick="this.select()">${esc(code)}</textarea>`,
        cancel: null,
      });
    }
  } catch (e) {
    alertModal("Couldn't make the code", e.message);
  } finally {
    btn.disabled = false;
  }
});

$("bd-delete").addEventListener("click", async () => {
  const snap = bdLevels[bdIndex];
  if (!snap || !await confirmModal({
    title: "Delete this snapshot?",
    body: `${esc(bdChar)}'s level ${snap.level} snapshot is deleted. This can't be undone.`,
    ok: "Delete snapshot", danger: true,
  })) return;
  await api(`/api/builds/snapshot/${snap.id}/delete`, { method: "POST" }).catch((e) => alertModal("Couldn't delete it", e.message));
  bdCache.delete(snap.id);
  await loadBuilds();
});

// --- routing: #session (default), #overview, #campaign, #settings ---

const VIEWS = { session: "dashboard", overview: "overview-view", campaign: "campaign-view", builds: "builds-view", settings: "settings-view" };

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
  if (name === "builds") loadBuilds().catch(() => {});
  if (name === "session" && runsData) loadSessions().catch(() => {});
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
  // Polling only redraws the page: tracking, snapshots and prices run in the app itself.
  // While the window is minimized or hidden nothing is fetched or drawn; it catches up at once
  // when it's shown again.
  const shown = (view) => !document.hidden && (!view || !$(view).hidden);
  setInterval(() => { if (shown()) pollStatus(); }, 1000);
  setInterval(() => { if (shown()) pollRuns({ sessions: false }); }, 15000);
  setInterval(() => { if (shown()) tickTimer(); }, 250);
  setInterval(() => { if (shown("campaign-view")) tickCampaign(); }, 250);
  setInterval(() => { if (shown("campaign-view")) loadCampaign().catch(() => {}); }, 2000);
  setInterval(() => { if (shown("overview-view")) loadOverview().catch(() => {}); }, 60000);
  setInterval(() => { if (shown("builds-view")) loadBuilds().catch(() => {}); }, 20000);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    pollStatus().then(() => pollRuns()).catch(() => {});
    if (shown("campaign-view")) loadCampaign().catch(() => {});
    if (shown("overview-view")) loadOverview().catch(() => {});
    if (shown("builds-view")) loadBuilds().catch(() => {});
  });
})();
