"use client";

import { Quote } from "@/lib/types";
import { formatDate, formatDays, formatGb, formatToman } from "@/lib/format";

export function QuoteBox({ quote }: { quote: Quote | null }) {
  if (!quote) return <div className="text-sm text-slate-500">خلاصه هزینه پس از انتخاب حجم و تاریخ می‌آید.</div>;
  return (
    <div className="space-y-1 text-sm leading-7">
      <div>مبدأ: {formatDate(quote.base_jalali)} · پایان: {formatDate(quote.end_jalali)}</div>
      <div>حجم لازم: {formatGb(quote.need_gb)} · روز لازم: {formatDays(quote.need_days)}</div>
      <div>از اعتبار: {formatGb(quote.gb_from_credit)} و {formatDays(quote.days_from_credit)}</div>
      <div>خرید نقدی: {formatGb(quote.gb_paid)} و {formatDays(quote.days_paid)}</div>
      <div className="font-bold">مبلغ: {formatToman(quote.toman)}</div>
      {Number(quote.gb_refund) > 0 || quote.days_refund > 0 ? (
        <div>برگشت به اعتبار: {formatGb(quote.gb_refund)} و {formatDays(quote.days_refund)}</div>
      ) : null}
      {quote.used_gb ? <div>مصرف فعلی: {formatGb(quote.used_gb)}</div> : null}
      {quote.message ? <div className="text-red-700">{quote.message}</div> : null}
      {!quote.affordable && !quote.message ? <div className="text-red-700">کسری: {formatToman(quote.shortage_toman)}</div> : null}
    </div>
  );
}
