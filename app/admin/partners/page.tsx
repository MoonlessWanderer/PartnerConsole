"use client";

import Link from "next/link";
import { useParams, usePathname, useRouter } from "next/navigation";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { formatAmount, formatDays, formatGb, formatToman } from "@/lib/format";
import type { Product } from "@/lib/types";
import { Badge, Banner, Button, Field, inputClass } from "@/components/ui";
import { ActionLink, Facts, NavRow, PageHead, ResponsiveTable, Wallet } from "@/components/Design";
import { Modal } from "@/components/Modal";

type Partner = { id:number; username:string; balance_toman:number; banned:boolean };
type Detail = Partner & { credits:Array<Product & { credit_gb:string; credit_days:number }> };
const root="/admin/partners";

function badge(partner:Partner) {
  return <Badge tone={partner.banned?"bad":"ok"}>{partner.banned?"مسدود":"فعال"}</Badge>;
}

export default function PartnersPage() {
  const params=useParams<{id?:string}>();
  const pathname=usePathname(), router=useRouter();
  const createOpen=pathname===root+"/new";
  const base=root+"/"+params.id;
  const credits=pathname===base+"/credits";
  const wallet=pathname===base+"/wallet";
  const adjustCredit=pathname===base+"/credits/adjust";
  const access=pathname===base+"/access";
  const [items,setItems]=useState<Partner[]|null>(null);
  const [error,setError]=useState("");
  const [username,setUsername]=useState("");
  const [password,setPassword]=useState("");
  const [selected,setSelected]=useState<Detail|null>(null);
  const [asset,setAsset]=useState<"gb"|"days">("gb");
  const [amount,setAmount]=useState("");
  const [productId,setProductId]=useState(0);
  const [confirmation,setConfirmation]=useState<"balance"|"access"|null>(null);
  const [pending,setPending]=useState(false);
  const [notice,setNotice]=useState("");
  const inFlight=useRef(false);

  useEffect(()=>{
    let active=true;
    if (createOpen) return;
    if (params.id) {
      api<Detail>(base).then(item=>{
        if(!active) return;
        setSelected(item);
        const requested=Number(new URLSearchParams(window.location.search).get("product"));
        setProductId(item.credits.some(p=>p.id===requested)?requested:item.credits[0]?.id||0);
      }).catch(err=>{if(active)setError(err.message);});
    } else {
      api<{items:Partner[]}>(root).then(data=>{if(active)setItems(data.items);}).catch(err=>{if(active)setError(err.message);});
    }
    return()=>{active=false;};
  },[params.id,base,createOpen]);

  async function mutation(action:()=>Promise<void>) {
    if (inFlight.current) return;
    inFlight.current=true;setPending(true);setError("");setNotice("");
    try { await action(); }
    catch (err) { setError(err instanceof Error?err.message:"عملیات ناموفق بود."); }
    finally { inFlight.current=false;setPending(false); }
  }

  function create(event:FormEvent) {
    event.preventDefault();
    return mutation(async()=>{
      await api(root,{method:"POST",body:{username,password}});
      router.push(root);
    });
  }

  const kind=wallet?"toman":asset;
  function adjust() {
    if(!selected||confirmation!=="balance") return;
    return mutation(async()=>{
      const data=await api<Pick<Detail,"id"|"balance_toman"|"credits">>(base+"/adjust",{
        method:"POST",body:{asset:kind,amount,product_id:kind==="toman"?null:productId},
      });
      setSelected(current=>current?.id===data.id?{...current,...data}:current);
      setAmount("");setConfirmation(null);setNotice("تعدیل ثبت شد.");
    });
  }

  function changeAccess() {
    if(!selected||confirmation!=="access") return;
    return mutation(async()=>{
      await api(base+(selected.banned?"/unban":"/ban"),{method:"POST"});
      setConfirmation(null);
      setNotice("وضعیت دسترسی تغییر کرد.");
      try { setSelected(await api<Detail>(base)); }
      catch { setError("وضعیت تغییر کرد، اما تازه‌سازی حساب ممکن نشد. صفحه را بازخوانی کنید."); }
    });
  }

  const feedback=<>{error&&!confirmation&&<Banner>{error}</Banner>}{notice&&<Banner tone="ok">{notice}</Banner>}</>;
  if(createOpen) return <div className="space-y-4">
    <PageHead title="ایجاد همکار" back={{href:root,label:"همکاران"}}/>{feedback}
    <form className="pc-card pc-form-card mx-auto max-w-2xl" onSubmit={create}>
      <h2 className="pc-section-title">اطلاعات حساب</h2>
      <Field label="نام کاربری"><input className={inputClass} dir="ltr" value={username} onChange={e=>setUsername(e.target.value)} required /></Field>
      <Field label="رمز (حداقل ۸ نویسه)"><input className={inputClass} type="password" dir="ltr" autoComplete="new-password" minLength={8} value={password} onChange={e=>setPassword(e.target.value)} required /></Field>
      <div className="flex gap-2"><Button type="submit" disabled={pending}>{pending?"در حال ثبت…":"ساخت همکار"}</Button><ActionLink href={root} secondary>انصراف</ActionLink></div>
    </form>
  </div>;

  if (!params.id) return <div className="space-y-4">
    <PageHead title="همکاران" description="مدیریت حساب‌های همکار" action={<ActionLink href={root+"/new"} icon="plus">همکار جدید</ActionLink>}/>
    {feedback}
    {items ? <ResponsiveTable label="همکاران" icon="user" columns={["نام کاربری","موجودی کیف پول","وضعیت حساب","جزئیات"]}
      empty="همکاری نیست." rows={items.map(item=>{
        const name=<bdi dir="ltr">{item.username}</bdi>;
        const balance=formatToman(item.balance_toman), status=badge(item);
        const detail=<ActionLink secondary href={root+"/"+item.id}>جزئیات</ActionLink>;
        return {id:item.id,cells:[name,balance,status,detail],title:name,summary:balance,aside:status,action:detail};
      })}/> : !error&&<p role="status" className="pc-card pc-quiet">در حال بارگیری همکاران…</p>}
  </div>;

  const title=wallet?"تعدیل موجودی":adjustCredit?"تعدیل اعتبار محصول":access?"مدیریت دسترسی همکار":credits?"اعتبارهای همکار":"پروفایل همکار";
  const product=selected?.credits.find(item=>item.id===productId);
  return <div className="space-y-4">
    <PageHead title={title} back={{href:credits||wallet||adjustCredit||access?base:root,label:credits||wallet||adjustCredit||access?"پروفایل همکار":"همکاران"}}/>
    {feedback}
    {!selected ? !error&&<p role="status" className="pc-card pc-quiet">در حال بارگیری حساب…</p> : <>
      <div className="pc-identity">
        <span className="pc-avatar">{selected.username.charAt(0).toUpperCase()}</span>
        <div className="min-w-0"><bdi dir="ltr" className="break-all font-bold">{selected.username}</bdi><div className="pc-quiet">حساب همکار</div></div>
        <div className="ms-auto">{badge(selected)}</div>
      </div>
      {!(wallet||adjustCredit) && <nav className="pc-tabs" aria-label="بخش‌های پروفایل همکار">
        <Link href={base} aria-current={!credits&&!access?"page":undefined}>نمای کلی</Link>
        <Link href={base+"/credits"} aria-current={credits?"page":undefined}>اعتبارها</Link>
        <Link href={base+"/access"} aria-current={access?"page":undefined}>مدیریت</Link>
      </nav>}

      {credits ? <ResponsiveTable label="اعتبارهای همکار" icon="product" columns={["محصول","اعتبار حجم","اعتبار روز","نرخ","عملیات"]}
        empty="اعتبار محصولی برای نمایش وجود ندارد." rows={selected.credits.map(item=>{
          const action=<ActionLink href={base+"/credits/adjust?product="+item.id} secondary>تعدیل</ActionLink>;
          const gb=formatGb(item.credit_gb), days=formatDays(item.credit_days);
          const rate=<>{formatToman(item.price_per_gb)} / گیگ · {formatToman(item.price_per_day)} / روز</>;
          return {id:item.id,cells:[item.name,gb,days,rate,action],title:item.name,summary:<>{gb} · {days}<br/>{rate}</>,action};
        })}/> : wallet||adjustCredit ? <div className="pc-grid-two">
        <form className="pc-card pc-form-card" onSubmit={event=>{event.preventDefault();if(amount.trim()&&(wallet||productId))setConfirmation("balance");}}>
          <h2 className="pc-section-title">تعدیل دارایی</h2>
          {adjustCredit ? <>
            <Field label="نوع دارایی"><select className={inputClass} value={asset} onChange={e=>setAsset(e.target.value==="gb"?"gb":"days")}>
              <option value="gb">اعتبار حجم محصول</option><option value="days">اعتبار روز محصول</option>
            </select></Field>
            <Field label="محصول"><select className={inputClass} value={productId} onChange={e=>setProductId(Number(e.target.value))} required>
              <option value={0} disabled>انتخاب محصول</option>
              {selected.credits.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}
            </select></Field>
          </> : <Facts rows={[["نوع دارایی","تومان سراسری"]]}/>}
          <Field label="مقدار (منفی برای کسر)"><input className={inputClass} dir="ltr" value={amount} onChange={e=>setAmount(e.target.value)} required /></Field>
          <div className="flex gap-2"><Button type="submit" disabled={pending||(!wallet&&!productId)}>بررسی تعدیل</Button><ActionLink href={base} secondary>انصراف</ActionLink></div>
        </form>
        <section className="pc-card">
          <h2 className="pc-section-title">نمای کلی دارایی</h2>
          <Facts rows={[["مانده فعلی",wallet?formatToman(selected.balance_toman):product?(asset==="gb"?formatGb(product.credit_gb):formatDays(product.credit_days)):"—"]]}/>
          <Banner tone="ok">این تغییر در دفترکل مالی ثبت می‌شود.</Banner>
        </section>
      </div> : access ? <section className="pc-card space-y-3">
        <h2 className="pc-section-title">وضعیت حساب</h2>
        <Facts rows={[["دسترسی همکار",badge(selected)]]}/>
        <Banner tone="warn">{selected.banned?"فقط اشتراک‌هایی باز می‌شوند که به‌خاطر مسدودی همکار بسته شده‌اند.":"مسدود کردن همکار، اشتراک‌های باز او را هم می‌بندد."}</Banner>
        <div className="flex gap-2"><Button tone={selected.banned?"primary":"danger"} onClick={()=>setConfirmation("access")}>
          {selected.banned?"رفع مسدودیت همکار":"مسدود کردن همکار"}</Button><ActionLink href={base} secondary>انصراف</ActionLink></div>
      </section> : <>
        <div className="pc-grid-two">
          <Wallet amount={formatToman(selected.balance_toman)}/>
          <section className="pc-card"><h2 className="pc-section-title">اطلاعات حساب</h2>
            <Facts rows={[["نام کاربری",<bdi dir="ltr">{selected.username}</bdi>],["وضعیت",badge(selected)]]}/></section>
        </div>
        <section className="pc-card">
          <h2 className="pc-section-title">مدیریت حساب</h2>
          <NavRow href={base+"/wallet"} icon="wallet" title="تعدیل موجودی" description="افزایش یا کاهش موجودی کیف پول"/>
          <NavRow href={base+"/credits"} icon="product" title="اعتبار محصولات" description="مشاهده و تعدیل اعتبار حجم و روز"/>
          <NavRow href={base+"/access"} icon="shield" title="مدیریت دسترسی همکار"/>
        </section>
      </>}

      {confirmation && <Modal title={confirmation==="balance"?"تأیید تعدیل":selected.banned?"رفع مسدودیت همکار":"مسدود کردن همکار"}
        error={error} onClose={()=>{if(!pending)setConfirmation(null);}}>
        <div className="space-y-4">
          {confirmation==="balance" ? <Facts rows={[
            ["همکار",<bdi dir="ltr">{selected.username}</bdi>],
            ["تغییر ثبت‌شده",Number.isFinite(Number(amount))?<bdi>{formatAmount(kind,amount)}</bdi>:amount],
            ...(wallet?[]:[[ "محصول",product?.name||"—"] as [string,string]]),
          ]}/> : <Banner tone="warn">{selected.banned?"فقط اشتراک‌هایی باز می‌شوند که به‌خاطر مسدودی همکار بسته شده‌اند.":"این کار اشتراک‌های باز همکار را هم می‌بندد."}</Banner>}
          <div className="flex gap-2"><Button disabled={pending} tone={confirmation==="access"&&!selected.banned?"danger":"primary"}
            onClick={confirmation==="balance"?adjust:changeAccess}>{pending?"در حال ثبت…":confirmation==="balance"?"تأیید و اعمال تعدیل":selected.banned?"رفع مسدودیت":"مسدود کردن"}</Button>
            <Button tone="ghost" disabled={pending} onClick={()=>setConfirmation(null)}>انصراف</Button></div>
        </div>
      </Modal>}
    </>}
  </div>;
}
