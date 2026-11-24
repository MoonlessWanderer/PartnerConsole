"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState, type RefObject } from "react";
import { Banner } from "@/components/ui";
import { api } from "@/lib/api";
import { STATUS_LABEL, formatDate, formatDays, formatGb, formatInt } from "@/lib/format";
import type { SubscriptionRow } from "@/lib/types";
import styles from "./subscriptions.module.css";

const subscriptionsPath = "/partner/subscriptions";
const pageSize = 50;
const searchDelay = 300;

type SubscriptionPage = { items: SubscriptionRow[]; total: number };
type Result = { data: SubscriptionPage } | { error: string };

function readOffset(value: string | null) {
  if (!value || !/^\d+$/.test(value)) return 0;
  const offset = Number(value);
  return Number.isSafeInteger(offset) ? offset : 0;
}

function Username({ row, showStatus }: { row: SubscriptionRow; showStatus?: boolean }) {
  return (
    <span className={styles.usernameGroup} dir="ltr">
      <bdi className={styles.username} dir="ltr">{row.username}</bdi>
      {row.status === "active" ? (
        <svg
          className={styles.activeIcon}
          width="16"
          height="16"
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          role="img"
          aria-label="فعال"
        >
          <circle cx="8" cy="8" r="6.25" />
          <path d="m5.25 8 1.75 1.75 3.75-3.75" />
        </svg>
      ) : showStatus ? (
        <span className={styles.statusLabel} dir="rtl">{STATUS_LABEL[row.status] ?? row.status}</span>
      ) : null}
    </span>
  );
}

function SubscriptionSearch({
  search,
  pendingSearch,
}: {
  search: string;
  pendingSearch: RefObject<number | undefined>;
}) {
  const [draft, setDraft] = useState(search);
  const appliedSearch = useRef(search);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    function syncFromHistory() {
      window.clearTimeout(pendingSearch.current);
      if (window.location.pathname !== subscriptionsPath) return;
      const restoredSearch = new URLSearchParams(window.location.search).get("q") ?? "";
      appliedSearch.current = restoredSearch;
      setDraft(restoredSearch);
    }

    if (appliedSearch.current !== search) syncFromHistory();
    window.addEventListener("popstate", syncFromHistory);
    window.addEventListener("pageshow", syncFromHistory);
    return () => {
      window.removeEventListener("popstate", syncFromHistory);
      window.removeEventListener("pageshow", syncFromHistory);
    };
  }, [search, pendingSearch]);

  function replaceSearch(url: URL, value: string) {
    const nextSearch = value.trim() ? value : "";
    if (url.pathname !== subscriptionsPath || url.searchParams.get("q") === (nextSearch || null)) return;

    if (nextSearch) {
      url.searchParams.set("q", nextSearch);
    } else {
      url.searchParams.delete("q");
    }
    url.searchParams.delete("offset");
    appliedSearch.current = nextSearch;
    window.history.replaceState(null, "", url);
  }

  function updateDraft(value: string) {
    setDraft(value);
    window.clearTimeout(pendingSearch.current);

    const url = new URL(window.location.href);
    if (!value) {
      replaceSearch(url, "");
      return;
    }

    const currentSearch = url.searchParams.get("q");
    pendingSearch.current = window.setTimeout(() => {
      const currentUrl = new URL(window.location.href);
      if (currentUrl.searchParams.get("q") === currentSearch) {
        replaceSearch(currentUrl, value);
      }
    }, searchDelay);
  }

  function clearSearch() {
    updateDraft("");
    input.current?.focus();
  }

  return (
    <form className={styles.searchForm} role="search" onSubmit={(event) => event.preventDefault()}>
      <div className={styles.searchControl}>
        <input
          ref={input}
          id="partner-subscriptions-search"
          name="q"
          type="search"
          dir="ltr"
          aria-label="جست‌وجوی نام کاربری"
          placeholder="Enter the username"
          value={draft}
          onChange={(event) => updateDraft(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              clearSearch();
            }
          }}
        />
        <button
          className={styles.searchAction}
          type="button"
          aria-label={draft ? "پاک‌کردن جست‌وجو" : "جست‌وجوی نام کاربری"}
          onClick={clearSearch}
        >
          {draft ? (
            <span className={styles.clearIcon} aria-hidden="true">×</span>
          ) : (
            <span className={styles.searchIcon} aria-hidden="true" />
          )}
        </button>
      </div>
    </form>
  );
}

function SubscriptionResults({
  query,
  offset,
  detailQuery,
  onPageChange,
  onNavigate,
}: {
  query: string;
  offset: number;
  detailQuery: string;
  onPageChange: (offset: number) => void;
  onNavigate: () => void;
}) {
  const [result, setResult] = useState<Result | null>(null);

  useEffect(() => {
    let active = true;
    const params = new URLSearchParams({ q: query, limit: String(pageSize), offset: String(offset) });

    function publish(result: Result) {
      if (!active || window.location.pathname !== subscriptionsPath) return;
      const current = new URLSearchParams(window.location.search);
      if ((current.get("q") ?? "").trim() === query && readOffset(current.get("offset")) === offset) {
        setResult(result);
      }
    }

    api<SubscriptionPage>(`${subscriptionsPath}?${params}`).then(
      (data) => publish({ data }),
      (error: unknown) => publish({ error: error instanceof Error ? error.message : "دریافت اشتراک‌ها ممکن نشد." }),
    );
    return () => { active = false; };
  }, [query, offset]);

  if (result && "error" in result) {
    return <div className={styles.error}><Banner>{result.error}</Banner></div>;
  }

  const data = result?.data;
  const hasPrevious = offset > 0;
  const hasNext = data ? offset + pageSize < data.total : false;

  return (
    <>
      <p className={styles.count} id="partner-subscription-count" role="status">
        {data ? `${formatInt(data.total)} اشتراک` : "در حال بارگذاری اشتراک‌ها…"}
      </p>
      {data && (
        <>
          {data.items.length ? (
            <>
              <table className={styles.table} aria-labelledby="partner-subscriptions-title">
                <thead>
                  <tr>
                    <th scope="col">محصول</th>
                    <th scope="col">سقف حجم</th>
                    <th scope="col">تاریخ انقضا</th>
                    <th scope="col">روز باقی‌مانده</th>
                    <th scope="col" aria-label="مدیریت" />
                    <th className={styles.userColumn} scope="col">کاربر</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((row) => (
                    <tr key={row.id}>
                      <td>{row.product_name ?? "—"}</td>
                      <td>{formatGb(row.traffic_cap_gb)}</td>
                      <td><bdi dir="ltr">{formatDate(row.expired_jalali)}</bdi></td>
                      <td>{formatDays(row.days_left)}</td>
                      <td>
                        <Link
                          className={styles.manageLink}
                          href={`${subscriptionsPath}/${row.id}${detailQuery}`}
                          aria-label={`مدیریت ${row.username}`}
                          onNavigate={onNavigate}
                          prefetch={false}
                        >
                          مدیریت
                        </Link>
                      </td>
                      <th className={styles.userColumn} scope="row"><Username row={row} showStatus /></th>
                    </tr>
                  ))}
                </tbody>
              </table>

              <ul className={styles.mobileList} role="list">
                {data.items.map((row) => (
                  <li key={row.id}>
                    <Link
                      className={styles.rowLink}
                      href={`${subscriptionsPath}/${row.id}${detailQuery}`}
                      onNavigate={onNavigate}
                      prefetch={false}
                    >
                      <span className={styles.rowIdentity}>
                        <Username row={row} />
                        <span className={styles.rowProduct} dir="rtl">{row.product_name ?? "—"}</span>
                      </span>
                      <span className={styles.rowMeta}>
                        {row.status === "active" ? (
                          <span>{formatDays(row.days_left)} مانده</span>
                        ) : (
                          <>
                            <span className={styles.statusLabel}>{STATUS_LABEL[row.status] ?? row.status}</span>
                            <bdi dir="ltr">{formatDate(row.expired_jalali)}</bdi>
                          </>
                        )}
                      </span>
                      <svg
                        className={styles.rowChevron}
                        width="16"
                        height="16"
                        viewBox="0 0 16 16"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.5"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        direction="ltr"
                        aria-hidden="true"
                      >
                        <path d="M6 3l5 5-5 5" />
                      </svg>
                    </Link>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className={styles.empty}>
              {data.total > 0 ? "در این صفحه اشتراکی نیست." :
                query ? "اشتراکی با این نام کاربری پیدا نشد" : "اشتراکی ندارید."}
            </p>
          )}
          {(hasPrevious || hasNext) && (
            <nav className={styles.pagination} aria-label="صفحه‌بندی اشتراک‌ها">
              <button
                type="button"
                disabled={!hasPrevious}
                onClick={() => {
                  const lastPageOffset = Math.max(0, Math.floor((data.total - 1) / pageSize) * pageSize);
                  onPageChange(Math.min(Math.max(0, offset - pageSize), lastPageOffset));
                }}
              >
                صفحه قبل
              </button>
              <button type="button" disabled={!hasNext} onClick={() => onPageChange(offset + pageSize)}>
                صفحه بعد
              </button>
            </nav>
          )}
        </>
      )}
    </>
  );
}

function SubscriptionList() {
  const params = useSearchParams();
  const pendingSearch = useRef<number | undefined>(undefined);
  const results = useRef<HTMLElement>(null);
  const search = params.get("q") ?? "";
  const query = search.trim();
  const offset = readOffset(params.get("offset"));

  const listParams = new URLSearchParams();
  if (query) listParams.set("q", search);
  if (offset) listParams.set("offset", String(offset));
  const listQuery = listParams.toString();
  const detailQuery = listQuery ? `?${new URLSearchParams({ listQuery })}` : "";

  useEffect(() => {
    function cancelSearch() {
      window.clearTimeout(pendingSearch.current);
    }
    window.addEventListener("pagehide", cancelSearch);
    return () => {
      window.removeEventListener("pagehide", cancelSearch);
      cancelSearch();
    };
  }, []);

  function cancelPendingSearch() {
    window.clearTimeout(pendingSearch.current);
  }

  function changePage(nextOffset: number) {
    const url = new URL(window.location.href);
    if (url.pathname !== subscriptionsPath) return;
    if (nextOffset) {
      url.searchParams.set("offset", String(nextOffset));
    } else {
      url.searchParams.delete("offset");
    }
    window.history.replaceState(null, "", url);
    results.current?.focus({ preventScroll: true });
  }

  return (
    <>
      <header className={styles.listHeader}>
        <div className={styles.headerGrid}>
          <h2 id="partner-subscriptions-title">اشتراک‌ها</h2>
          <Link
            className={styles.createAction}
            href={`${subscriptionsPath}/new`}
            onNavigate={cancelPendingSearch}
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true">
              <path d="M8 3v10M3 8h10" />
            </svg>
            ساخت اشتراک
          </Link>
          <SubscriptionSearch search={search} pendingSearch={pendingSearch} />
        </div>
      </header>

      <section ref={results} className={styles.panel} aria-labelledby="partner-subscriptions-title" tabIndex={-1}>
        <SubscriptionResults
          key={JSON.stringify([query, offset])}
          query={query}
          offset={offset}
          detailQuery={detailQuery}
          onPageChange={changePage}
          onNavigate={cancelPendingSearch}
        />
      </section>
    </>
  );
}

export default function PartnerSubscriptions() {
  return (
    <Suspense fallback={<p className={styles.count} role="status">در حال بارگذاری اشتراک‌ها…</p>}>
      <SubscriptionList />
    </Suspense>
  );
}
