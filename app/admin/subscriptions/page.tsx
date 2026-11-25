"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { STATUS_LABEL, formatDate, formatDays, formatGb } from "@/lib/format";
import type { SubscriptionRow } from "@/lib/types";
import { Badge, Banner, Button, inputClass } from "@/components/ui";
import { ActionLink, Facts, PageHead, ResponsiveTable } from "@/components/Design";
import { Icon, IconTile } from "@/components/Icons";
import { Modal } from "@/components/Modal";

const root="/admin/subscriptions";
function badge(row: SubscriptionRow) {
  return <Badge tone={row.status==="active"?"ok":row.status==="expired"?"warn":row.status.startsWith("banned")?"bad":"slate"}>{STATUS_LABEL[row.status]||row.status}</Badge>;
}

function Index() {
  const router=useRouter();
  const query=(useSearchParams().get("q")||"");
  const [draft,setDraft]=useState(query);
  const [items,setItems]=useState<SubscriptionRow[]|null>(null);
  const [error,setError]=useState("");
  useEffect(()=>setDraft(query),[query]);
  useEffect(()=>{
    let active=true;
    setItems(null);setError("");
    api<{items:SubscriptionRow[]}>(root+"?q="+encodeURIComponent(query)).then(data=>{if(active)setItems(data.items);})
      .catch(err=>{if(active)setError(err.message);});
    return ()=>{active=false;};
  },[query]);
  function visit(q:string) {
    router.replace(q?root+"?"+new URLSearchParams({q}):root,{scroll:false});
  }
  return <div className="space-y-4">
    <PageHead title="اشتراک‌ها" description="مشاهده و مدیریت دسترسی اشتراک‌ها" />
    <form className="pc-card flex flex-wrap gap-2" role="search" onSubmit={event=>{event.preventDefault();visit(draft.trim());}}>
      <input className={inputClass+" flex-1 min-w-0"} aria-label="نام کاربری" placeholder="جست‌وجوی نام کاربری" dir="ltr" value={draft} onChange={e=>setDraft(e.target.value)} />
      <Button type="submit" tone="ghost"><Icon name="search"/>جست‌وجو</Button>
      {draft && <Button tone="ghost" onClick={()=>{setDraft("");visit("");}}>پاک‌کردن</Button>}
    </form>
    {error ? <Banner>{error}</Banner> : !items ? <p role="status" className="pc-card">در حال بارگیری…</p> :
      <ResponsiveTable label="اشتراک‌ها" icon="layers" columns={["نام کاربری","همکار","محصول","سقف حجم","انقضا","مانده روز","وضعیت","جزئیات"]}
        empty={query?"اشتراکی با این نام کاربری پیدا نشد.":"اشتراکی نیست."} rows={items.map(row=>{
          const name=<bdi dir="ltr">{row.username}</bdi>;
          const status=badge(row);
          const action=<ActionLink href={root+"/"+row.id+(query?"?"+new URLSearchParams({q:query}):"")} secondary>جزئیات</ActionLink>;
          return {id:row.id,cells:[name,<bdi dir="ltr">{row.partner_username}</bdi>,row.product_name,formatGb(row.traffic_cap_gb),<bdi dir="ltr">{formatDate(row.expired_jalali)}</bdi>,formatDays(row.days_left),status,action],
            title:name,summary:<>{row.product_name} · {formatDays(row.days_left)}</>,aside:status,action};
        })}/>}
  </div>;
}

function Detail({ id }: { id:string }) {
  const query=useSearchParams().get("q");
  const back=query?root+"?"+new URLSearchParams({q:query}):root;
  const route=root+"/"+id;
  const [row,setRow]=useState<SubscriptionRow|null>(null);
  const [error,setError]=useState("");
  const [note,setNote]=useState("");
  const [confirm,setConfirm]=useState(false);
  const [pending,setPending]=useState(false);
  const inFlight=useRef(false);
  useEffect(()=>{
    let active=true;
    api<SubscriptionRow>(route).then(data=>{if(active)setRow(data);}).catch(err=>{if(active)setError(err.message);});
    return ()=>{active=false;};
  },[route]);

  async function changeAccess() {
    if(!row||inFlight.current) return;
    inFlight.current=true;setPending(true);setError("");setNote("");
    try {
      await api(route+(row.banned?"/unban":"/ban"),{method:"POST"});
      setConfirm(false);setNote("وضعیت دسترسی اشتراک تغییر کرد.");
      try { setRow(await api<SubscriptionRow>(route)); }
      catch { setError("وضعیت تغییر کرد، اما بارگیری مجدد اشتراک ممکن نشد."); }
    } catch(err) { setError(err instanceof Error?err.message:"تغییر مسدودی ناموفق بود"); }
    finally { inFlight.current=false;setPending(false); }
  }

  async function copy() {
    if(!row?.subscribe_url) return;
    setError(""); setNote("");
    try { await navigator.clipboard.writeText(row.subscribe_url);setNote("لینک کپی شد."); }
    catch { setError("کپی لینک ممکن نشد."); }
  }

  const action=row?.banned?"رفع مسدودیت":"مسدود کردن اشتراک";
  const warning=row?.banned?"مسدودی این اشتراک برداشته می‌شود.":"با مسدود کردن، زمان اشتراک همچنان می‌گذرد.";
  const cap=Number(row?.traffic_cap_gb),used=row?.used_gb==null?null:Number(row.used_gb);
  return <div className="space-y-4">
    <PageHead title="جزئیات اشتراک" back={{href:back,label:"اشتراک‌ها"}}/>
    {error&&!confirm&&<Banner>{error}</Banner>}
    {note&&<Banner tone="ok">{note}</Banner>}
    {!row ? !error&&<p role="status" className="pc-card">در حال بارگیری…</p> : <>
      <div className="pc-identity"><IconTile name="layers"/><div><bdi dir="ltr" className="break-all font-bold">{row.username}</bdi><p className="pc-quiet">{row.product_name}</p></div><div className="ms-auto">{badge(row)}</div></div>
      <div className="pc-grid-two">
        <section className="pc-card"><h2 className="pc-section-title">مشخصات اشتراک</h2><Facts rows={[
          ["همکار",<bdi dir="ltr">{row.partner_username||"—"}</bdi>],["محصول",row.product_name||"—"],["پنل",row.panel_name||"—"],["سقف حجم",formatGb(row.traffic_cap_gb)],
          ["تاریخ انقضا",<bdi dir="ltr">{formatDate(row.expired_jalali)}</bdi>],["زمان باقی‌مانده",formatDays(row.days_left)],
        ]}/></section>
        <section className="pc-card space-y-3"><h2 className="pc-section-title">مصرف ترافیک</h2>
          {row.live_error ? <Banner>{row.live_error}</Banner> : <>
            {used!==null&&Number.isFinite(used)&&cap>0&&<progress className="pc-progress" value={Math.min(used,cap)} max={cap} aria-label="مصرف ترافیک"/>}
            <Facts rows={[["مصرف",formatGb(row.used_gb)],["دانلود",formatGb(row.download_gb)],["آپلود",formatGb(row.upload_gb)]]}/>
            {row.subscribe_url ? <div className="pc-copy-control"><input className={inputClass} dir="ltr" readOnly aria-label="لینک اشتراک" value={row.subscribe_url}/><Button tone="ghost" onClick={copy}><Icon name="copy"/>کپی</Button></div>:
             <span className="pc-quiet">لینک از پنل دریافت نشد.</span>}
          </>}
        </section>
      </div>
      <section className="pc-card space-y-3">
        <h2 className="pc-section-title">دسترسی اشتراک</h2>
        <Facts rows={[["وضعیت",badge(row)]]}/>
        <Button tone={row.banned?"primary":"danger"} onClick={()=>setConfirm(true)}>{action}</Button>
      </section>
      {confirm&&<Modal title={action} error={error} onClose={()=>{if(!pending)setConfirm(false);}}>
        <Banner tone="warn">{warning}</Banner>
        <div className="mt-4 flex gap-2"><Button disabled={pending} tone={row.banned?"primary":"danger"} onClick={changeAccess}>{pending?"در حال ثبت…":action}</Button>
          <Button tone="ghost" disabled={pending} onClick={()=>setConfirm(false)}>انصراف</Button></div>
      </Modal>}
    </>}
  </div>;
}

export default function AdminSubscriptions() {
  const {id}=useParams<{id?:string}>();
  return <Suspense fallback={<p role="status">در حال بارگیری…</p>}>{id?<Detail key={id} id={id}/>:<Index/>}</Suspense>;
}
