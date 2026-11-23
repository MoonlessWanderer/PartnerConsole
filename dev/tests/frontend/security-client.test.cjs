const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

function fixture(fetch, timers = { setTimeout, clearTimeout }) {
  const storage = new Map([["pp_token", "synthetic-token"], ["pp_role", "partner"], ["pp_username", "shop1"]]);
  const localStorage = { getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) };
  const redirects = [];
  const states = [false, true, "shop1", true, "", false];
  let index = 0;
  const runtime = { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
  const modules = {};
  const context = { process: { env: {} }, window: { location: {} }, localStorage, fetch, AbortController, ...timers,
    sessionStorage: { getItem: () => "/gate/k3p8n2" }, exports: {}, require: (name) => modules[name] };
  function compile(file) {
    context.exports = {};
    const source = fs.readFileSync(path.join(__dirname, "../../..", file), "utf8");
    const js = ts.transpileModule(source, { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX,
    } }).outputText;
    vm.runInNewContext(`(function () {\n${js}\n})();`, context, { filename: file });
    return context.exports;
  }
  const api = compile("lib/api.ts");
  modules["@/lib/api"] = api;
  modules["react/jsx-runtime"] = runtime;
  modules["next/link"] = { default: "Link" };
  modules["next/navigation"] = { usePathname: () => "/partner", useRouter: () => ({ replace: (to) => redirects.push(to) }) };
  modules["react"] = { useEffect() {}, useId: () => `test-id-${index++}`, useRef(initial) {
    const slot = index++;
    return states[slot] ??= { current: initial };
  }, useState(initial) {
    const slot = index++;
    if (states[slot] === undefined) states[slot] = initial;
    return [states[slot], (value) => { states[slot] = typeof value === "function" ? value(states[slot]) : value; }];
  } };
  modules["@/components/ui"] = { Banner: "Banner" };
  const { AppShell } = compile("components/Shell.tsx");
  function render() {
    index = 0;
    return AppShell({ role: "partner", items: [], loginPath: "/", children: "content" });
  }
  function nodes(tree, predicate) {
    if (!tree || typeof tree !== "object") return [];
    if (Array.isArray(tree)) return tree.flatMap((child) => nodes(child, predicate));
    return [...(predicate(tree) ? [tree] : []), ...nodes(tree.props?.children, predicate)];
  }
  function logoutButton() {
    return nodes(render(), (node) => node.type === "button" && ["خروج", "در حال خروج…"].includes(node.props.children))[0];
  }
  return { api, storage, redirects, render, logoutButton, nodes, states, context, compile, modules,
    resetRender: () => { index = 0; } };
}

test("logout waits for server revocation before clearing and redirecting", async () => {
  let resolve;
  let sent;
  const f = fixture((url, options) => {
    sent = { url, options };
    return new Promise((done) => { resolve = done; });
  });
  const pending = f.logoutButton().props.onClick();
  assert.equal(f.storage.get("pp_token"), "synthetic-token");
  assert.equal(f.redirects.length, 0);
  assert.equal(f.logoutButton().props.disabled, true);
  assert.equal(sent.options.method, "POST");
  assert.equal(sent.options.headers.Authorization, "Bearer synthetic-token");
  assert.ok(sent.url.endsWith("/api/partner/auth/logout"));
  resolve({ ok: true, status: 200 });
  await pending;
  assert.equal(f.storage.has("pp_token"), false);
  assert.deepEqual(f.redirects, ["/"]);
});

for (const failure of ["network", 429, 500]) {
  test(`logout retains session and allows retry after ${failure}`, async () => {
    let attempts = 0;
    const f = fixture(async () => {
      attempts += 1;
      if (attempts > 1) return { ok: true, status: 200 };
      if (failure === "network") throw Error("offline");
      return { ok: false, status: failure };
    });
    await f.logoutButton().props.onClick();
    assert.equal(f.storage.get("pp_token"), "synthetic-token");
    assert.equal(f.redirects.length, 0);
    assert.equal(f.logoutButton().props.disabled, false);
    assert.equal(f.nodes(f.render(), (node) => node.type === "Banner").length, 1);
    await f.logoutButton().props.onClick();
    assert.equal(f.storage.has("pp_token"), false);
  });
}

for (const status of [401, 403]) {
  test(`already-invalid session can exit after ${status}`, async () => {
    const f = fixture(async () => ({ ok: false, status }));
    await f.logoutButton().props.onClick();
    assert.equal(f.storage.has("pp_token"), false);
    assert.deepEqual(f.redirects, ["/"]);
  });
}

test("normal API unauthorized redirect behavior is preserved", async () => {
  const f = fixture(async () => ({ ok: false, status: 401, json: async () => ({ detail: "invalid" }) }));
  await assert.rejects(f.api.api("/partner/me"));
  assert.equal(f.storage.has("pp_token"), false);
  assert.equal(f.context.window.location.href, "/");
});

test("Next.js resolves the patched PostCSS dependency", () => {
  const nextFolder = path.dirname(require.resolve("next/package.json"));
  const resolved = require.resolve("postcss/package.json", { paths: [nextFolder] });
  const version = require(resolved).version.split(".").map(Number);
  assert.equal(version[0], 8);
  assert.ok(version[1] > 5 || (version[1] === 5 && version[2] >= 33));
});

test("an unreadable success response cannot confirm a mutation", async () => {
  const f = fixture(async () => ({ ok: true, status: 200, json: async () => { throw Error("raw payload"); } }));
  await assert.rejects(f.api.api("/partner/subscriptions/1/delete", { method: "POST" }),
    (error) => error.status === 502 && error.message.includes("وضعیت عملیات"));
});

test("an unstructured upstream failure never displays its raw message", async () => {
  const f = fixture(async () => ({ ok: false, status: 502, json: async () => ({ detail: "synthetic-technical-detail" }) }));
  await assert.rejects(f.api.api("/partner/me"),
    (error) => error.status === 502 && !error.message.includes("synthetic-technical-detail"));
});

test("a durable operation message from the API is preserved", async () => {
  const f = fixture(async () => ({
    ok: false, status: 503, json: async () => ({ code: "operation_status", detail: "عملیات شماره ۴۲ ذخیره شد" }),
  }));
  await assert.rejects(f.api.api("/partner/subscriptions", { method: "POST" }), /شماره ۴۲/);
});

test("deadline includes response body and makes logout retryable without clearing the token", async () => {
  const shortTimers = { setTimeout: (callback) => setTimeout(callback, 5), clearTimeout };
  let attempts = 0;
  let signal;
  const f = fixture(async (_url, options) => {
    signal = options.signal;
    attempts++;
    if (attempts === 1) return { ok: true, status: 200, json: () => new Promise(() => {}) };
    if (attempts === 2) return new Promise(() => {});
    return { ok: true, status: 200 };
  }, shortTimers);
  await assert.rejects(f.api.api("/partner/me"), /مهلت/);
  assert.equal(signal.aborted, true);
  await f.logoutButton().props.onClick();
  assert.equal(signal.aborted, true);
  assert.equal(f.logoutButton().props.disabled, false);
  assert.equal(f.storage.get("pp_token"), "synthetic-token");
  await f.logoutButton().props.onClick();
  assert.equal(f.storage.has("pp_token"), false);
});

test("admin balance lookup reports a network failure in the page banner", async () => {
  const f = fixture(async () => { throw Error("synthetic-raw-network-error"); });
  f.states.splice(0, f.states.length, [{ id: 1, username: "shop", balance_toman: 0, banned: false }]);
  f.modules["@/lib/format"] = { formatToman: String };
  Object.assign(f.modules["@/components/ui"], { Button: "Button", Field: "Field", TableWrap: "TableWrap" });
  f.modules["@/components/Modal"] = { Modal: "Modal" };
  const PartnersPage = f.compile("app/admin/partners/page.tsx").default;
  const render = () => { f.resetRender(); return PartnersPage(); };
  const [balance] = f.nodes(render(), (n) => n.type === "Button" && n.props.children === "موجودی");
  await balance.props.onClick();
  const [banner] = f.nodes(render(), (n) => n.type === "Banner");
  assert.match(banner.props.children, /ارتباط با سرور/);
  assert.doesNotMatch(banner.props.children, /synthetic-raw-network-error/);
});

test("wallet adjustment submits once and retains the partner identity from a partial API response", async () => {
  let requests = 0;
  let finish;
  const f = fixture(async (url) => {
    if (url.endsWith("/adjust")) {
      requests++;
      return new Promise((resolve) => { finish = resolve; });
    }
    return { ok: true, json: async () => ({ items: [] }) };
  });
  f.states.splice(0, f.states.length, [], "", false, "", "",
    { id: 7, username: "shop7", balance_toman: 10, banned: true, credits: [] }, "toman", "4", 0);
  f.modules["@/lib/format"] = { formatToman: String };
  Object.assign(f.modules["@/components/ui"], { Button: "Button", Field: "Field", TableWrap: "TableWrap" });
  f.modules["@/components/Modal"] = { Modal: "Modal" };
  const PartnersPage = f.compile("app/admin/partners/page.tsx").default;
  const render = () => { f.resetRender(); return PartnersPage(); };
  const [form] = f.nodes(render(), (n) => n.type === "form");
  const event = { preventDefault() {} };
  const first = form.props.onSubmit(event);
  await form.props.onSubmit(event);
  assert.equal(requests, 1);
  const [pending] = f.nodes(render(), (n) => n.type === "Button" && n.props.type === "submit");
  assert.equal(pending.props.disabled, true);
  finish({ ok: true, json: async () => ({ id: 7, balance_toman: 14, credits: [] }) });
  await first;
  assert.equal(f.states[5].username, "shop7");
  assert.equal(f.states[5].banned, true);
  assert.equal(f.states[5].balance_toman, 14);
});

test("modal names its dialog and places the error inside the active dialog", () => {
  const f = fixture(async () => { throw Error("unexpected fetch"); });
  const { Modal } = f.compile("components/Modal.tsx");
  const dialog = Modal({ title: "تعدیل", error: "موجودی کافی نیست", onClose() {}, children: null });
  assert.equal(dialog.type, "dialog");
  const [title] = f.nodes(dialog, (n) => n.type === "h2");
  assert.equal(dialog.props["aria-labelledby"], title.props.id);
  const [error] = f.nodes(dialog, (n) => n.type === "Banner");
  assert.equal(error.props.children, "موجودی کافی نیست");
});
