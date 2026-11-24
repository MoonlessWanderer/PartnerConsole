"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ASSET_LABEL, REASON_LABEL, formatAmount, formatDate } from "@/lib/format";
import { PageHead, ResponsiveTable } from "@/components/Design";
import { Banner } from "@/components/ui";

type Row = {
  id: number; partner_username:string|null; asset:string; amount:string; product_name:string|null;
  reason:string; actor_type:string; created_jalali:string|null;
};

export default function LedgerPage({ scope }: { scope:"admin"|"partner" }) {
  const [items,setItems]=useState<Row[]|null>(null);
  const [error,setError]=useState("");
  useEffect(()=>{
    let active=true;
    api<{items:Row[]}>(`/${scope}/ledger`).then(data=>{if(active)setItems(data.items);}).catch(err=>{if(active)setError(err.message);});
    return ()=>{active=false;};
  },[scope]);
  return <div className="space-y-4">
    <PageHead title="دفترکل" description="سوابق تغییر موجودی، اعتبار حجم و اعتبار روز" />
    {error ? <Banner>{error}</Banner> : !items ? <p className="pc-card" role="status">در حال بارگیری…</p> :
      <ResponsiveTable label="دفترکل" icon="receipt" columns={["دلیل","همکار","دارایی","مقدار","محصول","عامل","زمان"]} rows={items.map(row=>{
        const reason=REASON_LABEL[row.reason]||row.reason;
        const actor=row.actor_type==="admin"?"مدیر":row.actor_type==="partner"?"همکار":"سیستم";
        const date=<bdi dir="ltr">{formatDate(row.created_jalali)}</bdi>;
        const amount=<bdi dir="ltr" className={Number(row.amount)<0?"pc-danger-text":"pc-positive-text"}>{formatAmount(row.asset,row.amount)}</bdi>;
        return {id:row.id,cells:[reason,<bdi dir="ltr">{row.partner_username||"—"}</bdi>,ASSET_LABEL[row.asset]||row.asset,amount,row.product_name||"—",actor,date],
          title:reason,summary:<>{row.partner_username&&<bdi dir="ltr">{row.partner_username} · </bdi>}{row.product_name&&<>{row.product_name} · </>}{actor} · {date}</>,aside:amount};
      })}/>}
  </div>;
}
