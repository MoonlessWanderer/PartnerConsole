"use client";

import type { ReactNode } from "react";
import { AppShell, type NavItem } from "@/components/Shell";
import { ADMIN_ENTRY } from "@/lib/adminEntry";

const items: NavItem[] = [
  { href: "/admin", label: "داشبورد", icon: "home" },
  { href: "/admin/panels", label: "پنل‌ها", icon: "panels" },
  { href: "/admin/products", label: "محصولات", icon: "product" },
  { href: "/admin/partners", label: "همکاران", icon: "users" },
  { href: "/admin/subscriptions", label: "اشتراک‌ها", icon: "layers" },
  { href: "/admin/ledger", label: "دفترکل", icon: "receipt" },
  { href: "/admin/operations", label: "عملیات ناتمام", icon: "clock" },
];

export default function AdminLayout({ children }: { children: ReactNode }) {
  return <AppShell role="admin" items={items} loginPath={ADMIN_ENTRY}>{children}</AppShell>;
}
