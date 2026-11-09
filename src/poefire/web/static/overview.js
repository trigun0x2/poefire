"use strict";
// The Overview page: stash value over time and the most valuable items.

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
