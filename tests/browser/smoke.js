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

  await browser.close();
  if (problems.length) {
    console.error(`\n${problems.length} problem(s)`);
    process.exit(1);
  }
  console.log("\nall pages ok");
})().catch((e) => { console.error(e); process.exit(1); });
