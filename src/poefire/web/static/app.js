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
      : `Prices ${ago(p.fetched_at)}` + (p.chaos_per_divine ? ` · 1d = ${nf0.format(p.chaos_per_divine)}c` : "");
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

  // Current run: just the map and its elapsed time, in the top bar.
  const run = s.run;
  $("now").hidden = !run;
  if (run) {
    $("now-name").textContent = run.map_name;
    $("now").classList.toggle("paused", run.status !== "active");
    $("now-dot").className = `live-dot${run.status === "active" ? "" : " paused"}`;
    $("now").title = run.status === "active" ? "In map" : "Paused: in hideout";
  }
  tickTimer();
}

function tickTimer() {
  const run = status?.run;
  if (!run) return;
  const extra = run.status === "active" ? (performance.now() - statusAt) / 1000 : 0;
  $("now-timer").textContent = duration(run.active_seconds + extra);
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

function renderRuns() {
  const inSession = new Set(runsData.session_run_ids);
  const rows = runsData.runs;
  $("runs-empty").hidden = rows.length > 0;
  const current = selectedRun ?? rows[0]?.id;
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
    ${d.loot.unpriced ? `<div class="note">${d.loot.unpriced} item kind${d.loot.unpriced === 1 ? "" : "s"} without a poe.ninja price (rares, magic gear…) not counted.</div>` : ""}
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
  renderRuns();
  await renderDetail();
}

async function loadLeagues() {
  const l = await api("/api/leagues");
  $("league").innerHTML = l.options.map((o) => `<option${o === l.current ? " selected" : ""}>${esc(o)}</option>`).join("");
}

$("league").addEventListener("change", async (e) => {
  await api("/api/league", { method: "POST", body: JSON.stringify({ league: e.target.value }) });
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
  $("set-league").innerHTML = leagues.map((l) => `<option${l === s.league ? " selected" : ""}>${esc(l)}</option>`).join("");
  $("set-run-start").checked = s.snapshot_on_run_start;
  $("set-delay").value = s.snapshot_delay;
  $("set-deaths").value = s.characters.join(", ");
  renderAccountPickers();
  setStatus("set-save-status", s.loot.enabled ? "Loot tracking is on." : `Loot tracking is off: ${s.loot.reason}`, s.loot.enabled ? "ok" : "warn");
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
$("set-league").addEventListener("change", renderAccountPickers);

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
    league: $("set-league").value,
    character: $("set-character").value,
    characters: $("set-deaths").value.split(",").map((x) => x.trim()).filter(Boolean),
    stash_tabs: [...$("set-tabs").querySelectorAll("input:checked")].map((i) => i.value),
    snapshot_on_run_start: $("set-run-start").checked,
    snapshot_delay: Number($("set-delay").value || 0),
  };
  $("set-save").disabled = true;
  try {
    const r = await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
    await loadSettings();
    await loadLeagues().catch(() => {});
    showWarnings(r, "Saved.");
  } catch (err) {
    setStatus("set-save-status", `✗ ${err.message}`, "bad");
  } finally {
    $("set-save").disabled = false;
  }
});

// --- routing: #settings shows the settings page, anything else the dashboard ---

function route() {
  const onSettings = location.hash === "#settings";
  $("settings-view").hidden = !onSettings;
  $("dashboard").hidden = onSettings;
  $("nav-settings").hidden = onSettings;
  $("nav-dashboard").hidden = !onSettings;
  if (onSettings) loadSettings().catch((e) => setStatus("set-save-status", `✗ ${e.message}`, "bad"));
}
window.addEventListener("hashchange", route);

(async function init() {
  route();
  await loadLeagues().catch(() => {});
  await pollStatus();
  if (!runsData) await pollRuns();
  setInterval(pollStatus, 1000);
  setInterval(pollRuns, 15000);
  setInterval(tickTimer, 250);
})();
