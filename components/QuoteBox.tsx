"use client";

import type { ReactNode } from "react";
import { Facts } from "@/components/Design";
import { Banner } from "@/components/ui";
import type { Quote } from "@/lib/types";
import { formatDate, formatDays, formatGb, formatToman } from "@/lib/format";

export function QuoteBox({ quote }: { quote: Quote | null }) {
  if (!quote) return <p className="pc-quiet" role="status">خلاصهٔ هزینه پس از دریافت پیش‌فاکتور نمایش داده می‌شود.</p>;

  const rows: [string, ReactNode][] = [
    ["مبدأ", <bdi dir="ltr">{formatDate(quote.base_jalali)}</bdi>],
    ["پایان", <bdi dir="ltr">{formatDate(quote.end_jalali)}</bdi>],
    ["حجم و مدت لازم", <>{formatGb(quote.need_gb)} و {formatDays(quote.need_days)}</>],
    ["سهم اعتبار", <>{formatGb(quote.gb_from_credit)} و {formatDays(quote.days_from_credit)}</>],
    ["سهم کیف پول", <>{formatGb(quote.gb_paid)} و {formatDays(quote.days_paid)}</>],
  ];
  if (Number(quote.gb_refund) > 0 || quote.days_refund > 0) {
    rows.push(["برگشت به اعتبار", <>{formatGb(quote.gb_refund)} و {formatDays(quote.days_refund)}</>]);
  }
  if (quote.used_gb) rows.push(["مصرف فعلی", formatGb(quote.used_gb)]);

  return (
    <div className="space-y-3 text-sm">
      <Facts rows={rows}/>
      <div className="pc-quote-total">
        <Facts rows={[["برداشت از کیف پول", formatToman(quote.toman)]]}/>
      </div>
      {quote.message ? <Banner>{quote.message}</Banner> :
        !quote.affordable && <Banner>کسری موجودی: {formatToman(quote.shortage_toman)}</Banner>}
    </div>
  );
}
