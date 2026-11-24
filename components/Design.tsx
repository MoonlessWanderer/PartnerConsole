import Link from "next/link";
import type { ReactNode } from "react";
import { Icon, IconTile, type IconName } from "@/components/Icons";

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

export function Wallet({ amount }: { amount: string }) {
  return <section className="pc-wallet pc-financial" aria-label="موجودی کیف پول">
    <IconTile name="wallet" />
    <div className="text-sm">موجودی کیف پول</div>
    <strong>{amount}</strong>
    <small dir="ltr" className="justify-self-end">PARTNERCONSOLE</small>
  </section>;
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
  label: string; columns: string[]; rows: ResponsiveRow[]; empty?: string; icon: IconName;
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
      {rows.map(row => <li className="pc-list-row" key={row.id}>
        <IconTile name={icon} />
        <div className="min-w-0 flex-1">
          <div className="break-words text-sm font-bold">{row.title}</div>
          {row.summary && <div className="break-words pc-quiet">{row.summary}</div>}
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1">{row.aside}{row.action}</div>
      </li>)}
    </ul>
  </div>;
}
