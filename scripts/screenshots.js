// README screenshots of the app running on demo data (scripts/demo_data.py).
//
//   node scripts/screenshots.js http://127.0.0.1:8473 docs/images
//
// Needs Playwright with Chromium, and internet for the item icons (the game's own, from its
// CDN). The Windows-only bits (hotkeys) are shown as they look in the Windows app.
const { chromium } = require("playwright");
const path = require("path");

const [base, outDir] = process.argv.slice(2);
async function fixWindowsOnly(route) {
  const res = await route.fetch();
  const data = await res.json();
  const ok = (h) => h && Object.assign(h, { active: true, error: null });
  ok(data.hotkey);
  if (data.overlay) { data.overlay.available = true; ok(data.overlay.toggle); ok(data.overlay.move); }
  await route.fulfill({ response: res, json: data });
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 });
  await page.route("**/api/status", fixWindowsOnly);
  await page.route("**/api/campaign", fixWindowsOnly);
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));

  async function shot(hash, name, prepare) {
    await page.goto(`${base}/#${hash}`);
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(900);
    if (prepare) await prepare();
    await page.waitForTimeout(600);
    await page.screenshot({ path: path.join(outDir, `${name}.png`) });
  }

  await shot("session", "session");
  await shot("overview", "overview");
  await shot("campaign", "campaign", async () => {
    await page.click('#cp-runs-body tr:has-text("Tornado Shot league start")');
  });
  await page.setViewportSize({ width: 1400, height: 600 });
  await shot("builds", "builds", async () => {
    const last = await page.$eval("#bd-slider", (s) => s.max);
    await page.$eval("#bd-slider", (s, v) => { s.value = v; s.dispatchEvent(new Event("input")); }, last);
  });
  console.log(errors.length ? `page errors: ${errors.join("; ")}` : "ok");
  await browser.close();
})();
