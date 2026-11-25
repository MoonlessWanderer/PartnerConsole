"use client";

import { DraftLeaveWarning } from "@/components/DraftLeaveWarning";

import { PageHead, Facts, WalletDebit } from "@/components/Design";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { JalaliDateField } from "@/components/JalaliDateField";
import { PurchaseWalletBreakdown } from "@/components/PurchaseWalletBreakdown";
import { Banner } from "@/components/ui";
import styles from "@/components/SubscriptionPurchase.module.css";
import { api, ApiError, sessionToken } from "@/lib/api";
import { formatDate, formatDays, formatGb, formatToman } from "@/lib/format";
import { addJalaliDays, parseJalali, todayJalali } from "@/lib/jalali";
import type { Product, Quote } from "@/lib/types";

type PurchaseQuote = Quote & { gb_cost_toman: number; days_cost_toman: number };

const usernamePattern = "[A-Za-z0-9_]{3,32}";
const validUsername = new RegExp(`^${usernamePattern}$`);
const initialTrafficGb = "10";

export default function NewSubscriptionPage() {
  const router = useRouter();
  const [reviewSession] = useState(sessionToken);
  const [today] = useState(todayJalali);
  const [products, setProducts] = useState<Product[] | null>(null);
  const [draft, setDraft] = useState({ product_id: 0, username: "", traffic_cap_gb: initialTrafficGb, end_date: "", note: "" });
  const [calendarOpen, setCalendarOpen] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const [quoteVersion, setQuoteVersion] = useState(0);
  const [error, setError] = useState("");
  const [reprice, setReprice] = useState("");
  const [followUp, setFollowUp] = useState("");
  const [pending, setPending] = useState(false);
  const submitting = useRef(false);
  const dateControl = useRef<HTMLButtonElement>(null);

  const request = useMemo(() => ({ draft, version: quoteVersion }), [draft, quoteVersion]);
  const [review, setReview] = useState<{ request: typeof request; quote?: PurchaseQuote; error?: string } | null>(null);
  const quote = review?.request === request ? review.quote : undefined;
  const quoteError = review?.request === request ? review.error : undefined;
  const product = products?.find((item) => item.id === draft.product_id);
  const volume = Number(draft.traffic_cap_gb);
  const canQuote = !!product && validUsername.test(draft.username) && Number.isInteger(volume) &&
    volume >= 1 && volume <= 1_000_000 && !!parseJalali(draft.end_date) && draft.end_date > today;
  const inputsLocked = pending || Boolean(followUp);
  const dirty = Boolean(draft.username || draft.note || draft.end_date) ||
    draft.traffic_cap_gb !== initialTrafficGb || draft.product_id !== (products?.[0]?.id ?? 0);
  useEffect(() => {
    if (reviewOpen) heading.current?.focus();
  }, [reviewOpen]);

  useEffect(() => {
    let current = true;
    api<{ items: Product[] }>("/partner/products", { reviewSession }).then((data) => {
      if (!current) return;
      const active = data.items.filter((item) => item.active);
      setProducts(active);
      if (active[0]) setDraft((old) => ({ ...old, product_id: active[0].id }));
    }).catch((err) => {
      if (current) setError(err.message);
    });
    return () => { current = false; };
  }, [reviewSession]);

  useEffect(() => {
    if (!canQuote) return;
    let current = true;
    const timer = window.setTimeout(() => {
      api<PurchaseQuote>("/partner/subscriptions/quote", {
        method: "POST",
        reviewSession,
        body: { action: "create", ...request.draft },
      }).then((data) => {
        if (!current) return;
        setProducts((items) => items?.map((item) => item.id === request.draft.product_id
          ? { ...item, credit_gb: data.credit_gb, credit_days: data.credit_days,
            price_per_gb: data.price_per_gb, price_per_day: data.price_per_day }
          : item) ?? null);
        setReview({ request, quote: data });
      }).catch((err) => {
        if (current) setReview({ request, error: err.message });
      });
    }, 250);
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [canQuote, request, reviewSession]);

  function change<K extends keyof typeof draft>(key: K, value: (typeof draft)[K]) {
    setDraft((old) => ({ ...old, [key]: value }));
    setError("");
    setReprice("");
  }

  function chooseEnd(value: string) {
    change("end_date", value);
    setCalendarOpen(false);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if ((!reviewOpen && window.matchMedia("(max-width: 680px)").matches) || submitting.current || !quote?.affordable || !quote.consent_token || sessionToken() !== reviewSession) return;
    submitting.current = true;
    setPending(true);
    setError("");
    try {
      const created = await api<{ id: number }>("/partner/subscriptions", {
        method: "POST",
        reviewSession,
        body: { ...request.draft, note: request.draft.note || null, consent_token: quote.consent_token },
      });
      router.replace(`/partner/subscriptions/${created.id}`);
      return;
    } catch (err) {
      if (err instanceof ApiError && err.code === "financial_consent") {
        setFollowUp("");
        setReprice(err.message);
        setReview(null);
        setQuoteVersion((version) => version + 1);
      } else if (err instanceof ApiError && (err.code === "operation_status" || err.status === 0)) {
        setFollowUp(err.message);
      } else {
        setError(err instanceof Error ? err.message : "ساخت ناموفق بود");
      }
      submitting.current = false;
      setPending(false);
    }
  }

  return (
    <div className={styles.page}>
      <DraftLeaveWarning dirty={!inputsLocked && dirty}/>
      <PageHead title={reviewOpen ? "بررسی پیش‌فاکتور" : "ساخت اشتراک"} back={{href:"/partner/subscriptions",label:"اشتراک‌ها"}} />
      <form className={styles.layout} data-review={reviewOpen} onSubmit={submit}>
        <section className={styles.card+" pc-purchase-fields"} aria-labelledby="purchase-fields" hidden={reviewOpen}>
          <h2 id="purchase-fields">مشخصات اشتراک</h2>
          {products === null && !error && <p className={styles.empty} role="status">در حال بارگیری محصولات…</p>}
          {products?.length === 0 && <p className={styles.empty}>محصول فعالی برای ساخت اشتراک وجود ندارد.</p>}
          <fieldset className={styles.fieldset} disabled={inputsLocked || !products?.length}>
            <div className={styles.field}>
              <label htmlFor="purchase-product">محصول</label>
              <select id="purchase-product" value={draft.product_id} onChange={(event) => change("product_id", Number(event.target.value))}>
                {products?.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} — اعتبار {formatGb(item.credit_gb)} / {item.credit_days == null ? "—" : formatDays(item.credit_days)}
                  </option>
                ))}
              </select>
            </div>
            {product && <dl className={styles.productFacts} aria-label="نرخ و اعتبار محصول">
              <div><dt>نرخ هر گیگ</dt><dd>{formatToman(product.price_per_gb)}</dd></div>
              <div><dt>نرخ هر روز</dt><dd>{formatToman(product.price_per_day)}</dd></div>
              <div><dt>اعتبار حجم</dt><dd>{formatGb(product.credit_gb)}</dd></div>
              <div><dt>اعتبار روز</dt><dd>{product.credit_days == null ? "—" : formatDays(product.credit_days)}</dd></div>
            </dl>}
            <div className={styles.fieldsRow}>
              <div className={styles.field}>
                <label htmlFor="purchase-username">نام کاربری</label>
                <input id="purchase-username" className={styles.ltr} dir="ltr" required pattern={usernamePattern}
                  minLength={3} maxLength={32} autoComplete="off" autoCapitalize="none" spellCheck={false}
                  value={draft.username} onChange={(event) => change("username", event.target.value)} aria-describedby="purchase-username-hint" />
              </div>
              <div className={styles.field}>
                <label htmlFor="purchase-volume">حجم (گیگ)</label>
                <input id="purchase-volume" type="number" min={1} max={1_000_000} step={1} required
                  value={draft.traffic_cap_gb} onChange={(event) => change("traffic_cap_gb", event.target.value)} />
              </div>
            </div>
            <p className={styles.hint} id="purchase-username-hint">۳ تا ۳۲ نویسه از حروف انگلیسی، عدد یا زیرخط</p>
            <div className={styles.field}>
              <div className={styles.fieldLabel} id="purchase-expiry">تاریخ پایان شمسی</div>
              <div className={styles.dateActions}>
                <button type="button" ref={dateControl} className={styles.dateToggle} onClick={() => setCalendarOpen(!calendarOpen)}
                  aria-controls="purchase-calendar" aria-expanded={calendarOpen}>
                  <svg aria-hidden="true" width="17" height="17" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.4">
                    <rect x="3" y="5" width="14" height="12" rx="2" />
                    <path strokeLinecap="round" d="M3 9h14M7 3v4m6-4v4" />
                  </svg>
                  {draft.end_date ? <bdi dir="ltr">{formatDate(draft.end_date)}</bdi> : "انتخاب تاریخ"}
                </button>
                <div className={styles.terms} role="group" aria-label="مدت‌های پیشنهادی">
                  {[31, 62, 93].map((days) => {
                    const last = addJalaliDays(today, days);
                    return <button type="button" key={days} aria-pressed={draft.end_date === last} onClick={() => chooseEnd(last)}>{formatDays(days)}</button>;
                  })}
                </div>
              </div>
              <div id="purchase-calendar" hidden={!calendarOpen} role="group" aria-labelledby="purchase-expiry">
                {calendarOpen && <JalaliDateField value={draft.end_date} minExclusive={today}
                  onChange={(value) => { chooseEnd(value); dateControl.current?.focus(); }} />}
              </div>
            </div>
            <details className={styles.note}>
              <summary>یادداشت مشتری <span>اختیاری</span></summary>
              <textarea className={styles.noteInput} aria-label="یادداشت مشتری" rows={2} maxLength={2000}
                value={draft.note} onChange={(event) => change("note", event.target.value)} />
            </details>
          </fieldset>
          <div className="pc-mobile-review">
            {!reviewOpen && (error || quoteError) && <div className="mb-2"><Banner>{error||quoteError}</Banner></div>}
            <button type="button" className={styles.submit} disabled={!quote && !quoteError && !error}
              onClick={()=>setReviewOpen(true)}>مشاهده پیش‌فاکتور</button>
          </div>
        </section>

        <section className={`${styles.card} ${styles.quote}`} aria-labelledby="purchase-quote" data-review-open={reviewOpen}>
          <div className={styles.quoteHeading}>
            <h2 id="purchase-quote" ref={heading} tabIndex={-1}>پیش‌فاکتور</h2>
            {quote && <p>{formatGb(quote.need_gb)} · {formatDays(quote.need_days)}</p>}
          </div>
          <div className={styles.messages} aria-live="polite">
            {error && <Banner>{error}</Banner>}
            {followUp && <Banner tone="warn">{followUp}</Banner>}
            {reprice && <Banner tone="warn">{reprice}</Banner>}
            {quoteError && <Banner>{quoteError}</Banner>}
          </div>
          {quote ? (
            <>
              <dl className={styles.identity} aria-label="اشتراک مقصد">
                <div><dt>نام کاربری</dt><dd><bdi dir="ltr">{request.draft.username.toLowerCase()}</bdi></dd></div>
                <div><dt>محصول</dt><dd>{quote.product_name}</dd></div>
              </dl>
              <section className={styles.credit} aria-labelledby="purchase-credit-title">
                <h3 id="purchase-credit-title">از اعتبار محصول</h3>
                <dl>
                  <div><dt>اعتبار حجم مصرفی</dt><dd>{formatGb(quote.gb_from_credit)}</dd></div>
                  <div><dt>اعتبار روز مصرفی</dt><dd>{formatDays(quote.days_from_credit)}</dd></div>
                </dl>
              </section>
              <PurchaseWalletBreakdown
                gb={quote.gb_paid} days={quote.days_paid}
                gbRate={quote.price_per_gb} dayRate={quote.price_per_day}
                gbCost={quote.gb_cost_toman} dayCost={quote.days_cost_toman}
              />
              {!quote.affordable && <p className={styles.notice} role="status">
                {quote.message && <span>{quote.message}</span>}
                {quote.shortage_toman > 0 && <>کسری موجودی <strong>{formatToman(quote.shortage_toman)}</strong></>}
              </p>}
              <WalletDebit amount={quote.toman} />
            </>
          ) : !quoteError && (
            <p className={styles.empty} role="status">
              {canQuote ? "در حال دریافت پیش‌فاکتور…" : "برای دریافت پیش‌فاکتور، نام کاربری، حجم و تاریخ پایان را تکمیل کنید."}
            </p>
          )}
          <button className={styles.submit} type="submit" disabled={pending || !quote?.affordable || !quote.consent_token}>
            {pending ? "در حال بررسی…" : followUp ? "پیگیری همان عملیات" : "ثبت اشتراک"}
          </button>
          {reviewOpen ? <button className="pc-button pc-button-outline mt-2" type="button" disabled={inputsLocked}
            onClick={()=>setReviewOpen(false)}>بازگشت به مشخصات</button> :
            <button className="pc-button pc-button-outline pc-desktop-review mt-2" type="button" disabled={!quote}
              onClick={()=>setReviewOpen(true)}>بررسی پیش‌فاکتور</button>}
        </section>
        {reviewOpen&&<aside className="pc-card">
          <h2 className="pc-section-title">خلاصه درخواست</h2>
          <Facts rows={[["حجم درخواستی",formatGb(quote?.need_gb)],["مدت درخواستی",quote?formatDays(quote.need_days):"—"],
            ["تاریخ پایان",<bdi dir="ltr">{formatDate(request.draft.end_date)}</bdi>],
            ["موجودی کیف پول",quote?formatToman(quote.balance_toman):"—"]]}/>
        </aside>}
      </form>
    </div>
  );
}
