"use strict";
// The Campaign page: the run, splits, top runs, history, zones, and where you lost time.

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
  $("cp-tabs-run").textContent = r ? `${cpSelected != null ? "Showing the run of" : "Run in progress, started"} ${dateTimeOf(r.started_at)}${r.character ? ` · ${r.character}` : ""}` : "";
  $("cp-analysis-empty").hidden = (r?.worst_zones || []).length > 0;
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

// Time in towns and hideouts during act n, against your least town time in that act.
function townCell(r, n) {
  const t = r?.town?.[n];
  if (t == null) return '<span class="dim">—</span>';
  const best = r.best_town?.[n], pb = r.pb_town?.[n];
  const tip = [best != null ? `Your best: ${duration(best)}` : null, pb != null ? `Your PB: ${duration(pb)}` : null]
    .filter(Boolean).join(" · ") || "No other run to compare with yet";
  const d = best != null ? t - best : null;
  const delta = d == null ? "" : ` <span class="town-d ${d > 0.5 ? "behind" : "ahead"}">${d > 0.5 ? "+" : "−"}${duration(Math.abs(d))}</span>`;
  return `<span title="${esc(tip)}">${duration(t)}${delta}</span>`;
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
      <td class="num">${townCell(r, n)}</td>
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
  const pics = (g.layouts || []).map((l, i) => `<a href="${esc(l.src)}" data-lightbox="${i}" title="${esc(l.label)}">
      <img src="${esc(l.src)}" alt="${esc(l.label || g.title)}" loading="lazy">${esc(l.label)}</a>`).join("");
  const poi = (g.points_of_interest || []).map((p) => `<span class="chip">${esc(p)}</span>`).join("");
  const read = (g.zone_read || []).map((p) => `<p>${esc(p)}</p>`).join("");
  const simple = (g.simple_read || []).length
    ? `<div class="cp-simple"><div class="cp-simple-title">Simple Read</div>${g.simple_read.map((p) => `<p>${esc(p)}</p>`).join("")}</div>` : "";
  return `<div class="cp-guide">
      <div class="cp-layouts">${pics || '<div class="hint">No layout pictures yet.</div>'}</div>
      <div class="cp-read">${simple}${poi ? `<div class="poi">${poi}</div>` : ""}${read}
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

// Layout pictures open in a viewer in the app (arrows / ← → step through the zone's pictures)
let lightbox = { items: [], at: 0 };
function showLightbox(i) {
  const { items } = lightbox;
  lightbox.at = (i + items.length) % items.length;
  const it = items[lightbox.at];
  $("lightbox-img").src = it.src;
  $("lightbox-img").alt = it.label;
  $("lightbox-label").textContent = it.label;
  $("lightbox-count").textContent = items.length > 1 ? `${lightbox.at + 1} / ${items.length}` : "";
  $("lightbox-prev").hidden = $("lightbox-next").hidden = items.length < 2;
}
$("cp-worst").addEventListener("click", (e) => {
  const a = e.target.closest("a[data-lightbox]");
  if (!a) return;
  e.preventDefault();
  const links = [...a.closest(".cp-layouts").querySelectorAll("a[data-lightbox]")];
  lightbox.items = links.map((l) => ({ src: l.getAttribute("href"), label: l.title }));
  showLightbox(links.indexOf(a));
  $("lightbox").showModal();
});
$("lightbox-prev").addEventListener("click", () => showLightbox(lightbox.at - 1));
$("lightbox-next").addEventListener("click", () => showLightbox(lightbox.at + 1));
$("lightbox-close").addEventListener("click", () => $("lightbox").close());
$("lightbox").addEventListener("click", (e) => { if (e.target === e.currentTarget) $("lightbox").close(); });
$("lightbox").addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") showLightbox(lightbox.at - 1);
  else if (e.key === "ArrowRight") showLightbox(lightbox.at + 1);
});

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
  if (li) { showCampaignTab("run"); selectCampaignRun(Number(li.dataset.id)).catch(() => {}); }
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
  if (tr) { showCampaignTab("run"); selectCampaignRun(Number(tr.dataset.id)).catch(() => {}); }
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

// --- tabs: Run (the run and its splits), History (top runs, all runs), Analysis ---

let cpTab = (() => { try { return localStorage.getItem("poefire.cpTab") || "run"; } catch { return "run"; } })();
function showCampaignTab(tab) {
  cpTab = tab;
  $("campaign-view").dataset.tab = tab;
  for (const b of document.querySelectorAll(".cp-tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  try { localStorage.setItem("poefire.cpTab", tab); } catch { /* private mode */ }
}
document.querySelector(".cp-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (b) showCampaignTab(b.dataset.tab);
});
showCampaignTab(cpTab);
