"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { formatToman } from "@/lib/format";
import { Product } from "@/lib/types";
import { Badge, Banner, Button, Field, inputClass } from "@/components/ui";
import { ActionLink, Facts, PageHead, ResponsiveTable } from "@/components/Design";

type Panel = { id: number; name: string };
type Plan = { id: number; name: string; reset_traffic_method: number | null; warnings: string[]; device_limit: number | null; speed_limit: number | null };

export default function ProductsPage() {
  const [items, setItems] = useState<Product[]>([]);
  const [panels, setPanels] = useState<Panel[]>([]);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [error, setError] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [saved, setSaved] = useState(false);
  const router = useRouter();
  const open = usePathname() === "/admin/products/new";
  const inFlight = useRef(false);
  const planRequest = useRef(0);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
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
    load().catch((err) => setError(err.message)).finally(() => setLoading(false));
  }, []);

  async function loadPlans(panelId: number) {
    setPlans([]);
    setWarnings([]);
    const version = ++planRequest.current;
    if (!panelId) return;
    try {
      const data = await api<{ items: Plan[] }>(`/admin/panels/${panelId}/plans`);
      if (version === planRequest.current) setPlans(data.items);
    } catch (err) {
      if (version === planRequest.current) setError(err instanceof Error ? err.message : "خواندن پلن ناموفق بود");
    }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (inFlight.current || !form.panel_id || !form.plan_id) return;
    inFlight.current = true;
    setSaving(true);
    setError("");
    try {
      const data = await api<{ warnings: string[] }>("/admin/products", { method: "POST", body: form });
      setWarnings(data.warnings || []);
      if (!data.warnings?.length) router.push("/admin/products");
      else setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "خطا");
    } finally {
      inFlight.current = false;
      setSaving(false);
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

  if (open) {
    const plan=plans.find(item=>item.id===form.plan_id);
    return <div className="space-y-4">
      <PageHead title="محصول جدید" back={{href:"/admin/products",label:"محصولات"}} />
      {error && <Banner>{error}</Banner>}
      <div className="pc-grid-two">
        <form className="pc-card pc-form-card" onSubmit={save}>
            <h2 className="pc-section-title">مشخصات محصول</h2>
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
            <Field label="وضعیت"><select className={inputClass} value={form.active?"active":"disabled"} onChange={e=>setForm({...form,active:e.target.value==="active"})}>
              <option value="active">فعال</option><option value="disabled">غیرفعال</option>
            </select></Field>
            {saved && <Banner tone="ok">محصول ثبت شد.</Banner>}
            <div className="flex flex-wrap gap-2"><Button type="submit" disabled={saving||saved||!form.plan_id}>{saving?"در حال ثبت…":"ساخت محصول"}</Button>
              <ActionLink href="/admin/products" secondary>{saved?"بازگشت به محصولات":"انصراف"}</ActionLink></div>
          </form>
        <section className="pc-card">
          <h2 className="pc-section-title">پلن انتخاب‌شده</h2>
          <Facts rows={[
            ["نام پلن",plan?.name||"—"],["پنل",panels.find(item=>item.id===form.panel_id)?.name||"—"],
            ["محدودیت دستگاه",plan?.device_limit??"—"],["محدودیت سرعت",plan?.speed_limit??"—"],
            ["روش ریست ترافیک",plan?.reset_traffic_method??"—"],
          ]}/>
        </section>
      </div>
    </div>;
  }
  return <div className="space-y-4">
    <PageHead title="محصولات" description="مدیریت محصولات و نرخ‌های فروش" action={<ActionLink href="/admin/products/new" icon="plus">محصول جدید</ActionLink>}/>
    {error && <Banner>{error}</Banner>}
    {loading ? <p className="pc-card pc-quiet" role="status">در حال بارگیری محصولات…</p> :
      <ResponsiveTable label="محصولات" icon="product" columns={["نام محصول","پنل","پلن","دامنه","نرخ هر گیگ","نرخ هر روز","وضعیت","عملیات"]}
        empty="محصولی نیست." rows={items.map(product=>{
          const badge=<Badge tone={product.active?"ok":"slate"}>{product.active?"فعال":"غیرفعال"}</Badge>;
          const action=<Button tone="ghost" onClick={()=>toggle(product)}>{product.active?"خاموش کردن":"روشن کردن"}</Button>;
          return {id:product.id,cells:[product.name,product.panel_name,product.plan_id,<bdi dir="ltr" className="break-all">{product.email_domain}</bdi>,formatToman(product.price_per_gb),formatToman(product.price_per_day),badge,action],
            title:product.name,summary:<><span>{product.panel_name} · {product.plan_id}</span><div><bdi dir="ltr">{product.email_domain}</bdi></div><div>{formatToman(product.price_per_gb)} / گیگ · {formatToman(product.price_per_day)} / روز</div></>,aside:badge,action};
        })}/>}
  </div>;
}
