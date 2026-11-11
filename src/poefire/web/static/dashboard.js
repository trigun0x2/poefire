"use strict";
// Status polling, banners (update, cookie, league), the sidebar and the Session page (maps, sessions, leagues).

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
  renderLeagueBanners(s);
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
  // The campaign run in progress: its timer on every page, not only the Campaign tab.
  const cp = s.campaign;
  $("side-campaign").hidden = !cp;
  if (cp) {
    $("side-campaign").classList.toggle("paused", !!cp.paused);
    $("side-cp-meta").textContent = [cp.paused ? "Paused" : null, cp.act ? (cp.act > 10 ? "Epilogue" : `Act ${cp.act}`) : null,
      cp.character].filter(Boolean).join(" · ") || "Waiting for your character to level up";
  }
  const v = s.value;
  $("side-value").innerHTML = !v ? '<span class="dim">—</span>'
    : v.divine != null ? `<span class="divine">${nf2.format(v.divine)}d</span>` : chaos(v.chaos);
  $("side-value").title = v ? `${nf0.format(v.chaos)}c at ${timeOf(v.at)}` : "No snapshot yet";
  tickTimer();
}

function tickTimer() {
  const cp = status?.campaign;
  if (cp) $("side-cp-timer").textContent = duration(cp.rta + (cp.paused ? 0 : (performance.now() - statusAt) / 1000));
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

// A new league: your newest character plays somewhere else, or your watched tabs are gone.
function renderLeagueBanners(s) {
  const lh = s.league_hint, b = $("league-banner");
  b.hidden = !lh;
  if (lh && b.dataset.key !== lh.league) {
    b.dataset.key = lh.league;
    const who = lh.character ? ` (${esc(lh.character)}${lh.level ? `, level ${lh.level}` : ""})` : "";
    b.innerHTML = `<span><b>Playing ${esc(lh.league)}?</b> Your newest character${who} is in ${esc(lh.league)}, ` +
      `but poefire tracks ${esc(lh.current)}. Loot, stash tabs and prices follow the league you pick.</span>` +
      `<button type="button" class="btn primary" data-act="switch">Switch to ${esc(lh.league)}</button>` +
      '<button type="button" class="btn" data-act="dismiss">Not now</button>';
  }
  if (!lh) b.dataset.key = "";
  const th = s.tabs_hint, t = $("tabs-banner");
  const showTabs = th && !lh && location.hash !== "#settings";
  t.hidden = !showTabs;
  if (showTabs) {
    t.innerHTML = th.none_found
      ? `<span><b>None of your watched stash tabs are in ${esc(th.league)}.</b> Only your inventory is tracked: pick this league's tabs in Settings.</span><a class="btn primary" href="#settings">Pick tabs</a>`
      : `<span><b>Some watched tabs aren't in ${esc(th.league)}:</b> ${th.missing.map(esc).join(", ")}. Loot moved into other tabs isn't counted.</span><a class="btn" href="#settings">Check tabs</a>`;
  }
}
$("league-banner").addEventListener("click", async (e) => {
  const act = e.target.closest("button[data-act]")?.dataset.act;
  const lh = status?.league_hint;
  if (!act || !lh) return;
  try {
    if (act === "switch") {
      await api("/api/league", { method: "POST", body: JSON.stringify({ league: lh.league }) });
      await loadLeagues().catch(() => {});
    } else {
      await api("/api/league/hint/dismiss", { method: "POST" });
    }
    $("league-banner").hidden = true;
    await pollStatus();
  } catch (err) {
    alertModal("That didn't work", err.message);
  }
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
      <span class="name" title="${esc(i.sold ? `${i.name}: its payment isn't counted as loot` : i.name)}">${esc(i.name)}${i.sold ? ' <span class="chip">trade</span>' : ""}</span>
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
    tile("Loot / hour", s.chaos_per_hour == null ? "—" : `${nf0.format(s.chaos_per_hour)}c`,
      (s.divine_per_hour == null ? "" : `${nf2.format(s.divine_per_hour)}d · `) + "with hideout time"),
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
      <span class="name" title="${esc(i.sold ? `${i.name}: its payment isn't counted as loot` : i.name)}">${esc(i.name)}${i.sold ? ' <span class="chip">trade</span>' : ""}</span>
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
  renderMapStats();
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
      <td class="num">${s.chaos_per_hour == null ? '<span class="dim">—</span>' : chaos(s.chaos_per_hour)}</td>
      <td class="num">${s.deaths || '<span class="dim">0</span>'}</td>
      <td class="num">${del}</td>
    </tr>`;
  }).join("");
}

const mapsSort = loadSort("maps", { key: "maps", dir: -1 });

/** Finished maps of the shown session grouped by map: which maps pay best per hour. */
function renderMapStats() {
  const rows = sortRows(runsData.maps || [], mapsSort.key, mapsSort.dir);
  const dash = '<span class="dim">—</span>';
  $("maps-sub").textContent = `· finished maps ${viewSession == null ? "this" : "in that"} session, by map`;
  $("maps-empty").hidden = rows.length > 0;
  const top = Math.max(0, ...rows.map((m) => m.chaos_per_hour || 0));
  $("maps-body").innerHTML = rows.map((m) => `<tr>
      <td>${esc(m.map_name)}</td>
      <td class="num">${m.maps}${m.loot_tracked_maps < m.maps ? ` <span class="dim" title="maps with loot data">(${m.loot_tracked_maps})</span>` : ""}</td>
      <td class="num">${duration(m.avg_seconds)}</td>
      <td class="num">${m.avg_chaos == null ? dash : chaos(m.avg_chaos)}</td>
      <td class="num">${m.best_chaos == null ? dash : chaos(m.best_chaos)}</td>
      <td class="num${m.chaos_per_hour && m.chaos_per_hour === top && rows.length > 1 ? " best-rate" : ""}">${m.chaos_per_hour == null ? dash : chaos(m.chaos_per_hour)}</td>
      <td class="num">${m.deaths || '<span class="dim">0</span>'}</td>
    </tr>`).join("");
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
