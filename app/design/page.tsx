import Link from "next/link";

export default function DesignPage() {
  return (
    <main className="mx-auto max-w-3xl space-y-6 p-4">
      <h1 className="text-2xl font-bold">ظاهر پنل</h1>
      <p className="text-slate-600">سایدبار تیره، دکمهٔ اصلی به رنگ سبزآبی، جدول و تقویم شمسی. فونت وزیرمتن از داخل پروژه لود می‌شود.</p>
      <div className="flex flex-wrap gap-3">
        <span className="inline-flex min-h-11 items-center rounded-xl bg-brand px-4 text-white">دکمه اصلی</span>
        <span className="inline-flex min-h-11 items-center rounded-xl border border-slate-300 bg-white px-4">دکمه ثانویه</span>
        <span className="inline-flex min-h-11 items-center rounded-xl bg-red-700 px-4 text-white">عملیات خطرناک</span>
      </div>
      <div className="overflow-hidden rounded-2xl border">
        <div className="bg-ink px-4 py-3 text-white">سایدبار</div>
        <div className="bg-white p-4 text-sm">۱۴۰۵/۰۸/۲۹ · ۱۲٬۵۰۰ تومان</div>
      </div>
      <Link href="/" className="text-brand">بازگشت</Link>
    </main>
  );
}
