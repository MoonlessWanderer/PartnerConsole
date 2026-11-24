"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { PageHead } from "@/components/Design";
import { IconTile } from "@/components/Icons";
import { Banner } from "@/components/ui";
import { api } from "@/lib/api";
import { ASSET_LABEL, REASON_LABEL, formatAmount, formatDate } from "@/lib/format";
import styles from "./ledger.module.css";

type Row = {
  id: number;
  asset: string;
  amount: string;
  product_name: string | null;
  subscription_id: number | null;
  subscription_username: string | null;
  reason: string;
  actor_type: string;
  created_jalali: string | null;
};

function Change({ row }: { row: Row }) {
  const value = Number(row.amount);
  const unit = ASSET_LABEL[row.asset] ?? row.asset;
  const text = ASSET_LABEL[row.asset] ? formatAmount(row.asset, row.amount) : row.amount;
  const amount = text.endsWith(" " + unit) ? text.slice(0, -unit.length - 1) : text;
  return <span className={styles.change} data-direction={value > 0 ? "up" : value < 0 ? "down" : "none"}>
    <bdi dir="ltr">{amount}</bdi><span>{unit}</span>
  </span>;
}

function Actor({ row }: { row: Row }) {
  return row.actor_type === "admin" ? "ادمین" : row.actor_type === "partner" ? "همکار" : "سیستم";
}

function Reason({ row }: { row: Row }) {
  return REASON_LABEL[row.reason] ?? row.reason;
}

function Subscription({ row }: { row: Row }) {
  if (row.subscription_id === null || !row.subscription_username) return "—";
  return (
    <Link className={styles.subscription} href={`/partner/subscriptions/${row.subscription_id}`} prefetch={false}>
      <bdi dir="ltr">{row.subscription_username}</bdi>
    </Link>
  );
}

function DateValue({ row }: { row: Row }) {
  return <bdi dir="ltr">{formatDate(row.created_jalali)}</bdi>;
}

export default function PartnerLedger() {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let current = true;
    api<{ items: Row[] }>("/partner/ledger").then((result) => {
      if (current) setRows(result.items);
    }).catch((err) => {
      if (current) setError(err.message);
    });
    return () => { current = false; };
  }, []);

  const heading=<PageHead title="تاریخچه تراکنش‌ها" description="سوابق تغییرات تومان، اعتبار حجم و اعتبار روز"/>;
  if (error) return <>{heading}<Banner>{error}</Banner></>;
  if (!rows) return <>{heading}<p className={styles.empty} role="status">در حال بارگیری تاریخچه…</p></>;
  if (!rows.length) return <>{heading}<p className={styles.empty}>رکوردی نیست.</p></>;

  return (
    <>{heading}<section className={styles.panel} aria-label="تراکنش‌های همکار">
      <table className={styles.table} aria-label="تراکنش‌ها">
        <thead><tr><th scope="col">دلیل</th><th scope="col">تغییر</th><th scope="col">محصول</th>
          <th scope="col">اشتراک</th><th scope="col">عامل</th><th scope="col">زمان</th></tr></thead>
        <tbody>{rows.map((row) => (
          <tr key={row.id} data-ledger-id={row.id}>
            <th scope="row"><Reason row={row} /></th>
            <td><Change row={row} /></td>
            <td>{row.product_name || "—"}</td>
            <td><Subscription row={row} /></td>
            <td><Actor row={row} /></td>
            <td><DateValue row={row} /></td>
          </tr>
        ))}</tbody>
      </table>
      <ul className={styles.list} aria-label="تراکنش‌ها">
        {rows.map((row) => (
          <li key={row.id} data-ledger-id={row.id}>
            <div className={styles.line}><strong className="flex items-center gap-2"><IconTile name="receipt"/><Reason row={row} /></strong><Change row={row} /></div>
            <div className={styles.line}><span>{row.product_name || "—"}</span><DateValue row={row} /></div>
            <div className={styles.line}><span><Subscription row={row} /></span><span><Actor row={row} /></span></div>
          </li>
        ))}
      </ul>
    </section></>
  );
}
