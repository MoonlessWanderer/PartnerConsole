"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { setTimeout: sleep } = require("node:timers/promises");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE);

const [executablePath, work] = process.argv.slice(2);
const origin = "http://127.0.0.1:3741";
const api = "http://127.0.0.1:8741";
const panel = "http://127.0.0.1:18788";
const password = crypto.randomBytes(24).toString("base64url");
const route = "/api/partner/subscriptions";
const purchasePage = "/partner/subscriptions/new";
const button = (page, name) => page.getByRole("button", { name, exact: true });

async function json(base, endpoint, { body, token, method = body === undefined ? "GET" : "POST" } = {}) {
  const res = await fetch(base + endpoint, {
    method,
    headers: { ...(token && { Authorization: `Bearer ${token}` }), ...(body && { "Content-Type": "application/json" }) },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(10000),
  });
  assert.equal(res.status, 200, `${endpoint} returned ${res.status}`);
  return res.json();
}

async function reviewPurchase(page, username, productId) {
  await page.goto(origin + purchasePage);
  const product = page.getByLabel("محصول", { exact: true });
  await product.locator("option[value='" + productId + "']").waitFor({ state: "attached" });
  await product.selectOption(String(productId));
  await page.getByRole("textbox", { name: /^نام کاربری(?: \(a-z، عدد، _\))?$/u }).fill(username);
  await page.getByLabel("حجم (گیگ)", { exact: true }).fill("2");
  return review(page, page, /^\+?۳۱ روز$/u);
}

async function review(page, scope, label) {
  const response = await post(page, route + "/quote", () => button(scope, label).click());
  assert.equal(response.status(), 200);
  const quote = await response.json();
  const amount = page.getByText(/^مبلغ:/).or(
    page.locator("dt").filter({ hasText: /^برداشت از کیف پول$/ }).locator("xpath=../dd"),
  );
  await amount.waitFor();
  const shown = (await amount.innerText()).trim().replace(/^مبلغ:\s*/u, "").replace(/[,٬]/gu, "").replace(/\s+/gu, " ");
  const expected = new Intl.NumberFormat("fa-IR", { useGrouping: false }).format(quote.toman);
  assert.equal(shown, expected + " تومان", "visible amount differs from the quote");
  return quote;
}

async function post(page, endpoint, action) {
  const [response] = await Promise.all([
    page.waitForResponse(res => res.url() === api + endpoint && res.request().method() === "POST"),
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
  const { product } = await admin("products", {
    panel_id: fixturePanel.id, plan_id: 7, name: "Upgrade plan", email_domain: "upgrade.example.test",
    price_per_gb: 1000, price_per_day: 100,
  });
  const username = "fixture_upgrade_a";
  const account = await admin("partners", { username, password });
  await admin("partners/" + account.id + "/adjust", { asset: "toman", amount: 100000 });

  const browser = await chromium.launch({ executablePath, headless: true });
  try {
    const context = await browser.newContext();
    await context.route("**/*", route => [origin, api].includes(new URL(route.request().url()).origin) ? route.continue() : route.abort());
    const purchase = await context.newPage();
    const assets = new Set();
    purchase.on("response", response => {
      if (response.ok() && response.url().startsWith(origin + "/_next/static/")) assets.add(response.request().resourceType());
    });
    await purchase.goto(origin);
    const form = purchase.locator("form");
    await purchase.waitForFunction(() => Object.keys(document.querySelector("form") || {}).some(key => key.startsWith("__react")));
    await form.locator("input").nth(0).fill(username);
    await form.locator("input").nth(1).fill(password);
    assert.equal((await post(purchase, "/api/partner/auth/login", () => button(form, "ورود").click())).status(), 200);
    await purchase.waitForURL(origin + "/partner");
    const token = await purchase.evaluate(() => localStorage.getItem("pp_token"));
    const mine = (endpoint) => json(api, "/api/partner/" + endpoint, { token });
    const snapshot = async () => {
      const counters = await json(panel, "/_lab/consent-writes");
      return {
        wallet: (await mine("me")).balance_toman,
        ledger: (await mine("ledger")).items.map(row => [row.id, row.amount, row.asset, row.reason, row.subscription_id]),
        subscriptions: (await mine("subscriptions")).items.map(row => [row.id, row.username]),
        operations: (await admin("operations?limit=100")).items.map(row => [row.id, row.status]),
        writes: counters.count,
        generates: counters.generate_count,
        panelUsers: (await json(panel, "/_lab/users")).total,
      };
    };
    const submit = () => post(purchase, route, () => button(purchase, "ثبت اشتراک").click());
    await reviewPurchase(purchase, "fixture_retained_one", product.id);
    const retained = await submit();
    assert.equal(retained.status(), 200);
    const sub = await retained.json();
    await purchase.waitForURL(origin + "/partner/subscriptions/" + sub.id);
    await json(panel, `/_lab/users/${sub.v2_user_id}/traffic`, { body: { u: 1073741824, d: 0 } });
    const reset = await context.newPage();
    await reset.goto(origin + "/partner/subscriptions/" + sub.id);
    const dialog = reset.getByRole("dialog");
    const resetRoute = route + `/${sub.id}/reset-traffic`;
    const submitReset = () => post(reset, resetRoute, () => button(dialog, "تأیید").click());
    const oldReset = await review(reset, reset, "ریست ترافیک");
    const oldPurchase = await reviewPurchase(purchase, "fixture_after_update", product.id);
    assert.ok(oldReset.consent_token && oldPurchase.consent_token);
    assert.equal(oldPurchase.toman, 5100);
    assert.equal(oldReset.toman, 1000);
    const beforeUpgrade = await snapshot();

    fs.writeFileSync(path.join(work, "ready"), "");
    for (let attempt = 0; !fs.existsSync(path.join(work, "updated")); attempt++) {
      assert.ok(attempt < 1200, "installer Update did not finish");
      await sleep(1000);
    }
    assert.ok((await adminLogin()).access_token, "installed administrator credentials did not survive Update");
    assert.deepEqual(await snapshot(), beforeUpgrade, "upgrade changed the browser account's financial state");
    // Change the fixture rate so the quotes signed by v1.0.9 are stale.
    await json(api, "/api/admin/products/" + product.id, {
      token: adminToken, method: "PUT",
      body: { name: product.name, email_domain: product.email_domain, price_per_gb: 1100, price_per_day: 100, active: true },
    });
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
    const newPurchase = await reviewPurchase(purchase, "fixture_after_update", product.id);
    assert.ok(newPurchase.consent_token);
    assert.equal(newPurchase.toman, 5300);
    const purchaseResponse = await submit();
    assert.equal(purchaseResponse.status(), 200);
    const created = await purchaseResponse.json();
    await purchase.waitForURL(origin + "/partner/subscriptions/" + created.id);
    const purchased = await snapshot();
    const oldEntries = new Set(before.ledger.map(([id]) => id));
    const added = purchased.ledger.filter(([id]) => !oldEntries.has(id));
    assert.deepEqual(added.map(([, amount, asset, reason, subId]) => [Number(amount), asset, reason, subId]),
                     [[-newPurchase.toman, "toman", "create", created.id]]);
    assert.equal(purchased.generates, before.generates + 1);
    assert.equal(purchased.panelUsers, before.panelUsers + 1);
    const { data: upstream } = await json(panel, "/_lab/users/" + created.v2_user_id);
    assert.equal(upstream.email, "fixture_after_update@upgrade.example.test");
    assert.equal(upstream.plan_id, 7);
    assert.equal(upstream.transfer_enable, 2 * 1073741824);
    assert.equal(upstream.expired_at, created.expired_at);
    console.log("Browser purchase after upgrade: pre-approved 5300 toman, one ledger withdrawal, one matching subscription.");
    assert.equal(before.wallet - purchased.wallet, newPurchase.toman);
    assert.equal(purchased.ledger.length, before.ledger.length + 1);
    assert.equal(purchased.subscriptions.length, before.subscriptions.length + 1);
    assert.ok(purchased.subscriptions.some(([id, name]) => id === created.id && name === "fixture_after_update"));
    assert.ok(assets.has("script") && assets.has("stylesheet"), "production JavaScript or CSS missing");

    await reset.reload();
    await button(reset.getByRole("navigation", { name: "بخش‌های اشتراک" }), "مدیریت").click();
    const newReset = await review(reset, reset, "ریست ترافیک");
    assert.ok(newReset.consent_token);
    assert.equal(newReset.toman, 1100);
    const newResetResponse = await post(reset, resetRoute, () => button(reset, "تأیید ریست ترافیک").click());
    assert.equal(newResetResponse.status(), 200);
    const settled = await snapshot();
    assert.equal(purchased.wallet - settled.wallet, newReset.toman);
    assert.equal(settled.ledger.length, purchased.ledger.length + 1);
    assert.equal(settled.writes, purchased.writes + 1);
    console.log("Refreshed Chromium tabs loaded the new bundles and completed signed purchase and reset at the quoted amounts.");
    fs.writeFileSync(path.join(work, "complete"), "");
  } finally {
    await browser.close();
  }
}

verify().catch(error => {
  console.error(error.stack);
  process.exit(1);
});
