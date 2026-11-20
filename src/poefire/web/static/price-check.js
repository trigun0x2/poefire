"use strict";
// The price-check popup: the item's mods as trade filters (tick/untick, min/max), the 5
// cheapest listings and the average. Opened by the price check key (Ctrl+D) in the game.

let pc = null; // the current check, as /api/pricecheck/current returns it
let pcShown = 0; // id of the check on screen
const pcSearched = new Set(); // checks searched with their default filters already

const GROUPS = [["item", "Item"], ["enchant", "Enchant"], ["implicit", "Implicit"],
  ["fractured", "Fractured"], ["explicit", "Explicit"], ["crafted", "Crafted"]];

function priceText(chaosValue) {
  return chaos(chaosValue);
}

function listedAgo(iso) {
  if (!iso) return "";
  const min = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (min < 60) return `${min} min ago`;
  const h = Math.round(min / 60);
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} days ago`;
}

function setPcStatus(text, cls = "") {
  const el = $("pc-status");
  el.className = `hint ${cls}`;
  el.textContent = text;
}

function renderHeader() {
  const it = pc.item;
  const name = $("pc-name");
  name.textContent = it.name + (it.stack > 1 ? ` ×${it.stack}` : "");
  name.className = `pc-name ${String(it.rarity).toLowerCase().replace(/\s.*/, "")}`;
  $("pc-sub").textContent = [it.class, it.links >= 5 ? `${it.links}-link` : "", it.corrupted ? "corrupted" : "",
    pc.kind === "trade" ? `trade site · ${pc.league}` : ""].filter(Boolean).join(" · ");
}

function renderBulk() {
  const r = pc.ninja;
  $("pc-bulk-price").innerHTML = r.chaos_total == null ? '<span class="dim">no price</span>'
    : `${priceText(r.chaos_total)}${pc.item.stack > 1 ? ` <span class="muted">(${money0(r.chaos_each)} each)</span>` : ""}`;
  $("pc-bulk-note").textContent = (r.chaos_total == null ? `${r.note}. ` : `poe.ninja (${r.league}). `) +
    "Currency, fragments, cards and other bulk items are bought and sold on the in-game Currency Exchange.";
}

function filterRow(f) {
  const num = (bound) => `<input type="number" step="any" data-bound="${bound}" placeholder="${bound}" value="${f[bound] ?? ""}" aria-label="${bound}">`;
  return `<div class="pc-filter${f.enabled ? "" : " off"}" data-key="${esc(f.key)}">
    <label><input type="checkbox"${f.enabled ? " checked" : ""}><span class="text">${esc(f.label)}</span></label>
    ${f.key === "corrupted" ? "<span></span><span></span>" : num("min") + num("max")}
  </div>`;
}

function renderFilters() {
  const filters = pc.check?.filters || [];
  $("pc-filters").innerHTML = GROUPS.map(([g, title]) => {
    const rows = filters.filter((f) => f.group === g);
    return rows.length ? `<div class="pc-group">${title}</div>${rows.map(filterRow).join("")}` : "";
  }).join("");
  const un = pc.check?.unmatched || [];
  $("pc-unmatched").textContent = un.length ? `Not searchable: ${un.join(" · ")}` : "";
}

function renderResult() {
  const r = pc.result;
  $("pc-results").hidden = !r;
  if (pc.error) setPcStatus(pc.error, "bad");
  if (!r) return;
  if (!pc.error) setPcStatus(`${r.total} listed`, "ok");
  $("pc-avg").innerHTML = r.average_chaos == null ? '<span class="muted">No listings with these filters: untick some and search again.</span>'
    : `Average <b>${priceText(r.average_chaos)}</b> <span class="muted">of the ${r.averaged} cheapest${r.averaged >= 10 ? " (without the cheapest and dearest few)" : ""}</span>`;
  $("pc-rows").innerHTML = r.cheapest.map((l) =>
    `<tr><td>${esc(nf1.format(l.amount))} ${esc(l.currency)}</td><td class="num">${l.currency === "Chaos Orb" ? "" : priceText(l.chaos)}</td><td class="num muted">${esc(listedAgo(l.indexed))}</td></tr>`).join("");
  const link = $("pc-link");
  link.hidden = !r.url;
  if (r.url) link.href = r.url;
}

function render() {
  const has = !!pc;
  $("pc-empty").hidden = has;
  $("pc-bulk").hidden = !has || pc.kind !== "bulk";
  $("pc-form").hidden = !has || pc.kind !== "trade" || !pc.check;
  if (!has) { $("pc-results").hidden = true; return; }
  runsData = { chaos_per_divine: pc.chaos_per_divine }; // for chaos()/money0() in core.js
  renderHeader();
  if (pc.kind === "bulk") { renderBulk(); $("pc-results").hidden = true; return; }
  if (pcShown !== pc.id) renderFilters();
  setPcStatus(pc.error || "", pc.error ? "bad" : "");
  renderResult();
}

function choices() {
  return [...document.querySelectorAll(".pc-filter")].map((row) => {
    const val = (b) => {
      const input = row.querySelector(`input[data-bound="${b}"]`);
      return input && input.value !== "" ? Number(input.value) : null;
    };
    return { key: row.dataset.key, enabled: row.querySelector("input[type=checkbox]").checked, min: val("min"), max: val("max") };
  });
}

async function search() {
  if (!pc || pc.kind !== "trade" || !pc.check) return;
  const id = pc.id;
  pcSearched.add(id);
  $("pc-search").disabled = true;
  setPcStatus("Searching the trade site…");
  try {
    const r = await api("/api/pricecheck/search", { method: "POST", body: JSON.stringify({ id, filters: choices() }) });
    if (pc && pc.id === id) { pc = r.check; pcShown = pc.id; render(); }
  } catch (e) {
    setPcStatus(e.message, "bad");
    if (/newer price check/.test(e.message)) await pcLoad();
  } finally {
    $("pc-search").disabled = false;
  }
}

/** Load the newest check; a new one is shown and searched with its default filters. */
async function pcLoad() {
  let r;
  try { r = await api("/api/pricecheck/current"); } catch { return; }
  const next = r.check;
  if (!next || (pc && next.id === pc.id)) { if (!pc) render(); return; }
  pc = next;
  render();
  pcShown = pc.id;
  if (pc.kind === "trade" && pc.check && !pc.result && !pc.error && !pcSearched.has(pc.id)) search();
}
window.pcLoad = pcLoad; // the app calls it when it shows the popup

function closePopup() {
  if (window.pywebview?.api?.close) window.pywebview.api.close();
}

$("pc-form").addEventListener("submit", (e) => { e.preventDefault(); search(); });
$("pc-filters").addEventListener("change", (e) => {
  const row = e.target.closest(".pc-filter");
  if (!row) return;
  if (e.target.type === "checkbox") row.classList.toggle("off", !e.target.checked);
  else if (e.target.value !== "") { row.querySelector("input[type=checkbox]").checked = true; row.classList.remove("off"); }
});
$("pc-close").addEventListener("click", closePopup);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePopup(); });
pcLoad();
setInterval(pcLoad, 1500); // a new Ctrl+D while open (local request, cheap)
