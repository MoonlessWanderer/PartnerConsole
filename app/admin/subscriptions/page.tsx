"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { STATUS_LABEL, formatDate, formatDays, formatGb } from "@/lib/format";
import { SubscriptionRow } from "@/lib/types";
import { Badge, Banner, Button, TableWrap, inputClass } from "@/components/ui";
import { Modal } from "@/components/Modal";

function tone(status: string): "ok" | "warn" | "bad" | "slate" {
  if (status === "active") return "ok";
  if (status === "expired") return "warn";
  if (status.startsWith("banned")) return "bad";
  return "slate";
}

export default function AdminSubscriptions() {
  const [items, setItems] = useState<SubscriptionRow[]>([]);
  const [q, setQ] = useState("");
  const [error, setError] = useState("");
  const [current, setCurrent] = useState<SubscriptionRow | null>(null);

  async function load(search = q) {
    const data = await api<{ items: SubscriptionRow[] }>(`/admin/subscriptions?q=${encodeURIComponent(search)}`);
    setItems(data.items);
  }
  useEffect(() => {
    load("").catch((err) => setError(err.message));
  }, []);

  async function open(id: number) {
    setError("");
    try {
      setCurrent(await api<SubscriptionRow>(`/admin/subscriptions/${id}`));
    } catch (err) {
      setError(err instanceof Error ? err.message : "خطا");
    }
  }

  async function ban(banned: boolean) {
    if (!current) return;
    const agreed = window.confirm(banned ? "این اشتراک مسدود شود؟ زمانش همچنان می‌گذرد." : "مسدودی این اشتراک برداشته شود؟");
    if (!agreed) return;
    setError("");
    try {
      await api(`/admin/subscriptions/${current.id}/${banned ? "ban" : "unban"}`, { method: "POST" });
      await open(current.id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "تغییر مسدودی ناموفق بود");
    }
  }

  return (
    <div className="space-y-4">
      <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); load().catch((err) => setError(err.message)); }}>
        <input className={inputClass} placeholder="جستجوی نام کاربری" value={q} onChange={(e) => setQ(e.target.value)} />
        <Button type="submit">جستجو</Button>
      </form>
      {error && !current ? <Banner>{error}</Banner> : null}
      <TableWrap>
        <table>
          <thead>
            <tr><th>کاربر</th><th>همکار</th><th>محصول</th><th>سقف</th><th>انقضا</th><th>مانده روز</th><th>وضعیت</th><th></th></tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <tr key={row.id}>
                <td>{row.username}</td>
                <td>{row.partner_username}</td>
                <td>{row.product_name}</td>
                <td>{formatGb(row.traffic_cap_gb)}</td>
                <td>{formatDate(row.expired_jalali)}</td>
                <td>{formatDays(row.days_left)}</td>
                <td><Badge tone={tone(row.status)}>{STATUS_LABEL[row.status] || row.status}</Badge></td>
                <td><Button tone="ghost" onClick={() => open(row.id)}>جزئیات</Button></td>
              </tr>
            ))}
            {items.length === 0 ? <tr><td colSpan={8}>اشتراکی نیست.</td></tr> : null}
          </tbody>
        </table>
      </TableWrap>
      {current ? (
        <Modal title={current.username} error={error} onClose={() => setCurrent(null)}>
          <div className="space-y-2 text-sm leading-7">
            <div>محصول: {current.product_name} · پنل: {current.panel_name}</div>
            <div>سقف: {formatGb(current.traffic_cap_gb)} · انقضا: {formatDate(current.expired_jalali)}</div>
            {current.live_error ? <Banner>{current.live_error}</Banner> : (
              <>
                <div>مصرف: {formatGb(current.used_gb)} (آپلود {formatGb(current.upload_gb)} / دانلود {formatGb(current.download_gb)})</div>
                <div className="break-all">لینک: {current.subscribe_url || "از پنل نیامد"}</div>
              </>
            )}
            <div className="pt-2">
              {current.banned ? <Button onClick={() => ban(false)}>رفع مسدودیت</Button> : <Button tone="danger" onClick={() => ban(true)}>مسدود کردن اشتراک</Button>}
            </div>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
