"use strict";
// In-game overlay: polls /api/overlay; timers tick locally between polls.

const $ = (id) => document.getElementById(id);
let data = null;
let fetchedAt = 0;

function duration(sec) {
  if (sec == null) return "—";
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}
function signed(sec) {
  if (sec == null) return '<span class="dim">—</span>';
  const cls = sec < 0 ? "ahead" : sec > 0 ? "behind" : "";
  return `<span class="${cls}">${sec < 0 ? "−" : "+"}${duration(Math.abs(sec))}</span>`;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}
const nf0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const chaos = (v) => (Math.abs(v) >= 100 ? nf0.format(v) : nf1.format(v)) + "c";

let moving = false;

// called by the app (move hotkey, or after the button)
window.setMoving = (on) => {
  moving = on;
  document.body.classList.toggle("moving", on);
  $("move-hint").hidden = !on;
  $("drag-cover").hidden = !on;
  $("ov-btn").innerHTML = on ? "&#128274; Lock" : "&#10021; Move";
  $("ov-btn").title = on ? "Lock the overlay here (clicks pass through to the game again)"
    : "Unlock to drag the overlay (the move hotkey does the same)";
};

$("ov-btn").addEventListener("click", () => {
  const api = window.pywebview?.api;
  if (!api) return; // only in the app's overlay window
  if (moving) api.lock(); else api.move();
});

function elapsed() {
  return (performance.now() - fetchedAt) / 1000;
}

function render() {
  const d = data;
  $("campaign").hidden = !d || d.mode !== "campaign";
  $("mapping").hidden = !d || d.mode !== "mapping";
  $("waiting").hidden = !!d;
  if (!d) return;
  if (d.mode === "campaign") {
    $("c-meta").textContent = [d.act ? (d.act > 10 ? "Epilogue" : `Act ${d.act}`) : null, d.class].filter(Boolean).join(" · ");
    $("c-pb").textContent = d.pb_class ? `vs your best ${d.pb_class} run` : d.base_class ? `no finished ${d.base_class} run yet` : "PB once your class is known";
    $("c-areas").innerHTML = d.areas.map((a) => `<tr>
      <td class="n" title="${esc(a.name)}">${esc(a.name)}</td>
      <td class="t">${duration(a.rta)}</td>
      <td class="p">${a.pb_rta == null ? "—" : duration(a.pb_rta)}</td>
      <td class="d">${signed(a.delta)}</td></tr>`).join("") || '<tr><td class="dim">No areas yet</td></tr>';
  } else {
    $("m-avg").innerHTML = d.avg_chaos == null ? '<span class="dim">—</span>'
      : `${chaos(d.avg_chaos)}${d.avg_divine == null ? "" : ` <span class="divine">${nf2.format(d.avg_divine)}d</span>`}`;
    $("m-total").innerHTML = d.maps
      ? `${d.chaos == null ? "—" : `<span class="chaos">${chaos(d.chaos)}</span>`}${d.divine == null ? "" : ` <span class="divine">${nf2.format(d.divine)}d</span>`} · ${d.maps} map${d.maps === 1 ? "" : "s"}`
      : "No maps yet";
    $("m-map-name").textContent = d.current ? d.current.name : "Not in a map";
  }
  tick();
}

function tick() {
  const d = data;
  if (!d) return;
  if (d.mode === "campaign") {
    const t = d.rta + (d.paused ? 0 : elapsed());
    $("c-timer").textContent = d.paused ? `${duration(t)} ⏸` : duration(t);
    $("c-timer").classList.toggle("paused", !!d.paused);
  } else if (d.current) {
    const t = d.current.seconds + (d.current.status === "active" ? elapsed() : 0);
    $("m-map-time").textContent = duration(t) + (d.current.status === "active" ? "" : " (hideout)");
  } else {
    $("m-map-time").textContent = "";
  }
}

async function poll() {
  if (document.hidden) return; // not shown over the game: nothing to draw
  try {
    const r = await fetch("/api/overlay");
    if (r.ok) {
      data = await r.json();
      fetchedAt = performance.now();
      render();
    }
  } catch { /* the app is restarting */ }
}

poll();
setInterval(poll, 1000);
setInterval(() => { if (!document.hidden) tick(); }, 250);
document.addEventListener("visibilitychange", poll);
