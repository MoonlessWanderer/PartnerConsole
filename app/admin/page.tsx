"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { formatInt } from "@/lib/format";
import { Card } from "@/components/ui";

export default function AdminHome() {
  const [data, setData] = useState<Record<string, number> | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api<Record<string, number>>("/admin/dashboard").then(setData).catch((err) => setError(err.message));
  }, []);
  const cards = [
    ["پنل‌ها", data?.panels],
    ["محصولات", data?.products],
    ["همکاران", data?.partners],
    ["اشتراک‌ها", data?.subscriptions],
    ["همکار مسدود", data?.banned_partners],
  ];
  return (
    <div className="space-y-4">
      {error ? <p className="text-red-700">{error}</p> : null}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {cards.map(([label, value]) => (
          <Card key={String(label)}>
            <div className="text-sm text-slate-500">{label}</div>
            <div className="mt-2 text-2xl font-bold">{value === undefined ? "…" : formatInt(value as number)}</div>
          </Card>
        ))}
      </div>
    </div>
  );
}
