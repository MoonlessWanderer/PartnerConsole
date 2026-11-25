"use client";

import { ReactNode, useEffect, useId, useRef } from "react";
import { Banner } from "@/components/ui";

export function Modal({
  title,
  children,
  onClose,
  error,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  error?: string;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    dialog.showModal();
    return () => {
      dialog.close();
    };
  }, []);

  return (
    <dialog ref={dialogRef} aria-labelledby={titleId}
      onCancel={(event) => { event.preventDefault(); onClose(); }}
      className="fixed inset-0 m-0 h-full max-h-none w-full max-w-none items-end justify-center bg-transparent p-3 open:flex sm:items-center">
      <div className="absolute inset-0 bg-slate-900/50" aria-hidden="true" onClick={onClose} />
      <div className="pc-dialog-panel">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 id={titleId} className="text-base font-bold">{title}</h2>
          <button type="button" onClick={onClose} className="min-h-11 min-w-11 rounded-xl text-slate-500 hover:bg-slate-100" aria-label="بستن پنجره">
            ×
          </button>
        </div>
        {error ? <div className="mb-3"><Banner>{error}</Banner></div> : null}
        {children}
      </div>
    </dialog>
  );
}
