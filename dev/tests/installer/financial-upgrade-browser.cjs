"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const https = require("node:https");
const path = require("node:path");
const { setTimeout: sleep } = require("node:timers/promises");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE);

const [executablePath, work, origin] = process.argv.slice(2);
const api = "http://127.0.0.1:8741";
const panel = "http://127.0.0.1:18788";
const password = crypto.randomBytes(24).toString("base64url");
const route = "/api/partner/subscriptions";
const purchasePage = "/partner/subscriptions/new";
const button = (page, name) => page.getByRole("button", { name, exact: true });

async function json(base, endpoint, { body, token } = {}) {
  const res = await fetch(base + endpoint, {
    method: body === undefined ? "GET" : "POST",
    headers: { ...(token && { Authorization: `Bearer ${token}` }), ...(body && { "Content-Type": "application/json" }) },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(10000),
  });
  assert.equal(res.status, 200, `${endpoint} returned ${res.status}`);
  return res.json();
}

async function reviewPurchase(page, username) {
  await page.goto(origin + purchasePage);
  await page.getByLabel("محصول", { exact: true }).locator("option").first().waitFor({ state: "attached" });
  await page.getByLabel("نام کاربری (a-z، عدد، _)", { exact: true }).fill(username);
  await page.getByLabel("حجم (گیگ)", { exact: true }).fill("2");
  return review(page, page, "+۳۱ روز");
}

async function review(page, scope, label) {
  const response = await post(page, route + "/quote", () => button(scope, label).click());
  assert.equal(response.status(), 200);
  await page.getByText(/^مبلغ:/).waitFor();
  return response.json();
}

async function post(page, endpoint, action) {
  const [response] = await Promise.all([
    page.waitForResponse(res => res.url() === origin + endpoint && res.request().method() === "POST"),
    action(),
  ]);
  return response;
}

async function verify() {
  const adminLogin = () => json(api, "/api/admin/auth/login", {
    body: { username: process.env.PC_FIXTURE_ADMIN, password: process.env.PC_FIXTURE_PASSWORD },
  });
  const { access_token: adminToken } = await adminLogin();
  const admin = (endpoint, body) => json(api, "/api/admin/" + endpoint, { token: adminToken, body });
  for (let attempt = 0; ; attempt++) {
    try { await json(panel, "/_lab/health"); break; }
    catch (error) { if (attempt === 40) throw error; await sleep(250); }
  }
  const fixturePanel = await admin("panels", {
    name: "Upgrade fixture", base_url: panel, secure_path: "labsecure", admin_email: "admin@panel.test", admin_password: "secret",
  });
  await admin("products", {
    panel_id: fixturePanel.id, plan_id: 7, name: "Upgrade plan", email_domain: "upgrade.example.test",
    price_per_gb: 1000, price_per_day: 100,
  });
  for (const username of ["fixture_upgrade_a", "fixture_upgrade_b"]) {
    const account = await admin("partners", { username, password });
    await admin(`partners/${account.id}/adjust`, { asset: "toman", amount: 100000 });
  }

  const server = https.createServer({ key: fs.readFileSync(path.join(work, "key.pem")), cert: fs.readFileSync(path.join(work, "cert.pem")) }, (req, res) => {
    const port = req.url.startsWith("/api/") ? 8741 : 3741;
    const upstream = http.request({ hostname: "127.0.0.1", port, path: req.url, method: req.method, headers: req.headers }, response => {
      res.writeHead(response.statusCode, response.headers);
      response.pipe(res);
    });
    upstream.on("error", () => { res.writeHead(502); res.end(); });
    req.pipe(upstream);
  });
  await new Promise(resolve => server.listen(18443, "127.0.0.1", resolve));
  const browser = await chromium.launch({ executablePath, headless: true });
  try {
    const context = await browser.newContext({ ignoreHTTPSErrors: true });
    await context.route("**/*", route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    const purchase = await context.newPage();
    const assets = new Set();
    purchase.on("response", response => {
      if (response.ok() && response.url().startsWith(origin + "/_next/static/")) assets.add(response.request().resourceType());
    });
    await purchase.goto(origin);
    const form = purchase.locator("form");
    await purchase.waitForFunction(() => Object.keys(document.querySelector("form") || {}).some(key => key.startsWith("__react")));
    await form.locator("input").nth(0).fill("fixture_upgrade_a");
    await form.locator("input").nth(1).fill(password);
    assert.equal((await post(purchase, "/api/partner/auth/login", () => button(form, "ورود").click())).status(), 200);
    await purchase.waitForURL(origin + "/partner");
    const token = await purchase.evaluate(() => localStorage.getItem("pp_token"));
    const mine = (endpoint) => json(api, "/api/partner/" + endpoint, { token });
    const snapshot = async () => ({
      wallet: (await mine("me")).balance_toman,
      ledger: (await mine("ledger")).items.map(row => [row.id, row.amount, row.asset, row.reason]),
      subscriptions: (await mine("subscriptions")).items.map(row => [row.id, row.username]),
      operations: (await admin("operations?limit=100")).items.map(row => [row.id, row.status]),
      writes: (await json(panel, "/_lab/consent-writes")).count,
    });
    const submit = () => post(purchase, route, () => button(purchase, "ثبت اشتراک").click());
    let sub;
    for (const username of ["fixture_retained_one", "fixture_retained_two"]) {
      await reviewPurchase(purchase, username);
      const result = await submit();
      assert.equal(result.status(), 200);
      sub = await result.json();
      await purchase.waitForURL(origin + "/partner/subscriptions/" + sub.id);
    }
    await json(panel, `/_lab/users/${sub.v2_user_id}/traffic`, { body: { u: 1073741824, d: 0 } });
    const reset = await context.newPage();
    await reset.goto(origin + "/partner/subscriptions/" + sub.id);
    const dialog = reset.getByRole("dialog");
    const resetRoute = route + `/${sub.id}/reset-traffic`;
    const submitReset = () => post(reset, resetRoute, () => button(dialog, "تأیید").click());
    const oldReset = await review(reset, reset, "ریست ترافیک");
    const oldPurchase = await reviewPurchase(purchase, "fixture_after_update");
    assert.ok(!oldReset.consent_token && !oldPurchase.consent_token);

    fs.writeFileSync(path.join(work, "ready"), "");
    for (let attempt = 0; !fs.existsSync(path.join(work, "updated")); attempt++) {
      assert.ok(attempt < 1200, "installer Update did not finish");
      await sleep(1000);
    }
    assert.ok((await adminLogin()).access_token, "installed administrator credentials did not survive Update");
    const before = await snapshot();
    for (const submitOld of [submit, submitReset]) {
      const response = await submitOld();
      assert.equal(response.status(), 409);
      assert.equal((await response.json()).code, "financial_consent");
      assert.deepEqual(await snapshot(), before, "a rejected old tab changed financial state");
    }
    console.log("Old Chromium tabs rejected for purchase and reset with no financial or upstream changes.");

    assets.clear();
    await purchase.reload();
    const newPurchase = await reviewPurchase(purchase, "fixture_after_update");
    assert.ok(newPurchase.consent_token);
    assert.equal((await submit()).status(), 200);
    const purchased = await snapshot();
    assert.equal(before.wallet - purchased.wallet, newPurchase.toman);
    assert.equal(purchased.ledger.length, before.ledger.length + 1);
    assert.equal(purchased.subscriptions.length, before.subscriptions.length + 1);
    assert.ok(purchased.subscriptions.some(([, username]) => username === "fixture_after_update"));
    assert.ok(assets.has("script") && assets.has("stylesheet"), "production JavaScript or CSS missing");

    await reset.reload();
    const newReset = await review(reset, reset, "ریست ترافیک");
    assert.ok(newReset.consent_token);
    assert.equal(newReset.toman, 1000);
    assert.equal((await submitReset()).status(), 200);
    const settled = await snapshot();
    assert.equal(purchased.wallet - settled.wallet, newReset.toman);
    assert.equal(settled.ledger.length, purchased.ledger.length + 1);
    assert.equal(settled.writes, purchased.writes + 1);
    console.log("Refreshed Chromium tabs loaded the new bundles and completed signed purchase and reset at the quoted amounts.");
    fs.writeFileSync(path.join(work, "complete"), "");
  } finally {
    await browser.close();
    server.close();
  }
}

verify().catch(error => {
  console.error(error.stack);
  process.exit(1);
});
