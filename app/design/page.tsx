import { notFound } from "next/navigation";
import { BrandMark, Icon, type IconName } from "@/components/Icons";
import { Badge, Banner, Button, Field, inputClass } from "@/components/ui";
import { PageHead } from "@/components/Design";

const swatches = [
  ["یشمی", "#07584f", "--pc-brand"],
  ["یشمی تیره", "#063c36", "--pc-brand-deep"],
  ["پس‌زمینه", "#f7f4ef", "--pc-bg"],
  ["سطح", "#fffefc", "--pc-surface"],
  ["متن", "#162725", "--pc-text"],
];

const entries: [string, IconName][] = [
  ["داشبورد", "home"], ["پنل‌ها", "panels"], ["محصولات", "product"], ["همکاران", "users"],
  ["اشتراک‌ها", "layers"], ["دفترکل", "receipt"], ["عملیات ناتمام", "clock"],
];

export default function DesignPage() {
  if (process.env.NODE_ENV === "production") notFound();
  return <div className="pc-shell">
    <aside className="pc-side pc-desktop-only" aria-hidden="true">
      <div className="pc-side-brand"><BrandMark/></div><p className="pc-side-role">مدیریت سیستم</p>
      <nav>{entries.map(([label, icon]) => <span className="pc-nav-link" key={label}><Icon name={icon}/>{label}</span>)}</nav>
      <div className="pc-side-account">
        <div className="pc-account"><span className="pc-avatar">م</span><span className="text-sm font-bold">مدیر سیستم</span></div>
        <div className="pc-quiet my-3 flex gap-2"><Icon name="exit"/>خروج از حساب</div>
      </div>
    </aside>
    <div className="pc-shell-content">
      <header className="pc-top">
        <span className="pc-role-indicator pc-desktop-only">پنل مدیریت</span>
        <span className="pc-mobile-only"><BrandMark/></span>
        <span className="pc-account pc-desktop-only"><span>مدیر سیستم</span><span className="pc-avatar">م</span></span>
        <span className="pc-mobile-only rounded-lg border px-3 py-2" aria-hidden="true"><Icon name="menu"/></span>
      </header>
      <main className="pc-main space-y-4">
        <PageHead title="سیستم طراحی" description="مرجع بصری مشترک PartnerConsole"/>
        <section className="pc-card space-y-4">
          <h2 className="pc-section-title">رنگ و تایپوگرافی</h2>
          <div className="grid grid-cols-5 gap-2">{swatches.map(([label, hex, name]) => <div className="min-w-0 text-center" key={name}>
            <div className="h-12 rounded-md border" style={{ background: `var(${name})`, borderColor: "var(--pc-border)" }}/>
            <div className="pc-quiet mt-1">{label}</div><bdi dir="ltr" className="block break-all text-[10px] pc-quiet">{hex}</bdi>
          </div>)}</div>
          <div className="border-t pt-3" style={{ borderColor: "var(--pc-border)" }}>
            <strong className="text-2xl">عنوان صفحه · ۱۲٬۵۸۰٬۰۰۰</strong>
            <h3 className="pc-section-title">عنوان بخش</h3><p className="pc-quiet">متن معمولی و اطلاعات کمکی</p>
          </div>
        </section>
        <section className="pc-card space-y-4">
          <h2 className="pc-section-title">کنترل‌ها و وضعیت‌ها</h2>
          <div className="flex flex-wrap items-center gap-2">
            <Button><Icon name="plus"/>عملیات اصلی</Button><Button tone="ghost">عملیات ثانویه</Button>
            <Button tone="danger"><Icon name="trash"/>عملیات حساس</Button>
            <Badge tone="ok">فعال</Badge><Badge tone="warn">نیازمند بررسی</Badge><Badge tone="bad">مسدود</Badge>
          </div>
          <Field label="نام کاربری"><input className={inputClass} defaultValue="arya_net" dir="ltr"/></Field>
          <Banner tone="ok">اطلاعات · وضعیت و راهنمای مرتبط</Banner>
        </section>
      </main>
      <nav aria-hidden="true" className="pc-mobile-nav">
        {([["داشبورد", "home"], ["همکاران", "users"], ["اشتراک‌ها", "layers"], ["بیشتر", "more"]] as [string, IconName][])
          .map(([label, icon]) => <span className="flex flex-1 flex-col items-center justify-center gap-1 text-[11px] pc-quiet" key={label}><Icon name={icon}/>{label}</span>)}
      </nav>
    </div>
  </div>;
}
