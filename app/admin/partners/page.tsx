"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { formatDays, formatGb, formatToman } from "@/lib/format";
import { Product } from "@/lib/types";
import { Banner, Button, Field, TableWrap, inputClass } from "@/components/ui";
import { Modal } from "@/components/Modal";

type Partner = { id: number; username: string; balance_toman: number; banned: boolean };
type Detail = Partner & { credits: Array<Product & { credit_gb: string; credit_days: number }> };

export default function PartnersPage() {
  const [items, setItems] = useState<Partner[]>([]);
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [selected, setSelected] = useState<Detail | null>(null);
  const [asset, setAsset] = useState("toman");
  const [amount, setAmount] = useState("");
  const [productId, setProductId] = useState(0);
  const adjustmentInFlight = useRef(false);
  const [adjusting, setAdjusting] = useState(false);

  async function load() {
    const data = await api<{ items: Partner[] }>("/admin/partners");
    setItems(data.items);
  }
  useEffect(() => {
    load().catch((err) => setError(err.message));
  }, []);

  async function openPartner(id: number) {
    const data = await api<Detail>(`/admin/partners/${id}`);
    setSelected(data);
    setProductId(data.credits[0]?.id || 0);
  }

  async function viewBalance(id: number) {
    setError("");
    try {
      await openPartner(id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "دریافت موجودی همکار ممکن نشد.");
    }
  }

  async function create(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      await api("/admin/partners", { method: "POST", body: { username, password } });
      setCreateOpen(false);
      setUsername("");
      setPassword("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "خطا");
    }
  }

  async function adjust(event: FormEvent) {
    event.preventDefault();
    if (!selected || adjustmentInFlight.current) return;
    adjustmentInFlight.current = true;
    setAdjusting(true);
    setError("");
    try {
      const data = await api<Pick<Detail, "id" | "balance_toman" | "credits">>(`/admin/partners/${selected.id}/adjust`, {
        method: "POST",
        body: { asset, amount, product_id: asset === "toman" ? null : productId },
      });
      setSelected((current) => current?.id === data.id ? { ...current, ...data } : current);
      setAmount("");
      await load().catch(() => setError("موجودی ثبت شد، اما تازه‌سازی فهرست ممکن نشد."));
    } catch (err) {
      setError(err instanceof Error ? err.message : "خطا");
    } finally {
      adjustmentInFlight.current = false;
      setAdjusting(false);
    }
  }

  async function ban(banned: boolean) {
    if (!selected) return;
    const agreed = window.confirm(banned ? "همکار مسدود شود و اشتراک‌های بازش هم بسته شوند؟" : "فقط اشتراک‌هایی که به‌خاطر مسدودی همکار بسته شده‌اند باز شوند؟");
    if (!agreed) return;
    setError("");
    try {
      await api(`/admin/partners/${selected.id}/${banned ? "ban" : "unban"}`, { method: "POST" });
      await openPartner(selected.id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "خطا");
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-bold">همکاران</h2>
        <Button onClick={() => setCreateOpen(true)}>همکار جدید</Button>
      </div>
      {error && !createOpen ? <Banner>{error}</Banner> : null}
      <TableWrap>
        <table>
          <thead><tr><th>نام کاربری</th><th>تومان</th><th>وضعیت</th><th></th></tr></thead>
          <tbody>
            {items.map((partner) => (
              <tr key={partner.id}>
                <td>{partner.username}</td>
                <td>{formatToman(partner.balance_toman)}</td>
                <td>{partner.banned ? "مسدود" : "فعال"}</td>
                <td><Button tone="soft" onClick={() => viewBalance(partner.id)}>موجودی</Button></td>
              </tr>
            ))}
            {items.length === 0 ? <tr><td colSpan={4}>همکاری نیست.</td></tr> : null}
          </tbody>
        </table>
      </TableWrap>
      {selected ? (
        <div className="grid gap-4 lg:grid-cols-2">
          <form className="space-y-3 rounded-2xl bg-white p-4" onSubmit={adjust}>
            <h3 className="font-bold">تعدیل موجودی {selected.username}</h3>
            <p className="text-sm">مانده تومان: {formatToman(selected.balance_toman)}</p>
            <Field label="دارایی">
              <select className={inputClass} value={asset} onChange={(e) => setAsset(e.target.value)}>
                <option value="toman">تومان سراسری</option>
                <option value="gb">اعتبار گیگ محصول</option>
                <option value="days">اعتبار روز محصول</option>
              </select>
            </Field>
            {asset !== "toman" ? (
              <Field label="محصول">
                <select className={inputClass} value={productId} onChange={(e) => setProductId(Number(e.target.value))}>
                  {selected.credits.map((product) => <option key={product.id} value={product.id}>{product.name}</option>)}
                </select>
              </Field>
            ) : null}
            <Field label="مقدار (منفی برای کسر)">
              <input className={inputClass} value={amount} onChange={(e) => setAmount(e.target.value)} required />
            </Field>
            <Button type="submit" disabled={adjusting}>{adjusting ? "در حال ثبت…" : "اعمال"}</Button>
            <div className="pt-2">
              {selected.banned ? <Button tone="soft" onClick={() => ban(false)}>رفع مسدودیت همکار</Button> : <Button tone="danger" onClick={() => ban(true)}>مسدود کردن همکار</Button>}
            </div>
          </form>
          <div className="space-y-2">
            {selected.credits.map((product) => (
              <div key={product.id} className="rounded-2xl bg-white p-3 text-sm">
                <div className="font-bold">{product.name}</div>
                <div>گیگ: {formatGb(product.credit_gb)} · روز: {formatDays(product.credit_days)}</div>
                <div className="text-slate-500">نرخ {formatToman(product.price_per_gb)} / {formatToman(product.price_per_day)}</div>
              </div>
            ))}
          </div>
        </div>
      ) : null}
      {createOpen ? (
        <Modal title="همکار جدید" error={error} onClose={() => setCreateOpen(false)}>
          <form className="space-y-3" onSubmit={create}>
            <Field label="نام کاربری"><input className={inputClass} value={username} onChange={(e) => setUsername(e.target.value)} required /></Field>
            <Field label="رمز (حداقل ۸ نویسه)"><input className={inputClass} type="password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={8} /></Field>
            <Button type="submit" className="w-full">ساخت</Button>
          </form>
        </Modal>
      ) : null}
    </div>
  );
}
