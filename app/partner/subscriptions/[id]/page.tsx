"use client";

import { DraftLeaveWarning } from "@/components/DraftLeaveWarning";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { JalaliDateField } from "@/components/JalaliDateField";
import { Facts, PageHead } from "@/components/Design";
import { Icon, IconTile, type IconName } from "@/components/Icons";
import { QuoteBox } from "@/components/QuoteBox";
import { Badge, Banner, Button, Field, inputClass } from "@/components/ui";
import { api, ApiError, sessionToken } from "@/lib/api";
import { STATUS_LABEL, faDigits, formatDate, formatDays, formatGb, formatToman } from "@/lib/format";
import { addJalaliDays, todayJalali } from "@/lib/jalali";
import { Quote, SubscriptionRow } from "@/lib/types";
import styles from "./subscription-details.module.css";

const subscriptionsPath = "/partner/subscriptions";
const trafficNumber = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 2 });
const trafficPercent = new Intl.NumberFormat("fa-IR", { style: "percent", maximumFractionDigits: 1 });

function nonnegativeGb(value: string | null | undefined) {
  if (value == null || value.trim() === "") return null;
  const amount = Number(value);
  return Number.isFinite(amount) && amount >= 0 ? amount : null;
}

type Mode = null | "increase" | "decrease" | "reset" | "delete" | "ban" | "link";

function SubscriptionBackLink() {
  const params = useSearchParams();
  const listQuery = new URLSearchParams(params.get("listQuery") ?? "");
  const returnQuery = new URLSearchParams();
  const search = listQuery.get("q");
  if (search?.trim()) returnQuery.set("q", search);

  const rawOffset = listQuery.get("offset");
  if (rawOffset && /^\d+$/.test(rawOffset)) {
    const offset = Number(rawOffset);
    if (offset > 0 && Number.isSafeInteger(offset)) returnQuery.set("offset", String(offset));
  }

  const query = returnQuery.toString();
  return (
    <Link
      className={styles.backLink}
      href={query ? `${subscriptionsPath}?${query}` : subscriptionsPath}
      aria-label="بازگشت به اشتراک‌ها"
      prefetch={false}
    >
      <span className={styles.backArrow} dir="ltr" aria-hidden="true">→</span>
      اشتراک‌ها
    </Link>
  );
}

export default function SubscriptionDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const [reviewSession] = useState(sessionToken);
  const [quoteVersion, setQuoteVersion] = useState(0);
  const [row, setRow] = useState<SubscriptionRow | null>(null);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [mode, setMode] = useState<Mode>(null);
  const [gb, setGb] = useState("");
  const [end, setEnd] = useState("");
  const [note, setNote] = useState("");
  const [quote, setQuote] = useState<Quote | null>(null);
  const [quoteError, setQuoteError] = useState("");
  const [pending, setPending] = useState(false);
  const [tab, setTab] = useState<"overview"|"usage"|"manage">("overview");
  const [noteOpen, setNoteOpen] = useState(false);
  const [calendarOpen, setCalendarOpen] = useState(false);
  const inFlight = useRef(false);
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (mode || noteOpen) heading.current?.focus();
  }, [mode, noteOpen]);

  async function load() {
    const data = await api<SubscriptionRow>(`/partner/subscriptions/${params.id}`);
    setRow(data);
    setNote(data.note || "");
    setGb(data.traffic_cap_gb);
  }
  useEffect(() => {
    load().catch((err) => setError(err.message));
  }, [params.id]);

  useEffect(() => {
    setQuote(null);
    setQuoteError("");
    if (!mode || !row || mode === "ban" || mode === "link") return;
    if ((mode === "increase" || mode === "decrease") && !end && gb === row.traffic_cap_gb) {
      return;
    }
    const body: Record<string, unknown> = { action: mode === "reset" ? "reset_traffic" : mode, subscription_id: row.id };
    if (mode === "increase" || mode === "decrease") {
      if (gb) body.traffic_cap_gb = gb;
      if (end) body.end_date = end;
    }
    let cancelled = false;
    const handle = setTimeout(() => {
      api<Quote>("/partner/subscriptions/quote", { method: "POST", body, reviewSession })
        .then((data) => {
          if (cancelled) return;
          setQuote(data);
        })
        .catch((err) => {
          if (cancelled) return;
          setQuote(null);
          setQuoteError(err.message);
        });
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [mode, gb, end, row, reviewSession, quoteVersion]);

  async function run(path: string, body?: Record<string, unknown>) {
    const paid = path !== "/reset-link" && path !== "/ban" && path !== "/unban";
    if (inFlight.current || (paid && (!quote?.consent_token || !quote.affordable)) || sessionToken() !== reviewSession) return;
    inFlight.current = true;
    setPending(true);
    setError("");
    setInfo("");
    try {
      const result = await api<Record<string, unknown>>(`/partner/subscriptions/${params.id}${path}`, {
        method: "POST", reviewSession, body: paid ? { ...body, consent_token: quote?.consent_token } : body,
      });
      if (typeof result.cost_toman === "number") setInfo(`مبلغ کسرشده: ${formatToman(result.cost_toman)}`);
      if (typeof result.subscribe_url === "string") {
        setInfo("لینک جدید آماده است.");
        setRow((current) => current ? { ...current, subscribe_url: String(result.subscribe_url) } : current);
      }
      if (path === "/delete") {
        router.replace("/partner/subscriptions");
        return;
      }
      setMode(null);
      await load().catch(() => {
        setError("عملیات انجام شد، اما وضعیت تازه دریافت نشد. صفحه را دوباره بارگذاری کنید.");
      });
    } catch (err) {
      if (err instanceof ApiError && err.code === "financial_consent") {
        setQuote(null);
        setQuoteVersion((version) => version + 1);
      }
      setError(err instanceof Error ? err.message : "عملیات ناموفق بود");
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  }

  async function copyLink(url: string | null | undefined) {
    if (!url) return;
    setError("");
    setInfo("");
    try {
      await navigator.clipboard.writeText(url);
      setInfo("لینک کپی شد.");
    } catch {
      setError("کپی لینک ممکن نشد.");
    }
  }



  async function saveNote() {
    if (inFlight.current || sessionToken() !== reviewSession) return;
    inFlight.current = true;
    setPending(true);setError("");setInfo("");
    try {
      const result=await api<SubscriptionRow>(`/partner/subscriptions/${params.id}/note`,{method:"POST",body:{note},reviewSession});
      setRow(current=>current?{...current,note:result.note}:current);
      setNote(result.note||"");
      setNoteOpen(false);
      setInfo("یادداشت ذخیره شد.");
    } catch(err) {
      setError(err instanceof Error?err.message:"ذخیره یادداشت ممکن نشد.");
    } finally {inFlight.current=false;setPending(false);}
  }

  const backLink = <Suspense fallback={null}><SubscriptionBackLink /></Suspense>;

  if (!row) {
    return (
      <div className={styles.page}>
        {backLink}
        <p>{error || "در حال بارگذاری…"}</p>
      </div>
    );
  }

  const isActive = row.status === "active";
  const baseDate = row.status === "active" ? row.expired_jalali : todayJalali();
  const statusLabel = STATUS_LABEL[row.status];

  const cap = nonnegativeGb(row.traffic_cap_gb);
  const used = row.live_error ? null : nonnegativeGb(row.used_gb);
  const hasCap = cap !== null && cap > 0;
  const remaining = hasCap && used !== null ? Math.max(0, cap - used) : null;
  const usedShare = hasCap && used !== null ? used / cap : null;
  const percent = usedShare !== null && Number.isFinite(usedShare) ? trafficPercent.format(usedShare) : null;
  const usageLabel = used === null
    ? "مقدار مصرف در دسترس نیست."
    : `${trafficNumber.format(used)} گیگ${hasCap ? ` از ${trafficNumber.format(cap)} گیگ` : ""} مصرف شده`;

  const modeTitle=mode==="increase"?"افزایش اشتراک":mode==="decrease"?"کاهش اشتراک":mode==="reset"?"ریست ترافیک":
    mode==="link"?"ریست لینک":mode==="ban"?(row.banned?"رفع مسدودی اشتراک":"مسدود کردن اشتراک"):"حذف اشتراک";
  const status=<Badge tone={isActive?"ok":row.status.startsWith("banned")?"bad":"warn"}>{statusLabel||row.status}</Badge>;
  const identity=<div className="pc-identity">
    <IconTile name="layers"/>
    <div className="min-w-0"><bdi dir="ltr" className="break-all font-bold">{row.username}</bdi><p className="pc-quiet">{row.product_name}</p></div>
    <div className="ms-auto">{status}</div>
  </div>;

  function selectMode(next:Exclude<Mode,null>) {
    setMode(next);setEnd("");setGb(row!.traffic_cap_gb);setQuote(null);setError("");setInfo("");setCalendarOpen(false);
  }

  function confirmAction() {
    if(mode==="link") return run("/reset-link");
    if(mode==="ban") return run(row!.banned?"/unban":"/ban");
    if(mode==="increase"||mode==="decrease") return run("/"+mode,{traffic_cap_gb:gb||null,end_date:end||null});
    if(mode==="reset") return run("/reset-traffic");
    return run("/delete");
  }

  if (mode||noteOpen) {
    const changing=mode==="increase"||mode==="decrease";
    const neutral=mode==="ban"||mode==="link";
    const destructive=mode==="delete"||(mode==="ban"&&!row.banned);
    const title=noteOpen?"یادداشت مشتری":modeTitle;
    const quoteBody=<>
      <QuoteBox quote={quote}/>
      {quoteError&&<Banner>{quoteError}</Banner>}
    </>;
    const buttons=<div className="pc-actions-row">
      <Button tone={destructive?"danger":"primary"} disabled={pending||(!noteOpen&&!neutral&&(!quote?.consent_token||!quote.affordable))}
        onClick={noteOpen?saveNote:confirmAction}>
        {pending?"در حال اعمال…":noteOpen?"ذخیره یادداشت":mode==="increase"?"تأیید افزایش":mode==="decrease"?"تأیید کاهش":mode==="reset"?"تأیید ریست ترافیک":mode==="link"?"تأیید ریست لینک":mode==="delete"?"تأیید حذف":row.banned?"رفع مسدودی":"مسدود کردن"}
      </Button>
      <Button tone="ghost" disabled={pending} onClick={()=>{setMode(null);setNoteOpen(false);setError("");setNote(row.note||"");}}>انصراف</Button>
    </div>;
    return <div className="space-y-4">
      {backLink}
      <DraftLeaveWarning dirty={noteOpen && !pending && note !== (row.note || "")}/>
      <div className="pc-page-head"><h1 ref={heading} tabIndex={-1}>{title}</h1></div>
      {error&&<Banner>{error}</Banner>}
      {changing ? <div className="pc-grid-two">
        <section className="pc-card pc-form-card">
          <h2 className="pc-section-title">مشخصات جدید</h2>
          <Facts rows={[["سقف فعلی",formatGb(row.traffic_cap_gb)],["انقضای فعلی",<bdi dir="ltr">{formatDate(row.expired_jalali)}</bdi>]]}/>
          <Field label="سقف جدید (گیگ)"><input className={inputClass} type="number" min={1} step="1" value={gb} disabled={pending}
            onChange={e=>{setQuote(null);setGb(e.target.value);}}/></Field>
          <Field label="تاریخ پایان جدید">
            <Button tone="ghost" disabled={pending} onClick={()=>setCalendarOpen(!calendarOpen)}><Icon name="calendar"/>
              {end?<bdi dir="ltr">{formatDate(end)}</bdi>:"انتخاب تاریخ"}
            </Button>
            {mode==="increase"&&<div className="flex flex-wrap gap-2 mt-2">{[31,62,93].map(days=><Button key={days} disabled={pending} tone="soft"
              onClick={()=>{setQuote(null);setEnd(addJalaliDays(baseDate,days));}}>+{faDigits(days)} روز</Button>)}</div>}
            {calendarOpen&&<JalaliDateField value={end} onChange={value=>{if(!pending){setQuote(null);setEnd(value);setCalendarOpen(false);}}}
              minExclusive={mode==="decrease"?todayJalali():baseDate} maxExclusive={mode==="decrease"?row.expired_jalali:undefined}/>}
          </Field>
        </section>
        <section className="pc-card pc-form-card">
          <h2 className="pc-section-title">{mode==="decrease"?"اعتبار برگشتی":"پیش‌فاکتور"}</h2>
          {quoteBody}{buttons}
        </section>
      </div> : <section className="pc-card pc-form-card mx-auto max-w-2xl">
        <h2 className="pc-section-title">{title}</h2>
        {identity}
        {noteOpen ? <Field label="یادداشت مشتری"><textarea className={inputClass} rows={4} maxLength={2000} value={note} disabled={pending}
          onChange={event=>setNote(event.target.value)}/></Field> : <>
          <Banner tone="warn">{mode==="ban"?(row.banned?"فقط همین اشتراک باز می‌شود.":"زمان اشتراک در مسدودی هم می‌گذرد. این کار حذف نیست."):
            mode==="link"?"لینک فعلی باطل می‌شود و لینک جدید باید دوباره به مشتری داده شود.":mode==="delete"?
            "این اشتراک حذف می‌شود. اعتبار برگشتی طبق پیش‌فاکتور به اعتبار محصول برمی‌گردد.":"مصرف فعلی صفر می‌شود. اگر مبلغ پیش از ثبت تغییر کند، تأیید دوباره لازم است."}</Banner>
          {!neutral&&quoteBody}
          <Facts rows={[["وضعیت فعلی",status]]}/>
        </>}
        {buttons}
      </section>}
    </div>;
  }

  function action(title:string, icon:IconName, trigger:()=>void, description?:string, danger?:boolean, disabled?:boolean) {
    return <button type="button" className={"pc-management-row"+(danger?" pc-management-danger":"")} onClick={trigger} disabled={pending||disabled}>
      <IconTile name={icon}/><span><strong className="block text-sm">{title}</strong>{description&&<span className="pc-quiet">{description}</span>}</span><Icon name="left"/>
    </button>;
  }

  return <div className="pc-detail-overview">
    {backLink}
    <PageHead title={tab==="overview"?"جزئیات اشتراک":tab==="usage"?"مصرف و لینک اشتراک":"مدیریت اشتراک"}/>
    {error&&<Banner>{error}</Banner>}
    {info&&<Banner tone="ok">{info}</Banner>}
    {identity}
    <nav className="pc-tabs" aria-label="بخش‌های اشتراک">
      <button type="button" aria-pressed={tab==="overview"} onClick={()=>setTab("overview")}>نمای کلی</button>
      <button type="button" aria-pressed={tab==="usage"} onClick={()=>setTab("usage")}>مصرف و لینک</button>
      <button type="button" aria-pressed={tab==="manage"} onClick={()=>setTab("manage")}>مدیریت</button>
    </nav>

    {tab==="overview" ? <>
      <div className="pc-detail-metrics">
        <div className="pc-card"><IconTile name="layers"/><span className="pc-quiet">حجم باقی‌مانده</span><strong>{formatGb(remaining)}</strong><span className="pc-quiet">از سقف {formatGb(cap)}</span></div>
        <div className="pc-card"><IconTile name="calendar"/><span className="pc-quiet">زمان باقی‌مانده</span><strong>{formatDays(row.days_left)}</strong><bdi dir="ltr" className="pc-quiet">{formatDate(row.expired_jalali)}</bdi></div>
      </div>
      <section className="pc-card">
        <h2 className="pc-section-title">مشخصات اشتراک</h2>
        <Facts rows={[["محصول",row.product_name||"—"],["سقف حجم",formatGb(cap)],["تاریخ انقضا",<bdi dir="ltr">{formatDate(row.expired_jalali)}</bdi>]]}/>
      </section>
      {action("مصرف و لینک","link",()=>setTab("usage"),"مشاهدهٔ مصرف و کپی لینک")}
    </> : tab==="usage" ? <>
      <section className="pc-card space-y-3" aria-labelledby="subscription-usage">
        <h2 id="subscription-usage" className="pc-section-title">مصرف ترافیک</h2>
        {row.live_error&&<Banner>{row.live_error}</Banner>}
        <div className="flex justify-between gap-2 text-sm"><span>{usageLabel}</span>{percent&&<bdi aria-hidden="true">{percent}</bdi>}</div>
        {usedShare!==null&&percent!==null&&<progress className="pc-progress" max={1} value={Math.min(usedShare,1)} aria-label="سهم حجم مصرف‌شده" aria-valuetext={percent}/>}
        <Facts rows={[["دانلود",formatGb(row.live_error?null:nonnegativeGb(row.download_gb))],["آپلود",formatGb(row.live_error?null:nonnegativeGb(row.upload_gb))],
          ["حجم باقی‌مانده",formatGb(remaining)],["سقف مصرف",formatGb(cap)]]}/>
      </section>
      <section className="pc-card space-y-3">
        <h2 className="pc-section-title"><label htmlFor="subscription-link">لینک اشتراک</label></h2>
        {!row.live_error&&row.subscribe_url ? <>
          <div className="pc-copy-control">
            <input id="subscription-link" className={inputClass} dir="ltr" value={row.subscribe_url} readOnly/>
            <Button tone="ghost" onClick={()=>copyLink(row.subscribe_url)}><Icon name="copy"/>کپی لینک</Button>
          </div>
          {row.subscribe_url_fallback&&<p className="pc-quiet">لینک با مسیر استاندارد ساخته شد چون پنل آدرس نداد.</p>}
        </> : <p className="pc-quiet">لینک اشتراک در دسترس نیست.</p>}
      </section>
    </> : <>
      <section className="pc-card">
        <h2 className="pc-section-title">مدیریت اشتراک</h2>
        {action("افزایش","plus",()=>selectMode("increase"),"افزایش سقف حجم یا تاریخ پایان")}
        {action("کاهش","minus",()=>selectMode("decrease"),"کاهش سقف حجم یا تاریخ پایان")}
        {action("ریست ترافیک","reset",()=>selectMode("reset"))}
        {action("ریست لینک","link",()=>selectMode("link"))}
        {action("یادداشت مشتری","note",()=>{setError("");setNoteOpen(true);},row.note?"مشاهده و ویرایش یادداشت":"افزودن یادداشت")}
      </section>
      <section className="pc-card">
        <h2 className="pc-section-title">دسترسی و حذف</h2>
        {action(row.banned?"رفع مسدودی اشتراک":"مسدود کردن اشتراک","ban",()=>selectMode("ban"),
          row.blocked_by_partner_ban?"این اشتراک به‌خاطر مسدودی همکار بسته شده است.":undefined,!row.banned,row.blocked_by_partner_ban)}
        {action("حذف اشتراک","trash",()=>selectMode("delete"),undefined,true)}
      </section>
    </>}
  </div>;
}
