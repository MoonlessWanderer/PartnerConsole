"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { STATUS_LABEL, formatDate, formatDays, formatGb } from "@/lib/format";
import { SubscriptionRow } from "@/lib/types";
import { Badge, Button, TableWrap, inputClass } from "@/components/ui";

export default function PartnerSubscriptions() {
  const [items, setItems] = useState<SubscriptionRow[]>([]);
  const [q, setQ] = useState("");
  const [error, setError] = useState("");
  async function load(search = q) {
    const data = await api<{ items: SubscriptionRow[] }>(`/partner/subscriptions?q=${encodeURIComponent(search)}`);
    setItems(data.items);
  }
  useEffect(() => {
    load("").catch((err) => setError(err.message));
  }, []);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <form className="flex flex-1 gap-2" onSubmit={(e) => { e.preventDefault(); load().catch((err) => setError(err.message)); }}>
          <input className={inputClass} placeholder="نام کاربری" value={q} onChange={(e) => setQ(e.target.value)} />
          <Button type="submit">جستجو</Button>
        </form>
        <Link href="/partner/subscriptions/new" className="inline-flex min-h-11 items-center rounded-xl bg-brand px-4 text-sm text-white">ساخت</Link>
      </div>
      {error ? <p className="text-red-700">{error}</p> : null}
      <TableWrap>
        <table>
          <thead><tr><th>کاربر</th><th>محصول</th><th>سقف</th><th>انقضا</th><th>روز</th><th>وضعیت</th><th></th></tr></thead>
          <tbody>
            {items.map((row) => (
              <tr key={row.id}>
                <td>{row.username}</td>
                <td>{row.product_name}</td>
                <td>{formatGb(row.traffic_cap_gb)}</td>
                <td>{formatDate(row.expired_jalali)}</td>
                <td>{formatDays(row.days_left)}</td>
                <td><Badge tone={row.status === "active" ? "ok" : row.status === "expired" ? "warn" : "bad"}>{STATUS_LABEL[row.status]}</Badge></td>
                <td><Link className="inline-flex min-h-11 items-center text-brand" href={`/partner/subscriptions/${row.id}`}>مدیریت</Link></td>
              </tr>
            ))}
            {items.length === 0 ? <tr><td colSpan={7}>اشتراکی ندارید.</td></tr> : null}
          </tbody>
        </table>
      </TableWrap>
    </div>
  );
}
