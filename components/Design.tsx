import Link from "next/link";
import type { ReactNode } from "react";
import { Icon, IconTile, type IconName } from "@/components/Icons";
import { formatInt } from "@/lib/format";

export function PageHead({ title, description, action, back }: {
  title: ReactNode; description?: string; action?: ReactNode; back?: { href: string; label: string };
}) {
  return <header>
    {back && <nav className="pc-breadcrumb" aria-label="مسیر صفحه">
      <Link href={back.href}><Icon name="right" className="inline-block h-4 w-4" />{back.label}</Link>
    </nav>}
    <div className="pc-page-head">
      <div className="min-w-0"><h1>{title}</h1>{description && <p>{description}</p>}</div>
      {action}
    </div>
  </header>;
}

export function ActionLink({ href, children, icon, secondary }: {
  href: string; children: ReactNode; icon?: IconName; secondary?: boolean;
}) {
  return <Link href={href} className={`pc-button ${secondary ? "pc-button-outline" : "pc-button-primary"}`}>
    {icon && <Icon name={icon} />}{children}
  </Link>;
}

export function NavRow({ href, icon, title, description }: {
  href: string; icon: IconName; title: string; description?: string;
}) {
  return <Link href={href} className="pc-list-row">
    <IconTile name={icon} />
    <div className="min-w-0"><div className="text-sm font-bold">{title}</div>{description && <div className="pc-quiet">{description}</div>}</div>
    <Icon name="left" />
  </Link>;
}

function TomanAmount({ amount }: { amount: string | number }) {
  return <span className="pc-money pc-financial"><bdi dir="ltr">{formatInt(amount)}</bdi>{" "}<span>تومان</span></span>;
}

export function Wallet({ amount }: { amount: string | number }) {
  return <section className="pc-wallet pc-financial" aria-label="موجودی کیف پول">
    <IconTile name="wallet" />
    <div className="pc-wallet-label">موجودی کیف پول</div>
    <strong><TomanAmount amount={amount} /></strong>
    <div className="pc-wallet-caption"><span>موجودی قابل استفاده</span></div>
    <small className="pc-wallet-brand" aria-hidden="true"><bdi dir="ltr">PARTNERCONSOLE</bdi></small>
  </section>;
}

export function WalletDebit({ amount }: { amount: number }) {
  return <dl className="pc-quote-total"><div>
    <dt>برداشت از کیف پول</dt><dd><TomanAmount amount={amount} /></dd>
  </div></dl>;
}

export function Facts({ rows }: { rows: Array<[ReactNode, ReactNode]> }) {
  return <dl className="pc-meta">{rows.map(([label, value], index) => <div key={index}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>;
}

type ResponsiveRow = {
  id: number;
  cells: ReactNode[];
  title: ReactNode;
  summary?: ReactNode;
  aside?: ReactNode;
  action?: ReactNode;
};

export function ResponsiveTable({ label, columns, rows, empty = "رکوردی برای نمایش وجود ندارد.", icon }: {
  label: string; columns: string[]; rows: ResponsiveRow[]; empty?: string; icon?: IconName;
}) {
  if (!rows.length) return <p className="pc-card text-sm pc-quiet" role="status">{empty}</p>;
  return <div className="pc-table-wrap">
    <div className="pc-table pc-table-desktop">
      <table aria-label={label}>
        <thead><tr>{columns.map((name, index) => <th scope="col" key={index}>{name}</th>)}</tr></thead>
        <tbody>{rows.map(row => <tr key={row.id}>{row.cells.map((cell, index) => <td key={index}>{cell}</td>)}</tr>)}</tbody>
      </table>
    </div>
    <ul className="pc-card pc-list pc-table-mobile !p-0" aria-label={label}>
      {rows.map(row => <li className="pc-table-row" key={row.id}>
        <div className="pc-table-row-heading">
          {icon && <IconTile name={icon} />}
          <div className="pc-table-row-title">{row.title}</div>
          {row.aside && <div className="pc-table-row-aside">{row.aside}</div>}
        </div>
        {row.summary && <div className="pc-table-row-summary pc-quiet">{row.summary}</div>}
        {row.action && <div className="pc-table-row-action">{row.action}</div>}
      </li>)}
    </ul>
  </div>;
}
