"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { JalaliDateField } from "@/components/JalaliDateField";
import { Modal } from "@/components/Modal";
import { QuoteBox } from "@/components/QuoteBox";
import { Banner, Button, Field, inputClass } from "@/components/ui";
import { api, ApiError, sessionToken } from "@/lib/api";
import { STATUS_LABEL, faDigits, formatDate, formatDays, formatGb, formatToman } from "@/lib/format";
import { addJalaliDays, todayJalali } from "@/lib/jalali";
import { Quote, SubscriptionRow } from "@/lib/types";

type Mode = null | "increase" | "decrease" | "reset" | "delete" | "ban" | "link";

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

  async function saveNote() {
    setError("");
    try {
      await api(`/partner/subscriptions/${params.id}/note`, { method: "POST", body: { note } });
      setInfo("یادداشت ذخیره شد.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "خطا");
    }
  }

  if (!row) return <p>{error || "در حال بارگذاری…"}</p>;
  const baseDate = row.status === "active" ? row.expired_jalali : todayJalali();

  return (
    <div className="space-y-4">
      {error && !mode ? <Banner>{error}</Banner> : null}
      {info ? <Banner tone="ok">{info}</Banner> : null}
      <div className="rounded-2xl bg-white p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-xl font-bold">{row.username}</h2>
          <span className="rounded-full bg-slate-100 px-3 py-1 text-sm">{STATUS_LABEL[row.status]}</span>
        </div>
        <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
          <div>محصول: {row.product_name}</div>
          <div>پنل: {row.panel_name}</div>
          <div>سقف: {formatGb(row.traffic_cap_gb)}</div>
          <div>انقضا: {formatDate(row.expired_jalali)} ({formatDays(row.days_left)})</div>
          <div>شناسه پنل: {row.v2_user_id}</div>
          <div>موجودی تومان: {formatToman(row.balance_toman)}</div>
        </div>
        {row.live_error ? <div className="mt-3"><Banner>{row.live_error}</Banner></div> : (
          <div className="mt-3 space-y-2 text-sm">
            <div>مصرف زنده: {formatGb(row.used_gb)} (آپلود {formatGb(row.upload_gb)} / دانلود {formatGb(row.download_gb)})</div>
            <div className="break-all rounded-xl bg-slate-50 p-3">{row.subscribe_url || "لینک از پنل نیامد"}</div>
            {row.subscribe_url ? (
              <Button
                tone="soft"
                onClick={() => {
                  navigator.clipboard.writeText(row.subscribe_url || "").then(
                    () => setInfo("لینک کپی شد."),
                    () => setError("کپی لینک ممکن نشد."),
                  );
                }}
              >
                کپی لینک
              </Button>
            ) : null}
            {row.subscribe_url_fallback ? <p className="text-xs text-amber-800">لینک با مسیر استاندارد ساخته شد چون پنل آدرس نداد.</p> : null}
          </div>
        )}
      </div>
      <div className="flex flex-wrap gap-2">
        <Button disabled={pending} onClick={() => { setMode("increase"); setEnd(""); setQuote(null); setError(""); }}>افزایش</Button>
        <Button disabled={pending} tone="ghost" onClick={() => { setMode("decrease"); setEnd(""); setQuote(null); setError(""); }}>کاهش</Button>
        <Button disabled={pending} tone="ghost" onClick={() => { setMode("link"); setQuote(null); setError(""); }}>ریست لینک</Button>
        <Button disabled={pending} tone="soft" onClick={() => { setMode("reset"); setQuote(null); setError(""); }}>ریست ترافیک</Button>
        <Button disabled={pending} tone={row.banned ? "primary" : "danger"} onClick={() => { setMode("ban"); setQuote(null); setError(""); }}>
          {row.banned ? "رفع مسدودی" : "مسدود"}
        </Button>
        <Button disabled={pending} tone="danger" onClick={() => { setMode("delete"); setQuote(null); setError(""); }}>حذف</Button>
      </div>
      <div className="rounded-2xl bg-white p-4">
        <Field label="یادداشت مشتری">
          <textarea className={inputClass} rows={3} maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
        <div className="mt-3"><Button tone="ghost" onClick={saveNote}>ذخیره یادداشت</Button></div>
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
