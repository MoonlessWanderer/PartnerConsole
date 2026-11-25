"use strict";
// Opens a built sign-in page in a headless Chromium browser, submits fixture
// credentials and reports the API request the page tried to send. Requests are
// stopped in the page and at the network layer, so nothing leaves the browser.
// Usage: node login-origin-probe.cjs BROWSER PAGE_URL
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { performance } = require("node:perf_hooks");

const [browser, pageUrl] = process.argv.slice(2);
if (!browser || !pageUrl) {
  console.error("usage: node login-origin-probe.cjs BROWSER PAGE_URL");
  process.exit(2);
}
const pageOrigin = new URL(pageUrl).origin;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const capture = `(() => {
  window.__pcRequests = [];
  const record = (url, method) => {
    window.__pcRequests.push({ url: new URL(String(url), location.href).href, method: String(method || "GET").toUpperCase() });
  };
  window.fetch = function (input, init) {
    const url = typeof input === "string" || input instanceof URL ? input : input.url;
    record(url, (init && init.method) || (input && input.method));
    return Promise.reject(new TypeError("stopped by the login origin probe"));
  };
  XMLHttpRequest.prototype.open = function (method, url) {
    record(url, method);
    throw new TypeError("stopped by the login origin probe");
  };
})();`;

const submit = `(() => {
  const inputs = [...document.querySelectorAll("input")];
  const button = [...document.querySelectorAll("form button")].find(control => control.type === "submit");
  if (inputs.length < 2 || !button) return "form-missing";
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  for (const [input, value] of [[inputs[0], "fixture-user"], [inputs[1], "fixture-password-123"]]) {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }
  button.click();
  return "submitted";
})()`;

const hydrated = `(() => {
  const button = [...document.querySelectorAll("form button")].find(control => control.type === "submit");
  return !!button && Object.keys(button).some((key) => key.startsWith("__react"));
})()`;

async function main() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "pc-login-probe-"));
  const args = [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--disable-extensions", "--remote-debugging-pipe", `--user-data-dir=${profile}`, "about:blank",
  ];
  const startedAt = new Date().toISOString();
  const started = performance.now();
  const child = spawn(browser, args, { stdio: ["ignore", "pipe", "pipe", "pipe", "pipe"] });
  const pending = new Map();
  const listeners = new Map();
  let nextId = 0;
  let disconnected;
  let readyMs = null;
  let sessionId;
  let stdout = "", stderr = "";
  child.stdout.setEncoding("utf8").on("data", data => { stdout = (stdout + data).slice(-8192); });
  child.stderr.setEncoding("utf8").on("data", data => { stderr = (stderr + data).slice(-8192); });
  const closed = new Promise(resolve => child.once("close", resolve));
  const disconnect = (error) => {
    disconnected ??= error;
    for (const { finish } of pending.values()) finish(disconnected);
  };
  child.on("error", disconnect);
  child.on("exit", (code, signal) => disconnect(new Error(`Chrome exited (code=${code}, signal=${signal})`)));
  const input = child.stdio[3], output = child.stdio[4];
  input.on("error", disconnect);
  output.on("error", disconnect);
  output.on("end", () => disconnect(new Error("Chrome closed the DevTools pipe")));
  let buffer = "";
  output.setEncoding("utf8").on("data", data => {
    buffer += data;
    let end;
    while ((end = buffer.indexOf("\0")) !== -1) {
      const frame = buffer.slice(0, end);
      buffer = buffer.slice(end + 1);
      let message;
      try { message = JSON.parse(frame); }
      catch (error) { disconnect(error); return; }
      const request = pending.get(message.id);
      if (request) {
        request.finish(message.error ? new Error(message.error.message) : null, message.result);
      } else if (message.sessionId === sessionId && listeners.has(message.method)) {
        listeners.get(message.method)(message.params);
      }
    }
  });
  const send = (method, params = {}, session, timeout = 15000) => new Promise((resolve, reject) => {
    if (disconnected) { reject(disconnected); return; }
    const id = ++nextId;
    const finish = (error, result) => {
      clearTimeout(timer);
      pending.delete(id);
      if (error) reject(error);
      else resolve(result);
    };
    const timer = setTimeout(() => finish(new Error(`DevTools timed out waiting for ${method} after ${timeout} ms`)), timeout);
    pending.set(id, { finish });
    input.write(JSON.stringify({ id, method, params, ...(session && { sessionId: session }) }) + "\0");
  });
  try {
    // Allow for a cold Chrome start on a shared CI runner.
    const version = await send("Browser.getVersion", {}, undefined, 60_000);
    readyMs = Math.round(performance.now() - started);
    console.error(`login origin probe: ${version.product}, DevTools pipe ready in ${readyMs} ms`);
    const { targetId } = await send("Target.createTarget", { url: "about:blank" });
    ({ sessionId } = await send("Target.attachToTarget", { targetId, flatten: true }));
    const page = (method, params) => send(method, params, sessionId);
    const evaluate = async (expression) => {
      const { result, exceptionDetails } = await page("Runtime.evaluate", { expression, returnByValue: true });
      if (exceptionDetails) throw new Error(`sign-in browser evaluation failed: ${exceptionDetails.text}`);
      return result.value;
    };

    const blocked = [];
    listeners.set("Fetch.requestPaused", (params) => {
      const url = params.request.url;
      if (url.startsWith(pageOrigin + "/") || url === pageOrigin) {
        page("Fetch.continueRequest", { requestId: params.requestId }).catch(disconnect);
      } else {
        blocked.push({ url, method: params.request.method });
        page("Fetch.failRequest", { requestId: params.requestId, errorReason: "BlockedByClient" }).catch(disconnect);
      }
    });
    let loaded = false;
    listeners.set("Page.loadEventFired", () => { loaded = true; });

    await page("Page.enable");
    await page("Runtime.enable");
    await page("Fetch.enable", { patterns: [{ urlPattern: "*" }] });
    await page("Page.addScriptToEvaluateOnNewDocument", { source: capture });
    await page("Page.navigate", { url: pageUrl });
    for (let i = 0; i < 300 && !loaded; i += 1) await sleep(100);
    if (!loaded) throw new Error("the sign-in page did not load");
    let ready = false;
    for (let i = 0; i < 150 && !ready; i += 1) {
      ready = await evaluate(hydrated);
      if (!ready) await sleep(100);
    }
    if (!ready) throw new Error("the sign-in form did not hydrate");
    const state = await evaluate(submit);
    if (state !== "submitted") throw new Error(`the sign-in form could not be submitted: ${state}`);
    let requests = [];
    for (let i = 0; i < 50; i += 1) {
      requests = (await evaluate("window.__pcRequests")) || [];
      if (requests.some((item) => item.url.includes("/auth/login"))) break;
      await sleep(100);
    }
    const login = requests.filter((item) => item.url.includes("/auth/login"));
    process.stdout.write(JSON.stringify({ page: pageUrl, login, blocked }) + "\n");
  } catch (error) {
    const disk = location => {
      try {
        const { bavail, bsize } = fs.statfsSync(location);
        return bavail * bsize;
      } catch { return null; }
    };
    const profileLocks = fs.readdirSync(profile).filter(name => name.startsWith("Singleton"));
    let memoryEvents;
    try { memoryEvents = fs.readFileSync("/sys/fs/cgroup/memory.events", "utf8").trim(); } catch { /* optional outside Linux */ }
    console.error(JSON.stringify({
      browser, args, startedAt, elapsedMs: Math.round(performance.now() - started), readyMs,
      pid: child.pid, exitCode: child.exitCode, signal: child.signalCode, profileLocks,
      node: process.version, runnerImage: process.env.ImageOS, runnerVersion: process.env.ImageVersion,
      freeMemory: os.freemem(), totalMemory: os.totalmem(), memoryEvents,
      availableShm: disk("/dev/shm"), availableProfileDisk: disk(profile), stdout, stderr,
    }));
    throw error;
  } finally {
    child.kill();
    await Promise.race([closed, sleep(1000)]);
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGKILL");
      await Promise.race([closed, sleep(1000)]);
    }
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 3 });
  }
}

main().catch((error) => {
  console.error(`login origin probe: ${error.message}`);
  process.exit(1);
});
