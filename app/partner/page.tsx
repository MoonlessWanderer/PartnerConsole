"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { formatDays, formatGb, formatInt, formatToman } from "@/lib/format";
import { Product } from "@/lib/types";
import { Card } from "@/components/ui";

export default function PartnerHome() {
  const [data, setData] = useState<{ balance_toman: number; subscription_count: number; credits: Product[] } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api<{ balance_toman: number; subscription_count: number; credits: Product[] }>("/partner/dashboard")
      .then(setData)
      .catch((err) => setError(err.message));
  }, []);
  return (
    <div className="space-y-4">
      {error ? <p className="text-red-700">{error}</p> : null}
      <div className="grid gap-3 sm:grid-cols-2">
        <Card>
          <div className="text-sm text-slate-500">موجودی تومان</div>
          <div className="mt-2 text-2xl font-bold">{data ? formatToman(data.balance_toman) : "…"}</div>
        </Card>
        <Card>
          <div className="text-sm text-slate-500">تعداد اشتراک</div>
          <div className="mt-2 text-2xl font-bold">{data ? formatInt(data.subscription_count) : "…"}</div>
          <Link href="/partner/subscriptions/new" className="mt-3 inline-flex min-h-11 items-center text-sm text-brand">ساخت اشتراک</Link>
        </Card>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        {(data?.credits || []).map((product) => (
          <Card key={product.id}>
            <div className="font-bold">{product.name}</div>
            <div className="mt-1 text-sm text-slate-500">{product.active ? "فعال برای ساخت" : "فقط مدیریت قبلی"}</div>
            <div className="mt-2 text-sm">اعتبار {formatGb(product.credit_gb)} · {formatDays(product.credit_days)}</div>
            <div className="text-sm text-slate-500">نرخ {formatToman(product.price_per_gb)} و {formatToman(product.price_per_day)}</div>
          </Card>
        ))}
      </div>
    </div>
  );
}
