import { notFound } from "next/navigation";
import { BrandMark, IconTile } from "@/components/Icons";
import { Badge, Banner, Button, Field, inputClass } from "@/components/ui";
import { PageHead } from "@/components/Design";

const swatches=[
  ["یشمی","--pc-brand"],["یشمی تیره","--pc-brand-deep"],["پس‌زمینه","--pc-bg"],
  ["سطح","--pc-surface"],["متن","--pc-text"],
];

export default function DesignPage() {
  if (process.env.NODE_ENV==="production") notFound();
  return <div className="pc-shell">
    <header className="pc-top"><BrandMark/><IconTile name="home"/></header>
    <main className="mx-auto max-w-5xl p-4 sm:p-8 space-y-4">
      <PageHead title="سیستم طراحی" description="مرجع بصری مشترک PartnerConsole"/>
      <section className="pc-card space-y-4">
        <h2 className="pc-section-title">رنگ و تایپوگرافی</h2>
        <div className="grid grid-cols-5 gap-2">{swatches.map(([label,name])=><div key={name}>
          <div className="h-12 rounded-md border" style={{background:`var(${name})`,borderColor:"var(--pc-border)"}}/>
          <div className="pc-quiet mt-1">{label}</div>
        </div>)}</div>
        <div className="border-t pt-3" style={{borderColor:"var(--pc-border)"}}>
          <strong className="text-2xl">عنوان صفحه · ۱۲٬۵۸۰٬۰۰۰</strong>
          <h3 className="pc-section-title">عنوان بخش</h3><p className="pc-quiet">متن معمولی و اطلاعات کمکی</p>
        </div>
      </section>
      <section className="pc-card space-y-4">
        <h2 className="pc-section-title">کنترل‌ها و وضعیت‌ها</h2>
        <div className="flex flex-wrap items-center gap-2"><Button>عملیات اصلی</Button><Button tone="ghost">عملیات ثانویه</Button><Button tone="danger">عملیات حساس</Button>
          <Badge tone="ok">فعال</Badge><Badge tone="warn">نیازمند بررسی</Badge><Badge tone="bad">مسدود</Badge></div>
        <Field label="نام کاربری"><input className={inputClass} defaultValue="arya_net" dir="ltr"/></Field>
        <Banner tone="ok">اطلاعات · وضعیت و راهنمای مرتبط</Banner>
      </section>
    </main>
  </div>;
}
