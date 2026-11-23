export type JDate = { jy: number; jm: number; jd: number };
export type GDate = { gy: number; gm: number; gd: number };

const BREAKS = [-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210, 1635, 2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178];

function div(a: number, b: number) {
  return Math.trunc(a / b);
}

function mod(a: number, b: number) {
  return a - Math.trunc(a / b) * b;
}

function jalCal(jy: number) {
  const bl = BREAKS.length;
  let gy = jy + 621;
  let leapJ = -14;
  let jp = BREAKS[0];
  let jm = 0;
  let jump = 0;
  let leap = 0;
  let n = 0;
  if (jy < jp || jy >= BREAKS[bl - 1]) throw new Error("سال شمسی نامعتبر است");
  for (let i = 1; i < bl; i += 1) {
    jm = BREAKS[i];
    jump = jm - jp;
    if (jy < jm) break;
    leapJ = leapJ + div(jump, 33) * 8 + div(mod(jump, 33), 4);
    jp = jm;
  }
  n = jy - jp;
  leapJ = leapJ + div(n, 33) * 8 + div(mod(n, 33) + 3, 4);
  if (mod(jump, 33) === 4 && jump - n === 4) leapJ += 1;
  const leapG = div(gy, 4) - div((div(gy, 100) + 1) * 3, 4) - 150;
  const march = 20 + leapJ - leapG;
  if (jump - n < 6) n = n - jump + div(jump + 4, 33) * 33;
  leap = mod(mod(n + 1, 33) - 1, 4);
  if (leap === -1) leap = 4;
  return { leap, gy, march };
}

function g2d(gy: number, gm: number, gd: number) {
  let day = div((gy + div(gm - 8, 6) + 100100) * 1461, 4) + div(153 * mod(gm + 9, 12) + 2, 5) + gd - 34840408;
  day = day - div(div(gy + 100100 + div(gm - 8, 6), 100) * 3, 4) + 752;
  return day;
}

function d2g(jdn: number): GDate {
  let j = 4 * jdn + 139361631;
  j = j + div(div(4 * jdn + 183187720, 146097) * 3, 4) * 4 - 3908;
  const i = div(mod(j, 1461), 4) * 5 + 308;
  const gd = div(mod(i, 153), 5) + 1;
  const gm = mod(div(i, 153), 12) + 1;
  const gy = div(j, 1461) - 100100 + div(8 - gm, 6);
  return { gy, gm, gd };
}

function j2d(jy: number, jm: number, jd: number) {
  const r = jalCal(jy);
  return g2d(r.gy, 3, r.march) + (jm - 1) * 31 - div(jm, 7) * (jm - 7) + jd - 1;
}

function d2j(jdn: number): JDate {
  const g = d2g(jdn);
  let jy = g.gy - 621;
  const r = jalCal(jy);
  const jdn1f = g2d(g.gy, 3, r.march);
  let k = jdn - jdn1f;
  if (k >= 0) {
    if (k <= 185) {
      return { jy, jm: 1 + div(k, 31), jd: mod(k, 31) + 1 };
    }
    k -= 186;
  } else {
    jy -= 1;
    k += 179;
    if (r.leap === 1) k += 1;
  }
  return { jy, jm: 7 + div(k, 30), jd: mod(k, 30) + 1 };
}

export function toJalaali(gy: number, gm: number, gd: number): JDate {
  return d2j(g2d(gy, gm, gd));
}

export function toGregorian(jy: number, jm: number, jd: number): GDate {
  return d2g(j2d(jy, jm, jd));
}

export function isLeapJalaaliYear(jy: number) {
  return jalCal(jy).leap === 0;
}

export function monthLength(jy: number, jm: number) {
  if (jm <= 6) return 31;
  if (jm <= 11) return 30;
  return isLeapJalaaliYear(jy) ? 30 : 29;
}

export function isValidJalaali(jy: number, jm: number, jd: number) {
  return jy >= -61 && jy <= 3177 && jm >= 1 && jm <= 12 && jd >= 1 && jd <= monthLength(jy, jm);
}

export function padJalali(value: JDate) {
  return `${value.jy}/${String(value.jm).padStart(2, "0")}/${String(value.jd).padStart(2, "0")}`;
}

export function parseJalali(value: string): JDate | null {
  const match = /^(\d{4})\/(\d{2})\/(\d{2})$/.exec(value.trim());
  if (!match) return null;
  const jy = Number(match[1]);
  const jm = Number(match[2]);
  const jd = Number(match[3]);
  if (!isValidJalaali(jy, jm, jd)) return null;
  return { jy, jm, jd };
}

export function tehranGregorian(date = new Date()): GDate {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Tehran",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const bag: Record<string, string> = {};
  for (const part of parts) {
    if (part.type !== "literal") bag[part.type] = part.value;
  }
  return { gy: Number(bag.year), gm: Number(bag.month), gd: Number(bag.day) };
}

export function todayJalali() {
  const g = tehranGregorian();
  return padJalali(toJalaali(g.gy, g.gm, g.gd));
}

export function addJalaliDays(value: string, days: number) {
  const current = parseJalali(value);
  if (!current) return value;
  const g = toGregorian(current.jy, current.jm, current.jd);
  const utc = new Date(Date.UTC(g.gy, g.gm - 1, g.gd));
  utc.setUTCDate(utc.getUTCDate() + days);
  return padJalali(toJalaali(utc.getUTCFullYear(), utc.getUTCMonth() + 1, utc.getUTCDate()));
}

export function weekdayIndex(value: JDate) {
  const g = toGregorian(value.jy, value.jm, value.jd);
  const utc = new Date(Date.UTC(g.gy, g.gm - 1, g.gd));
  return (utc.getUTCDay() + 1) % 7;
}

export const MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"];
export const WEEKDAYS = ["ش", "ی", "د", "س", "چ", "پ", "ج"];
