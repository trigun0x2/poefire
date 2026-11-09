"use strict";
// The setup wizard: game log, account (cookie), league, stash tabs, step by step and checked
// as you go. Opens by itself until it's finished or skipped once; Settings can open it again.

const wz = { step: 0, settings: null, account: null, league: null, busy: false };
const WZ_SUGGEST = /currency|dump|frag|scarab|map|essence|div|deli|loot|sell|quad|^\d+$/i;

function wzSay(id, text, cls = "") {
  $(id).className = `hint ${cls}`;
  $(id).textContent = text;
}

async function openWizard() {
  wz.settings = await api("/api/settings");
  wz.account = null;
  wz.league = wz.settings.league;
  const s = wz.settings;
  $("wz-log").value = s.log_path || "";
  $("wz-log-found").innerHTML = s.log_path_exists
    ? `<span class="ok">✓ Found: <code>${esc(s.log_path_effective)}</code></span>`
    : '<span class="warn">Not found at the usual places yet. Paste the game folder below, or just start the game: poefire picks up the running game\'s log by itself.</span>';
  $("wz-account").value = s.account_name || "";
  $("wz-cookie").value = "";
  $("wz-cookie").placeholder = s.session_saved ? "A cookie is saved: leave empty to keep it" : "32 letters and numbers";
  for (const id of ["wz-log-status", "wz-account-status", "wz-tabs-status"]) wzSay(id, "");
  wzShow(0);
  if (!$("wizard").open) $("wizard").showModal();
  $("wz-next").focus();
}

function wzShow(step) {
  wz.step = step;
  for (const p of document.querySelectorAll(".wz-page")) p.hidden = Number(p.dataset.page) !== step;
  for (const li of $("wz-steps").children) {
    const n = Number(li.dataset.step);
    li.className = n === step ? "now" : n < step ? "done" : "";
  }
  $("wz-steps").hidden = step === 4;
  $("wz-sub").textContent = step < 4 ? `Step ${step + 1} of 4` : "Done";
  $("wz-back").hidden = step === 0 || step === 4;
  $("wz-later").hidden = step !== 1;
  $("wz-skip").hidden = step === 4;
  $("wz-next").textContent = step === 4 ? "Start using poefire" : step === 3 ? "Save" : "Next";
  $("wz-next").focus();
}

async function wzFinish() {
  await api("/api/onboarding/done", { method: "POST" }).catch(() => {});
  $("wizard").close();
  await pollStatus();
  if (!$("settings-view").hidden) loadSettings().catch(() => {});
}

function wzLeagues() {
  const chars = wz.account?.characters || [];
  const byLeague = new Map();
  for (const c of chars) {
    const l = byLeague.get(c.league) || { league: c.league, chars: [], current: false };
    l.chars.push(c);
    l.current ||= c.current;
    byLeague.set(c.league, l);
  }
  const leagues = [...byLeague.values()].sort((a, b) => (b.current - a.current) || (b.chars.length - a.chars.length));
  if (!wz.league || !byLeague.has(wz.league)) wz.league = leagues[0]?.league ?? wz.league;
  $("wz-leagues").innerHTML = leagues.map((l) => {
    const top = [...l.chars].sort((a, b) => (b.level ?? 0) - (a.level ?? 0)).slice(0, 3)
      .map((c) => `${esc(c.name)} ${c.level ?? ""}`).join(", ");
    return `<label class="wz-choice${l.league === wz.league ? " on" : ""}"><input type="radio" name="wz-league" value="${esc(l.league)}"${l.league === wz.league ? " checked" : ""}>
      <b>${esc(l.league)}</b>${l.current ? ' <span class="chip ok">playing now</span>' : ""}
      <span class="muted">${l.chars.length} character${l.chars.length === 1 ? "" : "s"}: ${top}${l.chars.length > 3 ? "…" : ""}</span></label>`;
  }).join("") || '<div class="hint">No characters on this account yet.</div>';
}

function wzTabs() {
  const tabs = wz.account?.tabs || [];
  const saved = new Set((wz.settings.stash_tabs || []).map((t) => t.toLowerCase()));
  const keep = tabs.some((t) => saved.has(String(t.name).toLowerCase()) || saved.has(String(t.id).toLowerCase()));
  $("wz-tabs").innerHTML = tabs.map((t) => {
    const on = keep ? saved.has(String(t.name).toLowerCase()) || saved.has(String(t.id).toLowerCase())
      : WZ_SUGGEST.test(String(t.name)) || /Currency|Fragment|Map|Essence|Divination/.test(String(t.type));
    return `<label><input type="checkbox" value="${esc(t.name)}"${on ? " checked" : ""}>${esc(t.name)}<span class="type">${esc(String(t.type || "").replace(/Stash$/, ""))}</span></label>`;
  }).join("") || '<span class="hint">No stash tabs in this league yet: you can pick them in Settings later.</span>';
}

async function wzNext() {
  if (wz.busy) return;
  wz.busy = true;
  $("wz-next").disabled = true;
  try {
    if (wz.step === 0) {
      const path = $("wz-log").value.trim();
      if (path !== (wz.settings.log_path || "")) {
        const r = await api("/api/settings", { method: "POST", body: JSON.stringify({ log_path: path }) });
        wz.settings = r;
        if (path && !r.log_path_exists) wzSay("wz-log-status", `${r.log_path_effective} doesn't exist yet: it's used once the game creates it.`, "warn");
      }
      wzShow(1);
    } else if (wz.step === 1) {
      const name = $("wz-account").value.trim(), cookie = $("wz-cookie").value.trim();
      if (!name || !/#\d{4}$/.test(name)) { wzSay("wz-account-status", "Enter your full account name, with the #1234 at the end.", "bad"); return; }
      if (!cookie && !wz.settings.session_saved) { wzSay("wz-account-status", "Paste your POESESSID (or skip this step).", "bad"); return; }
      wzSay("wz-account-status", "Checking…");
      if (name !== wz.settings.account_name) {
        wz.settings = await api("/api/settings", { method: "POST", body: JSON.stringify({ account_name: name }) });
      }
      if (cookie) await api("/api/settings/session", { method: "POST", body: JSON.stringify({ poesessid: cookie }) });
      const acc = await api("/api/settings/check-account", { method: "POST" });
      if (!acc.ok) { wzSay("wz-account-status", `✗ ${acc.error}`, "bad"); return; }
      wz.account = acc;
      wzSay("wz-account-status", `✓ Connected: ${acc.characters.length} characters`, "ok");
      wzLeagues();
      wzShow(2);
    } else if (wz.step === 2) {
      const chosen = document.querySelector('input[name="wz-league"]:checked')?.value || wz.league;
      if (chosen && chosen !== wz.settings.league) {
        await api("/api/league", { method: "POST", body: JSON.stringify({ league: chosen }) });
        wz.settings = await api("/api/settings");
        wz.account = await api("/api/settings/check-account", { method: "POST" }); // this league's tabs
      }
      wz.league = chosen;
      wzTabs();
      wzShow(3);
    } else if (wz.step === 3) {
      const tabs = [...$("wz-tabs").querySelectorAll("input:checked")].map((i) => i.value);
      if (tabs.length) {
        await api("/api/settings", { method: "POST", body: JSON.stringify({ stash_tabs: tabs }) });
      }
      const st = await api("/api/status");
      $("wz-summary").innerHTML = [
        `<li>Game log: ${st.log_path ? `<code>${esc(st.log_path)}</code>` : "found once the game runs"}</li>`,
        `<li>League: <b>${esc(st.league)}</b></li>`,
        `<li>Loot tracking: ${st.loot.enabled ? `on, ${tabs.length} tab${tabs.length === 1 ? "" : "s"} watched` : `off (${esc(st.loot.reason || "")})`}</li>`,
      ].join("");
      wzShow(4);
    } else {
      await wzFinish();
    }
  } catch (e) {
    const where = { 0: "wz-log-status", 1: "wz-account-status", 2: "wz-account-status", 3: "wz-tabs-status" }[wz.step];
    if (where) wzSay(where, `✗ ${e.message}`, "bad");
  } finally {
    wz.busy = false;
    $("wz-next").disabled = false;
  }
}

$("wz-next").addEventListener("click", wzNext);
$("wz-back").addEventListener("click", () => wzShow(Math.max(0, wz.step - 1)));
$("wz-later").addEventListener("click", async () => {
  const st = await api("/api/status").catch(() => null);
  $("wz-summary").innerHTML = `<li>Game log: ${st?.log_path ? `<code>${esc(st.log_path)}</code>` : "found once the game runs"}</li>` +
    "<li>Loot tracking: off for now. Connect your account later in Settings to see what your maps drop.</li>";
  wzShow(4);
});
$("wz-skip").addEventListener("click", wzFinish);
$("wizard").addEventListener("cancel", (e) => { e.preventDefault(); wzFinish(); }); // Esc = skip
$("wz-leagues").addEventListener("change", (e) => {
  for (const l of $("wz-leagues").querySelectorAll(".wz-choice")) l.classList.toggle("on", l.contains(e.target) && e.target.checked);
});
$("set-wizard").addEventListener("click", () => openWizard().catch((e) => alertModal("The setup couldn't open", e.message)));
