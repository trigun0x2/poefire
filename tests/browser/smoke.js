// Browser smoke test: every page of the app on demo data (scripts/demo_data.py), failing on
// any JavaScript error, failed /api/ request or missing content. Run by CI (ui job):
//
//   node tests/browser/smoke.js http://127.0.0.1:8473
//
// Needs Playwright with Chromium.
const { chromium } = require("playwright");

const base = process.argv[2] || "http://127.0.0.1:8473";
const problems = [];
const fail = (msg) => { problems.push(msg); console.error(`FAIL ${msg}`); };
const check = (cond, msg) => { if (!cond) fail(msg); else console.log(`ok   ${msg}`); };

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  page.on("pageerror", (e) => fail(`page error: ${e.message}`));
  page.on("console", (m) => {
    // failed image loads (no network for item icons in CI) aren't app errors
    if (m.type() === "error" && !/Failed to load resource/.test(m.text())) fail(`console error: ${m.text()}`);
  });
  page.on("response", (r) => {
    const url = new URL(r.url());
    if (url.pathname.startsWith("/api/") && r.status() >= 500) fail(`${r.status()} from ${url.pathname}`);
  });
  const go = async (hash) => {
    await page.goto(`${base}/#${hash}`);
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(800);
  };
  const count = (sel) => page.$$eval(sel, (els) => els.length);
  const text = (sel) => page.textContent(sel);

  // Session
  await go("session");
  check(await count("#runs-body tr") > 5, "session: maps listed");
  check(!(await page.$eval("#last-body", (e) => e.hidden)), "session: last map shown");
  check(await count("#sessions-body tr") >= 3, "session: session history listed");
  check(await count("#maps-body tr") >= 2, "session: per-map stats");
  check(/\d/.test(await text("#tiles")) && (await text("#tiles")).includes("Loot / hour"), "session: loot per hour tile");
  check(/\d/.test(await text("#side-avg")), "sidebar: avg / map");
  await page.click("#runs-body tr:nth-child(2)");
  await page.waitForTimeout(500);

  // Overview
  await go("overview");
  check(await count("#ov-items-body tr") > 5, "overview: items listed");
  check(/\d/.test(await text("#ov-total")), "overview: total value");

  // Campaign
  await go("campaign");
  const visible = (sel) => page.$eval(sel, (e) => getComputedStyle(e).display !== "none" && !e.hidden);
  await page.click('.cp-tabs button[data-tab="history"]');
  check(await visible(".cp-history") && !(await visible(".cp-splits")), "campaign: History tab");
  check(await count("#cp-runs-body tr") >= 4, "campaign: runs listed");
  check(await count("#cp-top li") === 3, "campaign: top 3 runs");
  await page.click('#cp-runs-body tr:has-text("Tornado Shot league start")');
  await page.waitForTimeout(800);
  check(await visible(".cp-splits"), "campaign: picking a run opens the Run tab");
  check(await count("#cp-acts-body tr") === 10, "campaign: act splits");
  check(/\d/.test(await text("#cp-acts-body tr:first-child")), "campaign: split times");
  await page.click('.cp-tabs button[data-tab="analysis"]');
  await page.waitForTimeout(300);
  check(await count("#cp-worst > li") >= 1 && await visible("#cp-worst-card"), "campaign: Analysis tab: where you lost time");
  await page.click('.cp-tabs button[data-tab="run"]');

  // Builds
  await go("builds");
  check(await count("#bd-character option") >= 1, "builds: characters");
  check(await count("#bd-gear > *") >= 1, "builds: gear shown");

  // Settings: opens clean, a change is flagged, discarding clears it
  await go("settings");
  check(!(await page.$eval("#save-bar", (e) => e.classList.contains("dirty"))), "settings: no unsaved changes on open");
  await page.click("#set-hotkey-sound");
  await page.waitForTimeout(200);
  check(await page.$eval("#save-bar", (e) => e.classList.contains("dirty")), "settings: a change is flagged");
  await page.click("#set-discard");
  await page.waitForTimeout(500);
  check(!(await page.$eval("#save-bar", (e) => e.classList.contains("dirty"))), "settings: discard clears it");

  // Setup wizard (from Settings), with the account check answered by a fake account
  await page.route("**/api/settings/session", (r) => r.fulfill({ json: { warnings: [] } }));
  await page.route("**/api/settings/check-account", (r) => r.fulfill({ json: {
    ok: true,
    characters: [{ name: "Fresh", league: "Keepers", level: 14, class: "Witch", current: true },
                 { name: "Old", league: "Standard", level: 92, class: "Ranger", current: false }],
    tabs: [{ id: "a", name: "Currency", type: "CurrencyStash" }, { id: "b", name: "Trade", type: "PremiumStash" },
           { id: "c", name: "Dump", type: "QuadStash" }],
  } }));
  await go("settings");
  await page.click("#set-tabs-refresh");
  await page.waitForTimeout(600);
  check(await count("#set-tabs label") >= 3, "settings: Refresh tabs loads the stash tabs");
  await page.click("#set-wizard");
  await page.waitForTimeout(500);
  check(await page.$eval("#wizard", (d) => d.open), "wizard: opens from Settings");
  await page.click("#wz-next"); // game
  await page.waitForTimeout(400);
  await page.fill("#wz-account", "Exile#1234");
  await page.fill("#wz-cookie", "0123456789abcdef0123456789abcdef");
  await page.click("#wz-next"); // account
  await page.waitForTimeout(600);
  check(await count("#wz-leagues .wz-choice") === 2, "wizard: leagues from the characters");
  check(await page.$eval('input[name="wz-league"]:checked', (i) => i.value) === "Keepers", "wizard: league being played is picked");
  await page.click("#wz-next"); // league
  await page.waitForTimeout(600);
  const ticked = await page.$$eval("#wz-tabs input:checked", (els) => els.map((e) => e.value));
  check(ticked.includes("Currency") && ticked.includes("Dump") && !ticked.includes("Trade"), "wizard: loot tabs suggested");
  await page.click("#wz-next"); // tabs: save
  await page.waitForTimeout(800);
  check(/Keepers/.test(await text("#wz-summary")), "wizard: summary");
  await page.click("#wz-next"); // done
  await page.waitForTimeout(500);
  check(!(await page.$eval("#wizard", (d) => d.open)), "wizard: closes");

  // Price-check popup, with the price check and the trade search answered by fake data
  const check0 = {
    id: 7, kind: "trade", league: "Keepers", chaos_per_divine: 180, result: null, error: null,
    item: { name: "Storm Loop Two-Stone Ring", class: "Rings", rarity: "Rare", stack: 1, corrupted: false, links: 0 },
    check: { name: null, base: "Two-Stone Ring", rarity: "Rare", unmatched: ["Grants Level 20 Mystery Skill"], filters: [
      { key: "corrupted", label: "Not corrupted", group: "item", enabled: true, value: 0, min: null, max: null, stat_id: null },
      { key: "stat:implicit.stat_fc", label: "+12% to Fire and Cold Resistances", group: "implicit", enabled: false, value: 12, min: 10, max: null, stat_id: "implicit.stat_fc" },
      { key: "stat:explicit.stat_life", label: "+45 to maximum Life", group: "explicit", enabled: true, value: 45, min: 40, max: null, stat_id: "explicit.stat_life" },
      { key: "stat:explicit.stat_light", label: "+40% to Lightning Resistance", group: "explicit", enabled: true, value: 40, min: 36, max: null, stat_id: "explicit.stat_light" },
    ] },
  };
  let searchedWith = null;
  const listing = (c) => ({ amount: c, currency: "Chaos Orb", chaos: c, indexed: new Date().toISOString() });
  await page.route("**/api/pricecheck/current", (r) => r.fulfill({ json: { check: check0 } }));
  await page.route("**/api/pricecheck/search", (r) => {
    searchedWith = JSON.parse(r.request().postData());
    r.fulfill({ json: { check: { ...check0, result: {
      total: 34, averaged: 20, average_chaos: 412.5, url: "https://www.pathofexile.com/trade/search/Keepers/Q1",
      cheapest: [12, 14, 15, 20, 31].map(listing) } } } });
  });
  await page.goto(`${base}/price-check`);
  await page.waitForTimeout(1200);
  check(await count(".pc-filter") === 4, "price check: the item's mods as filters");
  check(searchedWith && searchedWith.id === 7, "price check: searches when it opens");
  check(await count("#pc-rows tr") === 5, "price check: the 5 cheapest listings");
  check(/2\.29d/.test(await text("#pc-avg")), "price check: the average (in divines from 200c)");
  check(/Mystery Skill/.test(await text("#pc-unmatched")), "price check: unsearchable mods listed");
  await page.click('.pc-filter[data-key="stat:explicit.stat_light"] input[type=checkbox]');
  await page.fill('.pc-filter[data-key="stat:explicit.stat_life"] input[data-bound="min"]', "50");
  await page.click("#pc-search");
  await page.waitForTimeout(600);
  const sent = Object.fromEntries((searchedWith?.filters || []).map((f) => [f.key, f]));
  check(sent["stat:explicit.stat_light"]?.enabled === false && sent["stat:explicit.stat_life"]?.min === 50,
    "price check: ticks and min values go into the search");

  await browser.close();
  if (problems.length) {
    console.error(`\n${problems.length} problem(s)`);
    process.exit(1);
  }
  console.log("\nall pages ok");
})().catch((e) => { console.error(e); process.exit(1); });
