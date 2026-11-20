"use strict";
// The Settings page: fields, account check, unsaved changes, overlay opacity, diagnostics and hotkey fields.

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
  // the last check's tabs (saved on this PC), so the list shows without asking pathofexile.com
  if (!account && s.account_cache) account = s.account_cache;
  $("set-log").value = s.log_path;
  $("set-log").placeholder = s.log_path_detected || s.log_path_candidates[0];
  if (s.log_path_exists) setStatus("set-log-status", `✓ Reading ${s.log_path_effective}`, "ok");
  else if (s.log_path_effective) setStatus("set-log-status", `✗ ${s.log_path_effective} not found`, "bad");
  else setStatus("set-log-status", "✗ Client.txt not found in the usual install folders; enter its location", "bad");
  $("set-log-detected").hidden = !s.log_path_detected || s.log_path === s.log_path_detected;
  $("set-log-detected-path").textContent = s.log_path_detected || "";

  $("set-account").value = s.account_name;
  const chip = $("set-session-chip");
  const h = s.session_health;
  chip.className = `chip${h.expired ? " bad" : s.session_saved ? " ok" : ""}`;
  chip.textContent = s.session_from_env ? "set by POE_SESSID variable" : h.expired ? "expired" : s.session_saved ? "saved" : "not set";
  const lines = [];
  if (h.saved_at) {
    lines.push(h.expired
      ? `Rejected by the website ${since(h.expired_at)} (${dateOf(h.expired_at)}), ${h.last_ok ? `after working for ${span(h.worked_seconds)}` : "without ever working: check the account name"}. Paste a new one.`
      : `Saved ${dateOf(h.saved_at)}${h.last_ok ? ` · last worked ${since(h.last_ok)} · working for ${span(h.worked_seconds)}` : " · not used yet"}.`);
  }
  const past = h.history.filter((x) => x.lasted_seconds != null);
  if (past.length) {
    lines.push(`Previous cookies lasted: ${past.map((x) => `${span(x.lasted_seconds)}${x.expired ? "" : " (replaced while still working)"}`).join(", ")}.`);
  }
  $("set-session-health").className = `hint${h.expired ? " bad" : ""}`;
  $("set-session-health").textContent = lines.join(" ");
  $("set-session-clear").disabled = !s.session_saved || s.session_from_env;

  const leagues = (await api("/api/leagues")).options;
  $("set-league").value = s.league;
  $("set-league-options").innerHTML = leagues.map((l) => `<option value="${esc(l)}"></option>`).join("");
  $("set-price-league").value = s.price_league;
  $("set-price-league").placeholder = `Automatic: ${s.price_league_effective}`;
  $("set-price-options").innerHTML = s.ninja_leagues.map((l) => `<option value="${esc(l)}"></option>`).join("");
  renderLeagueHints();
  $("set-run-start").checked = s.snapshot_on_run_start;
  $("set-loot-mode").value = s.loot_mode || "per_map";
  renderLootModeHint();
  $("set-trade-sales").checked = s.trade_sales;
  $("set-version").textContent = `v${s.version}`;
  $("set-check-updates").checked = s.check_updates;
  $("set-hotkey").value = s.campaign_hotkey || "";
  $("set-hotkey-sound").checked = s.campaign_hotkey_sound;
  $("set-overlay").checked = s.overlay_enabled;
  $("set-overlay-key").value = s.overlay_toggle_hotkey || "";
  $("set-overlay-move").value = s.overlay_move_hotkey || "";
  $("set-debug-logging").checked = !!s.debug_logging;
  $("set-overlay-opacity").value = s.overlay_opacity ?? 90;
  $("set-overlay-opacity-value").textContent = `${$("set-overlay-opacity").value}%`;
  $("set-build-snapshots").checked = s.build_snapshots;
  $("set-build-stale").value = String(s.build_stale_hours ?? 24);
  renderShortcuts(s.chat_commands || [], s.search_strings || []);
  $("set-price-key").value = s.price_check_hotkey || "";
  renderHotkeyStatus();
  $("set-update-channel").value = s.update_channel;
  $("set-delay").value = s.snapshot_delay;
  $("set-deaths").value = s.characters.join(", ");
  renderDeathNames();
  renderAccountPickers();
  setStatus("set-save-status", s.loot.enabled ? "Loot tracking is on." : `Loot tracking is off: ${s.loot.reason}`, s.loot.enabled ? "ok" : "warn");
}

const PRIVATE_RE = /\(PL\d+\)\s*$/;

function renderLeagueHints() {
  const league = $("set-league").value.trim();
  const priv = PRIVATE_RE.test(league);
  const chars = account?.characters || [];
  const current = chars.find((c) => c.current);
  let hint = priv ? "Private league: stash and inventory are read from it as usual. " : "";
  if (current && current.league && current.league !== league) {
    hint += `Your current character ${esc(current.name)} is in <a href="#" id="set-league-use">${esc(current.league)}</a>.`;
  } else if (!priv) {
    hint += 'For a private league, enter its full name, e.g. <code>My League (PL12345)</code>.';
  }
  $("set-league-hint").innerHTML = hint;
  $("set-league-use")?.addEventListener("click", (e) => {
    e.preventDefault();
    $("set-league").value = current.league;
    renderLeagueHints();
    renderAccountPickers();
  });
  $("set-price-hint").innerHTML = priv
    ? "poe.ninja has no prices for private leagues. Leave empty to use the current challenge league, " +
      "or pick the league yours is based on (e.g. the hardcore one for a hardcore private league)."
    : "Leave empty to price from the league you play.";
}

function renderDeathNames() {
  const all = settings.log_characters || []; // most recently seen first
  if (!all.length) {
    $("set-deaths-seen").innerHTML = '<span class="hint">No deaths or level-ups in the log yet. Character names show up here once they appear.</span>';
    return;
  }
  const typed = $("set-deaths").value.split(",").map((x) => x.trim().toLowerCase()).filter(Boolean);
  // the last 5 characters, plus any already in the list
  const seen = all.filter((c, i) => i < 5 || typed.includes(c.name.toLowerCase()));
  $("set-deaths-seen").innerHTML = '<span class="hint">Seen in your log (click to add):</span>' + seen.map((c) => {
    const counted = !typed.length || typed.includes(c.name.toLowerCase());
    return `<button type="button" class="name-chip ${counted ? "on" : "off"}" data-name="${esc(c.name)}" title="${counted ? "Deaths counted" : "Deaths ignored: not in the list"}">` +
      `${esc(c.name)}<span>${c.deaths} death${c.deaths === 1 ? "" : "s"} · ${counted ? "counted" : "ignored"}</span></button>`;
  }).join("");
}

function renderAccountPickers() {
  const s = settings;
  const chars = account?.characters || [];
  const opts = [`<option value="">Current character (auto)</option>`]
    .concat(chars.filter((c) => c.league === $("set-league").value || c.name === s.character)
      .sort((a, b) => (b.level ?? 0) - (a.level ?? 0)).map((c) =>
      `<option value="${esc(c.name)}"${c.name === s.character ? " selected" : ""}>${esc(c.name)} · ${esc(c.class || "")} ${c.level ?? ""}</option>`));
  if (s.character && !chars.some((c) => c.name === s.character)) {
    opts.push(`<option value="${esc(s.character)}" selected>${esc(s.character)}</option>`);
  }
  $("set-character").innerHTML = opts.join("");

  // saved entries (names or ids, any case) -> as saved, so an unchanged pick saves the same
  const chosen = new Map(s.stash_tabs.map((t) => [t.toLowerCase(), t]));
  const tabs = account?.tabs || [];
  const known = new Set();
  let html = tabs.map((t) => {
    const saved = chosen.get(String(t.name).toLowerCase()) ?? chosen.get(String(t.id).toLowerCase());
    const on = saved != null;
    if (on) { known.add(String(t.name).toLowerCase()); known.add(String(t.id).toLowerCase()); }
    return `<label><input type="checkbox" value="${esc(saved ?? t.name)}" data-type="${esc(t.type || "")}"${on ? " checked" : ""}>${esc(t.name)}<span class="type">${esc(String(t.type || "").replace(/Stash$/, ""))}</span></label>`;
  }).join("");
  // tabs saved earlier but not in the loaded list (or nothing loaded yet)
  html += s.stash_tabs.filter((t) => !known.has(t.toLowerCase())).map((t) =>
    `<label><input type="checkbox" value="${esc(t)}" checked>${esc(t)}</label>`).join("");
  $("set-tabs").innerHTML = html || '<span class="hint">Press Refresh tabs to load your stash tabs.</span>';
  $("set-tabs-age").textContent = account?.fetched_at
    ? `Tab list from ${ago(account.fetched_at)}${account.tabs_league ? ` (${account.tabs_league})` : ""}. Refresh after adding or renaming tabs.`
    : "";
  renderDirty();
}

async function checkAccount() {
  setStatus("set-check-result", "Checking…");
  $("set-check").disabled = $("set-tabs-refresh").disabled = true;
  try {
    const r = await api("/api/settings/check-account", { method: "POST" });
    if (!r.ok) { setStatus("set-check-result", `✗ ${r.error}`, "bad"); return; }
    account = r;
    const inLeague = r.characters.filter((c) => c.league === $("set-league").value).length;
    const tabsIn = r.tabs_league && r.tabs_league !== $("set-league").value ? ` in ${r.tabs_league}` : "";
    setStatus("set-check-result", `✓ Connected: ${r.characters.length} characters (${inLeague} in ${$("set-league").value}), ${r.tabs.length} stash tabs${tabsIn}` +
      (r.league_note ? `. ${r.league_note}` : ""), r.league_note ? "warn" : "ok");
    const leagues = (await api("/api/leagues")).options; // now includes your characters' leagues
    $("set-league-options").innerHTML = leagues.map((l) => `<option value="${esc(l)}"></option>`).join("");
    renderLeagueHints();
    renderAccountPickers();
  } catch (e) {
    setStatus("set-check-result", `✗ ${e.message}`, "bad");
  } finally {
    $("set-check").disabled = $("set-tabs-refresh").disabled = false;
  }
}

function showWarnings(r, okText) {
  const w = r.warnings || [];
  if (w.length) setStatus("set-save-status", `Saved. ${w.join(" ")}`, "warn");
  else setStatus("set-save-status", r.loot.enabled ? okText : `${okText} Loot tracking is off: ${r.loot.reason}`, r.loot.enabled ? "ok" : "warn");
}

$("set-log-use").addEventListener("click", () => { $("set-log").value = settings.log_path_detected; });
$("set-league").addEventListener("input", () => { renderLeagueHints(); renderAccountPickers(); });

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
  if (!await confirmModal({
    title: "Remove the saved POESESSID?",
    body: "The cookie is deleted from this PC, and loot tracking stops until you add one again.",
    ok: "Remove", danger: true,
  })) return;
  await api("/api/settings/session/clear", { method: "POST" });
  account = null;
  await loadSettings();
  setStatus("set-check-result", "Cookie removed.");
});

$("set-check").addEventListener("click", checkAccount);
$("set-tabs-refresh").addEventListener("click", checkAccount);
$("set-update-check").addEventListener("click", async () => {
  setStatus("set-update-result", "Checking…");
  try {
    const u = await api("/api/update/check", { method: "POST" });
    if (u.error) setStatus("set-update-result", `✗ ${u.error}`, "bad");
    else if (u.available) setStatus("set-update-result", `v${u.available.version} is available, see the banner at the top.`, "ok");
    else setStatus("set-update-result", "You're on the latest version.", "ok");
    try { localStorage.removeItem("poefire.update.dismissed"); } catch { /* ignore */ }
    if (status) { status.update = u; renderUpdate(u); }
  } catch (e) {
    setStatus("set-update-result", `✗ ${e.message}`, "bad");
  }
});
$("set-deaths").addEventListener("input", renderDeathNames);
$("set-deaths-seen").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-name]");
  if (!b) return;
  const names = $("set-deaths").value.split(",").map((x) => x.trim()).filter(Boolean);
  if (!names.some((n) => n.toLowerCase() === b.dataset.name.toLowerCase())) names.push(b.dataset.name);
  $("set-deaths").value = names.join(", ");
  renderDeathNames();
});

function settingsBody() {
  return {
    price_check_hotkey: $("set-price-key").value,
    chat_commands: [...$("set-chat-rows").querySelectorAll(".shortcut-row")].map((r) => ({
      key: r.querySelector(".hk").value, text: r.querySelector(".text").value.trim(),
    })).filter((c) => c.text),
    search_strings: [...$("set-search-rows").querySelectorAll(".shortcut-row")].map((r) => ({
      key: r.querySelector(".hk").value, name: r.querySelector(".name").value.trim(), text: r.querySelector(".text").value,
    })).filter((x) => x.text.trim()),
    log_path: $("set-log").value,
    account_name: $("set-account").value,
    league: $("set-league").value.trim(),
    price_league: $("set-price-league").value.trim(),
    character: $("set-character").value,
    characters: $("set-deaths").value.split(",").map((x) => x.trim()).filter(Boolean),
    stash_tabs: [...$("set-tabs").querySelectorAll("input:checked")].map((i) => i.value),
    snapshot_on_run_start: $("set-run-start").checked,
    loot_mode: $("set-loot-mode").value,
    trade_sales: $("set-trade-sales").checked,
    snapshot_delay: Number($("set-delay").value || 0),
    check_updates: $("set-check-updates").checked,
    campaign_hotkey: $("set-hotkey").value,
    campaign_hotkey_sound: $("set-hotkey-sound").checked,
    overlay_enabled: $("set-overlay").checked,
    overlay_toggle_hotkey: $("set-overlay-key").value,
    overlay_move_hotkey: $("set-overlay-move").value,
    overlay_opacity: Number($("set-overlay-opacity").value),
    debug_logging: $("set-debug-logging").checked,
    build_snapshots: $("set-build-snapshots").checked,
    build_stale_hours: Math.max(0, Math.round(Number($("set-build-stale").value) || 0)),
    update_channel: $("set-update-channel").value,
  };
}

// --- unsaved changes: which fields differ from what's saved ---

// field -> [its element, its name in the save bar]. Opacity and verbose logging save at once.
const SETTING_FIELDS = {
  log_path: ["set-log", "Game log"], account_name: ["set-account", "Account name"],
  league: ["set-league", "League"], price_league: ["set-price-league", "Prices from"],
  character: ["set-character", "Character"], characters: ["set-deaths", "Deaths counted"],
  stash_tabs: ["set-tabs", "Stash tabs"], snapshot_on_run_start: ["set-run-start", "Map-start snapshot"],
  trade_sales: ["set-trade-sales", "Trade sales"], loot_mode: ["set-loot-mode", "Tracking mode"],
  snapshot_delay: ["set-delay", "Snapshot delay"], check_updates: ["set-check-updates", "Update checks"],
  campaign_hotkey: ["set-hotkey", "Campaign hotkey"], campaign_hotkey_sound: ["set-hotkey-sound", "Hotkey sound"],
  overlay_enabled: ["set-overlay", "Overlay"], overlay_toggle_hotkey: ["set-overlay-key", "Overlay hotkey"],
  overlay_move_hotkey: ["set-overlay-move", "Overlay move hotkey"],
  build_snapshots: ["set-build-snapshots", "Build snapshots"],
  build_stale_hours: ["set-build-stale", "Stale-level snapshots"],
  price_check_hotkey: ["set-price-key", "Price check hotkey"], chat_commands: ["set-chat-rows", "Chat commands"], search_strings: ["set-search-rows", "Searches"], update_channel: ["set-update-channel", "Update channel"],
};

function sameSetting(a, b) {
  const item = (x) => typeof x === "object" && x !== null ? JSON.stringify(x) : String(x).trim();
  const norm = (v) => Array.isArray(v) ? JSON.stringify(v.map(item).sort())
    : typeof v === "string" ? v.trim() : v ?? "";
  return norm(a) === norm(b);
}

function changedSettings() {
  if (!settings || $("settings-view").hidden) return [];
  const body = settingsBody();
  return Object.keys(SETTING_FIELDS).filter((k) => !sameSetting(body[k], settings[k] ?? (Array.isArray(body[k]) ? [] : "")));
}

function renderLootModeHint() {
  const interval = $("set-loot-mode").value === "interval";
  $("set-loot-mode-hint").textContent = interval
    ? "One snapshot every 10 minutes while you're mapping (and one when you start again), none around maps: far fewer requests, so rate limits are rare. The Session page shows the session's total loot, maps done and the average per map, not each map's loot."
    : "A snapshot when you're back from each map (and one when a map starts, below): each map's own loot. More requests.";
  $("set-run-start").disabled = interval;
}
$("set-loot-mode").addEventListener("change", renderLootModeHint);

/** What the watched tabs cost: GGG rate-limits these requests, one per tab per snapshot. */
function renderTabsCost() {
  const picked = [...$("set-tabs").querySelectorAll("input:checked")];
  const sectioned = picked.filter((i) => /^(MapStash|UniqueStash)$/.test(i.dataset.type || "")).length;
  const folders = picked.filter((i) => i.dataset.type === "Folder").length;
  const perSnapshot = 1 + picked.length + ($("set-trade-sales").checked ? 1 : 0);
  const interval = $("set-loot-mode").value === "interval";
  const perMap = $("set-run-start").checked ? 2 : 1;
  const extra = [
    sectioned ? `+1 per section of the ${sectioned === 1 ? "map/unique tab" : `${sectioned} map/unique tabs`} (map tab: Tier 16–17, Unique and Special only)` : "",
    folders ? `+1 per tab in the ${folders === 1 ? "folder" : `${folders} folders`}` : "",
  ].filter(Boolean).join(", ");
  const el = $("set-tabs-cost");
  el.className = `hint ${perSnapshot * perMap > 16 || sectioned ? "warn" : ""}`;
  const parts = ["inventory", `${picked.length} tab${picked.length === 1 ? "" : "s"}`]
    .concat($("set-trade-sales").checked ? ["trade history"] : []).join(" + ");
  el.textContent = `About ${perSnapshot} request${perSnapshot === 1 ? "" : "s"} per snapshot (${parts}${extra ? `; ${extra}` : ""}), ` +
    (interval ? "every 10 minutes while mapping. " : `up to ${perMap === 2 ? "2 snapshots" : "1 snapshot"} per map. `) +
    "pathofexile.com limits how many requests poefire may make: fewer tabs mean fewer waits.";
}

function renderDirty() {
  renderTabsCost();
  const changed = changedSettings();
  for (const [key, [id]] of Object.entries(SETTING_FIELDS)) {
    const field = $(id)?.closest(".field, .check, .tabs-list") || $(id);
    field?.classList.toggle("changed", changed.includes(key));
  }
  for (const b of $("set-tabs-nav").querySelectorAll("button[data-tab]")) {
    b.classList.toggle("has-changes", !!$(`set-panel-${b.dataset.tab}`).querySelector(".changed"));
  }
  $("save-bar").classList.toggle("dirty", changed.length > 0);
  $("set-discard").hidden = !changed.length;
  $("set-dirty").hidden = !changed.length;
  $("set-dirty").textContent = changed.length
    ? `Unsaved: ${changed.map((k) => SETTING_FIELDS[k][1]).join(", ")}` : "";
  if (changed.length) $("set-save-status").textContent = "";
  return changed;
}
for (const ev of ["input", "change", "keyup", "click"]) {
  $("settings-view").addEventListener(ev, () => setTimeout(renderDirty, 0));
}
$("set-discard").addEventListener("click", async () => {
  await loadSettings().catch(() => {});
  renderDirty();
});

$("settings-view").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = settingsBody();
  $("set-save").disabled = true;
  try {
    const leagueChanged = settings && body.league !== settings.league;
    const r = await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
    await loadSettings();
    if (leagueChanged && account) await checkAccount(); // stash tabs are per league
    await loadLeagues().catch(() => {});
    showWarnings(r, r.snapshot ? "Saved. Took a snapshot with your new stash tabs, so what's already in them doesn't count as loot." : "Saved.");
    renderDirty();
  } catch (err) {
    setStatus("set-save-status", `✗ ${err.message}`, "bad");
  } finally {
    $("set-save").disabled = false;
  }
});

// Overlay opacity: previewed live while dragging (the overlay shows for a moment), saved on release.
let opacityTimer = null;
function sendOpacity(save) {
  const percent = Number($("set-overlay-opacity").value);
  $("set-overlay-opacity-value").textContent = `${percent}%`;
  return api("/api/overlay/opacity", { method: "POST", body: JSON.stringify({ percent, save }) })
    .then(() => { if (save && settings) settings.overlay_opacity = percent; })
    .catch((err) => setStatus("set-save-status", `✗ ${err.message}`, "bad"));
}
$("set-overlay-opacity").addEventListener("input", () => {
  $("set-overlay-opacity-value").textContent = `${$("set-overlay-opacity").value}%`;
  clearTimeout(opacityTimer);
  opacityTimer = setTimeout(() => sendOpacity(false), 60);
});
$("set-overlay-opacity").addEventListener("change", () => {
  clearTimeout(opacityTimer);
  sendOpacity(true);
});

// --- diagnostics ---

$("set-debug-logging").addEventListener("change", async (e) => {
  const on = e.target.checked;
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify({ debug_logging: on }) });
    if (settings) settings.debug_logging = on;
    $("diag-result").textContent = on ? "Verbose logging is on." : "Verbose logging is off.";
  } catch (err) {
    e.target.checked = !on;
    $("diag-result").textContent = `✗ ${err.message}`;
  }
});
$("diag-report").addEventListener("click", async () => {
  const btn = $("diag-report");
  btn.disabled = true;
  $("diag-result").textContent = "Saving…";
  try {
    const r = await api("/api/diagnostics/report", { method: "POST" });
    const shown = await api("/api/diagnostics/reveal", { method: "POST", body: JSON.stringify({ what: r.name }) })
      .catch(() => ({ opened: false }));
    $("diag-result").innerHTML = `Saved <b>${esc(r.name)}</b> in <code>${esc(r.folder)}</code>${
      shown.opened ? " (opened in Explorer)" : ""}. <a href="/api/diagnostics/report/${encodeURIComponent(r.name)}" download>Download</a>`;
  } catch (err) {
    $("diag-result").textContent = `✗ The report couldn't be saved: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
});
$("diag-logs").addEventListener("click", async () => {
  try {
    const r = await api("/api/diagnostics/reveal", { method: "POST", body: JSON.stringify({ what: "logs" }) });
    if (!r.opened) $("diag-result").innerHTML = `The logs are in <code>${esc(r.path)}</code>.`;
  } catch (err) {
    $("diag-result").textContent = `✗ ${err.message}`;
  }
});

// --- tabs: one panel at a time; #settings/<tab> opens a given tab ---

const SETTINGS_TABS = ["general", "loot", "campaign", "shortcuts", "app"];
let settingsTab = "general";

/** Shows a settings tab. Switching tabs never reloads the form, so unsaved edits stay. */
function showSettingsTab(tab) {
  if (!SETTINGS_TABS.includes(tab)) tab = settingsTab;
  settingsTab = tab;
  for (const b of $("set-tabs-nav").querySelectorAll("button[data-tab]")) {
    b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  }
  for (const p of $("settings-view").querySelectorAll(".settings-panel")) p.hidden = p.dataset.panel !== tab;
  const hash = `#settings/${tab}`;
  if (location.hash !== hash) history.replaceState(null, "", hash); // no hashchange: no re-route
}
$("set-tabs-nav").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (!b) return;
  showSettingsTab(b.dataset.tab);
  $("content").scrollTop = 0;
});
// A field the browser rejects on Save may be on another tab: show that tab so it can be focused.
$("settings-view").addEventListener("invalid", (e) => {
  const panel = e.target.closest(".settings-panel");
  if (panel?.hidden) showSettingsTab(panel.dataset.panel);
}, true);


// --- hotkey field: click it, then press the combination ---

// hotkey fields: input id -> its status line and where its live status is in /api/status
const HOTKEY_FIELDS = {
  "set-hotkey": { status: "set-hotkey-status", live: () => status?.hotkey },
  "set-overlay-key": { status: "set-overlay-key-status", live: () => status?.overlay?.toggle },
  "set-overlay-move": { status: "set-overlay-move-status", live: () => status?.overlay?.move },
  "set-price-key": { status: "set-price-key-status", live: () => settings?.game_hotkeys?.price_check },
};

function renderHotkeyStatus() {
  for (const [id, f] of Object.entries(HOTKEY_FIELDS)) {
    const hk = f.live();
    const el = $(f.status);
    const value = $(id).value;
    if (!value) { el.className = "hint"; el.textContent = "Off."; continue; }
    if (!hk || hk.key !== value) { el.className = "hint"; el.textContent = "Save to apply."; continue; }
    el.className = `hint ${hk.error ? "bad" : hk.active ? "ok" : ""}`;
    el.textContent = hk.error ? `✗ ${hk.error}` : hk.active ? `✓ ${hk.key} is active.` : "Not active (global hotkeys need the Windows app).";
  }
  $("set-overlay-unavailable").hidden = !status?.overlay || status.overlay.available || !$("set-overlay").checked;
}
const KEY_NAMES = { " ": "Space", PageUp: "PgUp", PageDown: "PgDn", Insert: "Insert", Delete: "Delete", Home: "Home", End: "End", Pause: "Pause", ScrollLock: "ScrollLock" };
function keyName(e) {
  if (/^F\d{1,2}$/.test(e.key)) return e.key;
  if (e.code.startsWith("Numpad")) {
    const rest = e.code.slice(6);
    return /^\d$/.test(rest) ? `Num${rest}` : null;
  }
  if (/^Key[A-Z]$/.test(e.code)) return e.code.slice(3);
  if (/^Digit\d$/.test(e.code)) return e.code.slice(5);
  return KEY_NAMES[e.key] || null;
}
for (const id of Object.keys(HOTKEY_FIELDS)) {
  const input = $(id);
  input.addEventListener("focus", () => input.classList.add("capturing"));
  input.addEventListener("blur", () => input.classList.remove("capturing"));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Tab") return;
    e.preventDefault();
    if (["Control", "Alt", "Shift", "Meta"].includes(e.key)) return; // wait for the main key
    if (e.key === "Escape") { input.blur(); return; }
    const key = keyName(e);
    if (!key) return;
    const mods = [e.ctrlKey && "Ctrl", e.altKey && "Alt", e.shiftKey && "Shift", e.metaKey && "Win"].filter(Boolean);
    input.value = [...mods, key].join("+");
    renderHotkeyStatus();
  });
  $(`${id}-clear`).addEventListener("click", () => { input.value = ""; renderHotkeyStatus(); });
}

// --- in-game shortcuts: chat commands and stash/vendor searches, each on its own hotkey ---

function shortcutRow(kind, x) {
  const row = document.createElement("div");
  row.className = `shortcut-row ${kind}`;
  const err = settings?.game_hotkeys?.errors?.[kind === "chat" ? `${x.key}|${x.text}` : `${x.key}|${x.name}|${x.text}`];
  row.innerHTML = `<input class="hk" type="text" readonly placeholder="No hotkey" value="${esc(x.key || "")}" title="Click, then press keys (Esc to cancel, Delete to clear)">` +
    (kind === "search" ? `<input class="name" type="text" placeholder="Name" value="${esc(x.name || "")}" maxlength="60">` : "") +
    `<input class="text" type="text" spellcheck="false" placeholder="${kind === "chat" ? "/hideout" : "search text or regex"}" value="${esc(x.text || "")}" maxlength="${kind === "chat" ? 256 : 250}">` +
    '<button type="button" class="btn link danger remove">Remove</button>' +
    (err ? `<div class="hint bad err">✗ ${esc(err)}</div>` : "");
  return row;
}

function renderShortcuts(chat, searches) {
  $("set-chat-rows").replaceChildren(...chat.map((c) => shortcutRow("chat", c)));
  $("set-search-rows").replaceChildren(...searches.map((x) => shortcutRow("search", x)));
  const gh = settings?.game_hotkeys;
  $("set-shortcuts-status").className = `hint ${gh && !gh.available ? "warn" : ""}`;
  $("set-shortcuts-status").textContent = gh && !gh.available
    ? "In-game shortcuts need the Windows app."
    : gh?.last ? `Last sent: ${gh.last}` : "";
}

for (const [id, kind] of [["set-chat-rows", "chat"], ["set-search-rows", "search"]]) {
  const box = $(id);
  box.addEventListener("click", (e) => {
    if (e.target.closest(".remove")) { e.target.closest(".shortcut-row").remove(); renderDirty(); }
  });
  box.addEventListener("focusin", (e) => e.target.classList.contains("hk") && e.target.classList.add("capturing"));
  box.addEventListener("focusout", (e) => e.target.classList.remove("capturing"));
  box.addEventListener("keydown", (e) => {
    const input = e.target;
    if (!input.classList.contains("hk") || e.key === "Tab") return;
    e.preventDefault();
    if (["Control", "Alt", "Shift", "Meta"].includes(e.key)) return;
    if (e.key === "Escape") { input.blur(); return; }
    if (e.key === "Backspace" || e.key === "Delete") { input.value = ""; renderDirty(); return; }
    const key = keyName(e);
    if (!key) return;
    const mods = [e.ctrlKey && "Ctrl", e.altKey && "Alt", e.shiftKey && "Shift", e.metaKey && "Win"].filter(Boolean);
    input.value = [...mods, key].join("+");
    renderDirty();
  });
  $(kind === "chat" ? "set-chat-add" : "set-search-add").addEventListener("click", () => {
    box.append(shortcutRow(kind, { key: "", name: "", text: "" }));
    box.lastElementChild.querySelector(kind === "chat" ? ".text" : ".name").focus();
  });
}
