"use client";

import { useParams, usePathname, useRouter } from "next/navigation";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { formatDate, formatInt } from "@/lib/format";
import { ActionLink, PageHead, ResponsiveTable } from "@/components/Design";
import { Icon } from "@/components/Icons";
import { Badge, Banner, Button, Field, inputClass } from "@/components/ui";

type Panel = { id:number; name:string; base_url:string; secure_path:string; admin_email:string; status:string; last_ok_jalali:string|null };
const root="/admin/panels";
const blank={name:"",base_url:"",secure_path:"",admin_email:"",admin_password:"",status:"active"};

function status(panel:Panel) {
  return <Badge tone={panel.status==="active"?"ok":panel.status==="disabled"?"slate":"bad"}>
    {panel.status==="active"?"فعال":panel.status==="disabled"?"خاموش":"خطا"}
  </Badge>;
}

export default function PanelsPage() {
  const router=useRouter(), pathname=usePathname();
  const params=useParams<{id?:string}>();
  const editing=Boolean(params.id);
  const formView=editing || pathname === root+"/new";
  const [items,setItems]=useState<Panel[]|null>(null);
  const [form,setForm]=useState(blank);
  const [error,setError]=useState("");
  const [notice,setNotice]=useState("");
  const [pending,setPending]=useState("");
  const inFlight=useRef(false);

  async function load() {
    const result=await api<{items:Panel[]}>(root);
    setItems(result.items);
    return result.items;
  }

  useEffect(() => {
    let active=true;
    api<{items:Panel[]}>(root).then(result=>{
      if (!active) return;
      setItems(result.items);
      const panel=result.items.find(item=>String(item.id)===params.id);
      if (panel) setForm({name:panel.name,base_url:panel.base_url,secure_path:panel.secure_path,admin_email:panel.admin_email,admin_password:"",status:panel.status});
    }).catch(err=>{ if(active) setError(err.message); });
    return ()=>{active=false;};
  },[params.id]);

  async function save(event:FormEvent) {
    event.preventDefault();
    if(inFlight.current) return;
    inFlight.current=true;
    setPending("save");setError("");
    try {
      await api(editing ? root+"/"+params.id : root,{method:editing?"PUT":"POST",body:editing?{...form,admin_password:form.admin_password||null}:form});
      router.push(root);
    } catch(err) { setError(err instanceof Error?err.message:"ذخیرهٔ پنل انجام نشد."); }
    finally {inFlight.current=false;setPending("");}
  }

  async function test(id:number) {
    if(inFlight.current) return;
    inFlight.current=true;
    setPending("test");setError("");setNotice("");
    try {
      const data=await api<{plan_count:number}>(root+"/"+id+"/test",{method:"POST"});
      setNotice(`اتصال برقرار شد. ${formatInt(data.plan_count)} پلن خوانده شد.`);
      await load();
    } catch(err) {
      setError(err instanceof Error?err.message:"تست ناموفق بود");
      await load().catch(()=>undefined);
    } finally {inFlight.current=false;setPending("");}
  }

  const panel=items?.find(item=>String(item.id)===params.id);
  const feedback=<>{error && <Banner>{error}</Banner>}{notice && <Banner tone="ok">{notice}</Banner>}</>;
  if (formView) return <div className="space-y-4">
    <PageHead title={editing?"ویرایش پنل":"افزودن پنل"} back={{href:root,label:"پنل‌ها"}}
      action={panel&&<Button tone="ghost" disabled={Boolean(pending)} onClick={()=>test(panel.id)}><Icon name="reset"/>تست اتصال</Button>} />
    {feedback}
    {editing&&!panel ? <p role="status" className="pc-card">{items?"پنل پیدا نشد.":error?"اطلاعات پنل در دسترس نیست.":"در حال بارگیری پنل…"}</p> :
      <div className="pc-grid-two">
        <form className="pc-card pc-form-card" onSubmit={save}>
          <h2 className="pc-section-title">اطلاعات اتصال</h2>
          <Field label="نام پنل"><input className={inputClass} value={form.name} onChange={e=>setForm({...form,name:e.target.value})} required/></Field>
          <Field label="آدرس پنل"><input className={inputClass} dir="ltr" value={form.base_url} onChange={e=>setForm({...form,base_url:e.target.value})} placeholder="https://panel.example.com" required/></Field>
          <Field label="مسیر امن ادمین"><input className={inputClass} dir="ltr" value={form.secure_path} onChange={e=>setForm({...form,secure_path:e.target.value})} required/></Field>
          <Field label="ایمیل ادمین پنل"><input className={inputClass} dir="ltr" value={form.admin_email} onChange={e=>setForm({...form,admin_email:e.target.value})} required/></Field>
          <Field label={editing?"رمز جدید (خالی = بدون تغییر)":"رمز ادمین پنل"}>
            <input className={inputClass} dir="ltr" type="password" autoComplete="new-password" value={form.admin_password} onChange={e=>setForm({...form,admin_password:e.target.value})} required={!editing}/>
          </Field>
          <Field label="وضعیت"><select className={inputClass} value={form.status} onChange={e=>setForm({...form,status:e.target.value})}>
            <option value="active">فعال</option><option value="disabled">خاموش</option>
          </select></Field>
          <div className="flex flex-wrap gap-2">
            <Button type="submit" disabled={Boolean(pending)}>{pending==="save"?"در حال ذخیره…":"ذخیره پنل"}</Button>
            <ActionLink href={root} secondary>انصراف</ActionLink>
          </div>
        </form>
        <section className="pc-card">
          <h2 className="pc-section-title">{editing?"وضعیت اتصال":"راهنمای اتصال"}</h2>
          {panel ? <><p className="mb-2 text-sm">آخرین تست موفق <bdi>{formatDate(panel.last_ok_jalali)}</bdi></p>{status(panel)}</> :
            <Banner tone="ok">آدرس و اطلاعات ورود مدیر V2Board را وارد کنید.</Banner>}
        </section>
      </div>}
  </div>;

  return <div className="space-y-4">
    <PageHead title="پنل‌های V2Board" description="مدیریت پنل‌های متصل به سیستم"
      action={<ActionLink href={root+"/new"} icon="plus">پنل جدید</ActionLink>}/>
    {feedback}
    {items ? <ResponsiveTable label="پنل‌های V2Board" icon="panels" columns={["نام پنل","آدرس","وضعیت","آخرین تست","عملیات"]}
      empty="هنوز پنلی ثبت نشده است." rows={items.map(item=>{
        const badge=status(item);
        const edit=<ActionLink href={`${root}/${item.id}/edit`} secondary>ویرایش</ActionLink>;
        const check=<Button tone="ghost" disabled={Boolean(pending)} onClick={()=>test(item.id)}>تست اتصال</Button>;
        return {id:item.id,cells:[item.name,<bdi dir="ltr" className="break-all">{item.base_url}</bdi>,badge,formatDate(item.last_ok_jalali),<div className="flex flex-wrap gap-1">{check}{edit}</div>],title:item.name,summary:<bdi dir="ltr">{item.base_url}</bdi>,aside:badge,action:edit};
      })}/> : !error && <p className="pc-card pc-quiet" role="status">در حال بارگیری پنل‌ها…</p>}
  </div>;
}
