import { formatDays, formatToman } from "@/lib/format";
import styles from "./SubscriptionPurchase.module.css";

const volume = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 6 });

export function PurchaseWalletBreakdown({
  gb, days, gbRate, dayRate, gbCost, dayCost,
}: {
  gb: string | number;
  days: number;
  gbRate: number;
  dayRate: number;
  gbCost: number;
  dayCost: number;
}) {
  return (
    <>
      <h3>از کیف پول</h3>
      <dl className={styles.wallet}>
        <div>
          <dt>{volume.format(Number(gb))} گیگ × {formatToman(gbRate)}</dt>
          <dd>{formatToman(gbCost)}</dd>
        </div>
        <div>
          <dt>{formatDays(days)} × {formatToman(dayRate)}</dt>
          <dd>{formatToman(dayCost)}</dd>
        </div>
      </dl>
    </>
  );
}
