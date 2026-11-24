"use client";

import Link from "next/link";
import { ActionLink, NavRow, PageHead, Wallet } from "@/components/Design";
import { IconTile } from "@/components/Icons";
import { Banner } from "@/components/ui";
import { sessionUsername } from "@/lib/api";
import { formatInt, formatToman } from "@/lib/format";
import { usePartnerDashboard } from "@/lib/partner-dashboard";

export default function PartnerHome() {
  const {data, error} = usePartnerDashboard();
  const username = sessionUsername();
  return <>
    <PageHead title={username ? <>سلام، <bdi dir="ltr">{username}</bdi></> : "داشبورد همکار"}
      description="نمای کلی حساب و دسترسی به بخش‌های اصلی" />
    {error && <div className="mb-4"><Banner>{error}</Banner></div>}
    {data ? <div className="pc-partner-metrics">
      <Wallet amount={formatToman(data.balance_toman)} />
      <Link href="/partner/subscriptions" className="pc-card pc-count">
        <IconTile name="layers" />
        <span className="pc-quiet">تعداد اشتراک</span>
        <strong>{formatInt(data.subscription_count)}</strong>
        <span className="pc-quiet">کل اشتراک‌های ساخته‌شده</span>
      </Link>
    </div> : <p className="pc-card pc-quiet mb-4" role="status">{error ? "اطلاعات حساب در دسترس نیست." : "در حال بارگیری داشبورد…"}</p>}
    <div className="pc-dashboard-cta"><ActionLink href="/partner/subscriptions/new" icon="plus">ساخت اشتراک</ActionLink></div>
    <div className="pc-dashboard-shortcuts">
      <div className="pc-card !p-0"><NavRow href="/partner/products" icon="product" title="اعتبار و نرخ محصولات" description="مشاهدهٔ اعتبار حجم و روز و نرخ هر محصول" /></div>
      <div className="pc-card !p-0"><NavRow href="/partner/ledger" icon="receipt" title="تاریخچه تراکنش‌ها" description="سوابق تغییر موجودی و اعتبار" /></div>
    </div>
  </>;
}
