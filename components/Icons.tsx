import type { ReactNode } from "react";

const paths = {
  home: <><rect x="3" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="3" width="7" height="7" rx="1.4"/><rect x="3" y="14" width="7" height="7" rx="1.4"/><rect x="14" y="14" width="7" height="7" rx="1.4"/></>,
  panels: <><rect x="4" y="3" width="16" height="7" rx="2"/><rect x="4" y="14" width="16" height="7" rx="2"/><path d="M8 6.5h.01m0 11h.01M12 6.5h4m-4 11h4"/></>,
  product: <><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9L12 3Z"/><path d="m4 7.5 8 4.5 8-4.5M12 12v9M8 5.25l8 4.5"/></>,
  users: <><path d="M16 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2m18 0v-2a4 4 0 0 0-3-3.9M16 3.2a4 4 0 0 1 0 7.6"/><circle cx="9.5" cy="7" r="4"/></>,
  layers: <><path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/></>,
  wallet: <><path d="M20 8V7a3 3 0 0 0-3-3H6a3 3 0 0 0-3 3v10a3 3 0 0 0 3 3h11a3 3 0 0 0 3-3v-1"/><path d="M3 8h17a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1h-4a3 3 0 0 1 0-6h5m-5 3h.01"/></>,
  receipt: <><path d="M5 3h14v18l-3.5-2-3.5 2-3.5-2L5 21V3Z"/><path d="M8.5 8h7m-7 4h7m-7 4h4"/></>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  menu: <path d="M4 6h16M4 12h16M4 18h16"/>,
  more: <><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></>,
  close: <path d="m6 6 12 12M18 6 6 18"/>,
  exit: <path d="M11 4H5v16h6m3-13 5 5-5 5m5-5H9"/>,
  left: <path d="m15 6-6 6 6 6"/>,
  right: <path d="m9 6 6 6-6 6"/>,
  plus: <path d="M12 5v14m-7-7h14"/>,
  minus: <path d="M5 12h14"/>,
  user: <><circle cx="12" cy="8" r="3.6"/><path d="M5 20v-1.5A4.5 4.5 0 0 1 9.5 14h5a4.5 4.5 0 0 1 4.5 4.5V20"/></>,
  shield: <><path d="m12 3 7 3v5c0 5-3 8-7 10-4-2-7-5-7-10V6l7-3Z"/><path d="m9 12 2 2 4-4"/></>,
  eye: <><path d="M2 12s4-6 10-6 10 6 10 6-4 6-10 6-10-6-10-6Z"/><circle cx="12" cy="12" r="2.5"/></>,
  eyeOff: <><path d="m4 4 16 16M10 6.3A9 9 0 0 1 12 6c6 0 10 6 10 6a15 15 0 0 1-2.7 2.9M6.5 7.7A16 16 0 0 0 2 12s4 6 10 6a9 9 0 0 0 3.8-.8"/></>,
  lock: <><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 4v3"/></>,
  copy: <><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></>,
  search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/></>,
  calendar: <><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4m10-4v4M3 10h18"/></>,
  reset: <><path d="M20 7v5h-5M20 12a8 8 0 1 0-2.3 5.7"/></>,
  link: <><path d="m9.5 14.5 5-5m-7 2-2 2a3.5 3.5 0 0 0 5 5l2-2m-1-9 2-2a3.5 3.5 0 0 1 5 5l-2 2"/></>,
  ban: <><circle cx="12" cy="12" r="9"/><path d="m6 6 12 12"/></>,
  trash: <path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5"/>,
  note: <path d="M6 3h12v18H6zM9 8h6m-6 4h6m-6 4h4"/>,
  info: <><circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/></>,
} satisfies Record<string, ReactNode>;

export type IconName = keyof typeof paths;

export function Icon({ name, className = "pc-icon" }: { name: IconName; className?: string }) {
  return <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

export function BrandMark() {
  return <span className="pc-brandmark" dir="ltr">
    <svg viewBox="0 0 30 28" width="31" height="30" aria-hidden="true">
      <rect x="4" y="10" width="10" height="16" rx="5" transform="rotate(-40 9 18)" fill="#258275"/>
      <rect x="15" y="2" width="10" height="16" rx="5" transform="rotate(-40 20 10)" fill="var(--pc-brand)"/>
    </svg>
    <span><strong>Partner</strong><span className="font-normal">Console</span></span>
  </span>;
}

export function IconTile({ name }: { name: IconName }) {
  return <span className="pc-icon-tile"><Icon name={name} /></span>;
}
