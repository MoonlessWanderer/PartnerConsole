"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { formatInt } from "@/lib/format";
import { Banner } from "@/components/ui";
import { NavRow, PageHead } from "@/components/Design";
import { IconTile, type IconName } from "@/components/Icons";

const metrics: Array<{ key: string; label: string; icon: IconName }> = [
  { key: "panels", label: "پنل‌ها", icon: "panels" },
  { key: "products", label: "محصولات", icon: "product" },
  { key: "partners", label: "همکاران", icon: "users" },
  { key: "subscriptions", label: "اشتراک‌ها", icon: "layers" },
  { key: "banned_partners", label: "همکار مسدود", icon: "ban" },
];

export default function AdminHome() {
  const [data, setData] = useState<Record<string, number> | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api<Record<string, number>>("/admin/dashboard").then(setData).catch(err => setError(err.message));
  }, []);

  return <>
    <PageHead title="داشبورد مدیریت" description="نمای کلی سیستم و دسترسی به بخش‌های اصلی" />
    {error && <div className="mb-4"><Banner>{error}</Banner></div>}
    <div className="pc-admin-metrics" aria-busy={!data && !error}>
      {metrics.map(({ key, label, icon }) => <div className="pc-card pc-metric" key={key}>
        <IconTile name={icon} />
        <div><p>{label}</p><strong className={key === "banned_partners" ? "pc-danger-text" : undefined}>{data?.[key] === undefined ? "—" : formatInt(data[key])}</strong></div>
      </div>)}
    </div>
    <div className="pc-dashboard-groups">
      <section className="pc-card">
        <h2 className="pc-section-title">مدیریت منابع</h2>
        <NavRow href="/admin/panels" icon="panels" title="پنل‌های V2Board" description="اتصال و تنظیم پنل‌ها" />
        <NavRow href="/admin/products" icon="product" title="محصولات" description="تعرفه‌ها و پلن‌های قابل ارائه" />
      </section>
      <section className="pc-card">
        <h2 className="pc-section-title">مدیریت حساب‌ها</h2>
        <NavRow href="/admin/partners" icon="users" title="همکاران" description="حساب‌ها و اعتبارها" />
        <NavRow href="/admin/subscriptions" icon="layers" title="اشتراک‌ها" description="مشاهده و بررسی اشتراک‌ها" />
      </section>
      <section className="pc-card">
        <h2 className="pc-section-title">پیگیری و حسابرسی</h2>
        <NavRow href="/admin/operations" icon="clock" title="عملیات ناتمام" description="بررسی وضعیت و ثبت تصمیم" />
        <NavRow href="/admin/ledger" icon="receipt" title="دفترکل" description="بازبینی ثبت‌های مالی" />
      </section>
    </div>
  </>;
}
