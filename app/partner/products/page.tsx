"use client";

import { Facts, PageHead, ResponsiveTable } from "@/components/Design";
import { Badge, Banner } from "@/components/ui";
import { formatDays, formatGb, formatToman } from "@/lib/format";
import { usePartnerDashboard } from "@/lib/partner-dashboard";

export default function PartnerProducts() {
  const {data, error} = usePartnerDashboard();
  return <>
    <PageHead title="اعتبار و نرخ محصولات" description="اعتبار اختصاصی و تعرفهٔ هر محصول" />
    {error ? <Banner>{error}</Banner> : !data ? <p role="status" className="pc-card pc-quiet">در حال بارگیری…</p> :
      <ResponsiveTable label="اعتبار و نرخ محصولات"
        columns={["محصول", "اعتبار حجم", "اعتبار روز", "نرخ هر گیگ", "نرخ هر روز", "وضعیت"]}
        empty="محصولی برای نمایش وجود ندارد." rows={data.credits.map(item => {
          const status = <Badge tone={item.active ? "ok" : "slate"}>{item.active ? "قابل ساخت" : "غیرفعال برای ساخت"}</Badge>;
          const gb=formatGb(item.credit_gb), days=formatDays(item.credit_days);
          const gbRate=formatToman(item.price_per_gb), dayRate=formatToman(item.price_per_day);
          return {id:item.id,cells:[item.name,gb,days,gbRate,dayRate,status],title:item.name,aside:status,
            summary:<Facts rows={[["اعتبار حجم",gb],["اعتبار روز",days],["نرخ هر گیگ",gbRate],["نرخ هر روز",dayRate]]}/>};
        })} />}
  </>;
}
