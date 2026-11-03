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
  if (!r.ok) throw new Error(`${r.status} ${path}`);
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
  $("loot-pill").title = !l.enabled ? l.reason : l.last?.error || "Take a snapshot now";

  const run = s.run;
  $("current-empty").hidden = !!run;
  $("current-body").hidden = !run;
  if (run) {
    $("cur-map").textContent = run.map_name;
    $("cur-meta").textContent = `Level ${run.area_level} · started ${timeOf(run.started_at)}`;
    const st = $("cur-status");
    st.className = `status ${run.status}`;
    st.textContent = run.status === "active" ? "In map" : run.status === "paused" ? "Paused · hideout" : "Finished";
    $("cur-deaths").textContent = run.deaths;
    $("cur-loot").innerHTML = run.loot_tracked ? `${chaos(run.chaos)} ${divine(run.divine)}` : '<span class="dim">after hideout</span>';
    $("cur-zone").textContent = s.zone ? s.zone.name : "—";
  }
  tickTimer();
}

function tickTimer() {
  const run = status?.run;
  if (!run) return;
  const extra = run.status === "active" ? (performance.now() - statusAt) / 1000 : 0;
  const t = $("cur-timer");
  t.textContent = duration(run.active_seconds + extra);
  t.classList.toggle("paused", run.status !== "active");
}

function tile(label, value, sub = "", hero = false) {
  return `<div class="tile${hero ? " hero" : ""}"><div class="label">${label}</div>` +
    `<div class="value">${value}</div><div class="sub">${sub}</div></div>`;
}

function renderSession() {
  const s = runsData.session;
  if (!s.runs) {
    $("tiles").innerHTML = tile("Runs", "0", "Start mapping to see stats");
    $("session-since").textContent = "";
    return;
  }
  $("session-since").textContent = `· since ${timeOf(s.started_at)}`;
  const tracked = s.loot_tracked_runs;
  const lootSub = tracked < s.runs ? `${tracked} of ${s.runs} runs with loot data` : "net loot value";
  $("tiles").innerHTML = [
    tile("Chaos / hour", s.chaos_per_hour == null ? "—" : nf0.format(s.chaos_per_hour),
      s.chaos_per_hour_wall == null ? "map time" : `${nf0.format(s.chaos_per_hour_wall)} incl. hideout`, true),
    tile("Divine / hour", s.divine_per_hour == null ? "—" : nf2.format(s.divine_per_hour), "map time"),
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
      <td class="num">${r.chaos_per_hour == null ? '<span class="dim">—</span>' : nf0.format(r.chaos_per_hour)}</td>
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
    const key = status.run ? `${status.run.id}:${status.run.status}:${status.run.deaths}:${status.run.chaos}` : "";
    if (key !== lastRunKey) {
      lastRunKey = key;
      await pollRuns();
    }
  } catch (e) {
    $("prices-text").textContent = "Disconnected";
  }
}

async function pollRuns() {
  runsData = await api("/api/runs?limit=100");
  renderSession();
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
});
$("runs-body").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-id]");
  if (!tr) return;
  selectedRun = tr.dataset.id === runsData.runs[0]?.id ? null : tr.dataset.id;
  renderRuns();
  renderDetail();
});

(async function init() {
  await loadLeagues().catch(() => {});
  await pollStatus();
  if (!runsData) await pollRuns();
  setInterval(pollStatus, 1000);
  setInterval(pollRuns, 15000);
  setInterval(tickTimer, 250);
})();
