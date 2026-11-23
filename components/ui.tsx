"use client";

import { cloneElement, isValidElement, ReactElement, ReactNode, useId } from "react";

export function Button({
  children,
  onClick,
  type = "button",
  tone = "primary",
  disabled,
  className = "",
}: {
  children: ReactNode;
  onClick?: () => void;
  type?: "button" | "submit";
  tone?: "primary" | "ghost" | "danger" | "soft";
  disabled?: boolean;
  className?: string;
}) {
  const tones = {
    primary: "bg-brand text-white hover:bg-branddark",
    ghost: "bg-white text-slate-800 border border-slate-300 hover:bg-slate-50",
    danger: "bg-red-700 text-white hover:bg-red-800",
    soft: "bg-teal-50 text-brand hover:bg-teal-100",
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex min-h-11 items-center justify-center rounded-xl px-4 py-2 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${tones[tone]} ${className}`}
    >
      {children}
    </button>
  );
}

export function Field({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  const labelId = useId();
  const control = isValidElement(children) && ["input", "select", "textarea"].includes(String(children.type))
    ? children as ReactElement<{ id?: string }> : null;
  const controlId = control ? control.props.id || labelId : undefined;
  return (
    <div className="block space-y-1.5 text-sm" role={control ? undefined : "group"}
      aria-labelledby={control ? undefined : labelId}>
      {control
        ? <label htmlFor={controlId} className="block font-medium text-slate-700">{label}</label>
        : <div id={labelId} className="font-medium text-slate-700">{label}</div>}
      {control ? cloneElement(control, { id: controlId }) : children}
    </div>
  );
}

export const inputClass =
  "min-h-11 w-full rounded-xl border border-slate-300 bg-white px-3 text-base outline-none focus:border-brand focus:ring-2 focus:ring-teal-100";

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`rounded-2xl border border-slate-200 bg-white p-4 shadow-sm ${className}`}>{children}</section>;
}

export function Banner({ children, tone = "error" }: { children: ReactNode; tone?: "error" | "warn" | "ok" }) {
  const tones = {
    error: "bg-red-50 text-red-800 border-red-200",
    warn: "bg-amber-50 text-amber-900 border-amber-200",
    ok: "bg-emerald-50 text-emerald-900 border-emerald-200",
  };
  return <div role={tone === "error" ? "alert" : "status"}
    className={`rounded-xl border px-3 py-2 text-sm leading-6 ${tones[tone]}`}>{children}</div>;
}

export function TableWrap({ children }: { children: ReactNode }) {
  return <div className="overflow-x-auto rounded-2xl border border-slate-200 bg-white">{children}</div>;
}

export function Badge({ children, tone = "slate" }: { children: ReactNode; tone?: "slate" | "ok" | "warn" | "bad" }) {
  const tones = {
    slate: "bg-slate-100 text-slate-700",
    ok: "bg-emerald-100 text-emerald-800",
    warn: "bg-amber-100 text-amber-900",
    bad: "bg-red-100 text-red-800",
  };
  return <span className={`inline-flex rounded-full px-2.5 py-1 text-xs font-medium ${tones[tone]}`}>{children}</span>;
}
