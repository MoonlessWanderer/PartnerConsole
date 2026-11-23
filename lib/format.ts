const FA = "۰۱۲۳۴۵۶۷۸۹";

export function faDigits(value: string | number | null | undefined): string {
  return String(value ?? "").replace(/\d/g, (digit) => FA[Number(digit)] ?? digit);
}

export function formatInt(value: string | number | null | undefined): string {
  const num = Math.trunc(Number(value) || 0);
  return faDigits(num.toLocaleString("en-US"));
}

export function formatToman(value: string | number | null | undefined): string {
  return `${formatInt(value)} تومان`;
}

export function formatGb(value: string | number | null | undefined): string {
  if (value == null || value === "") return "—";
  const num = Number(value);
  if (!Number.isFinite(num)) return "—";
  const text = Number.isInteger(num) ? String(num) : num.toFixed(2);
  return `${faDigits(text)} گیگ`;
}

export function formatDays(value: string | number | null | undefined): string {
  return `${formatInt(value)} روز`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  return faDigits(value);
}

export const STATUS_LABEL: Record<string, string> = {
  active: "فعال",
  expired: "منقضی",
  banned: "مسدود",
  banned_partner: "مسدود به‌خاطر همکار",
};

export const REASON_LABEL: Record<string, string> = {
  create: "ساخت اشتراک",
  increase: "افزایش",
  decrease: "کاهش",
  delete_refund: "بازگشت حذف",
  reset_traffic: "ریست ترافیک",
  admin_adjust: "تعدیل ادمین",
  operation_rollback: "برگشت عملیات",
};

export const ASSET_LABEL: Record<string, string> = {
  toman: "تومان",
  gb: "گیگ",
  days: "روز",
};

export function formatAmount(asset: string, amount: string | number): string {
  const num = Number(amount);
  const sign = num > 0 ? "+" : "";
  if (asset === "toman") return `${sign}${formatToman(num)}`;
  if (asset === "days") return `${sign}${formatDays(num)}`;
  const text = Number.isInteger(num) ? String(num) : String(num);
  return `${sign}${faDigits(text)} گیگ`;
}
