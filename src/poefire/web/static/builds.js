"use strict";
// The Builds page: a character's gear, gems and passives at each level, and PoB codes.

// --- builds ---

let bdData = null; // /api/builds
let bdChar = null; // selected character
let bdLevels = []; // its snapshots, by level
let bdIndex = 0;
const bdCache = new Map(); // snapshot id -> detail

function staleText(hours) {
  return hours === 24 ? "once a day" : hours === 1 ? "every hour" : `every ${hours} hours`;
}

function bdStatus() {
  const d = bdData;
  const el = $("bd-status");
  let text = "", cls = "hint";
  if (!d.enabled) { text = "Build snapshots are off (Settings → Builds)."; cls += " warn"; }
  else if (!d.connected) { text = `Not connected to your account${d.reason ? `. ${lootReason(d.reason)}` : ""}. Build snapshots use the account set up for loot in Settings → General.`; cls += " warn"; }
  else if (d.pending.length) text = `Waiting for a zone change to capture: ${d.pending.map((p) => `${p.character} level ${p.level}`).join(", ")}.`;
  else if (d.last_error) { text = `Last snapshot failed: ${d.last_error}`; cls += " bad"; }
  else text = "Snapshots are taken at levels 5 and 10, then every level from 15, at your next zone change after the level-up" +
    (d.stale_hours ? `; from level 80 also ${staleText(d.stale_hours)} when your gear, gems or passives changed.` : ".");
  el.className = cls;
  el.textContent = text;
}

async function loadBuilds() {
  bdData = await api("/api/builds");
  const chars = bdData.characters;
  // listed by level, highest first; opens on the character captured most recently
  const latest = [...chars].sort((a, b) => (b.last_at || "").localeCompare(a.last_at || ""))[0];
  if (!bdChar || !chars.some((c) => c.character === bdChar)) bdChar = latest?.character ?? null;
  $("bd-character").innerHTML = chars.map((c) =>
    `<option value="${esc(c.character)}"${c.character === bdChar ? " selected" : ""}>${esc(c.character)} — ${esc(c.class || "?")} ${c.level}</option>`).join("");
  $("bd-character").disabled = !chars.length;
  $("bd-now").disabled = !bdChar || !bdData.connected;
  bdStatus();
  await loadBuildLevels();
}

async function loadBuildLevels(keepLevel = true) {
  if (!bdChar) { bdLevels = []; renderBuild(); return; }
  const prev = bdLevels[bdIndex]?.id;
  const atEnd = bdIndex === bdLevels.length - 1;
  bdLevels = (await api(`/api/builds/character/${encodeURIComponent(bdChar)}`)).snapshots;
  const keep = keepLevel && !atEnd ? bdLevels.findIndex((l) => l.id === prev) : -1;
  bdIndex = keep >= 0 ? keep : bdLevels.length - 1; // follow the newest unless you picked one
  const slider = $("bd-slider");
  slider.max = Math.max(bdLevels.length - 1, 0);
  slider.value = bdIndex;
  slider.disabled = bdLevels.length < 2;
  const n = bdLevels.length;
  // label every level when they fit, else every 5th; never closer than ~4% apart
  const every = n > 24 ? 5 : 1;
  const pos = (i) => (n > 1 ? (i / (n - 1)) * 100 : 0);
  const shown = [];
  bdLevels.forEach((l, i) => {
    if (!(i === 0 || i === n - 1 || l.level % every === 0 || !l.ok)) return;
    if (shown.length && bdLevels[shown.at(-1)].level === l.level) return; // one label per level
    if (shown.length && pos(i) - pos(shown.at(-1)) < 4) {
      if (i !== n - 1) return;
      shown.pop(); // keep the newest level's label
    }
    shown.push(i);
  });
  $("bd-ticks").innerHTML = shown.map((i) =>
    `<span class="${bdLevels[i].ok ? "" : "miss"}" style="left:${pos(i)}%" title="${bdLevels[i].ok ? "" : "missed"}">${bdLevels[i].level}</span>`).join("");
  await renderBuild();
}

/** Several snapshots at this level (not levelling: stale-level ones, or Snapshot now): pick one. */
function renderSameLevel(snap) {
  const same = bdLevels.map((l, i) => [l, i]).filter(([l]) => l.level === snap.level);
  const el = $("bd-same");
  el.hidden = same.length < 2;
  if (same.length < 2) return;
  el.innerHTML = `<span class="label">${same.length} snapshots at level ${snap.level}:</span>` + same.map(([l, i]) =>
    `<button type="button" data-i="${i}" class="${i === bdIndex ? "on" : ""}" title="${l.ok ? "" : "missed"}">${esc(dateTimeOf(l.taken_at))}</button>`).join("");
}
$("bd-same").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-i]");
  if (!b) return;
  bdIndex = Number(b.dataset.i);
  $("bd-slider").value = bdIndex;
  renderBuild().catch(() => {});
});

/** What changed since the snapshot before this one (the changed gear is outlined too). */
function renderBuildChanges(ch) {
  const el = $("bd-changes");
  el.hidden = !ch;
  if (!ch) return;
  const parts = [];
  const slots = (bdCache.get(bdLevels[bdIndex]?.id)?.gear || []).filter((g) => ch.slots.includes(g.key)).map((g) => g.slot);
  if (slots.length) parts.push(`gear: <b>${slots.map(esc).join(", ")}</b>`);
  if (ch.removed.length) parts.push(`removed: ${ch.removed.map(esc).join(", ")}`);
  if (ch.gems.length) parts.push(`new gems: <b>${ch.gems.map((g) => esc(g.replace(/ Support$/, ""))).join(", ")}</b>`);
  if (ch.passives) parts.push(`${ch.passives > 0 ? "+" : ""}${ch.passives} passive point${Math.abs(ch.passives) === 1 ? "" : "s"}`);
  else if (ch.tree_changed) parts.push("passive tree respecced");
  const since = `level ${ch.previous.level}, ${dateTimeOf(ch.previous.taken_at)}`;
  el.innerHTML = parts.length ? `Changed since the previous snapshot (${esc(since)}): ${parts.join(" · ")}`
    : `No changes since the previous snapshot (${esc(since)}).`;
}

async function renderBuild() {
  const snap = bdLevels[bdIndex];
  $("bd-empty").hidden = !!snap;
  $("bd-body").hidden = !snap;
  $("bd-level").textContent = snap ? snap.level : "";
  $("bd-prev").disabled = !snap || bdIndex === 0;
  $("bd-next").disabled = !snap || bdIndex >= bdLevels.length - 1;
  if (!snap) return;
  let d = bdCache.get(snap.id);
  if (!d) { d = await api(`/api/builds/snapshot/${snap.id}`); bdCache.set(snap.id, d); }
  if (bdLevels[bdIndex]?.id !== snap.id) return; // moved on meanwhile
  $("bd-title").textContent = `${d.character} · level ${d.level}${d.class ? ` · ${d.class}` : ""}`;
  $("bd-meta").textContent = `${dateTimeOf(d.taken_at)}${d.league ? ` · ${d.league}` : ""}`;
  renderSameLevel(snap);
  const ch = d.ok ? d.changes : null;
  renderBuildChanges(ch);
  const changedSlots = new Set(ch?.slots || []), newGems = new Set(ch?.gems || []);
  $("bd-pob").hidden = !d.ok;
  $("bd-missed").hidden = d.ok;
  $("bd-missed").textContent = d.ok ? "" : `This level was missed: ${d.error || "the build couldn't be fetched"}.`;
  if (!d.ok) { $("bd-stats").innerHTML = ""; $("bd-gear").innerHTML = ""; return; }
  const p = d.passives;
  const gems = d.gear.reduce((n, g) => n + g.gems.flat().length, 0);
  $("bd-stats").innerHTML = [
    ["Passives", p.allocated], ["Masteries", p.masteries], ["Jewels", p.jewels],
    ["Gems", gems], ["Bandit", p.bandit || "None"],
  ].map(([k, v]) => `<div><span class="label">${k}</span><span>${esc(v)}</span></div>`).join("");
  $("bd-gear").innerHTML = d.gear.map((g) => `
    <div class="bd-item${changedSlots.has(g.key) ? " changed" : ""}">
      ${g.icon ? `<img src="${esc(g.icon)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'noicon'}))">` : '<span class="noicon"></span>'}
      <div>
        <div class="slot">${esc(g.slot)}${g.links ? ` · <span class="ilinks">${esc(g.links)}</span>` : ""}${changedSlots.has(g.key) ? '<span class="new-tag">changed</span>' : ""}</div>
        <div class="iname r-${esc(g.rarity)}" title="${esc(g.name || g.base)}">${esc(g.name || g.base)}</div>
        ${g.name ? `<div class="ibase">${esc(g.base)}</div>` : ""}
        ${g.gems.length ? `<div class="gem-groups">${g.gems.map((grp) => `<div class="gem-group">${grp.map((x) =>
          `<span class="gem${x.support ? " support" : ""}${newGems.has(x.name) ? " new" : ""}" title="${newGems.has(x.name) ? "New since the previous snapshot. " : ""}Level ${x.level ?? "?"}${x.quality ? `, ${x.quality}% quality` : ""}">${esc(x.name.replace(/ Support$/, ""))} ${x.level ?? ""}</span>`).join("")}</div>`).join("")}</div>` : ""}
      </div>
    </div>`).join("");
}

$("bd-character").addEventListener("change", (e) => { bdChar = e.target.value; loadBuildLevels(false).catch(() => {}); });
$("bd-slider").addEventListener("input", (e) => { bdIndex = Number(e.target.value); renderBuild().catch(() => {}); });
$("bd-prev").addEventListener("click", () => { if (bdIndex > 0) { bdIndex--; $("bd-slider").value = bdIndex; renderBuild().catch(() => {}); } });
$("bd-next").addEventListener("click", () => { if (bdIndex < bdLevels.length - 1) { bdIndex++; $("bd-slider").value = bdIndex; renderBuild().catch(() => {}); } });
$("bd-now").addEventListener("click", async () => {
  if (!bdChar) return;
  try {
    await api(`/api/builds/character/${encodeURIComponent(bdChar)}/snapshot`, { method: "POST" });
    $("bd-status").className = "hint";
    $("bd-status").textContent = `Fetching ${bdChar}'s build…`;
    setTimeout(() => loadBuilds().catch(() => {}), 5000);
  } catch (e) { alertModal("Couldn't take a snapshot", e.message); }
});
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const t = Object.assign(document.createElement("textarea"), { value: text });
    document.body.append(t);
    t.select();
    const ok = document.execCommand("copy");
    t.remove();
    return ok;
  }
}

$("bd-pob").addEventListener("click", async () => {
  const snap = bdLevels[bdIndex];
  if (!snap) return;
  const btn = $("bd-pob");
  btn.disabled = true;
  try {
    const { code } = await api(`/api/builds/snapshot/${snap.id}/pob`);
    if (await copyText(code)) {
      btn.textContent = "Copied ✓";
      setTimeout(() => { btn.textContent = "Copy PoB code"; }, 2000);
    } else {
      await modal({
        title: "Path of Building code",
        body: `Copy this code, then in Path of Building: <b>Import/Export Build</b> → paste it under "Import from code".<textarea class="modal-code" readonly onclick="this.select()">${esc(code)}</textarea>`,
        cancel: null,
      });
    }
  } catch (e) {
    alertModal("Couldn't make the code", e.message);
  } finally {
    btn.disabled = false;
  }
});

$("bd-delete").addEventListener("click", async () => {
  const snap = bdLevels[bdIndex];
  if (!snap || !await confirmModal({
    title: "Delete this snapshot?",
    body: `${esc(bdChar)}'s level ${snap.level} snapshot is deleted. This can't be undone.`,
    ok: "Delete snapshot", danger: true,
  })) return;
  await api(`/api/builds/snapshot/${snap.id}/delete`, { method: "POST" }).catch((e) => alertModal("Couldn't delete it", e.message));
  bdCache.delete(snap.id);
  await loadBuilds();
});
