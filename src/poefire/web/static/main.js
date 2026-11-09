"use strict";
// Routing between pages, and start-up (loaded last: everything else is defined by then).

// --- routing: #session (default), #overview, #campaign, #settings ---

const VIEWS = { session: "dashboard", overview: "overview-view", campaign: "campaign-view", builds: "builds-view", settings: "settings-view" };

let routeView = null;
let leavingSettings = false;
async function route() {
  const name = Object.hasOwn(VIEWS, location.hash.slice(1)) ? location.hash.slice(1) : "session";
  if (routeView === "settings" && name !== "settings" && !leavingSettings && changedSettings().length) {
    const target = location.hash || "#session";
    history.replaceState(null, "", "#settings"); // stay until they choose
    const go = await confirmModal({
      title: "Leave without saving?",
      body: `You have unsaved settings: <b>${esc(changedSettings().map((k) => SETTING_FIELDS[k][1]).join(", "))}</b>.`,
      ok: "Discard and leave", cancel: "Keep editing", danger: true,
    });
    if (!go) return;
    leavingSettings = true;
    location.hash = target; // routes again
    return;
  }
  leavingSettings = false;
  routeView = name;
  for (const [key, id] of Object.entries(VIEWS)) {
    $(id).hidden = key !== name;
    $(`nav-${key}`).classList.toggle("active", key === name);
  }
  $("content").scrollTop = 0;
  renderSessionBanner(status?.loot.session);
  if (name === "settings") loadSettings().catch((e) => setStatus("set-save-status", `✗ ${e.message}`, "bad"));
  if (name === "overview") loadOverview().catch(() => {});
  if (name === "campaign") loadCampaign().catch(() => {});
  if (name === "builds") loadBuilds().catch(() => {});
  if (name === "session" && runsData) loadSessions().catch(() => {});
}
window.addEventListener("hashchange", route);

makeSortable("runs-table", "runs", runsSort, () => { if (runsData) renderRuns(); });
makeSortable("sessions-table", "sessions", sessionsSort, () => { if (sessionsData) renderSessions(); });
makeSortable("ov-items-table", "items", itemsSort, () => { if (ovItems) renderHoldings(); });

(async function init() {
  route();
  initWindowControls();
  await loadLeagues().catch(() => {});
  await pollStatus();
  if (!runsData) await pollRuns();
  // first start (or setup never finished): the setup wizard
  const setup = await api("/api/settings").catch(() => null);
  if (setup && !setup.onboarded) openWizard().catch(() => {});
  // Polling only redraws the page: tracking, snapshots and prices run in the app itself.
  // While the window is minimized or hidden nothing is fetched or drawn; it catches up at once
  // when it's shown again.
  const shown = (view) => !document.hidden && (!view || !$(view).hidden);
  setInterval(() => { if (shown()) pollStatus(); }, 1000);
  setInterval(() => { if (shown()) pollRuns({ sessions: false }); }, 15000);
  setInterval(() => { if (shown()) tickTimer(); }, 250);
  setInterval(() => { if (shown("campaign-view")) tickCampaign(); }, 250);
  setInterval(() => { if (shown("campaign-view")) loadCampaign().catch(() => {}); }, 2000);
  setInterval(() => { if (shown("overview-view")) loadOverview().catch(() => {}); }, 60000);
  setInterval(() => { if (shown("builds-view")) loadBuilds().catch(() => {}); }, 20000);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    pollStatus().then(() => pollRuns()).catch(() => {});
    if (shown("campaign-view")) loadCampaign().catch(() => {});
    if (shown("overview-view")) loadOverview().catch(() => {});
    if (shown("builds-view")) loadBuilds().catch(() => {});
  });
})();
