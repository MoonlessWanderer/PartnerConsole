"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Modal } from "@/components/Modal";
import { Button } from "@/components/ui";

export function DraftLeaveWarning({ dirty }: { dirty: boolean }) {
  const router = useRouter();
  const [destination, setDestination] = useState<string | null>(null);

  useEffect(() => {
    if (!dirty) return;

    function checkLink(event: MouseEvent) {
      if (event.defaultPrevented || event.button !== 0 || event.altKey || event.ctrlKey ||
          event.metaKey || event.shiftKey || !(event.target instanceof Element)) return;
      const link = event.target.closest("a[href]");
      if (!(link instanceof HTMLAnchorElement) || link.target || link.hasAttribute("download")) return;
      const url = new URL(link.href);
      if (url.origin !== location.origin || url.pathname === location.pathname) return;
      event.preventDefault();
      event.stopPropagation();
      setDestination(url.pathname + url.search + url.hash);
    }

    document.addEventListener("click", checkLink, true);
    return () => document.removeEventListener("click", checkLink, true);
  }, [dirty]);

  if (!dirty || !destination) return null;
  return (
    <Modal title="خروج از فرم" onClose={() => setDestination(null)}>
      <p className="text-sm">با خروج از این صفحه، تغییرات ذخیره‌نشده از بین می‌روند.</p>
      <div className="pc-actions-row mt-4">
        <Button onClick={() => setDestination(null)}>ادامهٔ ویرایش</Button>
        <Button tone="danger" onClick={() => {
          setDestination(null);
          router.push(destination);
        }}>خروج بدون ذخیره</Button>
      </div>
    </Modal>
  );
}
