"use strict";
// Shared helpers: element lookup, app state, number/time formatting, the API call and in-app dialogs.

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
