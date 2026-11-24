"use client";

import type { ReactNode } from "react";
import { AppShell } from "@/components/Shell";

const subscriptionsPath = "/partner/subscriptions";

const items = [
  { href: "/partner", label: "داشبورد", icon: "◫" },
  { href: subscriptionsPath, label: "اشتراک‌ها", icon: "▤" },
  { href: "/partner/ledger", label: "تاریخچه", icon: "◷" },
];

function resolveTitle(pathname: string) {
  if (pathname === `${subscriptionsPath}/new`) return "ساخت اشتراک";
  if (pathname.startsWith(`${subscriptionsPath}/`)) return "جزئیات اشتراک";
}

export default function PartnerLayout({ children }: { children: ReactNode }) {
  return (
    <AppShell role="partner" items={items} loginPath="/" resolveTitle={resolveTitle}>
      {children}
    </AppShell>
  );
}
