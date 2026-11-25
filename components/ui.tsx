"use client";

import { cloneElement, isValidElement, ReactElement, ReactNode, useId } from "react";

export function Button({
  children,
  label,
  onClick,
  type = "button",
  tone = "primary",
  disabled,
  className = "",
}: {
  children: ReactNode;
  label?: string;
  onClick?: () => void;
  type?: "button" | "submit";
  tone?: "primary" | "ghost" | "danger" | "soft";
  disabled?: boolean;
  className?: string;
}) {
  const tones = {
    primary: "pc-button-primary",
    ghost: "pc-button-outline",
    danger: "pc-button-danger",
    soft: "pc-button-soft",
  };
  return (
    <button
      type={type}
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
      className={`pc-button ${tones[tone]} ${className}`}
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
    <div className="pc-field" role={control ? undefined : "group"}
      aria-labelledby={control ? undefined : labelId}>
      {control
        ? <label htmlFor={controlId} className="block pc-field-label">{label}</label>
        : <div id={labelId} className="pc-field-label">{label}</div>}
      {control ? cloneElement(control, { id: controlId }) : children}
    </div>
  );
}

export const inputClass =
  "pc-input";

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`pc-card ${className}`}>{children}</section>;
}

export function Banner({ children, tone = "error" }: { children: ReactNode; tone?: "error" | "warn" | "ok" }) {
  const tones = {
    error: "pc-tone-bad",
    warn: "pc-tone-warn",
    ok: "pc-tone-ok",
  };
  return <div role={tone === "error" ? "alert" : "status"}
    className={`pc-banner ${tones[tone]}`}>{children}</div>;
}

export function TableWrap({ children }: { children: ReactNode }) {
  return <div className="pc-table">{children}</div>;
}

export function Badge({ children, tone = "slate" }: { children: ReactNode; tone?: "slate" | "ok" | "warn" | "bad" }) {
  const tones = {
    slate: "pc-tone-slate",
    ok: "pc-tone-ok",
    warn: "pc-tone-warn",
    bad: "pc-tone-bad",
  };
  return <span className={`pc-badge ${tones[tone]}`}>{children}</span>;
}
