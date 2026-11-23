const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

function load() {
  const source = fs.readFileSync(path.join(__dirname, "../../../lib/attention.ts"), "utf8");
  const js = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
  } }).outputText;
  const context = { exports: {} };
  vm.runInNewContext(`(function () {\n${js}\n})();`, context, { filename: "lib/attention.ts" });
  return context.exports;
}

const attention = load();

function op(extra = {}) {
  return { id: 7, status: "attention", kind: "reset_traffic", busy: false, retry_action: "resend_reset",
    shortfall_gb: null, shortfall_days: null, ...extra };
}

const ready = { decision: "close", note: "پنل بررسی شد", acceptShortfall: false, checkedPanel: true };

test("only attention rows that are not being processed offer decisions", () => {
  const decisions = (row) => [...attention.decisions(row)];
  assert.deepEqual(decisions(op()), ["retry", "close"]);
  assert.deepEqual(decisions(op({ retry_action: null })), ["close"]);
  for (const status of ["pending", "failed", "completed", "closed"]) {
    assert.deepEqual(decisions(op({ status })), []);
  }
  assert.deepEqual(decisions(op({ busy: true })), []);
  assert.match(attention.formProblem(op({ busy: true }), ready), /در حال پردازش/);
  assert.match(attention.formProblem(op({ status: "closed" }), ready), /وضعیت بررسی نیست/);
});

test("a note, an allowed decision and the panel check are mandatory", () => {
  assert.equal(attention.formProblem(op(), ready), null);
  assert.match(attention.formProblem(op(), { ...ready, decision: null }), /تصمیم/);
  assert.match(attention.formProblem(op({ retry_action: null }), { ...ready, decision: "retry" }), /تصمیم/);
  assert.match(attention.formProblem(op(), { ...ready, note: "   ab  " }), /توضیح/);
  assert.match(attention.formProblem(op(), { ...ready, note: "x".repeat(501) }), /حداکثر/);
  assert.match(attention.formProblem(op(), { ...ready, checkedPanel: false }), /بررسی وضعیت پنل/);
  assert.throws(() => attention.resolveBody(op(), { ...ready, note: "" }));
});

test("closing with a shortfall requires accepting it explicitly", () => {
  const gap = op({ kind: "decrease", retry_action: "collect_shortfall", shortfall_gb: "1.5", shortfall_days: 0 });
  assert.match(attention.formProblem(gap, ready), /اختلاف مالی/);
  assert.deepEqual({ ...attention.resolveBody(gap, { ...ready, acceptShortfall: true }) },
    { decision: "close", note: "پنل بررسی شد", accept_shortfall: true, seen_updated_at: undefined });
  // Retrying never carries an acceptance, even if the box was ticked before switching.
  assert.deepEqual({ ...attention.resolveBody(gap, { ...ready, decision: "retry", acceptShortfall: true }) },
    { decision: "retry", note: "پنل بررسی شد", accept_shortfall: false, seen_updated_at: undefined });
  assert.equal(attention.resolveBody(op(), ready).accept_shortfall, false);
});

test("every decision explains its effect and its risk in Persian", () => {
  const cases = [
    [op(), "retry", /ریست ترافیک یک بار دیگر/, /دوباره صفر/],
    [op({ kind: "increase", retry_action: "resend_change" }), "retry", /هزینهٔ جدیدی گرفته نمی‌شود/, /جایگزین/],
    [op({ kind: "decrease", retry_action: "collect_shortfall", shortfall_gb: "1" }), "retry", /پس گرفته شود/, /بخشی/],
    [op(), "close", /بدون تغییر موجودی/, /همان‌طور که هست/],
    [op({ shortfall_days: 2 }), "close", /پذیرفته‌شده/, /دیگر به‌طور خودکار/],
  ];
  for (const [row, decision, summary, warning] of cases) {
    const text = attention.explain(row, decision);
    assert.match(text.summary, summary);
    assert.match(text.warning, warning);
  }
});

test("decision labels never read like the modal's own close button", () => {
  for (const retry_action of ["resend_reset", "resend_change", "collect_shortfall", null]) {
    for (const decision of ["retry", "close"]) {
      const label = attention.decisionLabel(op({ retry_action }), decision);
      assert.notEqual(label, "بستن");
      assert.notEqual(label, "بستن پنجره");
    }
  }
  assert.match(attention.decisionLabel(op(), "close"), /بدون جابه‌جایی مالی/);
  assert.match(attention.decisionLabel(op(), "retry"), /ریست/);
});

test("every decision states its effect on the wallet, the ledger and the panel", () => {
  const reset = attention.effects(op({ charge_toman: 1000 }), "retry");
  assert.match(reset.wallet, /۱,۰۰۰ تومان قبلاً کسر شده و دوباره کسر نمی‌شود/);
  assert.match(reset.ledger, /ردیف جدیدی ثبت نمی‌شود/);
  assert.match(reset.panel, /یک درخواست ریست/);
  const change = attention.effects(op({ kind: "increase", retry_action: "resend_change", new_cap_gb: "23" }), "retry");
  assert.match(change.panel, /سقف را ۲۳ گیگ/);
  const del = attention.effects(op({ kind: "delete", retry_action: "resend_change" }), "retry");
  assert.match(del.panel, /حذف همین کاربر/);
  const collect = attention.effects(op({ kind: "decrease", retry_action: "collect_shortfall", shortfall_gb: "1.5", shortfall_days: 2 }), "retry");
  assert.match(collect.wallet, /۱\.۵۰ گیگ و ۲ روز/);
  assert.match(collect.ledger, /ردیف منفی/);
  assert.match(collect.panel, /هیچ درخواستی/);
  const close = attention.effects(op({ shortfall_gb: "1", shortfall_days: 0 }), "close");
  assert.match(close.wallet, /بدون تغییر. مانده ۱ گیگ/);
  assert.match(close.panel, /هیچ درخواستی/);
});

test("decisions the panel state rules out are hidden and explained", () => {
  const decisions = (row) => [...attention.decisions(row)];
  const missing = op({ kind: "increase", retry_action: "resend_change",
    decision_blocks: { retry: "کاربر این عملیات روی پنل وجود ندارد؛ ارسال دوباره به پنل ممکن نیست." } });
  assert.deepEqual(decisions(missing), ["close"]);
  assert.deepEqual([...attention.blockedDecisions(missing)].map((item) => item.decision), ["retry"]);
  assert.match(attention.formProblem(missing, { ...ready, decision: "retry" }), /تصمیم/);
  const del = op({ kind: "delete", retry_action: "resend_change", decision_blocks: { close: "بستن ناسازگار است" } });
  assert.deepEqual(decisions(del), ["retry"]);
  assert.match(attention.formProblem(del, ready), /تصمیم/);
  assert.deepEqual([...attention.blockedDecisions(op())], []);
  assert.match(attention.explain(del, "retry").summary, /حذف کاربر/);
});

test("retry warnings say the old value or lower usage proves nothing", () => {
  assert.match(attention.explain(op(), "retry").warning, /ریست زمان‌بندی‌شدهٔ پلن/);
  assert.match(attention.explain(op({ kind: "increase", retry_action: "resend_change" }), "retry").warning,
    /هم‌زمان/);
});

test("technical error texts are never shown raw", () => {
  assert.equal(attention.shownError("changed by another worker"), attention.TECHNICAL_ERROR);
  assert.equal(attention.shownError("عملیات شماره ۲ نیاز به بررسی دارد"), "عملیات شماره ۲ نیاز به بررسی دارد");
  assert.equal(attention.shownError(null), null);
});

test("the decision carries the version the admin saw", () => {
  const seen = "2026-11-20T10:00:00+00:00";
  assert.equal(attention.resolveBody(op({ updated_at: seen }), ready).seen_updated_at, seen);
});
