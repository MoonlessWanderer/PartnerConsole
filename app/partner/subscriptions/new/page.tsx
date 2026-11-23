"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { JalaliDateField } from "@/components/JalaliDateField";
import { QuoteBox } from "@/components/QuoteBox";
import { Banner, Button, Field, inputClass } from "@/components/ui";
import { api, ApiError, sessionToken } from "@/lib/api";
import { faDigits, formatDays, formatGb, formatToman } from "@/lib/format";
import { addJalaliDays, todayJalali } from "@/lib/jalali";
import { Product, Quote } from "@/lib/types";

export default function NewSubscriptionPage() {
  const router = useRouter();
  const [reviewSession] = useState(sessionToken);
  const [quoteVersion, setQuoteVersion] = useState(0);
  const [products, setProducts] = useState<Product[]>([]);
  const [productId, setProductId] = useState(0);
  const [username, setUsername] = useState("");
  const [gb, setGb] = useState("10");
  const [end, setEnd] = useState("");
  const [note, setNote] = useState("");
  const [quote, setQuote] = useState<Quote | null>(null);
  const [quoteError, setQuoteError] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const today = useMemo(() => todayJalali(), []);
  const product = products.find((item) => item.id === productId);

  useEffect(() => {
    api<{ items: Product[] }>("/partner/products").then((data) => {
      const active = data.items.filter((item) => item.active);
      setProducts(active);
      if (active[0]) setProductId(active[0].id);
    }).catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    setQuote(null);
    setQuoteError("");
    if (!productId || !gb || !end || !/^[a-zA-Z0-9_]{3,32}$/.test(username)) return;
    let cancelled = false;
    const handle = setTimeout(() => {
      api<Quote>("/partner/subscriptions/quote", {
        method: "POST",
        reviewSession,
        body: { action: "create", product_id: productId, traffic_cap_gb: gb, end_date: end, username, note },
      }).then((data) => {
        if (cancelled) return;
        setProducts((items) => items.map((item) => item.id === productId
          ? { ...item, credit_gb: data.credit_gb, credit_days: data.credit_days }
          : item));
        setQuote(data);
      }).catch((err) => {
        if (!cancelled) {
          setQuote(null);
          setQuoteError(err.message);
        }
      });
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [productId, gb, end, username, note, reviewSession, quoteVersion]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending || !quote?.consent_token) return;
    setPending(true);
    setError("");
    try {
      const created = await api<{ id: number }>("/partner/subscriptions", {
        method: "POST",
        reviewSession,
        body: { product_id: productId, username, traffic_cap_gb: gb, end_date: end, note: note || null, consent_token: quote.consent_token },
      });
      router.replace(`/partner/subscriptions/${created.id}`);
    } catch (err) {
      if (err instanceof ApiError && err.code === "financial_consent") {
        setQuote(null);
        setQuoteVersion((version) => version + 1);
      }
      setError(err instanceof Error ? err.message : "ساخت ناموفق بود");
    } finally {
      setPending(false);
    }
  }

  return (
    <form onSubmit={submit} className="grid gap-4 lg:grid-cols-[1fr_320px]">
      <div className="space-y-4 rounded-2xl bg-white p-4 pb-36 lg:pb-4">
        <h2 className="text-lg font-bold">ساخت اشتراک</h2>
        {error ? <Banner>{error}</Banner> : null}
        <Field label="محصول">
          <select className={inputClass} value={productId} onChange={(e) => setProductId(Number(e.target.value))}>
            {products.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name} — اعتبار {formatGb(item.credit_gb)} / {formatDays(item.credit_days)}
              </option>
            ))}
          </select>
        </Field>
        {product ? (
          <p className="text-sm text-slate-600">
            نرخ {formatToman(product.price_per_gb)} برای هر گیگ و {formatToman(product.price_per_day)} برای هر روز.
            اول اعتبار همین محصول مصرف می‌شود.
          </p>
        ) : <p className="text-sm">محصول فعالی نیست.</p>}
        <Field label="نام کاربری (a-z، عدد، _)">
          <input className={inputClass} value={username} onChange={(e) => setUsername(e.target.value)} required pattern="[A-Za-z0-9_]{3,32}" />
        </Field>
        <Field label="حجم (گیگ)">
          <input className={inputClass} type="number" min={1} step="1" value={gb} onChange={(e) => setGb(e.target.value)} required />
        </Field>
        <Field label="تاریخ پایان">
          <div className="mb-2 flex flex-wrap gap-2">
            {[31, 62, 93].map((days) => (
              <Button key={days} tone="soft" onClick={() => setEnd(addJalaliDays(today, days))}>+{faDigits(days)} روز</Button>
            ))}
          </div>
          <JalaliDateField value={end} onChange={setEnd} minExclusive={today} />
        </Field>
        <Field label="یادداشت مشتری">
          <textarea className={inputClass} value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} rows={3} />
        </Field>
      </div>
      <div className="sticky bottom-0 z-20 space-y-3 rounded-2xl border border-slate-200 bg-white p-4 shadow-lg lg:top-20 lg:self-start">
        <div className="font-bold">خلاصه هزینه</div>
        <QuoteBox quote={quote} />
        {quoteError ? <Banner>{quoteError}</Banner> : null}
        <Button type="submit" className="w-full" disabled={pending || !quote?.consent_token}>
          {pending ? "در حال ساخت…" : "ثبت اشتراک"}
        </Button>
      </div>
    </form>
  );
}
