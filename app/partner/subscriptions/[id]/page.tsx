"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { JalaliDateField } from "@/components/JalaliDateField";
import { Modal } from "@/components/Modal";
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
    if (pending || (paid && !quote?.consent_token)) return;
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
    setError("");
    try {
      await api(`/partner/subscriptions/${params.id}/note`, { method: "POST", body: { note } });
      setInfo("یادداشت ذخیره شد.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "خطا");
    }
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

  return (
    <div className={styles.page}>
      {backLink}
      {(error && !mode || info) ? (
        <div className={styles.messages}>
          {error && !mode ? <Banner>{error}</Banner> : null}
          {info ? <Banner tone="ok">{info}</Banner> : null}
        </div>
      ) : null}
      <div className={styles.grid}>
        <section className={`${styles.card} ${styles.summary}`} aria-labelledby="subscription-customer">
          <div className={styles.customer}>
            <h2 id="subscription-customer">
              <bdi dir="ltr">{row.username}</bdi>
              {isActive ? (
                <svg className={styles.active} role="img" aria-label="فعال" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.65">
                  <circle cx="10" cy="10" r="8" />
                  <path strokeLinecap="round" strokeLinejoin="round" d="m6.5 10 2.3 2.3 4.7-4.7" />
                </svg>
              ) : null}
            </h2>
            {!isActive ? <span dir="rtl"><Badge tone="warn">{typeof statusLabel === "string" ? statusLabel : row.status}</Badge></span> : null}
          </div>
          <dl className={styles.remaining}>
            <div><dt>زمان باقی‌مانده</dt><dd>{formatDays(row.days_left)}</dd></div>
            <div><dt>حجم باقی‌مانده</dt><dd>{formatGb(remaining)}</dd></div>
          </dl>
          <dl className={styles.facts}>
            <div><dt>محصول</dt><dd>{row.product_name ?? "—"}</dd></div>
            <div><dt>سقف حجم</dt><dd>{formatGb(cap)}</dd></div>
            <div><dt>تاریخ انقضا</dt><dd><bdi dir="ltr">{formatDate(row.expired_jalali)}</bdi></dd></div>
          </dl>
        </section>

        <div className={styles.workspace}>
          <section className={styles.card} aria-labelledby="subscription-usage">
            <h2 id="subscription-usage">مصرف ترافیک</h2>
            {row.live_error ? <div className={styles.notice}><Banner>{row.live_error}</Banner></div> : null}
            <div className={styles.traffic}>
              <div className={styles.usageSummary}>
                <p>{usageLabel}</p>
                {percent !== null ? <bdi aria-hidden="true">{percent}</bdi> : null}
              </div>
              {usedShare !== null && percent !== null ? (
                <progress className={styles.usageBar} max={1} value={Math.min(usedShare, 1)}
                  aria-label="سهم حجم مصرف‌شده" aria-valuetext={percent} />
              ) : null}
              <dl className={styles.trafficDirections}>
                <div><dt>دانلود</dt><dd>{formatGb(row.live_error ? null : nonnegativeGb(row.download_gb))}</dd></div>
                <div><dt>آپلود</dt><dd>{formatGb(row.live_error ? null : nonnegativeGb(row.upload_gb))}</dd></div>
              </dl>
            </div>
            {!row.live_error && (
              row.subscribe_url ? (
                <div className={styles.linkArea}>
                  <label htmlFor="subscription-link">لینک اشتراک</label>
                  <div className={styles.linkControl} dir="ltr">
                    <input id="subscription-link" value={row.subscribe_url} readOnly />
                    <button type="button" onClick={() => copyLink(row.subscribe_url)}>
                      <svg aria-hidden="true" width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
                        <rect x="7" y="7" width="9" height="10" rx="2" />
                        <path d="M5 12H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h7a1 1 0 0 1 1 1v1" />
                      </svg>
                      کپی لینک
                    </button>
                  </div>
                  {row.subscribe_url_fallback ? <p className={styles.fallback}>لینک با مسیر استاندارد ساخته شد چون پنل آدرس نداد.</p> : null}
                </div>
              ) : <p className={styles.notice}>لینک از پنل نیامد</p>
            )}
          </section>

          <section className={styles.card} aria-labelledby="subscription-management">
            <h2 id="subscription-management">مدیریت اشتراک</h2>
            <div className={styles.actions}>
              <button type="button" disabled={pending} className={styles.primary} onClick={() => { setMode("increase"); setEnd(""); setQuote(null); setError(""); }}>افزایش</button>
              <button type="button" disabled={pending} className={styles.secondary} onClick={() => { setMode("decrease"); setEnd(""); setQuote(null); setError(""); }}>کاهش</button>
              <button type="button" disabled={pending} className={styles.secondary} onClick={() => { setMode("reset"); setQuote(null); setError(""); }}>ریست ترافیک</button>
              <button type="button" disabled={pending} className={styles.secondary} onClick={() => { setMode("link"); setQuote(null); setError(""); }}>ریست لینک</button>
            </div>
            <div className={styles.sensitive} role="group" aria-label="عملیات حساس">
              <button type="button" disabled={pending} className={row.banned ? styles.unban : undefined} onClick={() => { setMode("ban"); setQuote(null); setError(""); }}>
                {row.banned ? "رفع مسدودی" : "مسدود"}
              </button>
              <button type="button" disabled={pending} onClick={() => { setMode("delete"); setQuote(null); setError(""); }}>حذف</button>
            </div>
          </section>

          <section className={styles.card} aria-labelledby="subscription-note-title">
            <h2 id="subscription-note-title"><label htmlFor="subscription-note">یادداشت مشتری</label></h2>
            <textarea id="subscription-note" className={styles.note} rows={2} maxLength={2000} value={note} onChange={(event) => setNote(event.target.value)} />
            <div className={styles.noteActions}>
              <button type="button" className={styles.secondary} onClick={saveNote}>ذخیره یادداشت</button>
            </div>
          </section>
        </div>
      </div>
      {mode ? (
        <Modal title={mode === "increase" ? "افزایش" : mode === "decrease" ? "کاهش" : mode === "reset" ? "ریست ترافیک" : mode === "link" ? "ریست لینک" : mode === "ban" ? (row.banned ? "رفع مسدودی" : "مسدود کردن") : "حذف اشتراک"} error={error} onClose={() => setMode(null)}>
          <div className="space-y-3">
            {mode === "ban" ? (
              <p className="text-sm leading-7">
                {row.banned ? "فقط همین اشتراک باز می‌شود." : "زمان اشتراک در مسدودی هم می‌گذرد. این کار حذف نیست."}
              </p>
            ) : null}
            {mode === "link" ? <p className="text-sm leading-7">لینک فعلی باطل می‌شود و لینک جدید باید دوباره به مشتری داده شود.</p> : null}
            {mode === "reset" && quote?.consent_token ? (
              <p className="text-sm leading-7">با تأیید، فقط مبلغ نمایش‌داده‌شده مجاز است. اگر مبلغ پیش از ثبت تغییر کند، تأیید دوباره لازم است.</p>
            ) : null}
            {mode === "increase" || mode === "decrease" ? (
              <>
                <Field label="سقف جدید (گیگ)">
                  <input className={inputClass} type="number" min={1} step="1" value={gb} onChange={(e) => setGb(e.target.value)} />
                </Field>
                <Field label="تاریخ پایان جدید">
                  {mode === "increase" ? (
                    <div className="mb-2 flex flex-wrap gap-2">
                      {[31, 62, 93].map((days) => (
                        <Button key={days} tone="soft" onClick={() => setEnd(addJalaliDays(baseDate, days))}>+{faDigits(days)} روز</Button>
                      ))}
                    </div>
                  ) : null}
                  <JalaliDateField
                    value={end}
                    onChange={setEnd}
                    minExclusive={mode === "decrease" ? todayJalali() : baseDate}
                    maxExclusive={mode === "decrease" ? row.expired_jalali : undefined}
                  />
                </Field>
              </>
            ) : null}
            {mode === "ban" || mode === "link" ? null : (
              <div className="rounded-xl bg-slate-50 p-3">
                <QuoteBox quote={quote} />
                {quoteError ? <Banner>{quoteError}</Banner> : null}
              </div>
            )}
            <Button
              className="w-full"
              tone={mode === "delete" || (mode === "ban" && !row.banned) ? "danger" : "primary"}
              disabled={pending || (mode !== "ban" && mode !== "link" && !quote?.consent_token)}
              onClick={() => {
                if (mode === "link") run("/reset-link");
                else if (mode === "ban") run(row.banned ? "/unban" : "/ban");
                else if (mode === "increase" || mode === "decrease") {
                  run(`/${mode}`, { traffic_cap_gb: gb || null, end_date: end || null });
                } else if (mode === "reset") run("/reset-traffic");
                else run("/delete");
              }}
            >
              {pending ? "در حال اعمال…" : "تأیید"}
            </Button>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
