"use client";

import type { ReactNode } from "react";
import { AppShell, type NavItem } from "@/components/Shell";

const items: NavItem[] = [
  { href: "/partner", label: "داشبورد", icon: "home" },
  { href: "/partner/subscriptions", label: "اشتراک‌ها", icon: "layers" },
  { href: "/partner/products", label: "محصولات", icon: "product" },
  { href: "/partner/ledger", label: "حساب مالی", icon: "wallet" },
];

export default function PartnerLayout({ children }: { children: ReactNode }) {
  return <AppShell role="partner" items={items} loginPath="/">{children}</AppShell>;
}
