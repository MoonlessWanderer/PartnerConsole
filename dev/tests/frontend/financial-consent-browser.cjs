const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE);

const [version, artifacts, scope] = process.argv.slice(2);
assert.ok(["baseline", "hotfix"].includes(version));
const baseline = version === "baseline";
assert.ok(scope === undefined || (!baseline && ["account-switch", "purchase-reprice", "purchase-doubleclick", "purchase-regression"].includes(scope)));
const web = process.env.PARTNER_TEST_WEB ?? "http://127.0.0.1:13741";
const api = process.env.PARTNER_TEST_API ?? "http://127.0.0.1:18741";
const lab = process.env.PARTNER_TEST_PANEL ?? "http://127.0.0.1:18788";
const traces = [], checks = [];
const password = crypto.randomBytes(24).toString("base64url");
const signal = AbortSignal.timeout(110000);
const button = (page, name) => page.getByRole("button", { name, exact: true });
const field = (page, name) => page.getByLabel(name, { exact: true });

const usernameField = (page) => field(page, "نام کاربری").or(field(page, "نام کاربری (a-z، عدد، _)"));
const reviewValue = (page) => page.locator("dt").filter({ hasText: /^برداشت از کیف پول$/ })
  .locator("xpath=../dd").or(page.getByText(/^مبلغ:/));
const creditValue = (page, label) => page.locator("dt").filter({ hasText: label }).locator("xpath=../dd");
async function assertCredit(page, oldText) {
  const updated = creditValue(page, /^اعتبار حجم مصرفی$/);
  if (await updated.count()) {
    assert.equal("از اعتبار: " + await updated.textContent() + " و " +
      await creditValue(page, /^اعتبار روز مصرفی$/).textContent(), oldText);
  } else assert.equal(await page.getByText(/^از اعتبار:/).textContent(), oldText);
}
async function openPurchase(page) {
  const direct = page.locator("aside").getByRole("link", { name: "ساخت اشتراک", exact: true });
  if (await direct.count()) {
    await direct.click();
    return;
  }
  await page.locator("aside").getByRole("link", { name: "اشتراک‌ها", exact: true }).click();
  await page.getByRole("main").getByRole("link", { name: "ساخت اشتراک", exact: true }).click();
}


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
  await page.screenshot({ path: path.join(artifacts, file), mask: [page.getByText(/token=/), page.getByRole("textbox", { name: "لینک اشتراک", exact: true })] });
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
  const result = await pending;
  assert.equal(result.status(), 200);
  await page.waitForURL(web + "/partner");
  return (await result.json()).access_token;
}

async function purchase(page, username, gb = 2) {
  await page.goto(web + "/partner/subscriptions/new");
  return fillPurchase(page, username, gb);
}

async function fillPurchase(page, username, gb) {
  await field(page, "محصول").locator("option").first().waitFor({ state: "attached" });
  await usernameField(page).fill(username);
  await field(page, "حجم (گیگ)").fill(String(gb));
  const pending = response(page, "/api/partner/subscriptions/quote");
  await button(page, "+۳۱ روز").or(button(page, "۳۱ روز")).click();
  const quote = await (await pending).json();
  await reviewValue(page).waitFor();
  return quote;
}

function accountLabel(page, account) {
  return page.locator("aside, header").getByText(account.username, { exact: true }).filter({ visible: true });
}

async function switchAccount(page, shared, first, second, snapshot) {
  const oldName = "fixture_switch";
  const stale = await purchase(page, oldName);
  const submit = button(page, "ثبت اشتراک");
  const amount = reviewValue(page);
  assert.equal(stale.affordable, true);
  assert.equal(stale.toman, 5100);
  assert.equal(await accountLabel(page, first).isVisible(), true);
  assert.equal(await submit.isEnabled(), true);
  const before = await snapshot();
  for (const account of [before.first, before.second]) {
    assert.ok(account.wallet >= stale.toman);
    assert.deepEqual(account.credits.map(row => row[0]), before.first.credits.map(row => row[0]));
  }
  const review = { account: first.username, amount: await amount.textContent(), confirmEnabled: true };
  await screen(page, "account-a-review.png");

  const secondTab = await shared.newPage();
  await secondTab.goto(web + "/partner");
  await accountLabel(secondTab, first).waitFor();
  const logout = response(secondTab, "/api/partner/auth/logout");
  await button(secondTab, "خروج").click();
  assert.equal((await logout).status(), 200);
  await secondTab.waitForURL(web + "/");
  await login(secondTab, second.username);
  await accountLabel(secondTab, second).waitFor();

  await page.bringToFront();
  const isPurchase = (request) => request.method() === "POST" && request.url() === api + "/api/partner/subscriptions";
  const requests = [];
  page.on("request", request => { if (isPurchase(request)) requests.push(request); });
  const name = usernameField(page);
  const oldReview = await name.isVisible() && await name.inputValue() === oldName;
  const confirmAvailable = oldReview && await submit.isVisible() && await submit.isEnabled() && await amount.isVisible();

  if (baseline) {
    const submitted = response(page, "/api/partner/subscriptions");
    await submit.click();
    assert.equal((await submitted).status(), 200);
  } else if (confirmAvailable) {
    // Do not approve a review generated after the account changed.
    await submit.click();
  }

  const accountUpdated = baseline || await accountLabel(page, second).waitFor({ timeout: 5000 }).then(() => true, () => false);
  const state = {
    path: new URL(page.url()).pathname,
    account: await accountLabel(page, second).isVisible() ? second.username : await accountLabel(page, first).isVisible() ? first.username : null,
    amount: await amount.count() ? await amount.first().textContent() : null,
    confirmEnabled: await submit.isVisible() && await submit.isEnabled(),
  };
  const statuses = await Promise.all(requests.map(async request => (await request.response())?.status() ?? null));
  const after = await snapshot();
  checks.push({ finding: "B-02", review, approved: stale.toman, confirmAvailable, requestCount: requests.length, statuses, state, before, after });
  if (baseline) {
    assert.equal(before.second.wallet - after.second.wallet, stale.toman);
    assert.ok(after.second.subscriptions.some(row => row[1] === oldName));
  } else {
    await screen(page, "account-a-draft-discarded.png");
    assert.deepEqual(after, before, "a stale review changed balances, financial operations or subscriptions");
    assert.ok(accountUpdated, "the original tab did not show account B after the shared login changed");
    assert.equal(state.amount, null);
    assert.equal(state.confirmEnabled, false);
    assert.equal(await name.isVisible() && await name.inputValue() === oldName, false);
  }
  return { stale, secondTab, beforeSwitch: before };
}

async function submitWithDoubleClick(page) {
  const route = "/api/partner/subscriptions";
  const isConfirmation = request => request.url() === api + route && request.method() === "POST";
  const release = Promise.withResolvers();
  const backendReady = Promise.withResolvers();
  let requests = 0;
  const delivered = [];
  page.on("request", request => { if (isConfirmation(request)) requests++; });
  page.on("response", result => { if (isConfirmation(result.request())) delivered.push(result.status()); });

  await page.route(api + route, async intercepted => {
    if (!isConfirmation(intercepted.request())) {
      await intercepted.continue();
      return;
    }
    try {
      const upstream = await intercepted.fetch({ timeout: 15000 });
      backendReady.resolve(upstream.status());
      await release.promise;
      await intercepted.fulfill({ response: upstream });
    } catch (error) {
      backendReady.reject(error);
      await intercepted.abort().catch(() => {});
    }
  });

  const submit = button(page, "ثبت اشتراک");
  await submit.scrollIntoViewIfNeeded();
  assert.equal(await submit.isEnabled(), true);
  const box = await submit.boundingBox();
  assert.ok(box);
  const sent = page.waitForRequest(isConfirmation, { timeout: 15000 });
  const confirmed = response(page, route);
  let waiting;
  try {
    // Deliver both pointer click sequences at the original position without waiting for the button to re-enable.
    await page.mouse.dblclick(box.x + box.width / 2, box.y + box.height / 2, { delay: 0 });
    await sent;
    const upstreamStatus = await backendReady.promise;
    const submitControl = page.locator("form button[type=submit]");
    waiting = { requests, delivered: delivered.length, upstreamStatus, disabled: await submitControl.isDisabled(),
      label: await submitControl.textContent(), path: new URL(page.url()).pathname };
    checks.push({ finding: "double-click-before-release", ...waiting });
  } finally {
    release.resolve();
  }

  const result = await confirmed;
  const created = await result.json();
  assert.equal(result.status(), 200);
  assert.ok(created.id && created.v2_user_id);
  await page.waitForURL(web + "/partner/subscriptions/" + created.id);
  await page.unroute(api + route);
  return { waiting, requests, delivered, created };
}

function assertSingleConfirmation(evidence) {
  const { label, ...waiting } = evidence.waiting;
  assert.ok(["در حال ساخت…", "در حال بررسی…"].includes(label));
  assert.deepEqual(waiting, { requests: 1, delivered: 0, upstreamStatus: 200, disabled: true,
    path: "/partner/subscriptions/new" });
  assert.equal(evidence.requests, 1);
  assert.deepEqual(evidence.delivered, [200]);
}

async function repricePurchase(page, context, owner, product, snapshot) {
  const route = "/api/partner/subscriptions", quoteRoute = route + "/quote";
  const target = "fixture_reprice", other = "fixture_credit_spend";
  const submit = button(page, "ثبت اشتراک");
  const currentAmount = reviewValue(page);
  const selectedProduct = field(page, "محصول").locator("option:checked");
  const funding = (quote) => [quote.toman, Number(quote.gb_from_credit), quote.days_from_credit];
  const credit = (state) => state.first.credits.map(([id, gb, days]) => [id, Number(gb), days]);
  const addedLedger = (before, after) => {
    const ids = new Set(before.first.ledger.map(row => row[0]));
    assert.deepEqual(after.first.ledger.filter(row => ids.has(row[0])), before.first.ledger);
    return after.first.ledger.filter(row => !ids.has(row[0])).map(([, amount, asset, reason]) =>
      [asset, Number(amount), reason]).sort((a, b) => a[0].localeCompare(b[0]));
  };
  const readReview = async (amount, usedCredit) => {
    await currentAmount.filter({ hasText: amount.replace("مبلغ: ", "") }).waitFor();
    await assertCredit(page, usedCredit);
    assert.equal(await submit.isEnabled(), true);
    return { amount: await currentAmount.textContent(), credit: usedCredit,
      product: await selectedProduct.textContent(), confirmEnabled: true };
  };
  const useSession = async (result) => {
    const headers = await result.request().allHeaders();
    assert.ok(headers.authorization === "Bearer " + owner.token, "purchase must use the original browser login");
  };
  const panelUser = async (created, username) => {
    assert.ok(created.id && created.v2_user_id);
    assert.equal(created.product_id, product.id);
    const { data: user } = ok(await request(lab, "/_lab/users/" + created.v2_user_id));
    assert.equal(user.email, username + "@fixture.example.test");
    assert.equal(user.id, created.v2_user_id);
  };

  const nav = (name) => page.locator("aside").getByRole("link", { name, exact: true });
  const readPage = async (link, path, apiPath) => {
    const pending = page.waitForResponse(result => {
      const url = new URL(result.url());
      return url.origin === api && url.pathname === apiPath && result.request().method() === "GET";
    }, { timeout: 10000 });
    await link.click();
    await page.waitForURL(web + path);
    const result = await pending;
    assert.equal(result.status(), 200);
    await useSession(result);
    return result.json();
  };
  const wallet = page.getByText("موجودی تومان", { exact: true }).or(page.getByText("موجودی کیف پول", { exact: true })).locator("..").getByText(/^[۰-۹,]+ تومان$/);

  const initial = await snapshot();
  assert.equal(initial.first.wallet, 100000);
  const initialList = await readPage(nav("اشتراک‌ها"), "/partner/subscriptions", route);
  assert.deepEqual(initialList.items, []);
  await page.getByText("اشتراکی ندارید.", { exact: true }).waitFor();
  const initialDashboard = await readPage(nav("داشبورد"), "/partner", "/api/partner/dashboard");
  assert.equal(initialDashboard.balance_toman, initial.first.wallet);
  await wallet.waitFor();
  assert.equal(await wallet.textContent(), "۱۰۰,۰۰۰ تومان");
  await openPurchase(page);
  await page.waitForURL(web + "/partner/subscriptions/new");
  assert.deepEqual(credit(initial), [[product.id, 7, 37]]);
  const posts = [];
  page.on("request", request => {
    const url = new URL(request.url());
    if (url.origin === api && request.method() === "POST" && [route, quoteRoute].includes(url.pathname)) {
      posts.push(url.pathname);
    }
  });
  const original = await fillPurchase(page, target, 10);
  // A needs 10 GB and 31 days. The rates are 1000 toman/GB and 100 toman/day.
  assert.deepEqual(funding(original), [3000, 7, 31]);
  assert.deepEqual(await snapshot(), initial);
  const beforeClick = await readReview("مبلغ: ۳,۰۰۰ تومان", "از اعتبار: ۷ گیگ و ۳۱ روز");
  assert.match(beforeClick.product, /اعتبار ۷ گیگ \/ ۳۷ روز$/);
  await screen(page, "purchase-original-quote.png");

  const otherPage = await context.newPage();
  const otherQuote = await purchase(otherPage, other, 4);
  assert.deepEqual(funding(otherQuote), [0, 4, 31]);
  const otherPending = response(otherPage, route);
  await button(otherPage, "ثبت اشتراک").click();
  const otherResult = await otherPending;
  assert.equal(otherResult.status(), 200);
  await useSession(otherResult);
  const createdOther = await otherResult.json();
  await otherPage.waitForURL(web + "/partner/subscriptions/" + createdOther.id);
  await panelUser(createdOther, other);
  const baseline = await snapshot();
  assert.equal(baseline.first.wallet, initial.first.wallet);
  assert.deepEqual(credit(baseline), [[product.id, 3, 6]]);
  assert.deepEqual(addedLedger(initial, baseline), [["days", -31, "create"], ["gb", -4, "create"]]);
  assert.deepEqual(baseline.first.subscriptions.map(row => row.slice(0, 2)), [[createdOther.id, other]]);
  assert.equal(baseline.panelUsers - initial.panelUsers, 1);
  assert.equal(baseline.operations.length - initial.operations.length, 1);

  await page.bringToFront();
  assert.equal(await currentAmount.textContent(), beforeClick.amount);
  const stalePending = response(page, route);
  const newQuotePending = response(page, quoteRoute);
  await submit.click();
  const rejected = await stalePending;
  assert.equal(rejected.status(), 409);
  await useSession(rejected);
  assert.ok(rejected.request().postDataJSON().consent_token === original.consent_token);
  const claimBytes = Buffer.from(original.consent_token.split(".")[1], "base64url");
  assert.ok(JSON.parse(claimBytes).exp > Date.now() / 1000, "the original quote must still be within its validity period");
  const { code, detail } = await rejected.json();
  assert.equal(code, "financial_consent");
  assert.equal(detail, "شرایط یا نشست تغییر کرده است. برآورد تازه را بررسی و دوباره تأیید کنید.");
  await page.getByText(detail, { exact: true }).waitFor();

  const quoted = await newQuotePending;
  assert.equal(quoted.status(), 200);
  await useSession(quoted);
  const fresh = await quoted.json();
  // B leaves 3 GB and 6 days: A now pays for 7 GB and 25 days.
  assert.deepEqual(funding(fresh), [9500, 3, 6]);
  assert.deepEqual([fresh.price_per_gb, fresh.price_per_day], [original.price_per_gb, original.price_per_day]);
  const afterRejection = await readReview("مبلغ: ۹,۵۰۰ تومان", "از اعتبار: ۳ گیگ و ۶ روز");
  await screen(page, "purchase-refreshed-quote.png");
  assert.deepEqual(posts, [quoteRoute, route, quoteRoute]);
  assert.deepEqual(await snapshot(), baseline, "the stale confirmation must have no financial or panel effects");
  assert.match(afterRejection.product, /اعتبار ۳ گیگ \/ ۶ روز$/,
    "the product selector did not show the refreshed balance: " + afterRejection.product);

  const completePending = response(page, route);
  await submit.click();
  const complete = await completePending;
  assert.equal(complete.status(), 200);
  await useSession(complete);
  assert.ok(complete.request().postDataJSON().consent_token === fresh.consent_token);
  const created = await complete.json();
  assert.notEqual(created.id, createdOther.id);
  assert.notEqual(created.v2_user_id, createdOther.v2_user_id);
  await page.waitForURL(web + "/partner/subscriptions/" + created.id);
  await panelUser(created, target);
  const settled = await snapshot();
  assert.equal(baseline.first.wallet - settled.first.wallet, 9500);
  assert.deepEqual(credit(settled), [[product.id, 0, 0]]);
  assert.deepEqual(addedLedger(baseline, settled), [
    ["days", -6, "create"], ["gb", -3, "create"], ["toman", -9500, "create"],
  ]);
  assert.deepEqual(settled.first.subscriptions.filter(row => row[0] !== createdOther.id).map(row => row.slice(0, 2)), [[created.id, target]]);
  assert.equal(settled.panelUsers - baseline.panelUsers, 1);
  // The panel generates the user and then applies the purchased cap.
  assert.equal(settled.writes - baseline.writes, 2);
  assert.equal(settled.operations.length - baseline.operations.length, 1);
  assert.deepEqual(settled.operations.filter(([id]) => !baseline.operations.some(([existing]) => existing === id))
    .map(([, status]) => status), ["completed"]);
  assert.deepEqual(posts, [quoteRoute, route, quoteRoute, route]);

  checks.push({ finding: "purchase-reprice", beforeClick, rejection: { status: rejected.status(), code, detail },
    afterRejection, confirmed: complete.status(), posts, initial, baseline, settled });

  const [storedId] = settled.first.subscriptions.find(([, username]) => username === target);
  assert.equal(storedId, created.id);
  assert.equal(settled.first.wallet, 90500);

  const observed = {};
  checks.push({ finding: "post-purchase-sync", observed });
  const dashboard = await readPage(nav("داشبورد"), "/partner", "/api/partner/dashboard");
  await wallet.waitFor();
  observed.dashboard = { apiWallet: dashboard.balance_toman, wallet: await wallet.textContent() };
  assert.equal(dashboard.balance_toman, settled.first.wallet, "dashboard API returned a stale wallet balance");
  assert.equal(observed.dashboard.wallet, "۹۰,۵۰۰ تومان", "dashboard rendered a stale wallet balance");

  const catalogRead = page.waitForResponse(result => result.url() === api + "/api/partner/products" && result.request().method() === "GET");
  await openPurchase(page);
  const catalog = await (await catalogRead).json();
  const catalogProduct = catalog.items.find(item => item.id === product.id);
  const productSelect = field(page, "محصول");
  await productSelect.selectOption(String(product.id));
  observed.product = {
    apiCredit: [Number(catalogProduct.credit_gb), catalogProduct.credit_days],
    label: await selectedProduct.textContent(),
  };
  assert.deepEqual(observed.product.apiCredit, [0, 0], "product API returned a stale credit balance");
  assert.match(observed.product.label, /اعتبار ۰ گیگ \/ ۰ روز$/, "the purchase form rendered a stale credit balance");

  const list = await readPage(nav("اشتراک‌ها"), "/partner/subscriptions", route);
  const management = page.getByRole("link", { name: /^مدیریت(?: |$)/ });
  await management.first().waitFor();
  const rowFor = (username) => page.getByRole("row").filter({ has: page.getByText(username, { exact: true }) });
  const targetRow = rowFor(target);
  observed.list = { apiIds: list.items.map(item => item.id), total: list.total,
    purchaseRows: await targetRow.count(), independentRows: await rowFor(other).count() };
  assert.deepEqual(observed.list.apiIds, [storedId, createdOther.id], "subscription list API returned stale or duplicate rows");
  assert.equal(observed.list.total, 2);
  assert.equal(observed.list.purchaseRows, 1);
  assert.equal(observed.list.independentRows, 1);
  assert.equal(await management.count(), 2);

  const detailPath = "/partner/subscriptions/" + storedId;
  const openDetails = targetRow.getByRole("link", { name: /^مدیریت(?: |$)/ });
  assert.equal(await openDetails.getAttribute("href"), detailPath);
  const opened = await readPage(openDetails, detailPath, route + "/" + storedId);
  const title = page.getByRole("main").getByText(target, { exact: true });
  await title.waitFor();
  observed.detail = { id: opened.id, username: await title.textContent(), path: new URL(page.url()).pathname };
  assert.equal(opened.id, storedId);
  assert.equal(opened.username, target);
  assert.equal(opened.v2_user_id, created.v2_user_id);
  assert.deepEqual(posts, [quoteRoute, route, quoteRoute, route]);
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
  async function account(username, loginForReads = true) {
    const item = ok(await admin("partners", { username, password }));
    ok(await admin("partners/" + item.id + "/adjust", { asset: "toman", amount: 100000 }));
    if (loginForReads) item.token = ok(await partner(null, "auth/login", { username, password })).access_token;
    return item;
  }
  async function snapshotFor(first, second) {
    const take = async (item) => ({
      wallet: ok(await partner(item.token, "me")).balance_toman,
      credits: ok(await partner(item.token, "products")).items.map(row => [row.id, row.credit_gb, row.credit_days]),
      ledger: ok(await partner(item.token, "ledger")).items.map(row => [row.id, row.amount, row.asset, row.reason]),
      subscriptions: ok(await partner(item.token, "subscriptions")).items.map(row => [row.id, row.username, row.traffic_cap_gb, row.expired_at]),
    });
    return {
      first: await take(first), second: second ? await take(second) : null,
      operations: ok(await admin("operations?limit=100")).items.map(row => [row.id, row.status]),
      writes: ok(await request(lab, "/_lab/consent-writes")).count,
      panelUsers: ok(await request(lab, "/_lab/users")).total,
    };
  }

  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || "/usr/bin/google-chrome", headless: true });
  try {
    async function context() {
      const profile = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      await profile.route("**/*", route => [api, web].includes(new URL(route.request().url()).origin) ? route.continue() : route.abort());
      return profile;
    }
    async function checkReprice() {
      const owner = await account("fixture_partner_reprice", false);
      for (const [asset, amount] of [["gb", 7], ["days", 37]]) {
        ok(await admin("partners/" + owner.id + "/adjust", { asset, amount, product_id: product.id }));
      }
      // Request interception disables the browser HTTP cache in Playwright.
      const profile = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      try {
        const page = await profile.newPage();
        owner.token = await login(page, owner.username);
        await repricePurchase(page, profile, owner, product, () => snapshotFor(owner));
      } finally {
        await profile.close();
      }
    }
    if (scope === "purchase-reprice") {
      await checkReprice();
      return;
    }
    async function checkDoubleClick() {
      const owner = await account("fixture_partner_doubleclick", false);
      for (const [asset, amount] of [["gb", 1], ["days", 5]]) {
        ok(await admin("partners/" + owner.id + "/adjust", { asset, amount, product_id: product.id }));
      }
      const profile = await context();
      try {
        const page = await profile.newPage();
        owner.token = await login(page, owner.username);
        const before = await snapshotFor(owner);
        assert.equal(before.first.wallet, 100000);
        assert.deepEqual(before.first.credits.map(([id, gb, days]) => [id, Number(gb), days]), [[product.id, 1, 5]]);
        const quote = await purchase(page, "fixture_doubleclick");
        // 2 GB and 31 days minus 1 GB and 5 days of credit, at 1000 toman/GB and 100 toman/day.
        const approved = [quote.toman, Number(quote.gb_from_credit), quote.days_from_credit];
        assert.deepEqual(approved, [3600, 1, 5]);
        await reviewValue(page).filter({ hasText: "۳,۶۰۰ تومان" }).waitFor();
        await assertCredit(page, "از اعتبار: ۱ گیگ و ۵ روز");
        assert.deepEqual(await snapshotFor(owner), before);
        const evidence = await submitWithDoubleClick(page);
        const { created } = evidence;
        const after = await snapshotFor(owner);
        const beforeLedger = new Set(before.first.ledger.map(row => row[0]));
        const ledger = after.first.ledger.filter(([id]) => !beforeLedger.has(id))
          .map(([, amount, asset, reason]) => [asset, Number(amount), reason])
          .sort((a, b) => a[0].localeCompare(b[0]));
        const { data: remote } = ok(await request(lab, "/_lab/users/" + created.v2_user_id));
        checks.push({ finding: "purchase-doubleclick", approved, requests: evidence.requests,
          responses: evidence.delivered, before, after, ledger, remoteUserId: remote.id });

        assertSingleConfirmation(evidence);
        assert.equal(before.first.wallet - after.first.wallet, 3600);
        assert.deepEqual(after.first.credits.map(([id, gb, days]) => [id, Number(gb), days]), [[product.id, 0, 0]]);
        assert.deepEqual(ledger, [["days", -5, "create"], ["gb", -1, "create"], ["toman", -3600, "create"]]);
        assert.deepEqual(after.operations.filter(([id]) => !before.operations.some(([old]) => old === id))
          .map(([, status]) => status), ["completed"]);
        assert.deepEqual(after.first.subscriptions.map(row => row.slice(0, 2)), [[created.id, "fixture_doubleclick"]]);
        assert.equal(after.panelUsers - before.panelUsers, 1);
        assert.equal(remote.id, created.v2_user_id);
        assert.equal(remote.email, "fixture_doubleclick@fixture.example.test");
      } finally {
        await profile.close();
      }
    }
    if (scope === "purchase-doubleclick" || scope === "purchase-regression") {
      await checkDoubleClick();
      if (scope === "purchase-doubleclick") return;
      await checkReprice();
    }
    const first = await account("fixture_partner_a"), second = await account("fixture_partner_b");
    const snapshot = () => snapshotFor(first, second);
    const shared = await context();
    const page = await shared.newPage();
    await login(page, first.username);
    if (scope === "account-switch" || scope === "purchase-regression") {
      await switchAccount(page, shared, first, second, snapshot);
      return;
    }
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

    const { stale, secondTab, beforeSwitch } = await switchAccount(page, shared, first, second, snapshot);
    const end = stale.end_jalali;
    if (!baseline) {
      const oldReview = { product_id: product.id, username: "fixture_switch", traffic_cap_gb: 2,
        end_date: end, consent_token: stale.consent_token };
      for (const account of [second, first]) {
        const blocked = await partner(account.token, "subscriptions", oldReview);
        assert.equal(blocked.status, 409);
      }
      assert.deepEqual(await snapshot(), beforeSwitch);
      await purchase(secondTab, "fixture_refresh");
      await secondTab.reload();
      assert.equal(await usernameField(secondTab).inputValue(), "");
      assert.equal(await button(secondTab, "ثبت اشتراک").isEnabled(), false);
      const fresh = await purchase(secondTab, "fixture_switch");
      assertSingleConfirmation(await submitWithDoubleClick(secondTab));
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
      await checkReprice();
    }
  } finally {
    await browser.close();
  }
}

run().then(() => console.log(version + ": " + (scope || (baseline ? "B-01 and B-02" : "B-01, B-02 and purchase repricing")) + " assertions passed")).catch(error => {
  console.error(error.name + ": " + error.message.split("\n")[0]);
  process.exitCode = 1;
}).finally(() => {
  fs.writeFileSync(path.join(artifacts, "sanitized-evidence.json"), JSON.stringify({ version, checks, traces }, null, 2));
});
