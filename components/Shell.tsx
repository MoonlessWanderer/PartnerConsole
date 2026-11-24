"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Fragment, ReactNode, useEffect, useRef, useState } from "react";
import { clearSession, logoutSession, sessionToken, sessionUsername } from "@/lib/api";
import { Banner } from "@/components/ui";

export type NavItem = { href: string; label: string; icon?: string };

export function AppShell({
  role,
  items,
  loginPath,
  resolveTitle,
  children,
}: {
  role: "admin" | "partner";
  items: NavItem[];
  loginPath: string;
  resolveTitle?: (pathname: string) => string | undefined;
  children: ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [activeSession, setActiveSession] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [desktop, setDesktop] = useState(false);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const isPartner = role === "partner";
  const menuButton = useRef<HTMLButtonElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const drawerWasOpen = useRef(false);

  function closeDrawer() {
    setOpen(false);
  }

  async function logout() {
    if (pending) return;
    setPending(true);
    setError("");
    try {
      await logoutSession(role);
      clearSession();
      router.replace(loginPath);
    } catch {
      setError("خروج انجام نشد. اتصال اینترنت را بررسی کنید و دوباره روی خروج بزنید.");
    } finally {
      setPending(false);
    }
  }

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

  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  useEffect(() => {
    const media = window.matchMedia("(min-width: 1280px)");
    const apply = () => setDesktop(media.matches);
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, []);

  useEffect(() => {
    const drawerOpen = isPartner && open && !desktop;
    if (drawerOpen) {
      closeButton.current?.focus();
    } else if (drawerWasOpen.current && isPartner && !desktop) {
      menuButton.current?.focus();
    }
    drawerWasOpen.current = drawerOpen;
  }, [isPartner, open, desktop]);

  const home = isPartner ? "/partner" : "/admin";
  const current = [...items].reverse().find((item) => pathname === item.href || (item.href !== home && pathname.startsWith(`${item.href}/`)));
  const title = resolveTitle?.(pathname) ?? current?.label ?? (isPartner ? "همکار" : "مدیریت");
  const showUsername = isPartner && Boolean(username);

  if (!activeSession) {
    return <div className="grid min-h-screen place-items-center text-slate-500">در حال بررسی نشست…</div>;
  }

  return (
    <div className="min-h-screen bg-slate-100 text-slate-900">
      {open ? (
        <button type="button" className="fixed inset-0 z-40 bg-slate-900/50 xl:hidden" aria-label="بستن منو" tabIndex={isPartner ? -1 : undefined} onClick={closeDrawer} />
      ) : null}
      <aside
        id={isPartner ? "partner-navigation" : undefined}
        inert={!open && !desktop}
        onKeyDown={isPartner ? (event) => { if (event.key === "Escape") closeDrawer(); } : undefined}
        className={`fixed inset-y-0 right-0 z-50 flex w-72 flex-col transition-transform xl:translate-x-0 ${open ? "translate-x-0" : "translate-x-full"} ${isPartner ? "border-e border-slate-200 bg-white text-slate-900" : "bg-ink text-slate-100"}`}
      >
        <div className="flex items-center justify-between px-4 py-4">
          {isPartner ? (
            <div className="flex items-center gap-3">
              <span className="grid h-9 w-9 place-items-center rounded-xl bg-violet-700 text-xl leading-none text-white" aria-hidden="true">▤</span>
              <div>
                <bdi className="block font-bold" dir="ltr">Partner Console</bdi>
                <div className="text-xs text-slate-600">پنل همکار</div>
              </div>
            </div>
          ) : (
            <div>
              <div className="text-xs text-slate-400">مدیر سیستم</div>
              <div className="font-bold">Partner Console</div>
            </div>
          )}
          <button type="button" ref={isPartner ? closeButton : undefined} className="min-h-11 min-w-11 rounded-xl xl:hidden" onClick={closeDrawer} aria-label="بستن منو">
            ×
          </button>
        </div>
        <nav aria-label={isPartner ? "بخش‌های پنل همکار" : undefined} className="flex-1 space-y-1 px-3">
          {items.map((item) => {
            const exact = pathname === item.href;
            const moreSpecific = items.some((other) => other.href.length > item.href.length && pathname.startsWith(other.href));
            const active = isPartner
              ? current === item
              : exact || (item.href !== home && pathname.startsWith(item.href) && !moreSpecific);
            const activeStyle = isPartner ? "bg-violet-100 font-bold text-violet-900" : "bg-brand text-white";
            const inactiveStyle = isPartner ? "text-slate-700 hover:bg-violet-50" : "hover:bg-white/10";
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`min-h-11 rounded-xl px-3 py-3 text-sm ${isPartner ? "flex items-center gap-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-700" : "block"} ${active ? activeStyle : inactiveStyle}`}
                aria-current={isPartner && active ? (exact ? "page" : "location") : undefined}
                onClick={isPartner ? closeDrawer : undefined}
              >
                {isPartner && item.icon ? <span className="w-5 shrink-0 text-center text-lg leading-none" aria-hidden="true">{item.icon}</span> : null}
                {item.label}
              </Link>
            );
          })}
        </nav>
        {!isPartner ? <div className="border-t border-white/10 p-4 text-sm text-slate-300">{username}</div> : null}
      </aside>
      <div className="xl:pr-72" inert={isPartner && open && !desktop ? true : undefined}>
        <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-slate-200 bg-white px-3">
          <button
            type="button"
            ref={isPartner ? menuButton : undefined}
            className={`inline-flex min-h-11 min-w-11 items-center justify-center rounded-xl border border-slate-200 xl:hidden ${isPartner ? "shrink-0" : ""}`}
            onClick={() => setOpen(true)}
            aria-label="باز کردن منو"
            aria-expanded={isPartner ? open : undefined}
            aria-controls={isPartner ? "partner-navigation" : undefined}
          >
            <span className="flex w-5 flex-col gap-1">
              <span className="h-0.5 bg-slate-800" />
              <span className="h-0.5 bg-slate-800" />
              <span className="h-0.5 bg-slate-800" />
            </span>
          </button>
          <h1 className={isPartner ? "min-w-0 truncate text-sm font-bold sm:text-base" : "text-base font-bold"}>{title}</h1>
          {showUsername ? (
            <bdi className="mr-auto block min-w-0 max-w-52 truncate text-sm text-slate-700" dir="ltr" title={username}>
              {username}
            </bdi>
          ) : null}
          <button
            type="button"
            className={`min-h-11 rounded-xl px-3 text-sm text-slate-600 hover:bg-slate-100 ${showUsername ? "shrink-0" : "ms-auto"}`}
            onClick={logout}
            disabled={pending}
          >
            {pending ? "در حال خروج…" : "خروج"}
          </button>
        </header>
        <main className="mx-auto w-full max-w-6xl p-3 md:p-6">
          {error ? <Banner>{error}</Banner> : null}
          {/* Replacing a login discards the prior account’s drafts and quotes. */}
          <Fragment key={activeSession}>{children}</Fragment>
        </main>
      </div>
    </div>
  );
}
