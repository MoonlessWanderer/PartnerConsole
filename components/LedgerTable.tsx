"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ASSET_LABEL, REASON_LABEL, formatAmount, formatDate } from "@/lib/format";
import { TableWrap } from "@/components/ui";

type Row = {
  id: number;
  partner_username: string | null;
  asset: string;
  amount: string;
  product_name: string | null;
  subscription_id: number | null;
  reason: string;
  actor_type: string;
  created_jalali: string | null;
};

export default function LedgerPage({ scope }: { scope: "admin" | "partner" }) {
  const [items, setItems] = useState<Row[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    api<{ items: Row[] }>(`/${scope}/ledger`).then((data) => setItems(data.items)).catch((err) => setError(err.message));
  }, [scope]);
  return (
    <div className="space-y-4">
      {error ? <p className="text-red-700">{error}</p> : null}
      <TableWrap>
        <table>
          <thead>
            <tr>
              {scope === "admin" ? <th>همکار</th> : null}
              <th>دارایی</th><th>مقدار</th><th>محصول</th><th>دلیل</th><th>عامل</th><th>زمان</th>
            </tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <tr key={row.id}>
                {scope === "admin" ? <td>{row.partner_username}</td> : null}
                <td>{ASSET_LABEL[row.asset] || row.asset}</td>
                <td>{formatAmount(row.asset, row.amount)}</td>
                <td>{row.product_name || "—"}</td>
                <td>{REASON_LABEL[row.reason] || row.reason}</td>
                <td>{row.actor_type === "admin" ? "ادمین" : row.actor_type === "partner" ? "همکار" : "سیستم"}</td>
                <td>{formatDate(row.created_jalali)}</td>
              </tr>
            ))}
            {items.length === 0 ? <tr><td colSpan={scope === "admin" ? 7 : 6}>رکوردی نیست.</td></tr> : null}
          </tbody>
        </table>
      </TableWrap>
    </div>
  );
}
