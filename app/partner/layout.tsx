"use client";

import type { ReactNode } from "react";
import { AppShell } from "@/components/Shell";

const items = [
  { href: "/partner", label: "داشبورد" },
  { href: "/partner/subscriptions", label: "اشتراک‌ها" },
  { href: "/partner/subscriptions/new", label: "ساخت اشتراک" },
  { href: "/partner/ledger", label: "تاریخچه" },
];

export default function PartnerLayout({ children }: { children: ReactNode }) {
  return (
    <AppShell role="partner" items={items} loginPath="/">
      {children}
    </AppShell>
  );
}
