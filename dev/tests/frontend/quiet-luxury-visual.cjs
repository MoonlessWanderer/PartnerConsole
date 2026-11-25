const assert = require("node:assert/strict");
const { randomBytes } = require("node:crypto");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE);

const web = process.env.PC_VISUAL_WEB || "http://127.0.0.1:13753";
const api = process.env.PC_VISUAL_API || "http://127.0.0.1:18753";
const lab = process.env.PC_VISUAL_LAB || "http://127.0.0.1:18758";
const designWeb = process.env.PC_VISUAL_DESIGN_WEB;
const output = process.argv[2];
const adminUsername = process.env.ADMIN_USERNAME;
const adminPassword = process.env.ADMIN_PASSWORD;
const runId = randomBytes(3).toString("hex");
const partnerUsername = "arya_" + runId;
const partnerPassword = randomBytes(18).toString("base64url");

function localUrl(value) {
  const parsed = new URL(value);
  assert.ok(["127.0.0.1", "localhost", "[::1]"].includes(parsed.hostname));
  assert.equal(parsed.protocol, "http:");
  return parsed.origin;
}
assert.ok(output && adminUsername && adminPassword, "Isolated output and fixture admin credentials are required");
const origins = [web, api, lab, ...(designWeb ? [designWeb] : [])].map(localUrl);

async function request(route, body, token) {
  const response = await fetch(api + "/api/" + route, {
    method: body === undefined ? "GET" : "POST",
    headers: { ...(token ? { Authorization: "Bearer " + token } : {}), ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(12000),
  });
  assert.equal(response.status, 200, route);
  return response.json();
}

function endIn(days) {
  const parts = new Intl.DateTimeFormat("en-US-u-ca-persian", {
    year: "numeric", month: "2-digit", day: "2-digit", timeZone: "Asia/Tehran",
  }).formatToParts(new Date(Date.now() + days * 86400000));
  return ["year", "month", "day"].map(key => parts.find(item => item.type === key).value).join("/");
}

async function seed() {
  const token = (await request("admin/auth/login", { username: adminUsername, password: adminPassword })).access_token;
  const admin = (route, body) => request("admin/" + route, body, token);
  const panel = await admin("panels", {
    name: "پنل اصلی", base_url: lab, secure_path: "labsecure", admin_email: "admin@panel.test", admin_password: "secret",
  });
  const { product } = await admin("products", {
    panel_id: panel.id, plan_id: 7, name: "حرفه‌ای", email_domain: "visual-" + runId + ".example.test", price_per_gb: 1000, price_per_day: 100,
  });
  const partner = await admin("partners", { username: partnerUsername, password: partnerPassword });
  await admin("partners/" + partner.id + "/adjust", { asset: "toman", amount: 1258000 });
  const partnerToken = (await request("partner/auth/login", { username: partnerUsername, password: partnerPassword })).access_token;
  let subscription;
  for (const [index, cap] of [[1, 50], [2, 20], [3, 40]]) {
    const draft = { product_id: product.id, username: "clt_" + runId + index, traffic_cap_gb: cap, end_date: endIn(21 + index), note: index === 1 ? "پیگیری تمدید اشتراک" : null };
    const quote = await request("partner/subscriptions/quote", { ...draft, action: "create" }, partnerToken);
    const row = await request("partner/subscriptions", { ...draft, consent_token: quote.consent_token }, partnerToken);
    if (!subscription) subscription = row;
  }
  const usage = await fetch(lab + "/_lab/users/" + subscription.v2_user_id + "/traffic", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ u: 3 * 1073741824, d: 15 * 1073741824 }),
  });
  assert.equal(usage.status, 200);
  await admin("partners/" + partner.id + "/adjust", { asset: "gb", amount: 3, product_id: product.id });
  await admin("partners/" + partner.id + "/adjust", { asset: "days", amount: 5, product_id: product.id });
  return { panel, product, partner, subscription };
}

const result = [];
const field = (p, name) => p.getByLabel(name, { exact: true });
const button = (p, name) => p.getByRole("button", { name, exact: true });
const h1 = (p, name) => p.getByRole("heading", { name, level: 1 });
const hiddenValues = (p) => [
  p.locator("input[type='password']:not([value='']), #subscription-link"),
  p.getByLabel("لینک اشتراک", { exact: true }).filter({ hasNot: p.locator("h2") }),
  p.getByLabel("مسیر امن ادمین", { exact: true }).and(p.locator("input:not([value=''])")),
  p.getByText(/token=/i),
];

async function capture(p, id, size) {
  await p.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all([400, 500, 700].map(weight => document.fonts.load(weight + " 16px Vazirmatn", "نام PartnerConsole")));
  });
  await p.mouse.move(-10, -10);
  const layout = await p.evaluate(() => ({
    font: getComputedStyle(document.body).fontFamily,
    persian: document.fonts.check("400 16px Vazirmatn", "موجودی ۱۲"),
    latin: document.fonts.check("700 16px Vazirmatn", "PartnerConsole"),
    fontFiles: performance.getEntriesByType("resource")
      .map(entry => new URL(entry.name))
      .filter(url => url.pathname.endsWith(".woff2"))
      .map(url => url.origin === location.origin ? url.pathname : url.origin),
    direction: document.documentElement.dir,
    viewport: document.documentElement.clientWidth,
    width: document.documentElement.scrollWidth,
  }));
  assert.match(layout.font, /^Vazirmatn/);
  assert.ok(layout.persian && layout.latin);
  for (const script of ["arabic", "latin"]) {
    for (const weight of [400, 500, 700]) {
      assert.ok(layout.fontFiles.includes("/fonts/vazirmatn/vazirmatn-" + script + "-" + weight + ".woff2"));
    }
  }
  assert.ok(layout.fontFiles.every(url => url.startsWith("/fonts/vazirmatn/")));
  assert.equal(layout.direction, "rtl");
  assert.ok(layout.width <= layout.viewport + 1, JSON.stringify(layout));
  const filename = String(id).padStart(2, "0") + "-" + size + ".png";
  await p.screenshot({ path: path.join(output, filename), animations: "disabled", mask: hiddenValues(p), maskColor: "#e3e3dc" });
  return { file: filename, layout };
}

async function login(p, role, id, size, origin = web) {
  await p.goto(origin + (role === "admin" ? "/gate/k3p8n2" : "/"));
  await h1(p, role === "admin" ? "ورود مدیر" : "ورود همکار").waitFor();
  if (id) result.push({ id, size, ...(await capture(p, id, size)) });
  await field(p, "نام کاربری").fill(role === "admin" ? adminUsername : partnerUsername);
  await field(p, "رمز عبور").fill(role === "admin" ? adminPassword : partnerPassword);
  await button(p, "ورود").click();
  await p.waitForURL(origin + "/" + role);
}

function operationFixture(fixture) {
  return {
    id: 1024, status: "attention", kind: "increase", partner_id: fixture.partner.id,
    product_id: fixture.product.id, subscription_id: fixture.subscription.id, username: fixture.subscription.username,
    last_error: "نتیجهٔ ارسال به پنل نامعلوم است.", created_at: "2026-11-24T18:00:00Z", updated_at: "2026-11-24T18:10:00Z",
    busy: false, charge_toman: 15000, charge_gb_from_credit: "2", charge_days_from_credit: 0,
    gb_back: null, days_back: null, old_cap_gb: "50", new_cap_gb: "60", panel_applied: false, panel_sent: true,
    shortfall_gb: "0", shortfall_days: 0, resolution: null, resolution_note: null, retry_action: "resend_change",
    panel_state: { available: true, exists: true, owned: true, transfer_enable_gb: "50", used_gb: "18", expired_jalali: endIn(22), banned: false, plan_id: 7 },
    decision_blocks: {},
  };
}

async function context(browser, viewport) {
  const mobile = viewport.width < 500;
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 1, locale: "fa-IR", isMobile: mobile, hasTouch: mobile, reducedMotion: "reduce" });
  await ctx.route("**/*", route => origins.includes(new URL(route.request().url()).origin) ? route.continue() : route.abort());
  return ctx;
}

async function exercise(browser, fixture, size, viewport) {
  const ctx = await context(browser, viewport);
  try {
    const admin = await ctx.newPage();
    const errors = [];
    admin.setDefaultTimeout(6500);
    admin.on("pageerror", error => errors.push(error.message));
    await login(admin, "admin", 1, size);
    const adminRoot = "/admin/partners/" + fixture.partner.id;
    const adminSub = "/admin/subscriptions/" + fixture.subscription.id;
    async function record(id, page, route, title, ready) {
      try {
        if (route) await page.goto(web + route);
        await h1(page, title).waitFor();
        if (ready) await ready();
        result.push({ id, size, ...(await capture(page, id, size)) });
      } catch (error) {
        const message = error instanceof Error ? error.message.split("\n")[0] : String(error);
        result.push({ id, size, error: message });
        console.error("Board", id, size, message);
      }
    }
    const panelReady = () => admin.getByText("پنل اصلی", { exact: true }).filter({visible:true}).first().waitFor();
    const nameReady = () => admin.getByText(partnerUsername, { exact: true }).filter({visible:true}).first().waitFor();
    const seedUser = fixture.subscription.username;
    const adminSubReady = () => admin.getByText(seedUser, { exact: true }).filter({visible:true}).first().waitFor();

    await record(3, admin, "/admin", "داشبورد مدیریت", () => admin.getByRole("link", { name: /پیگیری|عملیات ناتمام/ }).last().waitFor());
    await record(4, admin, "/admin/panels", "پنل‌های V2Board", panelReady);
    await record(5, admin, "/admin/panels/new", "افزودن پنل", () => field(admin, "نام پنل").waitFor());
    await record(6, admin, "/admin/panels/" + fixture.panel.id + "/edit", "ویرایش پنل", () => field(admin, "نام پنل").waitFor());
    await record(7, admin, "/admin/products", "محصولات", () => admin.getByText("حرفه‌ای", { exact: true }).filter({visible:true}).first().waitFor());
    await record(8, admin, "/admin/products/new", "محصول جدید", async () => {
      await field(admin, "پنل").locator("option").filter({ hasText: "پنل اصلی" }).first().waitFor({ state: "attached" });
      await field(admin, "پنل").selectOption(String(fixture.panel.id));
      await field(admin, "پلن V2Board").selectOption(String(fixture.product.plan_id));
    });
    await record(9, admin, "/admin/partners", "همکاران", nameReady);
    await record(10, admin, "/admin/partners/new", "ایجاد همکار", () => field(admin, "نام کاربری").waitFor());
    await record(11, admin, adminRoot, "پروفایل همکار", nameReady);
    await record(12, admin, adminRoot + "/credits", "اعتبارهای همکار", () => admin.getByText("حرفه‌ای", { exact: true }).filter({visible:true}).first().waitFor());
    await record(13, admin, adminRoot + "/wallet", "تعدیل موجودی", async () => field(admin, "مقدار (منفی برای کسر)").fill("-15000"));
    await record(14, admin, adminRoot + "/credits/adjust", "تعدیل اعتبار محصول", async () => field(admin, "مقدار (منفی برای کسر)").fill("4"));
    await record(15, admin, adminRoot + "/access", "مدیریت دسترسی همکار", () => button(admin, "مسدود کردن همکار").waitFor());
    await record(16, admin, "/admin/subscriptions", "اشتراک‌ها", adminSubReady);
    await record(18, admin, adminSub, "جزئیات اشتراک", adminSubReady);
    await record(19, admin, "/admin/ledger", "دفترکل", () => admin.getByRole("main").getByText("ساخت اشتراک", { exact: true }).filter({ visible: true }).first().waitFor());

    const op = operationFixture(fixture);
    const cors = { "Access-Control-Allow-Origin": web, "Access-Control-Allow-Headers": "Authorization, Content-Type", "Access-Control-Allow-Methods": "GET, OPTIONS" };
    await admin.route(api + "/api/admin/operations**", async route => {
      const request = route.request();
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
      assert.equal(request.method(), "GET", "Visual fixtures must not resolve operations");
      const endpoint = new URL(request.url()).pathname;
      return route.fulfill({ status: 200, headers: cors, json: endpoint.endsWith("/1024") ? op : { items: [op] } });
    });
    await record(21, admin, "/admin/operations", "عملیات ناتمام", () => admin.getByRole("link", { name: "جزئیات", exact: true }).waitFor());
    await record(22, admin, "/admin/operations/1024", "عملیات #۱۰۲۴", () => button(admin, "بررسی و تصمیم").waitFor());
    await button(admin, "بررسی و تصمیم").click();
    await record(23, admin, null, "رسیدگی به عملیات", async () => {
      await field(admin, "تصمیم").selectOption("retry");
      await field(admin, "توضیح تصمیم (الزامی)").fill("وضعیت پنل در محیط آزمایشی بررسی شد.");
      await admin.getByRole("checkbox", { name: /وضعیت پنل/ }).check();
    });
    await button(admin, "ادامه").click();
    await record(24, admin, null, "تأیید نهایی عملیات", () => button(admin, "تأیید و ثبت").waitFor());
    await admin.close();

    const partner = await ctx.newPage();
    partner.on("pageerror", error => errors.push(error.message));
    partner.setDefaultTimeout(6500);
    await login(partner, "partner", 2, size);
    const detailRoute = "/partner/subscriptions/" + fixture.subscription.id;
    const detailReady = () => partner.getByText(seedUser, { exact: true }).filter({visible:true}).first().waitFor();
    await record(17, partner, "/partner/subscriptions", "اشتراک‌ها", detailReady);
    await record(20, partner, "/partner/ledger", "تاریخچه تراکنش‌ها", () => partner.getByRole("main").getByText(seedUser, { exact: true }).filter({ visible: true }).first().waitFor());
    await record(25, partner, "/partner", new RegExp(partnerUsername), () => partner.getByText("موجودی کیف پول", { exact: true }).first().waitFor());
    await record(26, partner, "/partner/products", "اعتبار و نرخ محصولات", () => partner.getByText("حرفه‌ای", { exact: true }).filter({visible:true}).first().waitFor());
    await record(27, partner, "/partner/subscriptions/new", "ساخت اشتراک", async () => {
      await field(partner, "محصول").selectOption(String(fixture.product.id));
      await field(partner, "نام کاربری").fill("preview_" + runId);
      await field(partner, "حجم (گیگ)").fill("10");
      const ready = partner.waitForResponse(response => response.url() === api + "/api/partner/subscriptions/quote" && response.request().method() === "POST");
      await button(partner, "۳۱ روز").click();
      assert.equal((await ready).status(), 200);
    });
    await button(partner, viewport.width < 680 ? "مشاهده پیش‌فاکتور" : "بررسی پیش‌فاکتور").click();
    await record(28, partner, null, "بررسی پیش‌فاکتور", () => button(partner, "ثبت اشتراک").waitFor());
    await record(29, partner, detailRoute, "جزئیات اشتراک", detailReady);
    await partner.getByRole("navigation", { name: "بخش‌های اشتراک" }).getByRole("button", { name: "مصرف و لینک" }).click();
    await record(30, partner, null, "مصرف و لینک اشتراک", () => field(partner, "لینک اشتراک").waitFor());
    await button(partner, "مدیریت").click();
    await record(31, partner, null, "مدیریت اشتراک");
    for (const [id, label, title, volume] of [
      [32, /^افزایش /, "افزایش اشتراک", "60"],
      [33, /^کاهش /, "کاهش اشتراک", "40"],
      [34, /^ریست ترافیک/, "ریست ترافیک"],
      [35, /^ریست لینک/, "ریست لینک"],
      [36, /^مسدود کردن اشتراک/, "مسدود کردن اشتراک"],
      [37, /^حذف اشتراک/, "حذف اشتراک"],
      [38, /^یادداشت مشتری/, "یادداشت مشتری"],
    ]) {
      await partner.getByRole("button", { name: label }).click();
      await record(id, partner, null, title, async () => {
        if (volume) await field(partner, "سقف جدید (گیگ)").fill(volume);
        if ([32, 33, 34, 37].includes(id)) await partner.getByText("برداشت از کیف پول", { exact: true }).waitFor();
      });
      await button(partner, "انصراف").click();
    }
    assert.deepEqual(errors, [], "No unhandled UI errors");
  } finally {
    await ctx.close();
  }
}

async function inspectDesign(browser, size, viewport) {
  if (!designWeb) return;
  const ctx = await context(browser, viewport);
  try {
    const p = await ctx.newPage();
    await p.goto(designWeb + "/design");
    await h1(p, "سیستم طراحی").waitFor();
    result.push({ id: 39, size, ...(await capture(p, 39, size)) });
  } finally {
    await ctx.close();
  }
}


async function inspectResponsive(browser, fixture) {
  const sizes = [[1440, 900], [1280, 800], [1024, 768], [768, 1024], [390, 844], [320, 720], [720, 450]];
  const checks = [];
  for (const [width, height] of sizes) {
    const ctx = await context(browser, { width, height });
    try {
      const page = await ctx.newPage();
      page.setDefaultTimeout(8000);
      await login(page, "partner");
      await h1(page, new RegExp(partnerUsername)).waitFor();
      const noOverflow = async () => {
        assert.ok(await page.evaluate(() =>
          document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1
        ), width + "px overflow at " + new URL(page.url()).pathname);
      };
      await noOverflow();
      if (width < 1024) {
        const open = button(page, "باز کردن منو");
        if (width < 500) await open.tap();
        else await open.click();
        const menu = page.getByRole("dialog");
        await menu.waitFor();
        await page.keyboard.press("Escape");
        await menu.waitFor({ state: "hidden" });
        assert.ok(await open.evaluate(el => el === document.activeElement));
      }
      await page.goto(web + "/partner/subscriptions");
      const search = page.getByRole("searchbox", { name: "جست‌وجوی نام کاربری" });
      await search.fill(fixture.subscription.username);
      await page.waitForURL(url => url.searchParams.get("q") === fixture.subscription.username);
      const detail = page.locator("main a[href*='/partner/subscriptions/" + fixture.subscription.id + "?']").filter({ visible: true });
      await detail.waitFor();
      await noOverflow();
      await detail.focus();
      await page.keyboard.press("Enter");
      await h1(page, "جزئیات اشتراک").waitFor();
      await noOverflow();
      await page.getByRole("link", { name: "بازگشت به اشتراک‌ها" }).click();
      await search.waitFor();
      assert.equal(await search.inputValue(), fixture.subscription.username);
      await detail.waitFor();
      assert.equal(new URL(page.url()).searchParams.get("q"), fixture.subscription.username);
      await page.goto(web + "/partner/subscriptions/new");
      await h1(page, "ساخت اشتراک").waitFor();
      await field(page, "محصول").selectOption(String(fixture.product.id));
      const longUsername = "a_" + "0123456789".repeat(3);
      const username = field(page, "نام کاربری");
      await username.fill(longUsername);
      await noOverflow();
      await page.getByRole("main").getByRole("link", { name: "اشتراک‌ها", exact: true }).click();
      const warning = page.getByRole("dialog", { name: "خروج از فرم" });
      await warning.getByRole("button", { name: "ادامهٔ ویرایش" }).click();
      assert.equal(await username.inputValue(), longUsername);
      await page.getByRole("main").getByRole("link", { name: "اشتراک‌ها", exact: true }).click();
      await warning.getByRole("button", { name: "خروج بدون ذخیره" }).click();
      await page.waitForURL(web + "/partner/subscriptions");
      assert.notEqual(await page.evaluate(() => getComputedStyle(document.body).overflow), "hidden");
      await page.goto(web + "/partner/ledger");
      await h1(page, "تاریخچه تراکنش‌ها").waitFor();
      await page.getByText("ساخت اشتراک", { exact: true }).filter({ visible: true }).first().waitFor();
      await noOverflow();
      checks.push({ width, height, queryReturn: true, keyboardDetailLink: true, noViewportOverflow: true });
    } finally {
      await ctx.close();
    }
  }
  await fs.writeFile(path.join(output, "responsive-results.json"), JSON.stringify(checks, null, 2));
  console.log(JSON.stringify({ responsiveViewports: checks.length }));
}

async function run() {
  await fs.mkdir(output, { recursive: true });
  const sizes = [["desktop", { width: 1384, height: 910 }], ["mobile", { width: 386, height: 890 }]];
  const browser = await chromium.launch({ ...(process.env.CHROME ? { executablePath: process.env.CHROME } : {}), headless: true });
  try {
    if (process.env.PC_VISUAL_RESPONSIVE === "1") {
      await inspectResponsive(browser, await seed());
    } else if (process.env.PC_VISUAL_ONLY_DESIGN === "1") {
      for (const [size, viewport] of sizes) await inspectDesign(browser, size, viewport);
    } else {
      const fixture = await seed();
      const states = await Promise.allSettled(sizes.map(async ([size, viewport]) => {
        await exercise(browser, fixture, size, viewport);
        await inspectDesign(browser, size, viewport);
      }));
      for (const state of states) if (state.status === "rejected") console.error(state.reason.message);
      if (states.some(state => state.status === "rejected")) process.exitCode = 1;
    }
  } finally {
    await browser.close();
    result.sort((a, b) => a.id - b.id || a.size.localeCompare(b.size));
    if (process.env.PC_VISUAL_RESPONSIVE !== "1") await fs.writeFile(path.join(output, process.env.PC_VISUAL_ONLY_DESIGN === "1" ? "design-results.json" : "results.json"), JSON.stringify(result, null, 2));
    console.log(JSON.stringify({ screenshots: result.filter(item => item.file).length, failures: result.filter(item => item.error) }));
    if (result.some(item => item.error)) process.exitCode = 1;
  }
}

run().catch(error => { console.error(error.stack); process.exitCode = 1; });
