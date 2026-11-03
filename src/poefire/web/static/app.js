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
  const ld = $("loot-pill").querySelector(".dot");
  ld.className = "dot " + (!l.enabled ? "bad" : l.last && !l.last.complete ? "warn" : "ok");
  $("loot-text").textContent = !l.enabled
    ? "Loot tracking off"
    : l.last ? `Snapshot ${timeOf(l.last.at)}${l.last.complete ? "" : " failed"}` : "Loot tracking on";
  $("loot-pill").title = !l.enabled ? `${l.reason} (click to open Settings)` : l.last?.error || "Take a snapshot now";
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

function renderSidebarStats() {
  const m = runsData.last_map, r = runsData.recent;
  $("side-last-title").textContent = m ? `Last map · ${m.map_name}` : "Last map";
  $("side-last-loot").innerHTML = m && m.loot_tracked ? chaos(m.chaos) : '<span class="dim">—</span>';
  $("side-avg").innerHTML = r.maps ? chaos(r.avg_chaos) : '<span class="dim">—</span>';
  $("side-maps").textContent = runsData.session.runs ?? 0;
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
  if (!s.runs) {
    $("tiles").innerHTML = tile("Runs", "0", "Start mapping to see stats");
    $("session-since").textContent = "";
    return;
  }
  $("session-since").textContent = `· since ${timeOf(s.started_at)}`;
  const tracked = s.loot_tracked_runs;
  const lootSub = tracked < s.runs ? `${tracked} of ${s.runs} runs with loot data` : "net loot value";
  const recentSub = !r.maps ? "no maps with loot data yet"
    : (r.avg_divine == null ? "" : `${nf2.format(r.avg_divine)}d · `) +
      (r.maps < r.of ? `over ${r.maps} map${r.maps === 1 ? "" : "s"}` : `last ${r.of} maps`);
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
    ${d.loot.unpriced ? `<div class="note">${d.loot.unpriced} item kind${d.loot.unpriced === 1 ? "" : "s"} without a poe.ninja price, not counted in the value.</div>` : ""}
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

async function pollRuns() {
  runsData = await api("/api/runs?limit=100");
  renderSession();
  renderLastMap();
  renderSidebarStats();
  renderRuns();
  await renderDetail();
}

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
  if (status?.loot.enabled) api("/api/snapshot", { method: "POST" }).catch(() => {});
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
  chip.className = `chip${s.session_saved ? " ok" : ""}`;
  chip.textContent = s.session_from_env ? "set by POE_SESSID variable" : s.session_saved ? "saved" : "not set";
  $("set-session-clear").disabled = !s.session_saved || s.session_from_env;

  const leagues = (await api("/api/leagues")).options;
  $("set-league").value = s.league;
  $("set-league-options").innerHTML = leagues.map((l) => `<option value="${esc(l)}"></option>`).join("");
  $("set-price-league").value = s.price_league;
  $("set-price-league").placeholder = `Automatic: ${s.price_league_effective}`;
  $("set-price-options").innerHTML = s.ninja_leagues.map((l) => `<option value="${esc(l)}"></option>`).join("");
  renderLeagueHints();
  $("set-run-start").checked = s.snapshot_on_run_start;
  $("set-delay").value = s.snapshot_delay;
  $("set-deaths").value = s.characters.join(", ");
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
  $("ov-asof").textContent = d.taken_at ? `· as of ${new Date(d.taken_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}, current prices` : "";
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

// --- routing: #session (default), #overview, #settings ---

const VIEWS = { session: "dashboard", overview: "overview-view", settings: "settings-view" };

function route() {
  const name = Object.hasOwn(VIEWS, location.hash.slice(1)) ? location.hash.slice(1) : "session";
  for (const [key, id] of Object.entries(VIEWS)) {
    $(id).hidden = key !== name;
    $(`nav-${key}`).classList.toggle("active", key === name);
  }
  $("content").scrollTop = 0;
  if (name === "settings") loadSettings().catch((e) => setStatus("set-save-status", `✗ ${e.message}`, "bad"));
  if (name === "overview") loadOverview().catch(() => {});
}
window.addEventListener("hashchange", route);

makeSortable("runs-table", "runs", runsSort, () => { if (runsData) renderRuns(); });
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
  setInterval(() => { if (!$("overview-view").hidden) loadOverview().catch(() => {}); }, 60000);
})();
