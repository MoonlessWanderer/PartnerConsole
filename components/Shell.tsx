"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Fragment, ReactNode, useEffect, useState } from "react";
import { clearSession, logoutSession, sessionToken, sessionUsername } from "@/lib/api";
import { Banner } from "@/components/ui";

export type NavItem = { href: string; label: string };

export function AppShell({
  role,
  items,
  loginPath,
  children,
}: {
  role: "admin" | "partner";
  items: NavItem[];
  loginPath: string;
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

  const home = role === "admin" ? "/admin" : "/partner";
  const current = [...items].reverse().find((item) => pathname === item.href || (item.href !== home && pathname.startsWith(`${item.href}/`)));
  const title = current?.label || (role === "admin" ? "مدیریت" : "همکار");

  if (!activeSession) {
    return <div className="grid min-h-screen place-items-center text-slate-500">در حال بررسی نشست…</div>;
  }

  return (
    <div className="min-h-screen bg-slate-100 text-slate-900">
      {open ? (
        <button type="button" className="fixed inset-0 z-40 bg-slate-900/50 xl:hidden" aria-label="بستن منو" onClick={() => setOpen(false)} />
      ) : null}
      <aside
        inert={!open && !desktop}
        className={`fixed inset-y-0 right-0 z-50 flex w-72 flex-col bg-ink text-slate-100 transition-transform xl:translate-x-0 ${open ? "translate-x-0" : "translate-x-full"}`}
      >
        <div className="flex items-center justify-between px-4 py-4">
          <div>
            <div className="text-xs text-slate-400">{role === "admin" ? "مدیر سیستم" : "پنل همکار"}</div>
            <div className="font-bold">Partner Console</div>
          </div>
          <button type="button" className="min-h-11 min-w-11 rounded-xl xl:hidden" onClick={() => setOpen(false)} aria-label="بستن منو">
            ×
          </button>
        </div>
        <nav className="flex-1 space-y-1 px-3">
          {items.map((item) => {
            const home = role === "admin" ? "/admin" : "/partner";
            const moreSpecific = items.some((other) => other.href.length > item.href.length && pathname.startsWith(other.href));
            const active = pathname === item.href || (item.href !== home && pathname.startsWith(item.href) && !moreSpecific);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`block min-h-11 rounded-xl px-3 py-3 text-sm ${active ? "bg-brand text-white" : "hover:bg-white/10"}`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className="border-t border-white/10 p-4 text-sm text-slate-300">{username}</div>
      </aside>
      <div className="xl:pr-72">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-slate-200 bg-white px-3">
          <button
            type="button"
            className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-xl border border-slate-200 xl:hidden"
            onClick={() => setOpen(true)}
            aria-label="باز کردن منو"
          >
            <span className="flex w-5 flex-col gap-1">
              <span className="h-0.5 bg-slate-800" />
              <span className="h-0.5 bg-slate-800" />
              <span className="h-0.5 bg-slate-800" />
            </span>
          </button>
          <h1 className="text-base font-bold">{title}</h1>
          <button
            type="button"
            className="ms-auto min-h-11 rounded-xl px-3 text-sm text-slate-600 hover:bg-slate-100"
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
