"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { faDigits, formatGb, formatInt, formatToman } from "@/lib/format";
import {
  Decision,
  KIND_LABEL,
  blockedDecisions,
  NOTE_MAX,
  OPERATION_STATUS_LABEL,
  Operation,
  OperationDetail,
  OperationStatus,
  ResolveForm,
  decisionLabel,
  decisions,
  effects,
  explain,
  formProblem,
  hasShortfall,
  resolveBody,
  shownError,
  statusTone,
} from "@/lib/attention";
import { Badge, Banner, Button, Field, TableWrap, inputClass } from "@/components/ui";
import { Modal } from "@/components/Modal";

const FILTERS: { value: OperationStatus | ""; label: string }[] = [
  { value: "attention", label: "نیازمند بررسی" },
  { value: "pending", label: "در انتظار بازیابی" },
  { value: "failed", label: "ناموفق" },
  { value: "closed", label: "بسته‌شده" },
  { value: "completed", label: "انجام‌شده" },
  { value: "", label: "همه" },
];

const EMPTY_FORM: ResolveForm = { decision: null, note: "", acceptShortfall: false, checkedPanel: false };

function moment(value: string | null): string {
  if (!value) return "—";
  return faDigits(new Date(value).toLocaleString("fa-IR-u-nu-latn", { dateStyle: "short", timeStyle: "short" }));
}

function PanelStateView({ op }: { op: OperationDetail }) {
  const state = op.panel_state;
  if (!state.available) return <Banner tone="warn">وضعیت پنل: {state.reason}</Banner>;
  if (!state.exists) return <div>روی پنل: کاربر وجود ندارد.</div>;
  if (!state.owned) return <Banner tone="warn">کاربر این شناسه روی پنل متعلق به این اشتراک نیست؛ جزئیات آن نمایش داده نمی‌شود.</Banner>;
  return (
    <div className="rounded-xl bg-slate-50 p-3">
      <div className="font-medium">وضعیت فعلی پنل (فقط خواندنی)</div>
      <div>سقف: {formatGb(state.transfer_enable_gb)} · مصرف: {formatGb(state.used_gb)}</div>
      <div>انقضا: {state.expired_jalali ? faDigits(state.expired_jalali) : "—"} · {state.banned ? "مسدود" : "فعال"}</div>
    </div>
  );
}

function EffectsView({ op, decision }: { op: Operation; decision: Decision }) {
  const result = effects(op, decision);
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 rounded-xl border border-slate-200 p-3">
      <dt className="font-medium">کیف پول و اعتبار</dt><dd>{result.wallet}</dd>
      <dt className="font-medium">دفتر حساب</dt><dd>{result.ledger}</dd>
      <dt className="font-medium">پنل</dt><dd>{result.panel}</dd>
    </dl>
  );
}

function Amounts({ op }: { op: Operation }) {
  return (
    <>
      <div>نوع: {KIND_LABEL[op.kind] || op.kind} · کاربر: {op.username || "—"}</div>
      <div>همکار #{faDigits(op.partner_id)} · محصول #{faDigits(op.product_id)}</div>
      {op.charge_toman ? <div>هزینهٔ گرفته‌شده: {formatToman(op.charge_toman)}</div> : null}
      {Number(op.charge_gb_from_credit || 0) > 0 ? <div>از اعتبار گیگ: {formatGb(op.charge_gb_from_credit)}</div> : null}
      {Number(op.charge_days_from_credit || 0) > 0 ? <div>از اعتبار روز: {formatInt(op.charge_days_from_credit)} روز</div> : null}
      {op.gb_back ? <div>اعتبار برگشتی: {formatGb(op.gb_back)} و {formatInt(op.days_back)} روز</div> : null}
      {op.new_cap_gb ? <div>سقف: {op.old_cap_gb ? `${formatGb(op.old_cap_gb)} ← ` : ""}{formatGb(op.new_cap_gb)}</div> : null}
      {hasShortfall(op) ? (
        <Banner tone="warn">اختلاف مالی باقی‌مانده: {formatGb(op.shortfall_gb)} و {formatInt(op.shortfall_days)} روز</Banner>
      ) : null}
      <div>ارسال به پنل: {op.panel_applied ? "اعمال‌شده" : op.panel_sent ? "فرستاده شده، نتیجه نامعلوم" : "فرستاده نشده"}</div>
      {op.last_error ? <Banner>{shownError(op.last_error)}</Banner> : null}
      {op.resolution ? <div>تصمیم قبلی: {op.resolution === "close" ? "بستن عملیات" : "تلاش دوباره"} — {op.resolution_note || ""}</div> : null}
    </>
  );
}

export default function AdminOperations() {
  const [filter, setFilter] = useState<OperationStatus | "">("attention");
  const [items, setItems] = useState<Operation[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [current, setCurrent] = useState<OperationDetail | null>(null);
  const [form, setForm] = useState<ResolveForm>(EMPTY_FORM);
  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const inFlight = useRef(false);

  async function load(status = filter) {
    const query = status ? `?status=${status}` : "";
    const data = await api<{ items: Operation[] }>(`/admin/operations${query}`);
    setItems(data.items);
  }
  useEffect(() => {
    load(filter).catch((err) => setError(err.message));
  }, [filter]);

  async function open(id: number) {
    setError("");
    setNotice("");
    setForm(EMPTY_FORM);
    setConfirming(false);
    try {
      setCurrent(await api<OperationDetail>(`/admin/operations/${id}`));
    } catch (err) {
      setError(err instanceof Error ? err.message : "خواندن عملیات ناموفق بود");
    }
  }

  function close() {
    if (inFlight.current) return;
    setCurrent(null);
    setConfirming(false);
    setError("");
  }

  async function submit() {
    if (!current || inFlight.current) return;
    const problem = formProblem(current, form);
    if (problem) {
      setError(problem);
      setConfirming(false);
      return;
    }
    inFlight.current = true;
    setSubmitting(true);
    setError("");
    try {
      const result = await api<Operation>(`/admin/operations/${current.id}/resolve`, {
        method: "POST",
        body: resolveBody(current, form),
      });
      setNotice(`عملیات #${faDigits(result.id)}: ${OPERATION_STATUS_LABEL[result.status] || result.status}`);
      setCurrent(null);
      setConfirming(false);
      await load();
    } catch (err) {
      const message = err instanceof Error ? err.message : "ثبت تصمیم ناموفق بود";
      setConfirming(false);
      // Reload the current state first: opening clears the error, and this one must stay visible.
      await open(current.id).catch(() => undefined);
      setError(message);
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }

  const options = current ? decisions(current) : [];
  const blocked = current ? blockedDecisions(current) : [];
  const problem = current ? formProblem(current, form) : null;
  const chosen = current && form.decision ? explain(current, form.decision) : null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2" role="group" aria-label="فیلتر وضعیت">
        {FILTERS.map((item) => (
          <Button key={item.value || "all"} tone={filter === item.value ? "primary" : "ghost"} onClick={() => setFilter(item.value)}>
            {item.label}
          </Button>
        ))}
      </div>
      {error && !current ? <Banner>{error}</Banner> : null}
      {notice ? <Banner tone="ok">{notice}</Banner> : null}
      <TableWrap>
        <table>
          <thead>
            <tr><th>شماره</th><th>نوع</th><th>کاربر</th><th>وضعیت</th><th>مبلغ</th><th>آخرین خطا</th><th>به‌روزرسانی</th><th></th></tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <tr key={row.id}>
                <td>{faDigits(row.id)}</td>
                <td>{KIND_LABEL[row.kind] || row.kind}</td>
                <td>{row.username || "—"}</td>
                <td><Badge tone={statusTone(row.status)}>{OPERATION_STATUS_LABEL[row.status] || row.status}</Badge></td>
                <td>{row.charge_toman ? formatToman(row.charge_toman) : "—"}</td>
                <td className="max-w-xs truncate" title={shownError(row.last_error) || ""}>{shownError(row.last_error) || "—"}</td>
                <td>{moment(row.updated_at)}</td>
                <td><Button tone="ghost" onClick={() => open(row.id)}>جزئیات</Button></td>
              </tr>
            ))}
            {items.length === 0 ? <tr><td colSpan={8}>عملیاتی نیست.</td></tr> : null}
          </tbody>
        </table>
      </TableWrap>
      {current ? (
        <Modal title={`عملیات #${faDigits(current.id)}`} error={error} onClose={close}>
          <div className="space-y-2 text-sm leading-7">
            <div><Badge tone={statusTone(current.status)}>{OPERATION_STATUS_LABEL[current.status] || current.status}</Badge></div>
            <Amounts op={current} />
            <PanelStateView op={current} />
            {current.busy ? <Banner tone="warn">این عملیات همین حالا در حال پردازش است؛ کمی بعد دوباره باز کنید.</Banner> : null}
            {!confirming && current.status === "attention" ? blocked.map((item) => (
              <Banner key={item.decision} tone="warn">«{decisionLabel(current, item.decision)}» ممکن نیست: {item.reason}</Banner>
            )) : null}
            {options.length && !confirming ? (
              <form className="space-y-3 border-t border-slate-200 pt-3" onSubmit={(e) => { e.preventDefault(); if (!problem) setConfirming(true); }}>
                <Field label="تصمیم">
                  <div className="flex flex-wrap gap-2">
                    {options.map((decision: Decision) => (
                      <Button key={decision} tone={form.decision === decision ? (decision === "retry" ? "primary" : "danger") : "ghost"}
                        onClick={() => setForm({ ...form, decision, acceptShortfall: false, checkedPanel: false })}>
                        {decisionLabel(current, decision)}
                      </Button>
                    ))}
                  </div>
                </Field>
                {chosen ? (
                  <>
                    <div>{chosen.summary}</div>
                    <EffectsView op={current} decision={form.decision as Decision} />
                    <Banner tone="warn">{chosen.warning}</Banner>
                  </>
                ) : null}
                <Field label="توضیح تصمیم (الزامی)">
                  <textarea className={`${inputClass} min-h-24 py-2`} maxLength={NOTE_MAX} value={form.note}
                    onChange={(e) => setForm({ ...form, note: e.target.value })} />
                </Field>
                {form.decision === "close" && hasShortfall(current) ? (
                  <label className="flex items-start gap-2">
                    <input type="checkbox" className="mt-1.5" checked={form.acceptShortfall}
                      onChange={(e) => setForm({ ...form, acceptShortfall: e.target.checked })} />
                    <span>اختلاف {formatGb(current.shortfall_gb)} و {formatInt(current.shortfall_days)} روز را می‌پذیرم؛ این مقدار دیگر پس گرفته نمی‌شود.</span>
                  </label>
                ) : null}
                <label className="flex items-start gap-2">
                  <input type="checkbox" className="mt-1.5" checked={form.checkedPanel}
                    onChange={(e) => setForm({ ...form, checkedPanel: e.target.checked })} />
                  <span>وضعیت پنل را بررسی کردم و پیامد این تصمیم را می‌دانم.</span>
                </label>
                {problem && form.decision ? <div className="text-slate-500">{problem}</div> : null}
                <Button type="submit" disabled={Boolean(problem)}>ادامه</Button>
              </form>
            ) : null}
            {confirming && chosen ? (
              <div className="space-y-3 border-t border-slate-200 pt-3">
                <div className="font-bold">تأیید نهایی: {decisionLabel(current, form.decision as Decision)}</div>
                <div>{chosen.summary}</div>
                <EffectsView op={current} decision={form.decision as Decision} />
                <Banner tone="warn">{chosen.warning}</Banner>
                <div className="break-words">توضیح: {form.note.trim()}</div>
                <div className="flex gap-2">
                  <Button tone={form.decision === "close" ? "danger" : "primary"} disabled={submitting} onClick={submit}>
                    {submitting ? "در حال ثبت…" : "تأیید و ثبت"}
                  </Button>
                  <Button tone="ghost" disabled={submitting} onClick={() => setConfirming(false)}>بازگشت</Button>
                </div>
              </div>
            ) : null}
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
