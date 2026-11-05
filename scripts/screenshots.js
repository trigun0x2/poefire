// README screenshots of the app running on demo data (scripts/demo_data.py).
//
//   node scripts/screenshots.js http://127.0.0.1:8473 docs/images
//
// Needs Playwright with Chromium. Item icons are drawn as simple coloured gems (the demo
// data points them at web.poecdn.com/demo/...), and the Windows-only bits (hotkeys) are
// shown as they look in the Windows app.
const { chromium } = require("playwright");
const path = require("path");

const [base, outDir] = process.argv.slice(2);
const COLORS = {
  divine: "#e9d9a8", exalted: "#d7c49a", chaos: "#c8a24a", annul: "#cfd7e6", vaal: "#b33a3a",
  alch: "#d9a441", fusing: "#7f9cc8", gcp: "#9fc7d8", scarab: "#c98b45", essence: "#8e5bc4",
  card: "#6aa0d8", splinter: "#7fb7c9",
};

function gemSvg(color) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="47" height="47" viewBox="0 0 47 47">
    <defs><radialGradient id="g" cx="35%" cy="30%" r="70%"><stop offset="0" stop-color="#fff" stop-opacity=".85"/>
    <stop offset=".35" stop-color="${color}"/><stop offset="1" stop-color="#1b1b1b"/></radialGradient></defs>
    <circle cx="23.5" cy="23.5" r="17" fill="url(#g)" stroke="#000" stroke-opacity=".5"/></svg>`;
}

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
  await page.route("https://web.poecdn.com/demo/*", (route) => {
    const slug = path.basename(new URL(route.request().url()).pathname, ".png");
    route.fulfill({ contentType: "image/svg+xml", body: gemSvg(COLORS[slug] || "#999") });
  });
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
