"use strict";
// The native window (frameless): title bar buttons, double-click to maximize, edge resizing.

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
