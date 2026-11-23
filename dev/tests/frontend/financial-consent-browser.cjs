const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE);

const [version, artifacts] = process.argv.slice(2);
assert.ok(["baseline", "hotfix"].includes(version));
const baseline = version === "baseline";
const web = "http://127.0.0.1:13741", api = "http://127.0.0.1:18741", lab = "http://127.0.0.1:18788";
const traces = [], checks = [];
const password = crypto.randomBytes(24).toString("base64url");
const signal = AbortSignal.timeout(110000);
const button = (page, name) => page.getByRole("button", { name, exact: true });
const field = (page, name) => page.getByLabel(name, { exact: true });

async function request(origin, route, body, token, method) {
  const response = await fetch(origin + route, {
    method: method || (body === undefined ? "GET" : "POST"),
    headers: { ...(token ? { Authorization: "Bearer " + token } : {}), ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  const data = await response.json().catch(() => null);
  traces.push({ method: method || (body === undefined ? "GET" : "POST"), route: route.split("?")[0], status: response.status });
  return { status: response.status, data };
}

function ok(result) {
  assert.equal(result.status, 200, "unexpected fixture HTTP status");
  return result.data;
}

async function ready(origin, route) {
  for (let attempt = 0; attempt < 100; attempt++) {
    try {
      if ((await fetch(origin + route, { signal: AbortSignal.timeout(700) })).ok) return;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 400));
  }
  throw new Error("fixture service not ready at " + origin);
}

function response(page, route) {
  return page.waitForResponse(res => res.url() === api + route && res.request().method() === "POST");
}

async function screen(page, file) {
  await page.screenshot({ path: path.join(artifacts, file), mask: [page.getByText(/token=/)] });
}

async function login(page, username) {
  await page.goto(web);
  const form = page.locator("form");
  await page.waitForFunction(() => {
    const form = document.querySelector("form");
    return form && Object.keys(form).some(key => key.startsWith("__react"));
  });
  await form.locator("input").nth(0).fill(username);
  await form.locator("input").nth(1).fill(password);
  const pending = response(page, "/api/partner/auth/login");
  await button(form, "ورود").click();
  assert.equal((await pending).status(), 200);
  await page.waitForURL(web + "/partner");
}

async function purchase(page, username) {
  await page.goto(web + "/partner/subscriptions/new");
  await field(page, "محصول").locator("option").first().waitFor({ state: "attached" });
  await field(page, "نام کاربری (a-z، عدد، _)").fill(username);
  await field(page, "حجم (گیگ)").fill("2");
  const pending = response(page, "/api/partner/subscriptions/quote");
  await button(page, "+۳۱ روز").click();
  const quote = await (await pending).json();
  await page.getByText(/^مبلغ:/).waitFor();
  return quote;
}

async function run() {
  await Promise.all([ready(web, "/"), ready(api, "/api/health"), ready(lab, "/_lab/health")]);
  const partner = (token, route, body) => request(api, "/api/partner/" + route, body, token);
  const adminToken = ok(await request(api, "/api/admin/auth/login", {
    username: process.env.ADMIN_USERNAME, password: process.env.ADMIN_PASSWORD,
  })).access_token;
  const admin = (route, body, method) => request(api, "/api/admin/" + route, body, adminToken, method);
  const panel = ok(await admin("panels", {
    name: "Fixture panel", base_url: lab, secure_path: "labsecure", admin_email: "admin@panel.test", admin_password: "secret",
  }));
  const productInput = { panel_id: panel.id, plan_id: 7, name: "Fixture plan", email_domain: "fixture.example.test", price_per_gb: 1000, price_per_day: 100 };
  const { product } = ok(await admin("products", productInput));
  async function account(username) {
    const item = ok(await admin("partners", { username, password }));
    ok(await admin("partners/" + item.id + "/adjust", { asset: "toman", amount: 100000 }));
    item.token = ok(await partner(null, "auth/login", { username, password })).access_token;
    return item;
  }
  const first = await account("fixture_partner_a"), second = await account("fixture_partner_b");
  async function snapshot() {
    const take = async (item) => ({
      wallet: ok(await partner(item.token, "me")).balance_toman,
      credits: ok(await partner(item.token, "products")).items.map(row => [row.id, row.credit_gb, row.credit_days]),
      ledger: ok(await partner(item.token, "ledger")).items.map(row => [row.id, row.amount, row.asset, row.reason]),
      subscriptions: ok(await partner(item.token, "subscriptions")).items.map(row => [row.id, row.username, row.traffic_cap_gb, row.expired_at]),
    });
    return {
      first: await take(first), second: await take(second),
      operations: ok(await admin("operations?limit=100")).items.map(row => [row.id, row.status]),
      writes: ok(await request(lab, "/_lab/consent-writes")).count,
    };
  }

  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || "/usr/bin/google-chrome", headless: true });
  try {
    const shared = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await shared.route("**/*", route => [api, web].includes(new URL(route.request().url()).origin) ? route.continue() : route.abort());
    const page = await shared.newPage();
    await login(page, first.username);
    await purchase(page, "fixture_reset");
    const created = response(page, "/api/partner/subscriptions");
    await button(page, "ثبت اشتراک").click();
    const sub = await (await created).json();
    assert.ok(sub.id && sub.v2_user_id);
    await page.waitForURL(web + "/partner/subscriptions/" + sub.id);
    const traffic = (gb) => request(lab, "/_lab/users/" + sub.v2_user_id + "/traffic", { u: gb * 1073741824, d: 0 }).then(ok);
    await traffic(1);
    const quotePending = response(page, "/api/partner/subscriptions/quote");
    await button(page, "ریست ترافیک").click();
    const quote = await (await quotePending).json();
    assert.equal(quote.toman, 1000);
    const dialog = page.getByRole("dialog");
    await dialog.getByText(/^مبلغ:/).waitFor();
    await screen(page, "reset-1000-before.png");
    await traffic(3);
    const before = await snapshot();
    const mutation = "/api/partner/subscriptions/" + sub.id + "/reset-traffic";
    const resetPending = response(page, mutation);
    await button(dialog, "تأیید").click();
    const reset = await resetPending;
    const after = await snapshot();
    checks.push({ finding: "B-01", route: mutation, approved: 1000, status: reset.status(), before, after });
    if (baseline) {
      assert.equal(reset.status(), 200);
      assert.equal(before.first.wallet - after.first.wallet, 3000);
      assert.equal(after.writes - before.writes, 1);
    } else {
      assert.equal(reset.status(), 409);
      assert.deepEqual(after, before);
      await dialog.getByText(/شرایط یا نشست تغییر کرده/).waitFor();
      await screen(page, "reset-stale-rejected.png");
      const freshPending = response(page, mutation);
      await button(dialog, "تأیید").click();
      assert.equal((await freshPending).status(), 200);
      const settled = await snapshot();
      assert.equal(before.first.wallet - settled.first.wallet, 3000);
      assert.equal(settled.writes - before.writes, 1);
      assert.equal(settled.first.ledger.length - before.first.ledger.length, 1);
    }

    const stale = await purchase(page, "fixture_switch");
    await screen(page, "account-a-review.png");
    const end = stale.end_jalali;
    const secondTab = await shared.newPage();
    await secondTab.goto(web + "/partner");
    const logout = response(secondTab, "/api/partner/auth/logout");
    await button(secondTab, "خروج").click();
    assert.equal((await logout).status(), 200);
    await secondTab.waitForURL(web + "/");
    await login(secondTab, second.username);
    const beforeSwitch = await snapshot();
    let rejected;
    if (baseline) {
      rejected = response(page, "/api/partner/subscriptions");
      await button(page, "ثبت اشتراک").click();
      assert.equal((await rejected).status(), 200);
    } else {
      await page.waitForFunction(() => !Array.from(document.querySelectorAll("input")).some(input => input.value === "fixture_switch"));
      const oldReview = { product_id: product.id, username: "fixture_switch", traffic_cap_gb: 2,
        end_date: end, consent_token: stale.consent_token };
      for (const account of [second, first]) {
        const blocked = await partner(account.token, "subscriptions", oldReview);
        assert.equal(blocked.status, 409);
      }
      await screen(page, "account-a-draft-discarded.png");
    }
    const afterSwitch = await snapshot();
    checks.push({ finding: "B-02", route: "/api/partner/subscriptions", approved: stale.toman, before: beforeSwitch, after: afterSwitch });
    if (baseline) {
      assert.equal(beforeSwitch.second.wallet - afterSwitch.second.wallet, stale.toman);
      assert.ok(afterSwitch.second.subscriptions.some(row => row[1] === "fixture_switch"));
    } else {
      assert.deepEqual(afterSwitch, beforeSwitch);
      await purchase(secondTab, "fixture_refresh");
      await secondTab.reload();
      assert.equal(await field(secondTab, "نام کاربری (a-z، عدد، _)").inputValue(), "");
      assert.equal(await button(secondTab, "ثبت اشتراک").isEnabled(), false);
      const fresh = await purchase(secondTab, "fixture_switch");
      const newPending = response(secondTab, "/api/partner/subscriptions");
      await button(secondTab, "ثبت اشتراک").dblclick();
      assert.equal((await newPending).status(), 200);
      const final = await snapshot();
      assert.equal(beforeSwitch.second.wallet - final.second.wallet, fresh.toman);
      assert.equal(final.second.subscriptions.filter(row => row[1] === "fixture_switch").length, 1);
      assert.equal(final.second.ledger.length - beforeSwitch.second.ledger.length, 1);
      assert.equal(final.operations.length - beforeSwitch.operations.length, 1);

      const session = await secondTab.evaluate(() => localStorage.getItem("pp_token"));
      const lost = { product_id: product.id, username: "fixture_lost_reply", traffic_cap_gb: 2, end_date: end };
      const proof = ok(await partner(session, "subscriptions/quote", { ...lost, action: "create" }));
      const confirmation = { ...lost, consent_token: proof.consent_token };
      ok(await request(lab, "/_lab/consent-drop-next-generate", {}));
      const simultaneous = await Promise.all([
        partner(session, "subscriptions", confirmation),
        partner(session, "subscriptions", confirmation),
      ]);
      assert.ok(simultaneous.every(result => [200, 503].includes(result.status)));
      const settled = ok(await partner(session, "subscriptions", confirmation));
      const once = await snapshot();
      assert.equal(ok(await request(lab, "/_lab/consent-lost-replies")).count, 1);
      assert.equal(final.second.wallet - once.second.wallet, proof.toman);
      assert.equal(once.operations.length - final.operations.length, 1);
      assert.equal(once.second.ledger.length - final.second.ledger.length, 1);
      assert.equal(once.second.subscriptions.filter(row => row[0] === settled.id).length, 1);
      checks.push({ finding: "MySQL lost response and competing confirms",
        statuses: simultaneous.map(result => result.status), settlement: proof.toman, before: final, after: once });

      const exitSecond = response(secondTab, "/api/partner/auth/logout");
      await button(secondTab, "خروج").click();
      assert.equal((await exitSecond).status(), 200);
      await secondTab.waitForURL(web + "/");
      await login(secondTab, first.username);
      const newFirstSession = await secondTab.evaluate(() => localStorage.getItem("pp_token"));
      const beforeReturn = await snapshot();
      const fromPreviousSession = await partner(newFirstSession, "subscriptions", {
        product_id: product.id, username: "fixture_switch", traffic_cap_gb: 2,
        end_date: end, consent_token: stale.consent_token,
      });
      assert.equal(fromPreviousSession.status, 409);
      assert.deepEqual(await snapshot(), beforeReturn);
      checks.push({ finding: "A to B to A across two tabs", status: fromPreviousSession.status });
    }
  } finally {
    await browser.close();
  }
}

run().then(() => console.log(version + ": B-01 and B-02 assertions passed")).catch(error => {
  console.error(error.name + ": " + error.message.split("\n")[0]);
  process.exitCode = 1;
}).finally(() => {
  fs.writeFileSync(path.join(artifacts, "sanitized-evidence.json"), JSON.stringify({ version, checks, traces }, null, 2));
});
