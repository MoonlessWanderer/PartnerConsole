"use client";

import { DraftLeaveWarning } from "@/components/DraftLeaveWarning";

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useParams } from "next/navigation";
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
  refundLabel,
  resolveBody,
  shownError,
  statusTone,
} from "@/lib/attention";
import { Badge, Banner, Button, Field, inputClass } from "@/components/ui";
import { ActionLink, Facts, PageHead, ResponsiveTable } from "@/components/Design";

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
    <div className="pc-operation-panel-state">
      <div className="font-medium">وضعیت فعلی پنل (فقط خواندنی)</div>
      <div>سقف: {formatGb(state.transfer_enable_gb)} · مصرف: {formatGb(state.used_gb)}</div>
      <div>انقضا: {state.expired_jalali ? faDigits(state.expired_jalali) : "—"} · {state.banned ? "مسدود" : "فعال"}</div>
    </div>
  );
}

function EffectsView({ op, decision }: { op: Operation; decision: Decision }) {
  const result = effects(op, decision);
  return (
    <div className="pc-operation-effects">
      <Facts rows={[["کیف پول و اعتبار", result.wallet], ["دفتر حساب", result.ledger], ["پنل", result.panel]]}/>
    </div>
  );
}

function Amounts({ op }: { op: Operation }) {
  const rows: Array<[string, ReactNode]> = [
    ["نوع", KIND_LABEL[op.kind] || op.kind],
    ["کاربر", <bdi dir="ltr">{op.username || "—"}</bdi>],
    ["همکار · محصول", `#${faDigits(op.partner_id)} · #${faDigits(op.product_id)}`],
  ];
  if (op.charge_toman) rows.push(["هزینهٔ گرفته‌شده", formatToman(op.charge_toman)]);
  if (Number(op.charge_gb_from_credit || 0) > 0) rows.push(["از اعتبار گیگ", formatGb(op.charge_gb_from_credit)]);
  if (Number(op.charge_days_from_credit || 0) > 0) rows.push(["از اعتبار روز", `${formatInt(op.charge_days_from_credit)} روز`]);
  if (op.gb_back) rows.push([refundLabel(op.refund_state), `${formatGb(op.gb_back)} و ${formatInt(op.days_back)} روز`]);
  if (op.new_cap_gb) rows.push(["سقف", `${op.old_cap_gb ? `${formatGb(op.old_cap_gb)} ← ` : ""}${formatGb(op.new_cap_gb)}`]);
  rows.push(["ارسال به پنل", op.panel_applied ? "اعمال‌شده" : op.panel_sent ? "فرستاده شده، نتیجه نامعلوم" : "فرستاده نشده"]);

  return (
    <>
      <Facts rows={rows}/>
      {hasShortfall(op) ? (
        <Banner tone="warn">اختلاف مالی باقی‌مانده: {formatGb(op.shortfall_gb)} و {formatInt(op.shortfall_days)} روز</Banner>
      ) : null}
      {op.last_error ? <Banner>{shownError(op.last_error)}</Banner> : null}
      {op.resolution ? <div>تصمیم قبلی: {op.resolution === "close" ? "بستن عملیات" : "تلاش دوباره"} — {op.resolution_note || ""}</div> : null}
    </>
  );
}

const root = "/admin/operations";

export default function AdminOperations() {
  const {id}=useParams<{id?:string}>();
  const [resolving,setResolving]=useState(false);
  const [loaded,setLoaded]=useState(false);
  const [filter, setFilter] = useState<OperationStatus | "">("attention");
  const [items, setItems] = useState<Operation[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [current, setCurrent] = useState<OperationDetail | null>(null);
  const [form, setForm] = useState<ResolveForm>(EMPTY_FORM);
  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const inFlight = useRef(false);

  useEffect(() => {
    const value=new URLSearchParams(window.location.search).get("status");
    const requested=FILTERS.find(item=>item.value===value)?.value;
    if(requested!==undefined) setFilter(requested);
    setLoaded(true);
  },[]);
  useEffect(() => {
    if(!loaded || id) return;
    let active=true;
    setError("");
    api<{items:Operation[]}>(root + (filter ? "?status="+filter : "")).then(data=>{
      if(active) setItems(data.items);
    }).catch(err=>{if(active)setError(err.message);});
    return()=>{active=false;};
  }, [filter,id,loaded]);
  useEffect(()=>{
    if(id && /^\d+$/.test(id)) open(Number(id));
    else if(id) setError("شناسهٔ عملیات معتبر نیست.");
  },[id]);

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
      setConfirming(false);
      setResolving(false);
      try { setCurrent(await api<OperationDetail>(root + "/" + result.id)); }
      catch { setError("تصمیم ثبت شد، اما بارگیری وضعیت تازه ممکن نشد."); }
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

  const listHref=root+"?"+new URLSearchParams({status:filter});
  const confirmation=confirming&&chosen&&current&&form.decision;

  if(!id) return <div className="space-y-4">
    <PageHead title="عملیات ناتمام" description="پیگیری وضعیت و تصمیم‌های بازیابی" />
    <div className="pc-tabs pc-operation-filters" role="group" aria-label="فیلتر وضعیت">
      {FILTERS.map(item=><button type="button" key={item.value||"all"} aria-pressed={filter===item.value}
        onClick={()=>{const url=new URL(window.location.href);url.searchParams.set("status",item.value);window.history.replaceState(null,"",url);setFilter(item.value);}}>{item.label}</button>)}
    </div>
    {error ? <Banner>{error}</Banner> :
      <ResponsiveTable label="عملیات" icon="clock" columns={["شماره","نوع","کاربر","وضعیت","مبلغ","آخرین خطا","به‌روزرسانی","جزئیات"]}
        empty="عملیاتی با این وضعیت پیدا نشد." rows={items.map(row=>{
          const kind=KIND_LABEL[row.kind]||row.kind;
          const status=<Badge tone={statusTone(row.status)}>{OPERATION_STATUS_LABEL[row.status]||row.status}</Badge>;
          const user=<bdi dir="ltr">{row.username||"—"}</bdi>;
          const action=<ActionLink href={root+"/"+row.id+"?"+new URLSearchParams({status:filter})} secondary>جزئیات</ActionLink>;
          return {id:row.id,cells:[faDigits(row.id),kind,user,status,row.charge_toman?formatToman(row.charge_toman):"—",shownError(row.last_error)||"—",moment(row.updated_at),action],
            title:kind+" #"+faDigits(row.id),summary:<>{user} · {moment(row.updated_at)}</>,aside:status,action};
        })}/>}
  </div>;

  return <div className="space-y-4">
    <PageHead title={confirmation?"تأیید نهایی عملیات":resolving?"رسیدگی به عملیات":"عملیات #"+faDigits(id)}
      back={{href:listHref,label:"عملیات ناتمام"}}/>
    {error && <Banner>{error}</Banner>}
    {notice && <Banner tone="ok">{notice}</Banner>}
    <DraftLeaveWarning dirty={resolving && !submitting && Boolean(form.decision || form.note.trim() || form.checkedPanel)}/>
    {!current ? !error&&<p role="status" className="pc-card">در حال بارگیری عملیات…</p> : confirmation ? (
      <section className="pc-card pc-form-card mx-auto max-w-2xl">
        <h2 className="pc-section-title">{decisionLabel(current, form.decision as Decision)}</h2>
        <Banner tone="warn">{chosen.warning}</Banner>
        <Facts rows={[["شماره عملیات",faDigits(current.id)],["توضیح",form.note.trim()]]}/>
        <p className="text-sm">{chosen.summary}</p>
        <EffectsView op={current} decision={form.decision as Decision}/>
        <div className="flex flex-wrap gap-2">
          <Button tone={form.decision==="close"?"danger":"primary"} disabled={submitting} onClick={submit}>{submitting?"در حال ثبت…":"تأیید و ثبت"}</Button>
          <Button tone="ghost" disabled={submitting} onClick={()=>setConfirming(false)}>بازگشت</Button>
        </div>
      </section>
    ) : resolving&&options.length ? (
      <div className="pc-grid-two">
        <form className="pc-card pc-form-card" onSubmit={e=>{e.preventDefault();if(!problem)setConfirming(true);}}>
          <h2 className="pc-section-title">تصمیم عملیات</h2>
          <Field label="تصمیم">
            <select className={inputClass} value={form.decision||""} onChange={e=>setForm({...form,decision:(e.target.value||null) as Decision|null,acceptShortfall:false,checkedPanel:false})} required>
              <option value="">انتخاب تصمیم</option>{options.map(option=><option key={option} value={option}>{decisionLabel(current,option)}</option>)}
            </select>
          </Field>
          <Field label="توضیح تصمیم (الزامی)">
            <textarea className={inputClass+" min-h-24"} maxLength={NOTE_MAX} value={form.note} onChange={e=>setForm({...form,note:e.target.value})}/>
          </Field>
          {form.decision==="close"&&hasShortfall(current)&&<label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1 pc-operation-check" checked={form.acceptShortfall} onChange={e=>setForm({...form,acceptShortfall:e.target.checked})}/>
            <span>اختلاف {formatGb(current.shortfall_gb)} و {formatInt(current.shortfall_days)} روز را می‌پذیرم؛ این مقدار دیگر پس گرفته نمی‌شود.</span>
          </label>}
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1 pc-operation-check" checked={form.checkedPanel} onChange={e=>setForm({...form,checkedPanel:e.target.checked})}/>
            <span>وضعیت پنل را بررسی کردم و پیامد این تصمیم را می‌دانم.</span>
          </label>
          {problem&&form.decision&&<p className="pc-quiet">{problem}</p>}
          <div className="flex gap-2"><Button type="submit" disabled={Boolean(problem)}>ادامه</Button><Button tone="ghost" onClick={()=>setResolving(false)}>انصراف</Button></div>
        </form>
        <section className="pc-card pc-form-card">
          <h2 className="pc-section-title">پیامدهای تصمیم</h2>
          {chosen ? <><p className="text-sm">{chosen.summary}</p><EffectsView op={current} decision={form.decision as Decision}/><Banner tone="warn">{chosen.warning}</Banner></> :
            <p className="pc-quiet">برای دیدن اثر این تصمیم بر کیف پول، اعتبار و پنل، یک گزینه انتخاب کنید.</p>}
        </section>
      </div>
    ) : <>
      <Badge tone={statusTone(current.status)}>{OPERATION_STATUS_LABEL[current.status]||current.status}</Badge>
      <div className="pc-grid-two">
        <section className="pc-card pc-operation-summary space-y-2 text-sm"><h2 className="pc-section-title">خلاصه عملیات</h2><Amounts op={current}/></section>
        <section className="pc-card space-y-2 text-sm"><h2 className="pc-section-title">وضعیت فعلی پنل</h2><PanelStateView op={current}/></section>
      </div>
      <section className="pc-card space-y-2">
        <h2 className="pc-section-title">رسیدگی</h2>
        {current.busy&&<Banner tone="warn">این عملیات همین حالا در حال پردازش است؛ کمی بعد دوباره باز کنید.</Banner>}
        {current.status==="attention"&&blocked.map(item=><Banner tone="warn" key={item.decision}>«{decisionLabel(current,item.decision)}» ممکن نیست: {item.reason}</Banner>)}
        {options.length>0&&<Button tone="ghost" onClick={()=>setResolving(true)}>بررسی و تصمیم</Button>}
        {!options.length&&!current.busy&&current.status!=="attention"&&<p className="pc-quiet">در این وضعیت تصمیم دیگری قابل ثبت نیست.</p>}
      </section>
    </>}
  </div>;
}
