"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Banner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { formatDays, formatGb, formatInt, formatToman } from "@/lib/format";
import type { Product } from "@/lib/types";
import styles from "./dashboard.module.css";

type DashboardResponse = {
  balance_toman: number;
  subscription_count: number;
  credits: Product[];
};

function ProductCard({ product }: { product: Product }) {
  const metrics = [
    ["اعتبار حجم", formatGb(product.credit_gb)],
    ["اعتبار روز", formatDays(product.credit_days)],
    ["نرخ هر گیگابایت", formatToman(product.price_per_gb)],
    ["نرخ هر روز", formatToman(product.price_per_day)],
  ];

  return (
    <article className={styles.product}>
      <header className={styles.productHeader}>
        <h3><bdi>{product.name}</bdi></h3>
        <span className={product.active ? styles.activeStatus : styles.inactiveStatus}>
          {product.active ? "قابل انتخاب برای ساخت" : "ساخت اشتراک جدید غیرفعال است"}
        </span>
      </header>
      <dl className={styles.productMetrics}>
        {metrics.map(([label, value]) => (
          <div key={label} className={styles.productMetric}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </article>
  );
}

export default function PartnerHome() {
  const [data, setData] = useState<DashboardResponse | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    api<DashboardResponse>("/partner/dashboard")
      .then((result) => {
        if (active) setData(result);
      })
      .catch((err: unknown) => {
        if (active) {
          setError(err instanceof ApiError && err.message.trim()
            ? err.message
            : "بارگیری اطلاعات داشبورد انجام نشد. دوباره تلاش کنید.");
        }
      });

    return () => {
      active = false;
    };
  }, []);

  if (error) return <Banner>{error}</Banner>;
  if (!data) return <p className={styles.status} role="status">در حال بارگیری داشبورد…</p>;

  return (
    <>
      <section className={styles.summary} aria-label="خلاصهٔ حساب">
        <div className={styles.wallet}>
          <p className={styles.summaryLabel}>موجودی کیف پول</p>
          <p className={styles.summaryValue}>{formatToman(data.balance_toman)}</p>
        </div>
        <Link href="/partner/subscriptions" className={styles.subscriptions}>
          <div>
            <p className={styles.summaryLabel}>تعداد اشتراک</p>
            <p className={styles.summaryValue}>{formatInt(data.subscription_count)}</p>
          </div>
          <span aria-hidden="true" className={styles.arrow}>←</span>
        </Link>
      </section>

      <section className={styles.products} aria-labelledby="dashboard-products">
        <h2 id="dashboard-products">اعتبار و نرخ محصولات</h2>
        {data.credits.length ? (
          <div className={styles.productGrid}>
            {data.credits.map((product) => <ProductCard key={product.id} product={product} />)}
          </div>
        ) : (
          <p className={styles.status}>محصولی برای نمایش وجود ندارد.</p>
        )}
      </section>
    </>
  );
}
