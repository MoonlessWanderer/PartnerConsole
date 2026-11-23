"use client";

import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { formatToman } from "@/lib/format";
import { Product } from "@/lib/types";
import { Banner, Button, Field, TableWrap, inputClass } from "@/components/ui";
import { Modal } from "@/components/Modal";

type Panel = { id: number; name: string };
type Plan = { id: number; name: string; reset_traffic_method: number | null; warnings: string[]; device_limit: number | null; speed_limit: number | null };

export default function ProductsPage() {
  const [items, setItems] = useState<Product[]>([]);
  const [panels, setPanels] = useState<Panel[]>([]);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [error, setError] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ panel_id: 0, plan_id: 0, name: "", email_domain: "", price_per_gb: 0, price_per_day: 0, active: true });

  async function load() {
    const [products, panelRows] = await Promise.all([
      api<{ items: Product[] }>("/admin/products"),
      api<{ items: Panel[] }>("/admin/panels"),
    ]);
    setItems(products.items);
    setPanels(panelRows.items);
  }
  useEffect(() => {
    load().catch((err) => setError(err.message));
  }, []);

  async function loadPlans(panelId: number) {
    setPlans([]);
    setWarnings([]);
    if (!panelId) return;
    try {
      const data = await api<{ items: Plan[] }>(`/admin/panels/${panelId}/plans`);
      setPlans(data.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : "خواندن پلن ناموفق بود");
    }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const data = await api<{ warnings: string[] }>("/admin/products", { method: "POST", body: form });
      setWarnings(data.warnings || []);
      setOpen(false);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "خطا");
    }
  }

  async function toggle(product: Product) {
    setError("");
    try {
      await api(`/admin/products/${product.id}`, {
        method: "PUT",
        body: {
          name: product.name,
          email_domain: product.email_domain,
          price_per_gb: product.price_per_gb,
          price_per_day: product.price_per_day,
          active: !product.active,
        },
      });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "تغییر وضعیت ناموفق بود");
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-bold">محصولات</h2>
        <Button onClick={() => { setOpen(true); setWarnings([]); }}>محصول جدید</Button>
      </div>
      {error && !open ? <Banner>{error}</Banner> : null}
      {warnings.map((item) => <Banner key={item} tone="warn">{item}</Banner>)}
      <TableWrap>
        <table>
          <thead>
            <tr>
              <th>نام</th><th>پنل</th><th>پلن</th><th>دامنه</th><th>هر گیگ</th><th>هر روز</th><th>وضعیت</th><th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((product) => (
              <tr key={product.id}>
                <td>{product.name}</td>
                <td>{product.panel_name}</td>
                <td>{product.plan_id}</td>
                <td>{product.email_domain}</td>
                <td>{formatToman(product.price_per_gb)}</td>
                <td>{formatToman(product.price_per_day)}</td>
                <td>{product.active ? "روشن" : "خاموش"}</td>
                <td><Button tone="ghost" onClick={() => toggle(product)}>{product.active ? "خاموش کردن" : "روشن کردن"}</Button></td>
              </tr>
            ))}
            {items.length === 0 ? <tr><td colSpan={8}>محصولی نیست.</td></tr> : null}
          </tbody>
        </table>
      </TableWrap>
      {open ? (
        <Modal title="محصول جدید" error={error} onClose={() => setOpen(false)}>
          <form className="space-y-3" onSubmit={save}>
            <Field label="پنل">
              <select className={inputClass} value={form.panel_id} onChange={(e) => { const id = Number(e.target.value); setForm({ ...form, panel_id: id, plan_id: 0 }); loadPlans(id); }} required>
                <option value={0}>انتخاب کنید</option>
                {panels.map((panel) => <option key={panel.id} value={panel.id}>{panel.name}</option>)}
              </select>
            </Field>
            <Field label="پلن V2Board">
              <select className={inputClass} value={form.plan_id} onChange={(e) => {
                const id = Number(e.target.value);
                const plan = plans.find((item) => item.id === id);
                setForm({ ...form, plan_id: id, name: form.name || plan?.name || "" });
                setWarnings(plan?.warnings || []);
              }} required>
                <option value={0}>انتخاب کنید</option>
                {plans.map((plan) => <option key={plan.id} value={plan.id}>{plan.name} — محدودیت دستگاه {plan.device_limit ?? "—"} / سرعت {plan.speed_limit ?? "—"}</option>)}
              </select>
            </Field>
            <Field label="نام نمایشی"><input className={inputClass} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required /></Field>
            <Field label="دامنه ایمیل بدون @">
              <input className={inputClass} value={form.email_domain} onChange={(e) => setForm({ ...form, email_domain: e.target.value })} placeholder="gold.example.com" required />
            </Field>
            <p className="text-xs leading-6 text-amber-800">دامنه باید از اعتبارسنجی email:strict پنل رد شود. از .local پرهیز کنید مگر پنل شما آن را بپذیرد.</p>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="تومان هر گیگ"><input className={inputClass} type="number" min={0} value={form.price_per_gb} onChange={(e) => setForm({ ...form, price_per_gb: Number(e.target.value) })} required /></Field>
              <Field label="تومان هر روز"><input className={inputClass} type="number" min={0} value={form.price_per_day} onChange={(e) => setForm({ ...form, price_per_day: Number(e.target.value) })} required /></Field>
            </div>
            {warnings.map((item) => <Banner key={item} tone="warn">{item}</Banner>)}
            <Button type="submit" className="w-full">ساخت محصول</Button>
          </form>
        </Modal>
      ) : null}
    </div>
  );
}
