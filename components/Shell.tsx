"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Fragment, type ReactNode, useEffect, useRef, useState } from "react";
import { clearSession, logoutSession, sessionToken, sessionUsername } from "@/lib/api";
import { Banner, Button } from "@/components/ui";
import { BrandMark, Icon, type IconName } from "@/components/Icons";

export type NavItem = { href: string; label: string; icon: IconName };

export function AppShell({ role, items, loginPath, children }: {
  role: "admin" | "partner";
  items: NavItem[];
  loginPath: string;
  children: ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const [activeSession, setActiveSession] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [open, setOpen] = useState(false);
  const drawer = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLElement | null>(null);
  const submitting = useRef(false);
  const roleLabel = role === "partner" ? "پنل همکار" : "پنل مدیریت";
  const home = role === "partner" ? "/partner" : "/admin";
  const current = items.find(item => pathname === item.href || (item.href !== home && pathname.startsWith(item.href + "/")));
  const mobileItems = role === "partner" ? items : items.filter(item => [home, "/admin/partners", "/admin/subscriptions"].includes(item.href));

  useEffect(() => {
    function syncSession() {
      const token = sessionToken();
      const valid = token && localStorage.getItem("pp_role") === role;
      setActiveSession(valid ? token : null);
      if (!valid) {
        router.replace(loginPath);
        return;
      }
      setUsername(sessionUsername());
    }
    syncSession();
    window.addEventListener("storage", syncSession);
    return () => window.removeEventListener("storage", syncSession);
  }, [role, router, loginPath]);

  useEffect(() => setOpen(false), [pathname]);

  useEffect(() => {
    const dialog = drawer.current;
    if (!open || !dialog) return;
    dialog.showModal();
    const desktop = window.matchMedia("(min-width: 1024px)");
    function closeOnDesktop() {
      if (desktop.matches) setOpen(false);
    }
    desktop.addEventListener("change", closeOnDesktop);
    return () => {
      dialog.close();
      desktop.removeEventListener("change", closeOnDesktop);
      trigger.current?.focus();
    };
  }, [open, activeSession]);

  async function logout() {
    if (submitting.current) return;
    submitting.current = true;
    setPending(true);
    setError("");
    try {
      await logoutSession(role);
      clearSession();
      router.replace(loginPath);
    } catch {
      setOpen(false);
      setError("خروج انجام نشد. اتصال اینترنت را بررسی کنید و دوباره روی خروج بزنید.");
    } finally {
      submitting.current = false;
      setPending(false);
    }
  }

  function navEntry(item: NavItem) {
    const active = current === item;
    return <Link className="pc-nav-link" href={item.href} key={item.href} onClick={() => setOpen(false)}
      aria-current={active ? (pathname === item.href ? "page" : "location") : undefined}>
      <Icon name={item.icon} />{item.label}
    </Link>;
  }

  function account() {
    return <div className="pc-side-account">
      <div className="pc-account">
        <span className="pc-avatar" aria-hidden="true">{username.slice(0, 1).toUpperCase() || "–"}</span>
        <div className="min-w-0">
          <bdi className="block truncate font-bold" dir="ltr" title={username}>{username}</bdi>
          <div className="pc-quiet">{role === "admin" ? "مدیر سیستم" : "حساب همکار"}</div>
        </div>
      </div>
      <Button tone="ghost" disabled={pending} onClick={logout}><Icon name="exit" />{pending ? "در حال خروج…" : "خروج"}</Button>
    </div>;
  }

  if (!activeSession) return <div role="status" className="grid min-h-screen place-items-center pc-quiet">در حال بررسی نشست…</div>;

  return <div className="pc-shell">
    <aside className="pc-side pc-desktop-only" aria-label={roleLabel}>
      <div className="pc-side-brand"><Link href={home} aria-label="خانهٔ پنل"><BrandMark /></Link></div>
      <p className="pc-side-role">{roleLabel}</p>
      <nav aria-label="منوی اصلی">{items.map(navEntry)}</nav>
      {account()}
    </aside>
    <div className="pc-shell-content">
      <header className="pc-top">
        <span className="pc-role-indicator pc-desktop-only">{roleLabel}</span>
        <Link href={home} className="pc-mobile-only" aria-label="خانهٔ پنل"><BrandMark /></Link>
        <span className="pc-account pc-desktop-only">
          <bdi dir="ltr" className="max-w-48 truncate" title={username}>{username}</bdi>
          <span className="pc-avatar" aria-hidden="true">{username.slice(0, 1).toUpperCase() || "–"}</span>
        </span>
        <button type="button" className="pc-mobile-only pc-menu-trigger" aria-label="باز کردن منو"
          aria-expanded={open} aria-controls="pc-navigation" onClick={event => { trigger.current = event.currentTarget; setOpen(true); }}>
          <Icon name="menu" />
        </button>
      </header>
      <main className="pc-main">
        {error && <div className="mb-4"><Banner>{error}</Banner></div>}
        <Fragment key={activeSession}>{children}</Fragment>
      </main>
      <nav className="pc-mobile-nav" aria-label="ناوبری پایین">
        {mobileItems.map(navEntry)}
        {role === "admin" && <button type="button" aria-expanded={open} aria-controls="pc-navigation"
          data-active={Boolean(current && !mobileItems.includes(current))}
          onClick={event => { trigger.current = event.currentTarget; setOpen(true); }}>
          <Icon name="more" />بیشتر
        </button>}
      </nav>
    </div>
    <dialog ref={drawer} id="pc-navigation" aria-labelledby="pc-navigation-title" className="pc-drawer"
      onCancel={event => { event.preventDefault(); setOpen(false); }}
      onClick={event => { if (event.target === event.currentTarget) setOpen(false); }}>
      <div className="pc-drawer-inner">
        <header className="pc-side-brand justify-between">
          <div id="pc-navigation-title"><BrandMark /></div>
          <button type="button" className="pc-menu-trigger" aria-label="بستن منو" onClick={() => setOpen(false)} autoFocus><Icon name="close" /></button>
        </header>
        <p className="pc-side-role">{roleLabel}</p>
        <nav aria-label="بخش‌های پنل">{items.map(navEntry)}</nav>
        {account()}
      </div>
    </dialog>
  </div>;
}
