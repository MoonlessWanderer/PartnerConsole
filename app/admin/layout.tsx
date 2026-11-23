"use client";

import type { ReactNode } from "react";
import { AppShell } from "@/components/Shell";
import { ADMIN_ENTRY } from "@/lib/adminEntry";

const items = [
  { href: "/admin", label: "داشبورد" },
  { href: "/admin/panels", label: "پنل‌ها" },
  { href: "/admin/products", label: "محصولات" },
  { href: "/admin/partners", label: "همکاران" },
  { href: "/admin/subscriptions", label: "اشتراک‌ها" },
  { href: "/admin/ledger", label: "دفترکل" },
  { href: "/admin/operations", label: "عملیات ناتمام" },
];

export default function AdminLayout({ children }: { children: ReactNode }) {
  return (
    <AppShell role="admin" items={items} loginPath={ADMIN_ENTRY}>
      {children}
    </AppShell>
  );
}
