export type OperationStatus = "pending" | "attention" | "failed" | "completed" | "closed";
export type RetryAction = "resend_reset" | "resend_change" | "collect_shortfall" | null;
export type Decision = "retry" | "close";

export type Operation = {
  id: number;
  status: OperationStatus;
  kind: string;
  partner_id: number;
  product_id: number;
  subscription_id: number | null;
  username: string | null;
  last_error: string | null;
  created_at: string | null;
  updated_at: string | null;
  busy: boolean;
  charge_toman: number;
  charge_gb_from_credit: string | null;
  charge_days_from_credit: number | null;
  gb_back: string | null;
  days_back: number | null;
  refund_state?: "reserved" | "credited" | "voided" | null;
  old_cap_gb: string | null;
  new_cap_gb: string | null;
  panel_applied: boolean;
  panel_sent: boolean;
  shortfall_gb: string | null;
  shortfall_days: number | null;
  resolution: string | null;
  resolution_note: string | null;
  retry_action: RetryAction;
};

export type PanelState =
  | { available: false; reason: string }
  | { available: true; exists: false }
  | { available: true; exists: true; owned: false }
  | {
      available: true;
      exists: true;
      owned: true;
      transfer_enable_gb: string;
      used_gb: string;
      expired_jalali: string | null;
      banned: boolean;
      plan_id: number | null;
    };

/** Decisions the server refuses for the panel state it just read, with the reason. */
export type DecisionBlocks = Partial<Record<Decision, string>>;

export type OperationDetail = Operation & { panel_state: PanelState; decision_blocks?: DecisionBlocks };

export type ResolveForm = {
  decision: Decision | null;
  note: string;
  acceptShortfall: boolean;
  checkedPanel: boolean;
};

export const NOTE_MIN = 5;
export const NOTE_MAX = 500;

export const KIND_LABEL: Record<string, string> = {
  create: "ساخت اشتراک",
  increase: "افزایش / تمدید",
  decrease: "کاهش",
  delete: "حذف اشتراک",
  reset_traffic: "ریست ترافیک",
  partner_ban: "مسدودی همکار",
};

export const OPERATION_STATUS_LABEL: Record<OperationStatus, string> = {
  pending: "در انتظار بازیابی",
  attention: "نیازمند بررسی",
  failed: "ناموفق و برگشت‌خورده",
  completed: "انجام‌شده",
  closed: "بسته‌شده توسط ادمین",
};

export function statusTone(status: string): "ok" | "warn" | "bad" | "slate" {
  if (status === "completed") return "ok";
  if (status === "attention") return "bad";
  if (status === "pending") return "warn";
  return "slate";
}

export const TECHNICAL_ERROR = "خطای فنی ثبت شد؛ جزئیات آن فقط در لاگ سرور نگه داشته می‌شود.";

/** The server already hides technical texts; never render one even if an older API sends it. */
export function shownError(text: string | null): string | null {
  if (!text) return null;
  return /[\u0600-\u06FF]/.test(text) ? text : TECHNICAL_ERROR;
}

export function hasShortfall(op: Pick<Operation, "shortfall_gb" | "shortfall_days">): boolean {
  return Number(op.shortfall_gb || 0) > 0 || Number(op.shortfall_days || 0) > 0;
}

export function refundLabel(state: Operation["refund_state"]): string {
  if (state === "reserved") return "اعتبار رزروشده";
  if (state === "voided") return "رزرو لغوشده";
  if (state === "credited") return "اعتبار افزوده‌شده";
  return "اعتبار برگشتی";
}

export function canResolve(op: Pick<Operation, "status" | "busy">): boolean {
  return op.status === "attention" && !op.busy;
}

function offered(op: Operation): Decision[] {
  if (!canResolve(op)) return [];
  return op.retry_action ? ["retry", "close"] : ["close"];
}

function blocksOf(op: Operation): DecisionBlocks {
  return (op as OperationDetail).decision_blocks || {};
}

export function decisions(op: Operation): Decision[] {
  const blocks = blocksOf(op);
  return offered(op).filter((decision) => !blocks[decision]);
}

/** Offered decisions the current panel state rules out, shown with the server's reason. */
export function blockedDecisions(op: Operation): { decision: Decision; reason: string }[] {
  const blocks = blocksOf(op);
  return offered(op)
    .filter((decision) => blocks[decision])
    .map((decision) => ({ decision, reason: blocks[decision] as string }));
}

const RETRY_LABEL: Record<Exclude<RetryAction, null>, string> = {
  resend_reset: "ارسال دوباره ریست به پنل",
  resend_change: "ارسال دوباره همان تغییر به پنل",
  collect_shortfall: "پس‌گرفتن مانده اعتبار",
};

/** Distinct from the modal's own close button ("بستن پنجره"). */
export function decisionLabel(op: Pick<Operation, "retry_action" | "refund_state">, decision: Decision): string {
  if (decision === "close") return "بستن عملیات بدون جابه‌جایی مالی";
  if (op.refund_state === "reserved" && op.retry_action === "resend_change") return "پیگیری تغییر در پنل";
  return op.retry_action ? RETRY_LABEL[op.retry_action] : "تلاش دوباره";
}

function fa(value: string | number | null | undefined): string {
  const num = Number(value ?? 0);
  const text = Number.isInteger(num) ? num.toLocaleString("en-US") : Number.isFinite(num) ? num.toFixed(2) : "—";
  return text.replace(/\d/g, (digit) => "۰۱۲۳۴۵۶۷۸۹"[Number(digit)]);
}

export type Effects = { wallet: string; ledger: string; panel: string };

/** The exact effect of a decision on the partner's wallet, the ledger and the panel. */
export function effects(op: Operation, decision: Decision): Effects {
  const noLedger = "ردیف جدیدی ثبت نمی‌شود؛ فقط تصمیم و توضیح شما روی همین عملیات ذخیره می‌شود.";
  const noPanel = "هیچ درخواستی به پنل فرستاده نمی‌شود.";
  if (decision === "close") {
    if (op.refund_state === "reserved") {
      return {
        wallet: "رزرو همین عملیات لغو می‌شود. موجودی قابل مصرف تغییری نمی‌کند.",
        ledger: noLedger,
        panel: "وضعیت پنل بررسی می‌شود و تغییری برای پنل فرستاده نمی‌شود.",
      };
    }
    return {
      wallet: hasShortfall(op)
        ? `بدون تغییر. مانده ${fa(op.shortfall_gb || 0)} گیگ و ${fa(op.shortfall_days || 0)} روز دیگر پس گرفته نمی‌شود.`
        : "بدون تغییر. هزینهٔ گرفته‌شده یا اعتبار برگشتی همان‌طور که هست می‌ماند.",
      ledger: noLedger,
      panel: noPanel,
    };
  }
  const kept = op.charge_toman ? `هزینهٔ ${fa(op.charge_toman)} تومان قبلاً کسر شده و دوباره کسر نمی‌شود.` : "هزینهٔ تازه‌ای کسر نمی‌شود.";
  if (op.retry_action === "resend_reset") {
    return { wallet: `بدون تغییر. ${kept}`, ledger: noLedger, panel: "یک درخواست ریست (صفر کردن مصرف) برای همین کاربر فرستاده می‌شود." };
  }
  if (op.retry_action === "resend_change") {
    if (op.refund_state === "reserved") {
      return {
        wallet: "اعتبار رزروشده فقط پس از تأیید نتیجهٔ پنل به موجودی قابل مصرف همین محصول افزوده می‌شود.",
        ledger: "پس از تأیید پنل، برای گیگ و روز برگشتیِ بیشتر از صفر ردیف جداگانه ثبت می‌شود.",
        panel: op.kind === "delete"
          ? "وضعیت همان کاربر بررسی می‌شود و در صورت نیاز حذف دوباره درخواست می‌شود."
          : "سقف و تاریخ پایان پنل بررسی می‌شوند و در صورت نیاز تغییر دوباره درخواست می‌شود.",
      };
    }
    const panel = op.kind === "delete"
      ? "یک درخواست حذف همین کاربر فرستاده می‌شود."
      : `یک درخواست ویرایش فرستاده می‌شود که سقف را ${op.new_cap_gb ? `${fa(op.new_cap_gb)} گیگ` : "مقدار ثبت‌شده"} و تاریخ را مقدار ثبت‌شده می‌کند.`;
    return { wallet: `بدون تغییر. ${kept}`, ledger: noLedger, panel };
  }
  return {
    wallet: `حداکثر ${fa(op.shortfall_gb || 0)} گیگ و ${fa(op.shortfall_days || 0)} روز از اعتبار همکار در این محصول کم می‌شود (به اندازهٔ اعتبار موجود).`,
    ledger: "برای هر مقدار کم‌شده یک ردیف منفی با علت «برگشت عملیات» ثبت می‌شود.",
    panel: noPanel,
  };
}

/** What the decision does, in the words shown to the admin before confirming. */
export function explain(op: Operation, decision: Decision): { summary: string; warning: string } {
  if (decision === "close") {
    if (op.refund_state === "reserved") {
      return {
        summary: "عملیات بسته و رزرو اعتبار آن لغو می‌شود.",
        warning: "موجودی قابل مصرف همکار تغییری نمی‌کند. وضعیت پنل پیش از بستن دوباره بررسی می‌شود.",
      };
    }
    if (hasShortfall(op)) {
      return {
        summary: "عملیات بسته می‌شود و اختلاف باقی‌مانده به‌عنوان پذیرفته‌شده ثبت می‌شود.",
        warning: "هیچ مبلغ یا اعتباری جابه‌جا نمی‌شود و چیزی به پنل فرستاده نمی‌شود. اختلاف ثبت‌شده دیگر به‌طور خودکار پس گرفته نمی‌شود.",
      };
    }
    return {
      summary: "عملیات بدون تغییر موجودی و بدون ارسال به پنل بسته می‌شود.",
      warning: "هزینهٔ گرفته‌شده یا اعتبار برگشتی همان‌طور که هست می‌ماند. فقط وقتی ببندید که وضعیت فعلی پنل را درست می‌دانید.",
    };
  }
  if (op.retry_action === "resend_reset") {
    return {
      summary: "ریست ترافیک یک بار دیگر به پنل فرستاده می‌شود و با همان هزینهٔ قبلی تکمیل می‌شود.",
      warning: "کاهش مصرف ثابت نمی‌کند ریست قبلی انجام شده یا نه؛ ریست دستی ادمین پنل یا ریست زمان‌بندی‌شدهٔ پلن هم همین اثر را دارد. فقط اگر در پنل مطمئن شدید ریست قبلی انجام نشده تأیید کنید؛ در غیر این صورت مصرف کاربر دوباره صفر می‌شود.",
    };
  }
  if (op.retry_action === "resend_change" && op.refund_state === "reserved") {
    return {
      summary: "نتیجهٔ پنل بررسی می‌شود. پس از تأیید، اعتبار رزروشدهٔ همین عملیات آزاد می‌شود.",
      warning: op.kind === "delete"
        ? "اگر کاربر هنوز روی پنل باشد، مالکیت او پیش از درخواست دوبارهٔ حذف بررسی می‌شود."
        : "مقدار قبلی پنل ثابت نمی‌کند درخواست پیشین اجرا نشده است. تغییرات هم‌زمان را پیش از تأیید بررسی کنید.",
    };
  }
  if (op.retry_action === "resend_change" && op.kind === "delete") {
    return {
      summary: "حذف کاربر یک بار دیگر از پنل خواسته می‌شود؛ اگر کاربر دیگر روی پنل نباشد، حذف بدون ارسال کامل می‌شود.",
      warning: "مبلغ حذف قبلاً برگشت داده شده و دوباره برنمی‌گردد. پیش از ارسال، مالکیت کاربر روی پنل دوباره بررسی می‌شود.",
    };
  }
  if (op.retry_action === "resend_change") {
    return {
      summary: "همان تغییر قبلی (سقف و تاریخ) یک بار دیگر به پنل فرستاده می‌شود؛ هزینهٔ جدیدی گرفته نمی‌شود.",
      warning: "برابر بودن مقدار فعلی پنل با مقدار قبلی ثابت نمی‌کند درخواست قبلی اجرا نشده؛ ممکن است کسی هم‌زمان آن را برگردانده باشد. فقط پس از بررسی پنل و تغییرات هم‌زمان تأیید کنید. پیش از ارسال، وضعیت پنل دوباره خوانده می‌شود و مقادیر فعلی پنل با مقدار ثبت‌شده جایگزین می‌شوند.",
    };
  }
  return {
    summary: "تلاش می‌شود باقی‌ماندهٔ اعتبار برگشتی از موجودی همکار پس گرفته شود.",
    warning: "اگر اعتبار کافی نباشد، فقط بخشی پس گرفته می‌شود و عملیات همچنان نیازمند بررسی می‌ماند.",
  };
}

export function formProblem(op: Operation, form: ResolveForm): string | null {
  if (!canResolve(op)) return op.busy ? "این عملیات همین حالا در حال پردازش است." : "این عملیات در وضعیت بررسی نیست.";
  if (!form.decision || !decisions(op).includes(form.decision)) return "یک تصمیم انتخاب کنید.";
  const note = form.note.trim();
  if (note.length < NOTE_MIN) return `توضیح تصمیم را بنویسید (دست‌کم ${NOTE_MIN} نویسه).`;
  if (note.length > NOTE_MAX) return `توضیح حداکثر ${NOTE_MAX} نویسه است.`;
  if (form.decision === "close" && hasShortfall(op) && !form.acceptShortfall) return "پذیرش اختلاف مالی را تأیید کنید.";
  if (!form.checkedPanel) return "بررسی وضعیت پنل و پیامد تصمیم را تأیید کنید.";
  return null;
}

export function resolveBody(op: Operation, form: ResolveForm) {
  if (formProblem(op, form)) throw new Error("invalid resolve form");
  return {
    decision: form.decision as Decision,
    note: form.note.trim(),
    accept_shortfall: form.decision === "close" && hasShortfall(op) && form.acceptShortfall,
    // The API refuses the decision if the operation changed after these details were read.
    seen_updated_at: op.updated_at,
  };
}
