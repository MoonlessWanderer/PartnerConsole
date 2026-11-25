"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Modal } from "@/components/Modal";
import { Button } from "@/components/ui";

type Destination = { href: string } | { historyKey: string };

type BrowserNavigation = EventTarget & {
  traverseTo(key: string): { finished: Promise<unknown> };
};

type HistoryNavigationEvent = Event & {
  navigationType: string;
  destination: { key: string; sameDocument: boolean; url: string };
};

function browserNavigation() {
  return (window as Window & { navigation?: BrowserNavigation }).navigation;
}

export function DraftLeaveWarning({ dirty }: { dirty: boolean }) {
  const router = useRouter();
  const [destination, setDestination] = useState<Destination | null>(null);
  const permittedHistoryKey = useRef<string | null>(null);

  useEffect(() => {
    if (!dirty) return;

    const navigation = browserNavigation();

    function checkLink(event: MouseEvent) {
      if (event.defaultPrevented || event.button !== 0 || event.altKey || event.ctrlKey ||
          event.metaKey || event.shiftKey || !(event.target instanceof Element)) return;
      const link = event.target.closest("a[href]");
      if (!(link instanceof HTMLAnchorElement) || link.target || link.hasAttribute("download")) return;
      const url = new URL(link.href);
      if (url.origin !== location.origin || url.pathname === location.pathname) return;
      event.preventDefault();
      event.stopPropagation();
      setDestination({ href: url.pathname + url.search + url.hash });
    }

    function protectDocument(event: BeforeUnloadEvent) {
      event.preventDefault();
      event.returnValue = true;
    }

    function checkHistory(event: Event) {
      const { navigationType, destination: target } = event as HistoryNavigationEvent;
      if (navigationType !== "traverse" || !target.sameDocument || !target.key) return;
      if (target.key === permittedHistoryKey.current) {
        permittedHistoryKey.current = null;
        return;
      }
      if (!event.cancelable || new URL(target.url).pathname === location.pathname) return;

      event.preventDefault();
      setDestination({ historyKey: target.key });
    }

    document.addEventListener("click", checkLink, true);
    window.addEventListener("beforeunload", protectDocument);
    navigation?.addEventListener("navigate", checkHistory);
    return () => {
      document.removeEventListener("click", checkLink, true);
      window.removeEventListener("beforeunload", protectDocument);
      navigation?.removeEventListener("navigate", checkHistory);
    };
  }, [dirty]);

  function discardDraft(target: Destination) {
    setDestination(null);
    if ("href" in target) {
      router.push(target.href);
      return;
    }

    // Replay the browser's actual entry so Back and Forward keep their normal order.
    permittedHistoryKey.current = target.historyKey;
    void browserNavigation()?.traverseTo(target.historyKey).finished.catch(() => {
      permittedHistoryKey.current = null;
    });
  }

  if (!dirty || !destination) return null;
  return (
    <Modal title="خروج از فرم" onClose={() => setDestination(null)}>
      <p className="text-sm">با خروج از این صفحه، تغییرات ذخیره‌نشده از بین می‌روند.</p>
      <div className="pc-actions-row mt-4">
        <Button onClick={() => setDestination(null)}>ادامهٔ ویرایش</Button>
        <Button tone="danger" onClick={() => discardDraft(destination)}>خروج بدون ذخیره</Button>
      </div>
    </Modal>
  );
}
