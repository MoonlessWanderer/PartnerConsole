"""Commit wallet changes before the panel call, then finish or reverse once.

A crash or a timeout after the commit leaves a pending row. The next attempt
continues that row and does not move money again. One lease at a time may
drive a row. An HTTP error reverses the wallet only when a follow-up read
shows the panel change did not land.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import secrets
import time
from datetime import timezone

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.errors import AppError, V2BoardError, public_error
from app.core.timeutil import end_of_day_ts, format_jalali_ts, utcnow
from app.integrations.v2board import PanelUserNotFound
from app.models import PanelOperation, Partner, Subscription, WalletLedger
from app.services import consent
from app.services.billing import Charge, D, dec_str, gb_to_bytes, qgb, reset_cost_toman

logger = logging.getLogger("partner.billing")

SAVED = (
    "ارتباط با پنل قطع شد. عملیات شماره {id} ذخیره شد و تلاش بعدی "
    "همان را بدون جابه‌جایی دوباره موجودی ادامه می‌دهد."
)
MISMATCH = (
    "عملیات ناتمام قبلی انجام شد و موجودی برای آن یک‌بار جابه‌جا شده است. "
    "وضعیت فعلی را ببینید و اگر تغییر دیگری لازم است درخواست را دوباره بفرستید."
)
ATTENTION = "عملیات شماره {id} نیاز به بررسی دارد و تا تعیین تکلیف، تغییر مالی این مورد ممکن نیست."
PRE_MONEY_NET = "ارتباط با پنل قطع شد. مبلغی جابه‌جا نشده است؛ درخواست را دوباره بفرستید."
BUSY = "این عملیات همین حالا در حال انجام است. کمی بعد دوباره تلاش کنید."
FOREIGN_USER = (
    "این نام کاربری از قبل روی پنل وجود دارد و توسط این سامانه ساخته نشده است. "
    "اشتراک ساخته نشد و مبلغ برگشت داده شد؛ نام کاربری دیگری انتخاب کنید."
)
CHANGE_UNPROVEN = (
    "تغییر قبلاً ممکن است به پنل رسیده باشد و وضعیت فعلی پنل اجرای آن را ثابت نمی‌کند. "
    "موجودی جابه‌جا نشد و چیزی دوباره فرستاده نشد؛ ادمین باید وضعیت پنل و تغییرات هم‌زمان را بررسی کند "
    "و ارسال دوباره یا بستن را انتخاب کند."
)
REPEATED_FAILURE = (
    "درخواست این عملیات چند بار بدون نتیجهٔ قطعی به پنل فرستاده شد. برای جلوگیری از تکرار بی‌پایان، "
    "ارسال خودکار متوقف شد؛ موجودی جابه‌جا نشد و ادمین باید تصمیم بگیرد."
)
MAX_AUTO_RESENDS = 2
DB_UNAVAILABLE = (
    "ثبت نتیجهٔ عملیات شماره {id} در دیتابیس ناموفق بود. موجودی برنگشت و عملیات برای بازیابی "
    "نگه داشته شد؛ بازیابی ابتدا وضعیت پنل را می‌خواند و بدون اثبات، چیزی را دوباره نمی‌فرستد."
)
RESET_UNPROVEN = (
    "پاسخ ریست ترافیک نرسید و مصرف فعلی ثابت نمی‌کند ریست انجام شده یا نه. "
    "مبلغ کسرشده برنگشت؛ ادمین باید ریست را دوباره بفرستد یا عملیات را ببندد."
)
STALE_DECISION = "وضعیت این عملیات پس از باز کردن جزئیات تغییر کرده است؛ جزئیات را دوباره بررسی کنید."
LEASE_SECONDS = 90
DEADLOCK_ATTEMPTS = 3
DEADLOCK_BACKOFF = 0.05
_lease_token: contextvars.ContextVar[str | None] = contextvars.ContextVar("panel_op_lease", default=None)


class LostLease(Exception):
    """The worker no longer owns the operation, so it must not move money."""


def _release_lock(db: Session) -> None:
    """SQLite's write lock lasts until commit. Panel HTTP must sit outside it."""
    db.commit()
    db.expire_all()


def _moment(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _subs():
    from app.services import subscriptions as subs

    return subs


def _err(exc: BaseException) -> str:
    return public_error(exc)[:2000]


def _loads(op: PanelOperation) -> dict:
    return json.loads(op.payload)


def _dump_charge(charge) -> dict:
    return {
        "need_gb": dec_str(charge.need_gb),
        "need_days": int(charge.need_days),
        "gb_from_credit": dec_str(charge.gb_from_credit),
        "days_from_credit": int(charge.days_from_credit),
        "gb_paid": dec_str(charge.gb_paid),
        "days_paid": int(charge.days_paid),
        "toman": int(charge.toman),
    }


def _load_charge(data: dict) -> Charge:
    raw = data["charge"]
    return Charge(
        need_gb=D(raw["need_gb"]),
        need_days=int(raw["need_days"]),
        gb_from_credit=D(raw["gb_from_credit"]),
        days_from_credit=int(raw["days_from_credit"]),
        gb_paid=D(raw["gb_paid"]),
        days_paid=int(raw["days_paid"]),
        toman=int(raw["toman"]),
    )


def _save_owned(db: Session, op_id: int, values: dict, *, release: bool = False) -> None:
    """Commit only while this worker still holds the lease. Otherwise roll back."""
    token = _lease_token.get()
    if not token:
        raise LostLease()
    current = db.get(PanelOperation, op_id)
    if current is not None:
        db.expire(current)
    payload = dict(values)
    payload["updated_at"] = utcnow()
    terminal = payload.get("status") not in (None, "pending")
    if release or terminal:
        payload["lease_token"] = None
        payload["lease_until"] = None
    else:
        payload["lease_until"] = int(time.time()) + LEASE_SECONDS
    result = db.execute(
        update(PanelOperation)
        .where(PanelOperation.id == op_id, PanelOperation.lease_token == token)
        .values(**payload)
    )
    if result.rowcount != 1:
        db.rollback()
        raise LostLease()
    db.flush()
    row = db.execute(
        select(PanelOperation.status, PanelOperation.lease_token).where(PanelOperation.id == op_id)
    ).one()
    if release or terminal:
        expected = payload.get("status")
        owned = row.lease_token is None and (expected is None or row.status == expected)
    else:
        owned = row.lease_token == token
    if not owned:
        db.rollback()
        raise LostLease()
    db.commit()


def _is_deadlock(exc: BaseException) -> bool:
    # Only InnoDB deadlocks (1213) roll back the whole transaction for us; lock wait
    # timeouts (1205) leave it open and are deliberately not treated the same way.
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", ())
    return isinstance(exc, OperationalError) and bool(args) and args[0] == 1213


def _complete_owned(db: Session, op: PanelOperation, data: dict) -> None:
    """Final `completed` write, repeated in a fresh session after a MySQL deadlock.

    Only for callers that committed everything else first: a deadlock rolls back the
    whole transaction, so nothing but this status UPDATE may be inside it.
    """
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("the final status write must run in its own transaction")
    values = {"payload": json.dumps(data, ensure_ascii=False), "status": "completed"}
    try:
        _save_owned(db, op.id, values)
        return
    except OperationalError as exc:
        if not _is_deadlock(exc):
            raise
        db.rollback()
        failure = exc
    token = _lease_token.get()
    for attempt in range(1, DEADLOCK_ATTEMPTS):
        logger.warning("عملیات %s: deadlock در ثبت وضعیت نهایی؛ تلاش %s", op.id, attempt + 1)
        time.sleep(DEADLOCK_BACKOFF * (2 ** (attempt - 1)) + secrets.randbelow(50) / 1000)
        fresh = Session(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
        try:
            row = fresh.execute(
                select(PanelOperation.status, PanelOperation.lease_token).where(PanelOperation.id == op.id)
            ).one_or_none()
            fresh.rollback()
            if row is None or row.status != "pending" or not token or row.lease_token != token:
                raise LostLease()
            _save_owned(fresh, op.id, values)
            db.expire_all()
            return
        except OperationalError as exc:
            fresh.rollback()
            if not _is_deadlock(exc):
                raise
            failure = exc
        finally:
            fresh.close()
    logger.error("عملیات %s: ثبت وضعیت نهایی بعد از %s deadlock ممکن نشد", op.id, DEADLOCK_ATTEMPTS)
    raise failure


def _write(db: Session, op: PanelOperation, data: dict, *, status: str | None = None, error: str | None = None) -> None:
    values = {"payload": json.dumps(data, ensure_ascii=False)}
    if status is not None:
        values["status"] = status
    if error is not None:
        values["last_error"] = error[:2000]
    _save_owned(db, op.id, values)


def _ambiguous(exc: BaseException) -> bool:
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError, TimeoutError, ConnectionError))


def _missing_user(exc: BaseException) -> bool:
    return isinstance(exc, PanelUserNotFound)


def _exists_user(exc: BaseException) -> bool:
    if _missing_user(exc):
        return False
    text = str(exc)
    lowered = text.lower()
    return "存在" in text or "exist" in lowered or "already" in lowered or "تکرار" in text or "ثبت شده" in text


def _pending_sub(db: Session, sub_id: int) -> PanelOperation | None:
    return db.scalar(
        select(PanelOperation)
        .where(PanelOperation.subscription_id == sub_id, PanelOperation.status == "pending")
        .order_by(PanelOperation.id.desc())
    )


def _attention_sub(db: Session, sub_id: int) -> PanelOperation | None:
    return db.scalar(
        select(PanelOperation)
        .where(PanelOperation.subscription_id == sub_id, PanelOperation.status == "attention")
        .order_by(PanelOperation.id.desc())
    )


def _pending_create(db: Session, product_id: int, username: str) -> PanelOperation | None:
    return db.scalar(
        select(PanelOperation).where(
            PanelOperation.product_id == product_id,
            PanelOperation.username == username,
            PanelOperation.kind == "create",
            PanelOperation.status == "pending",
        )
    )


def _attention_create(db: Session, product_id: int, username: str) -> PanelOperation | None:
    return db.scalar(
        select(PanelOperation).where(
            PanelOperation.product_id == product_id,
            PanelOperation.username == username,
            PanelOperation.kind == "create",
            PanelOperation.status == "attention",
        )
    )


def _call_panel(gateway, method: str, *args, **kwargs):
    try:
        return getattr(gateway, method)(*args, **kwargs)
    except AppError:
        raise
    except Exception as exc:
        if _ambiguous(exc):
            raise
        raise AppError("ارتباط با پنل ناموفق بود", 502) from exc


def _guard_pre_money(exc: BaseException) -> None:
    if _ambiguous(exc):
        raise AppError(PRE_MONEY_NET, 503) from exc
    raise exc


def _actor(data: dict, subs):
    return subs.Actor(str(data["actor_type"]), int(data["actor_id"]))


def _view(data: dict, product):
    from types import SimpleNamespace

    return SimpleNamespace(
        email_domain=data.get("email_domain") or product.email_domain,
        plan_id=int(data.get("plan_id") or product.plan_id),
    )


def _ledger_credit(db, subs, op: PanelOperation, data: dict, asset: str, amount, reason: str) -> None:
    subs._ledger(
        db,
        partner_id=op.partner_id,
        asset=asset,
        amount=amount,
        product_id=op.product_id,
        subscription_id=op.subscription_id,
        reason=reason,
        actor=_actor(data, subs),
    )


def _refund_charge(db, op: PanelOperation, data: dict, subs) -> None:
    charge = _load_charge(data)
    partner = db.get(Partner, op.partner_id)
    credit = subs.lock_credit(db, op.partner_id, op.product_id)
    if D(charge.gb_from_credit) > 0:
        credit.credit_gb = qgb(D(credit.credit_gb) + D(charge.gb_from_credit))
        _ledger_credit(db, subs, op, data, "gb", charge.gb_from_credit, "operation_rollback")
    if int(charge.days_from_credit) > 0:
        credit.credit_days = int(credit.credit_days) + int(charge.days_from_credit)
        _ledger_credit(db, subs, op, data, "days", charge.days_from_credit, "operation_rollback")
    if int(charge.toman) > 0:
        partner.balance_toman = int(partner.balance_toman) + int(charge.toman)
        _ledger_credit(db, subs, op, data, "toman", charge.toman, "operation_rollback")


def _take_back(db, op: PanelOperation, data: dict, subs, gb, days) -> None:
    credit = subs.lock_credit(db, op.partner_id, op.product_id)
    if D(gb) > 0:
        credit.credit_gb = qgb(D(credit.credit_gb) - D(gb))
        _ledger_credit(db, subs, op, data, "gb", -D(gb), "operation_rollback")
    if int(days) > 0:
        credit.credit_days = int(credit.credit_days) - int(days)
        _ledger_credit(db, subs, op, data, "days", -int(days), "operation_rollback")


def _clawback(db, op: PanelOperation, data: dict, subs) -> bool:
    gb = qgb(data.get("gb_back") or 0)
    days = int(data.get("days_back") or 0)
    credit = subs.lock_credit(db, op.partner_id, op.product_id)
    have_gb = qgb(credit.credit_gb)
    have_days = int(credit.credit_days)
    if have_gb < gb or have_days < days:
        take_gb = qgb(min(have_gb, gb))
        take_days = min(have_days, days)
        _take_back(db, op, data, subs, take_gb, take_days)
        data["shortfall_gb"] = dec_str(qgb(gb - take_gb))
        data["shortfall_days"] = int(days) - int(take_days)
        return False
    _take_back(db, op, data, subs, gb, days)
    return True


def _deferred_refund(data: dict) -> bool:
    # Old journal rows have no marker: their refund was credited when the row was opened.
    return data.get("refund_deferred") is True


def _settle_refund(db: Session, op: PanelOperation, data: dict) -> None:
    """Stage the reserved credit in the same transaction as the lease-fenced completion."""
    if not data.get("panel_applied"):
        raise RuntimeError("a reserved refund requires a confirmed panel operation")
    gb = qgb(data["gb_back"])
    days = int(data["days_back"])
    if not gb and not days:
        return
    subs = _subs()
    subs.lock_partner_row(db, op.partner_id, allow_banned=True)
    credit = subs.lock_credit(db, op.partner_id, op.product_id)
    deleting = op.kind == "delete"
    subs._give_credit(
        db, credit, op.partner_id, op.product_id, None if deleting else op.subscription_id,
        gb, days, "delete_refund" if deleting else "decrease", _actor(data, subs),
    )


def _revert_cap(db, op: PanelOperation, data: dict) -> None:
    sub = db.get(Subscription, op.subscription_id) if op.subscription_id else None
    if sub is None:
        return
    sub.traffic_cap_gb = qgb(data["old_cap"])
    sub.expired_at = int(data["old_expired_at"])
    sub.updated_at = utcnow()


def _mark(db, op: PanelOperation, data: dict, status: str, exc: BaseException) -> None:
    _save_owned(
        db,
        op.id,
        {
            "payload": json.dumps(data, ensure_ascii=False),
            "status": status,
            "last_error": _err(exc),
        },
        release=True,
    )


def _remove_remote_user(db, op: PanelOperation, data: dict, gateway) -> bool:
    user_id = data.get("v2_user_id")
    if not user_id:
        try:
            found = gateway.fetch_by_email(data["email"])
            user_id = int(found["id"])
            data["v2_user_id"] = user_id
        except V2BoardError as exc:
            if _missing_user(exc):
                return True
            _mark(db, op, data, "pending", exc)
            return False
        except Exception as exc:
            if _ambiguous(exc):
                _mark(db, op, data, "pending", exc)
                return False
            _mark(db, op, data, "pending", exc)
            return False
    try:
        gateway.delete_user(int(user_id))
    except Exception as exc:
        data["v2_user_id"] = int(user_id)
        _mark(db, op, data, "pending", exc)
        return False
    return True


def _compensate(db, op: PanelOperation, gateway, exc: BaseException) -> None:
    _save_owned(db, op.id, {})
    op = _reload(db, op.id)
    subs = _subs()
    data = _loads(op)
    if data.get("panel_applied"):
        _mark(db, op, data, "pending", exc)
        return
    kind = op.kind
    if kind == "create":
        # A user under this email that we never generated belongs to someone else.
        if not _never_generated(data) and not _remove_remote_user(db, op, data, gateway):
            return
        _refund_charge(db, op, data, subs)
        _mark(db, op, data, "failed", exc)
        return
    if kind == "increase":
        _revert_cap(db, op, data)
        _refund_charge(db, op, data, subs)
        _mark(db, op, data, "failed", exc)
        return
    if kind == "reset_traffic":
        partner = db.get(Partner, op.partner_id)
        cost = int(data.get("cost") or 0)
        if cost > 0:
            partner.balance_toman = int(partner.balance_toman) + cost
            _ledger_credit(db, subs, op, data, "toman", cost, "operation_rollback")
        _mark(db, op, data, "failed", exc)
        return
    if kind in {"decrease", "delete"}:
        deferred = _deferred_refund(data)
        if deferred:
            subs.lock_partner_row(db, op.partner_id, allow_banned=True)
        if kind == "decrease":
            _revert_cap(db, op, data)
        if not deferred and not _clawback(db, op, data, subs):
            _mark(
                db,
                op,
                data,
                "attention",
                AppError(f"{_err(exc)} | اعتبار برگشتی خرج شده و کامل برنگشت"),
            )
            logger.error("عملیات %s نیاز به بررسی دارد؛ برگشت اعتبار کامل نشد", op.id)
            return
        _mark(db, op, data, "failed", exc)
        return
    _mark(db, op, data, "attention", exc)


def _claim(db: Session, op_id: int, statuses: tuple[str, ...] = ("pending",)) -> str | None:
    token = secrets.token_hex(16)
    now = int(time.time())
    db.execute(
        update(PanelOperation)
        .where(
            PanelOperation.id == op_id,
            PanelOperation.status.in_(statuses),
            or_(PanelOperation.lease_until.is_(None), PanelOperation.lease_until <= now),
        )
        .values(lease_token=token, lease_until=now + LEASE_SECONDS, updated_at=utcnow())
    )
    db.commit()
    op = db.get(PanelOperation, op_id)
    if op is None:
        return None
    db.refresh(op)
    if op.lease_token != token:
        return None
    return token


def _remember(db: Session, op_id: int, exc: BaseException) -> None:
    db.rollback()
    op = db.get(PanelOperation, op_id)
    if op is None or op.status != "pending":
        return
    try:
        _save_owned(db, op_id, {"last_error": _err(exc), "status": "pending"}, release=True)
    except LostLease:
        return


def _after_failure(db: Session, op_id: int, gateway, exc: BaseException) -> None:
    db.rollback()
    op = db.get(PanelOperation, op_id)
    if op is None or op.status != "pending":
        return
    data = _loads(op)
    if data.get("panel_applied"):
        _remember(db, op_id, exc)
        return
    try:
        _compensate(db, op, gateway, exc)
    except LostLease:
        raise
    except Exception as failure:
        logger.error("جبران عملیات %s ناموفق بود (%s)", op_id, type(failure).__name__)
        try:
            _remember(db, op_id, exc)
        except Exception:
            db.rollback()


def _reload(db: Session, op_id: int) -> PanelOperation:
    op = db.get(PanelOperation, op_id)
    if op is None:
        raise AppError("عملیات پیدا نشد", 404)
    db.refresh(op)
    return op


def _read_remote(gateway, *, user_id: int | None = None, email: str | None = None):
    try:
        if user_id:
            return gateway.fetch_by_id(int(user_id)), "found"
        return gateway.fetch_by_email(email or ""), "found"
    except V2BoardError as exc:
        if _missing_user(exc):
            return None, "missing"
        return None, "unknown"
    except Exception:
        return None, "unknown"


def _panel_rejected(exc: BaseException | None) -> bool:
    while exc is not None:
        if isinstance(exc, V2BoardError) and exc.panel_rejected:
            return True
        exc = exc.__cause__
    return False


def _reset_may_have_landed(data: dict) -> bool:
    # Rows written before raw_before existed have an unknown send history.
    return bool(data.get("reset_sent")) or "raw_before" not in data


def _hold_for_review(db: Session, op_id: int, raw: int | None, message: str = RESET_UNPROVEN) -> None:
    op = _reload(db, op_id)
    data = _loads(op)
    if raw is not None:
        data["raw_after"] = int(raw)
    _mark(db, op, data, "attention", AppError(message))
    logger.error("عملیات %s: نتیجهٔ ارسال قبلی به پنل قابل اثبات نیست و به بررسی دستی رفت", op_id)


def _change_maybe_sent(data: dict) -> bool:
    """Rows written before the marker existed carry no key and may already have been sent."""
    return bool(data.get("panel_sent", True))


def _sent_change_landed(db: Session, op: PanelOperation, user, *, target) -> bool:
    """For a quota update that may have been sent: True when the panel already has it. The old value
    is no proof it never ran (an admin may have reverted it), so anything else holds for review."""
    if _same_quota(user, *target) is True:
        return True
    _hold_for_review(db, op.id, None, CHANGE_UNPROVEN)
    raise AppError(ATTENTION.format(id=op.id), 409)


def _panel_email(op: PanelOperation, data: dict, sub=None) -> str:
    domain = data.get("email_domain") or _subs().panel_identity(sub, sub.product)[0]
    return f"{op.username or sub.username}@{domain}"


def _ensure_owner(db: Session, op: PanelOperation, data: dict, user, email: str) -> None:
    """Never change a panel user we did not create. Money returns only if nothing was sent."""
    subs = _subs()
    if subs.owns_panel_user(user, email):
        return
    if _possibly_sent(data):
        data["foreign_target"] = True
        _write(db, op, data)
        _hold_for_review(db, op.id, None, subs.FOREIGN_TARGET)
        raise AppError(ATTENTION.format(id=op.id), 409)
    raise AppError(subs.FOREIGN_TARGET, 409)


def _db_failure(exc: BaseException) -> bool:
    return isinstance(exc, SQLAlchemyError)


def _possibly_sent(data: dict) -> bool:
    # A mutating panel request was committed as sent; its effect may exist without panel_applied.
    return any(data.get(key) for key in ("panel_sent", "reset_sent", "generate_sent", "refund_prior_send"))


def _keep_for_recovery(db: Session, op_id: int, exc: BaseException) -> None:
    """Leave the row pending without moving money; tolerate an unreachable database."""
    try:
        _remember(db, op_id, exc)
    except Exception as failure:
        try:
            db.rollback()
        except Exception:
            pass
        logger.error("عملیات %s برای بازیابی نگه داشته شد؛ ثبت خطا ممکن نشد (%s)", op_id, type(failure).__name__)
    raise AppError(DB_UNAVAILABLE.format(id=op_id) if _db_failure(exc) else SAVED.format(id=op_id), 503) from exc


def _count_resend(db: Session, op: PanelOperation, data: dict) -> None:
    """Bound automatic resends of a request that may already have reached the panel."""
    sent = int(data.get("auto_resends") or 0)
    if sent >= MAX_AUTO_RESENDS:
        _hold_for_review(db, op.id, None, REPEATED_FAILURE)
        raise AppError(ATTENTION.format(id=op.id), 409)
    data["auto_resends"] = sent + 1


def _send_marked(db: Session, op: PanelOperation, data: dict, call) -> None:
    """Commit panel_sent before a mutating panel request; clear it when no connection was made."""
    if data.get("panel_sent"):
        _count_resend(db, op, data)
        if _deferred_refund(data):
            data["refund_prior_send"] = True
    data["panel_sent"] = True
    _write(db, op, data)
    try:
        call()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        data["panel_sent"] = False
        _write(db, op, data)
        raise


def _same_quota(user, cap_gb, expired_at) -> bool | None:
    if not isinstance(user, dict) or "transfer_enable" not in user or user.get("expired_at") is None:
        return None
    try:
        return int(user["transfer_enable"]) == gb_to_bytes(cap_gb) and int(user["expired_at"]) == int(expired_at)
    except (TypeError, ValueError):
        return None


def _inspect(db: Session, op_id: int, gateway, exc: BaseException | None = None) -> str:
    """finish: panel already has the target. continue: keep going without a refund.
    reverse: a read shows the target was not applied. unknown: do not move money.
    attention: a read cannot prove either outcome; stop for a manual decision.
    """
    try:
        db.rollback()
        op = _reload(db, op_id)
        data = _loads(op)
        db.commit()
        if data.get("panel_applied"):
            return "finish"
        kind = op.kind
        if kind == "create" and not data.get("v2_user_id"):
            if _never_generated(data):
                return "reverse"
            user, state = _read_remote(gateway, email=data.get("email"))
            if state == "found" and isinstance(user, dict) and user.get("id") is not None:
                data["v2_user_id"] = int(user["id"])
                data["subscribe_url"] = user.get("subscribe_url") or ""
                data["subscribe_url_fallback"] = bool(user.get("subscribe_url_fallback"))
                _write(db, op, data)
                return "continue"
            # A generate behind a proxy error may still land; generating again is safe (unique email).
            return "unknown"
        if kind in {"create", "increase", "decrease"}:
            user_id = data.get("v2_user_id")
            deferred_decrease = kind == "decrease" and _deferred_refund(data)
            sub = db.get(Subscription, op.subscription_id) if op.subscription_id and not user_id else None
            if not user_id:
                user_id = sub.v2_user_id if sub is not None else None
            if not user_id:
                return "unknown"
            db.commit()
            user, state = _read_remote(gateway, user_id=int(user_id))
            if state != "found":
                if kind == "create" and state == "missing":
                    return "reverse"
                return "unknown"
            if deferred_decrease and not _subs().owns_panel_user(user, _panel_email(op, data, sub)):
                return "attention" if _possibly_sent(data) else "reverse"
            if kind == "create":
                matched = _same_quota(user, data["cap_gb"], data["expired_at"])
            else:
                matched = _same_quota(user, data["new_cap"], data["new_expired_at"])
            if matched is True:
                if deferred_decrease and not _possibly_sent(data):
                    return "unknown"
                data["panel_applied"] = True
                _write(db, op, data)
                return "finish"
            if kind == "create" and matched is False:
                return "reverse"
            previous = None if kind == "create" else _same_quota(user, data["old_cap"], data["old_expired_at"])
            # An old reading after a sent update proves nothing: behind a proxy error it may land later.
            if (matched is False and previous is True and not data.get("refund_prior_send")
                    and (not _change_maybe_sent(data) or _panel_rejected(exc))):
                return "reverse"
            if matched is False and (_change_maybe_sent(data) or data.get("refund_prior_send")):
                return "attention"
            return "unknown"
        if kind == "reset_traffic":
            try:
                info = gateway.get_user_info(int(data["v2_user_id"]))
            except Exception:
                return "unknown"
            if not isinstance(info, dict) or ("u" not in info and "d" not in info):
                return "unknown"
            raw = int(info.get("u") or 0) + int(info.get("d") or 0)
            # A usage drop is also what an admin or plan-scheduled reset produces: never proof of ours.
            never_tried = not _reset_may_have_landed(data) and not data.get("reset_tried")
            if never_tried or _panel_rejected(exc):
                return "reverse"
            data["raw_after"] = raw
            _write(db, op, data)
            return "attention"
        if kind == "delete":
            user, state = _read_remote(gateway, user_id=int(data["v2_user_id"]))
            if state == "missing":
                data["panel_applied"] = True
                _write(db, op, data)
                return "finish"
            if state == "found" and not _possibly_sent(data):
                return "reverse"
            if state == "found" and _deferred_refund(data):
                if not _subs().owns_panel_user(user, _panel_email(op, data)):
                    return "attention"
                if _panel_rejected(exc):
                    return "attention" if data.get("refund_prior_send") else "reverse"
            return "unknown"
        return "unknown"
    except LostLease:
        raise
    except Exception as failure:
        logger.error("خواندن وضعیت پنل برای عملیات %s ناموفق بود (%s)", op_id, type(failure).__name__)
        return "unknown"


def _raise_settled(db: Session, op_id: int, exc: BaseException) -> None:
    current = db.get(PanelOperation, op_id)
    if current is not None and current.status == "attention":
        raise AppError(ATTENTION.format(id=op_id), 409) from exc
    if current is not None and current.status == "pending":
        raise AppError(SAVED.format(id=op_id), 503) from exc
    if isinstance(exc, AppError):
        raise exc
    raise AppError("عملیات پنل ناموفق بود", 502) from exc


def _uncertain(exc: BaseException) -> bool:
    if isinstance(exc, V2BoardError):
        return True
    return isinstance(exc, AppError) and exc.status_code >= 500


def _recover(db: Session, op_id: int, gateway, exc: BaseException):
    if isinstance(exc, LostLease):
        raise AppError(BUSY, 503) from exc
    if _ambiguous(exc) or _db_failure(exc):
        _keep_for_recovery(db, op_id, exc)
    try:
        db.rollback()
        current = _reload(db, op_id)
        status, data = current.status, _loads(current)
    except LostLease:
        raise AppError(BUSY, 503) from exc
    except Exception:
        _keep_for_recovery(db, op_id, exc)
    if status != "pending":
        # The apply step already settled this row (attention, failed); do not recover it again.
        _raise_settled(db, op_id, exc)
    if _uncertain(exc) or _possibly_sent(data):
        verdict = _inspect(db, op_id, gateway, exc)
        if verdict == "attention":
            kind = _reload(db, op_id).kind
            _hold_for_review(db, op_id, None, RESET_UNPROVEN if kind == "reset_traffic" else CHANGE_UNPROVEN)
            raise AppError(ATTENTION.format(id=op_id), 409) from exc
        if verdict in {"finish", "continue"}:
            try:
                op = _reload(db, op_id)
                return _apply(db, op, gateway)
            except LostLease as exc2:
                raise AppError(BUSY, 503) from exc2
            except Exception as exc2:
                if _ambiguous(exc2) or _uncertain(exc2) or _db_failure(exc2):
                    _keep_for_recovery(db, op_id, exc2)
                if _settled(db, op_id):
                    _raise_settled(db, op_id, exc2)
                _keep_for_recovery(db, op_id, exc2)
        if verdict == "reverse":
            _after_failure(db, op_id, gateway, exc)
            _raise_settled(db, op_id, exc)
        logger.warning("عملیات %s بعد از خطا برنگشت؛ وضعیت پنل مشخص نیست", op_id)
        _keep_for_recovery(db, op_id, exc)
    _after_failure(db, op_id, gateway, exc)
    _raise_settled(db, op_id, exc)


def _settled(db: Session, op_id: int) -> bool:
    try:
        db.rollback()
        return _reload(db, op_id).status != "pending"
    except Exception:
        return False


def drive_operation(db: Session, op: PanelOperation, gateway):
    op_id = op.id
    op = db.get(PanelOperation, op_id)
    if op is None:
        raise AppError("عملیات پیدا نشد", 404)
    if op.status == "completed":
        return _result_from(db, op, gateway)
    if op.status != "pending":
        if op.status == "attention":
            raise AppError(ATTENTION.format(id=op_id), 409)
        raise AppError("این عملیات دیگر قابل ادامه نیست")
    token = _claim(db, op_id)
    if token is None:
        op = _reload(db, op_id)
        if op.status == "completed":
            return _result_from(db, op, gateway)
        if op.status == "attention":
            raise AppError(ATTENTION.format(id=op_id), 409)
        if op.status == "pending":
            raise AppError(BUSY, 503)
        raise AppError("این عملیات دیگر قابل ادامه نیست")
    handle = _lease_token.set(token)
    try:
        op = _reload(db, op_id)
        data = _loads(op)
        approved_panel = data.get("consent_panel")
        if approved_panel and approved_panel != getattr(gateway, "financial_recovery_target", None):
            _mark(db, op, data, "attention", AppError("مقصد پنل تغییر کرده است؛ عملیات برای بررسی متوقف شد.", 409))
            raise AppError(ATTENTION.format(id=op_id), 409)
        try:
            return _apply(db, op, gateway)
        except LostLease:
            raise AppError(BUSY, 503)
        except Exception as exc:
            return _recover(db, op_id, gateway, exc)
    finally:
        _lease_token.reset(handle)


def _result_from(db: Session, op: PanelOperation, gateway):
    data = _loads(op)
    if op.kind == "create":
        sub = db.scalar(
            select(Subscription).where(
                Subscription.id == (data.get("subscription_id") or op.subscription_id),
                Subscription.partner_id == op.partner_id,
                Subscription.product_id == op.product_id,
                Subscription.username == data["username"],
                Subscription.v2_user_id == data.get("v2_user_id"),
            )
        )
        if sub is None:
            raise AppError(SAVED.format(id=op.id), 503)
        fetched = _stored_fetch(data)
        if not fetched["subscribe_url"] and gateway is not None:
            try:
                live = gateway.fetch_by_id(int(data["v2_user_id"]))
                fetched = live
            except Exception as exc:
                if _ambiguous(exc):
                    raise AppError(SAVED.format(id=op.id), 503) from exc
        return {"sub": sub, "charge": _load_charge(data), "fetched": fetched}
    if op.kind in {"increase", "decrease"}:
        sub = db.get(Subscription, op.subscription_id) if op.subscription_id else None
        if sub is None:
            raise AppError("عملیات ثبت شده است؛ اشتراک دیگر موجود نیست.", 409)
        if op.kind == "increase":
            return {"sub": sub, "charge": _load_charge(data)}
        return {
            "sub": sub,
            "gb_back": D(data["gb_back"]),
            "days_back": int(data["days_back"]),
        }
    if op.kind == "delete":
        return {
            "gb_refund": dec_str(data["gb_back"]),
            "days_refund": int(data["days_back"]),
            "used_gb": dec_str(data["used_gb"]),
        }
    if op.kind == "reset_traffic":
        partner = db.get(Partner, op.partner_id)
        return {
            "cost_toman": int(data["cost"]),
            "used_gb": dec_str(data["used_gb"]),
            "balance_toman": int(partner.balance_toman),
        }
    raise AppError("عملیات نامعتبر است")


def _stored_fetch(data: dict) -> dict:
    return {
        "id": data.get("v2_user_id"),
        "subscribe_url": data.get("subscribe_url") or "",
        "subscribe_url_fallback": bool(data.get("subscribe_url_fallback")),
    }


def _apply(db: Session, op: PanelOperation, gateway):
    if op.kind == "create":
        return _apply_create(db, op, gateway)
    if op.kind == "increase":
        return _apply_increase(db, op, gateway)
    if op.kind == "decrease":
        return _apply_decrease(db, op, gateway)
    if op.kind == "delete":
        return _apply_delete(db, op, gateway)
    if op.kind == "reset_traffic":
        return _apply_reset(db, op, gateway)
    raise AppError("عملیات نامعتبر است")


class ForeignPanelUser(AppError):
    def __init__(self):
        super().__init__(FOREIGN_USER, 409)


def _never_generated(data: dict) -> bool:
    # Rows written before generate_sent existed keep the old adopt-on-retry behavior.
    return "generate_sent" in data and not data["generate_sent"]


def _find_or_generate(db: Session, op: PanelOperation, gateway, data: dict) -> dict:
    subs = _subs()
    try:
        found = _call_panel(gateway, "fetch_by_email", data["email"])
    except V2BoardError as exc:
        if not _missing_user(exc):
            raise
    else:
        if _never_generated(data):
            raise ForeignPanelUser()
        return found
    first_send = _never_generated(data)
    if not first_send:
        _count_resend(db, op, data)
    data["generate_sent"] = True
    _write(db, op, data)
    try:
        _call_panel(
            gateway,
            "generate_user",
            email_prefix=data["username"],
            email_suffix=data["email_domain"],
            plan_id=int(data["plan_id"]),
            expired_at=int(data["expired_at"]),
            password=secrets.token_urlsafe(12),
        )
    except V2BoardError as exc:
        if not _exists_user(exc):
            raise
        if first_send:
            # Someone else created this email between our lookup and generate.
            data["generate_sent"] = False
            _write(db, op, data)
            raise ForeignPanelUser() from exc
    return subs._fetch_new_user(gateway, data["email"])


def _apply_create(db: Session, op: PanelOperation, gateway):
    subs = _subs()
    data = _loads(op)
    product = subs.load_product(db, op.product_id)
    view = _view(data, product)
    db.commit()
    resumed = bool(data.get("v2_user_id"))
    if not resumed:
        try:
            fetched = _find_or_generate(db, op, gateway, data)
        except ForeignPanelUser as exc:
            _save_owned(db, op.id, {})
            op = _reload(db, op.id)
            data = _loads(op)
            _refund_charge(db, op, data, subs)
            _mark(db, op, data, "failed", exc)
            raise
        data["v2_user_id"] = int(fetched["id"])
        data["subscribe_url"] = fetched.get("subscribe_url") or ""
        data["subscribe_url_fallback"] = bool(fetched.get("subscribe_url_fallback"))
        _write(db, op, data)
    else:
        fetched = _stored_fetch(data)
    if not data.get("panel_applied"):
        plan = _call_panel(gateway, "get_plan", int(view.plan_id))
        landed = False
        if resumed and _change_maybe_sent(data):
            user = _call_panel(gateway, "get_user_info", int(data["v2_user_id"]))
            landed = _sent_change_landed(db, op, user, target=(data["cap_gb"], data["expired_at"]))
        if not landed:
            holder = type("Cap", (), {})()
            holder.username = data["username"]
            holder.v2_user_id = int(data["v2_user_id"])
            holder.traffic_cap_gb = qgb(data["cap_gb"])
            holder.expired_at = int(data["expired_at"])
            payload = subs.build_update(holder, view, plan, banned=False)
            _send_marked(db, op, data, lambda: _call_panel(gateway, "update_user", payload))
        data["panel_applied"] = True
        _write(db, op, data)
    sub = _ensure_local_sub(db, op, data)
    # Only the rows recorded when this operation took payment are eligible.
    # Older operations can lack this evidence.
    if ledger_ids := data.get("charge_ledger_ids"):
        for row in db.scalars(select(WalletLedger).where(
            WalletLedger.id.in_(ledger_ids),
            WalletLedger.partner_id == op.partner_id,
            WalletLedger.product_id == op.product_id,
            WalletLedger.reason == "create",
            WalletLedger.subscription_id.is_(None),
        )):
            row.subscription_id = sub.id
    data["subscription_id"] = sub.id
    _save_owned(db, op.id, {
        "subscription_id": sub.id,
        "payload": json.dumps(data, ensure_ascii=False),
        "status": "completed",
    })
    return {"sub": sub, "charge": _load_charge(data), "fetched": fetched}


def _ensure_local_sub(db: Session, op: PanelOperation, data: dict) -> Subscription:
    product_id = op.product_id
    partner_id = op.partner_id
    username = data["username"]
    v2_user_id = int(data["v2_user_id"])
    sub = db.scalar(
        select(Subscription).where(Subscription.product_id == product_id, Subscription.username == username)
    )
    if sub is not None:
        if sub.partner_id != partner_id or int(sub.v2_user_id) != v2_user_id:
            raise AppError("این نام کاربری به کاربر دیگری وصل است")
        return sub
    sub = Subscription(
        partner_id=partner_id,
        product_id=product_id,
        username=username,
        v2_user_id=v2_user_id,
        note=data.get("note"),
        email_domain=data.get("email_domain"),
        plan_id=int(data["plan_id"]) if data.get("plan_id") is not None else None,
        traffic_cap_gb=qgb(data["cap_gb"]),
        expired_at=int(data["expired_at"]),
        banned=False,
        blocked_by_partner_ban=False,
    )
    db.add(sub)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        sub = db.scalar(
            select(Subscription).where(Subscription.product_id == product_id, Subscription.username == username)
        )
        if sub is None or sub.partner_id != partner_id or int(sub.v2_user_id) != v2_user_id:
            raise AppError("این نام کاربری روی این محصول قبلاً ثبت شده") from exc
    return sub


def _apply_increase(db: Session, op: PanelOperation, gateway):
    return _apply_cap_change(db, op, gateway)


def _apply_decrease(db: Session, op: PanelOperation, gateway):
    outcome = _apply_cap_change(db, op, gateway)
    data = _loads(op)
    return {"sub": outcome["sub"], "gb_back": D(data["gb_back"]), "days_back": int(data["days_back"])}


def _apply_cap_change(db: Session, op: PanelOperation, gateway):
    subs = _subs()
    data = _loads(op)
    sub = db.get(Subscription, op.subscription_id)
    if sub is None:
        raise AppError("اشتراک این عملیات پیدا نشد")
    product = subs.load_product(db, op.product_id)
    view = _view(data, product)
    user_id = int(sub.v2_user_id)
    email = _panel_email(op, data, sub)
    db.commit()
    deferred_decrease = op.kind == "decrease" and _deferred_refund(data)
    if not data.get("panel_applied"):
        plan = _call_panel(gateway, "get_plan", int(view.plan_id))
        current = _call_panel(gateway, "get_user_info", user_id)
        _ensure_owner(db, op, data, current, email)
        target = (data["new_cap"], data["new_expired_at"])
        already_sent = _change_maybe_sent(data)
        if already_sent and not deferred_decrease:
            _sent_change_landed(db, op, current, target=target)
        if not already_sent:
            payload = subs.build_update(
                sub,
                view,
                plan,
                banned=bool(sub.banned),
                transfer_gb=qgb(data["new_cap"]),
                expired_at=int(data["new_expired_at"]),
                current=current,
            )
            _send_marked(db, op, data, lambda: _call_panel(gateway, "update_user", payload))
        if deferred_decrease:
            confirmed, state = _read_remote(gateway, user_id=user_id)
            if state != "found" or _same_quota(confirmed, *target) is None:
                raise V2BoardError("نتیجه کاهش اشتراک روی پنل قابل تأیید نیست")
            _ensure_owner(db, op, data, confirmed, email)
            _sent_change_landed(db, op, confirmed, target=target)
        data["panel_applied"] = True
        _write(db, op, data)
    if deferred_decrease:
        _settle_refund(db, op, data)
        _write(db, op, data, status="completed")
    else:
        _complete_owned(db, op, data)
    db.refresh(sub)
    if op.kind == "increase":
        return {"sub": sub, "charge": _load_charge(data)}
    return {"sub": sub}


def _apply_delete(db: Session, op: PanelOperation, gateway):
    data = _loads(op)
    deferred = _deferred_refund(data)
    db.commit()
    if not data.get("panel_applied"):
        user_id = int(data["v2_user_id"])
        # Only a validated lookup proves the user is gone or is still ours.
        user, state = _read_remote(gateway, user_id=user_id)
        if state == "unknown":
            raise V2BoardError("وضعیت کاربر روی پنل قابل خواندن نیست")
        if state == "found":
            _ensure_owner(db, op, data, user, _panel_email(op, data))
            _send_marked(db, op, data, lambda: _call_panel(gateway, "delete_user", user_id))
            if deferred:
                _confirmed, state = _read_remote(gateway, user_id=user_id)
                if state != "missing":
                    raise V2BoardError("حذف کاربر روی پنل هنوز تأیید نشده است")
        data["panel_applied"] = True
        _write(db, op, data)
    if deferred:
        _settle_refund(db, op, data)
    sub_id = op.subscription_id if deferred else op.subscription_id or data.get("subscription_id")
    sub = db.get(Subscription, int(sub_id)) if sub_id else None
    if sub is not None:
        for row in db.scalars(select(WalletLedger).where(WalletLedger.subscription_id == sub.id)):
            row.subscription_id = None
        db.delete(sub)
    _write(db, op, data, status="completed")
    return {
        "gb_refund": dec_str(data["gb_back"]),
        "days_refund": int(data["days_back"]),
        "used_gb": dec_str(data["used_gb"]),
    }


def _apply_reset(db: Session, op: PanelOperation, gateway):
    subs = _subs()
    data = _loads(op)
    sub = db.get(Subscription, op.subscription_id)
    if sub is None:
        raise AppError("اشتراک این عملیات پیدا نشد")
    email = _panel_email(op, data, sub)
    db.commit()
    if not data.get("panel_applied"):
        info = _call_panel(gateway, "get_user_info", int(data["v2_user_id"]))
        _ensure_owner(db, op, data, info, email)
        raw = int(info.get("u") or 0) + int(info.get("d") or 0)
        if _reset_may_have_landed(data):
            # An earlier reset may or may not have landed and no reading can tell ours from an
            # admin or scheduled reset: no completion, refund or second reset without the admin.
            _hold_for_review(db, op.id, raw)
            raise AppError(ATTENTION.format(id=op.id), 409)
        elif raw <= 0:
            if not data.get("reset_tried"):
                # No reset request was ever attempted, so zero usage is someone else's reset: refund it.
                raise AppError("مصرف این اشتراک پیش از ارسال ریست صفر شد؛ ریستی انجام نشد", 409)
        else:
            product = subs.load_product(db, op.product_id)
            view = _view(data, product)
            # Materialize every ORM attribute before releasing the transaction.
            # Loading product above starts BEGIN IMMEDIATE again on SQLite.
            holder, _view_unused, _plan_id, banned = subs._ban_holder(sub)
            _release_lock(db)
            plan = _call_panel(gateway, "get_plan", int(view.plan_id))
            data["raw_before"] = raw
            data["reset_sent"] = data["reset_tried"] = True
            _write(db, op, data)
            try:
                _call_panel(
                    gateway,
                    "update_user",
                    subs.build_update(holder, view, plan, banned=banned, reset_traffic=True, current=info),
                )
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # No connection was established, so the reset never reached the panel.
                data["reset_sent"] = False
                _write(db, op, data)
                raise
        data["panel_applied"] = True
        _write(db, op, data)
    _complete_owned(db, op, data)
    partner = db.get(Partner, op.partner_id)
    return {
        "cost_toman": int(data["cost"]),
        "used_gb": dec_str(data["used_gb"]),
        "balance_toman": int(partner.balance_toman),
    }


def _same_bounds(data: dict, traffic_cap_gb, end_date: str | None, subs) -> bool:
    old_cap = qgb(data["old_cap"])
    old_exp = int(data["old_expired_at"])
    new_cap = old_cap if traffic_cap_gb is None else subs._check_cap(traffic_cap_gb)
    new_exp = old_exp if not end_date else end_of_day_ts(subs._parse_end(end_date))
    return dec_str(new_cap) == dec_str(data["new_cap"]) and int(new_exp) == int(data["new_expired_at"])


def _resume_sub(db, sub_id: int, gateway, *, kind: str, traffic_cap_gb=None, end_date: str | None = None, bounds: bool = False):
    approved = consent.existing(db)
    if approved is not None:
        return drive_operation(db, approved, gateway)
    pending = _pending_sub(db, sub_id)
    if pending is None:
        attention = _attention_sub(db, sub_id)
        if attention is not None:
            raise AppError(ATTENTION.format(id=attention.id), 409)
        return None
    if db.info.get("financial_consent") is not None:
        raise AppError(ATTENTION.format(id=pending.id), 409)
    match = pending.kind == kind
    if match and bounds:
        try:
            match = _same_bounds(_loads(pending), traffic_cap_gb, end_date, _subs())
        except AppError:
            match = False
    outcome = drive_operation(db, pending, gateway)
    if not match:
        raise AppError(MISMATCH)
    return outcome


def _open(db: Session, op: PanelOperation) -> PanelOperation:
    db.add(op)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise
    return op


def _base_payload(actor, product, **extra) -> dict:
    sub = extra.pop("sub", None)
    if sub is not None:
        domain, plan_id = _subs().panel_identity(sub, product)
    else:
        domain, plan_id = product.email_domain, int(product.plan_id)
    body = {
        "actor_type": actor.type,
        "actor_id": int(actor.id),
        "email_domain": domain,
        "plan_id": int(plan_id),
        "panel_applied": False,
        "panel_sent": False,
    }
    body.update(extra)
    return body


def perform_create(
    db: Session,
    *,
    partner_id: int,
    product_id: int,
    username: str,
    traffic_cap_gb,
    end_date: str,
    note: str | None,
    actor,
    gateway,
    now=None,
):
    subs = _subs()
    if db.info.get("financial_consent") is not None:
        subs.lock_partner_row(db, partner_id)
        approved = consent.existing(db)
        if approved is not None:
            result = drive_operation(db, approved, gateway)
            return result["sub"], result["charge"], result["fetched"]
    username = subs.normalize_username(username)
    cap = subs._check_cap(traffic_cap_gb)
    end = subs._parse_end(end_date)
    current = subs.now_tehran(now)
    if end <= current.date():
        raise AppError("تاریخ پایان باید بعد از امروز باشد")
    days = subs.need_days(end, current.date())
    if days <= 0 or days > subs.MAX_DAYS:
        raise AppError("بازه زمانی نامعتبر است")
    note = (note or "").strip() or None
    if note and len(note) > 2000:
        raise AppError("یادداشت طولانی است")
    product = subs.load_product(db, product_id)
    if not product.active:
        raise AppError("این محصول خاموش است و ساخت اشتراک جدید ممکن نیست")
    subs._ensure_panel(product)
    partner = subs.lock_partner_row(db, partner_id)
    taken = db.scalar(
        select(Subscription.id).where(Subscription.product_id == product.id, Subscription.username == username)
    )
    if taken:
        raise AppError("این نام کاربری روی این محصول قبلاً ثبت شده")
    pending = _pending_create(db, product.id, username)
    expired_at = end_of_day_ts(end)
    if pending is not None:
        if pending.partner_id != partner.id:
            raise AppError("این نام کاربری روی این محصول در حال ثبت است")
        if db.info.get("financial_consent") is not None:
            raise AppError(ATTENTION.format(id=pending.id), 409)
        return _finish_create(db, pending, gateway, cap, expired_at)
    attention = _attention_create(db, product.id, username)
    if attention is not None:
        raise AppError(ATTENTION.format(id=attention.id), 409)
    # This preliminary read precedes a commit that releases locks for panel I/O.
    # Do not create a credit row before the final consent check.
    credit_gb, credit_days = subs.read_credit(db, partner.id, product.id)
    charge = subs.quote_charge(
        cap, days, credit_gb, credit_days, product.price_per_gb, product.price_per_day
    )
    if charge.toman > int(partner.balance_toman):
        # A changed quote requires review. A wallet-only shortfall keeps its existing error.
        consent.check(db, product.id, None, _dump_charge(charge))
        raise AppError("موجودی تومان یا اعتبار این محصول کافی نیست")
    plan_id = int(product.plan_id)
    _release_lock(db)
    try:
        _call_panel(gateway, "get_plan", plan_id)
    except Exception as exc:
        _guard_pre_money(exc)
    product = subs.load_product(db, product_id)
    partner = subs.lock_partner_row(db, partner_id)
    approved = consent.existing(db)
    if approved is not None:
        result = drive_operation(db, approved, gateway)
        return result["sub"], result["charge"], result["fetched"]
    taken = db.scalar(
        select(Subscription.id).where(Subscription.product_id == product.id, Subscription.username == username)
    )
    if taken:
        raise AppError("این نام کاربری روی این محصول قبلاً ثبت شده")
    pending = _pending_create(db, product.id, username)
    if pending is not None:
        if pending.partner_id != partner.id:
            raise AppError("این نام کاربری روی این محصول در حال ثبت است")
        if db.info.get("financial_consent") is not None:
            raise AppError(ATTENTION.format(id=pending.id), 409)
        return _finish_create(db, pending, gateway, cap, expired_at)
    attention = _attention_create(db, product.id, username)
    if attention is not None:
        raise AppError(ATTENTION.format(id=attention.id), 409)
    credit = subs.lock_credit(db, partner.id, product.id)
    charge = subs.quote_charge(
        cap, days, credit.credit_gb, credit.credit_days, product.price_per_gb, product.price_per_day
    )
    authorization = consent.check(db, product.id, None, _dump_charge(charge))
    if charge.toman > int(partner.balance_toman):
        raise AppError("موجودی تومان یا اعتبار این محصول کافی نیست")
    subs._take_charge(partner, credit, charge)
    data = _base_payload(
        actor,
        product,
        **authorization,
        username=username,
        email=f"{username}@{product.email_domain}",
        cap_gb=dec_str(cap),
        expired_at=int(expired_at),
        note=note,
        charge=_dump_charge(charge),
        generate_sent=False,
    )
    op = PanelOperation(
        partner_id=partner.id,
        product_id=product.id,
        subscription_id=None,
        username=username,
        kind="create",
        status="pending",
        payload="{}",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(op)
    rows = subs._charge_ledgers(db, partner.id, product.id, None, charge, "create", actor)
    try:
        db.flush()
        data["charge_ledger_ids"] = [row.id for row in rows]
        op.payload = json.dumps(data, ensure_ascii=False)
        db.commit()
    except IntegrityError:
        db.rollback()
        pending = _pending_create(db, product_id, username)
        if pending is None:
            raise AppError("این نام کاربری روی این محصول قبلاً ثبت شده")
        if pending.partner_id != partner_id:
            raise AppError("این نام کاربری روی این محصول در حال ثبت است")
        if db.info.get("financial_consent") is not None:
            raise AppError(ATTENTION.format(id=pending.id), 409)
        return _finish_create(db, pending, gateway, cap, expired_at)
    outcome = drive_operation(db, op, gateway)
    return outcome["sub"], outcome["charge"], outcome["fetched"]


def _finish_create(db, op: PanelOperation, gateway, cap, expired_at: int):
    data = _loads(op)
    match = dec_str(data.get("cap_gb")) == dec_str(cap) and int(data.get("expired_at") or 0) == int(expired_at)
    outcome = drive_operation(db, op, gateway)
    if not match:
        raise AppError(MISMATCH)
    return outcome["sub"], outcome["charge"], outcome["fetched"]


def perform_increase(
    db: Session,
    *,
    sub_id: int,
    partner_id: int | None,
    traffic_cap_gb,
    end_date: str | None,
    actor,
    gateway,
    now=None,
):
    subs = _subs()
    sub = subs.lock_subscription(db, sub_id, partner_id)
    resumed = _resume_sub(db, sub.id, gateway, kind="increase", traffic_cap_gb=traffic_cap_gb, end_date=end_date, bounds=True)
    if resumed is not None:
        return resumed["sub"], resumed["charge"]
    subs._ensure_panel(sub.product)
    subs._resolve_increase(sub, traffic_cap_gb, end_date, now)
    _domain, plan_id = subs.panel_identity(sub, sub.product)
    _release_lock(db)
    try:
        _call_panel(gateway, "get_plan", plan_id)
    except Exception as exc:
        _guard_pre_money(exc)
    sub = subs.lock_subscription(db, sub_id, partner_id)
    resumed = _resume_sub(db, sub.id, gateway, kind="increase", traffic_cap_gb=traffic_cap_gb, end_date=end_date, bounds=True)
    if resumed is not None:
        return resumed["sub"], resumed["charge"]
    new_cap, new_exp, need_gb, days, _base, _end = subs._resolve_increase(sub, traffic_cap_gb, end_date, now)
    old_cap = qgb(sub.traffic_cap_gb)
    old_exp = int(sub.expired_at)
    partner = subs.lock_partner_row(db, sub.partner_id)
    credit = subs.lock_credit(db, partner.id, sub.product_id)
    charge = subs.quote_charge(
        need_gb, days, credit.credit_gb, credit.credit_days, sub.product.price_per_gb, sub.product.price_per_day
    )
    authorization = consent.check(db, sub.product_id, sub, _dump_charge(charge))
    subs._take_charge(partner, credit, charge)
    sub.traffic_cap_gb = new_cap
    sub.expired_at = new_exp
    sub.updated_at = utcnow()
    subs._charge_ledgers(db, partner.id, sub.product_id, sub.id, charge, "increase", actor)
    data = _base_payload(
        actor,
        sub.product,
        sub=sub,
        **authorization,
        old_cap=dec_str(old_cap),
        new_cap=dec_str(new_cap),
        old_expired_at=old_exp,
        new_expired_at=int(new_exp),
        charge=_dump_charge(charge),
        subscription_id=sub.id,
    )
    op = PanelOperation(
        partner_id=partner.id,
        product_id=sub.product_id,
        subscription_id=sub.id,
        username=sub.username,
        kind="increase",
        status="pending",
        payload=json.dumps(data, ensure_ascii=False),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    try:
        _open(db, op)
    except IntegrityError:
        pending = _pending_sub(db, sub.id)
        if pending is None:
            raise AppError("یک عملیات دیگر روی این اشتراک در جریان است")
        resumed = _resume_sub(db, sub.id, gateway, kind="increase", traffic_cap_gb=traffic_cap_gb, end_date=end_date, bounds=True)
        if resumed is None:
            raise AppError("یک عملیات دیگر روی این اشتراک در جریان است")
        return resumed["sub"], resumed["charge"]
    outcome = drive_operation(db, op, gateway)
    return outcome["sub"], outcome["charge"]


def perform_decrease(
    db: Session,
    *,
    sub_id: int,
    partner_id: int | None,
    traffic_cap_gb,
    end_date: str | None,
    actor,
    gateway,
    now=None,
):
    subs = _subs()
    sub = subs.lock_subscription(db, sub_id, partner_id)
    resumed = _resume_sub(db, sub.id, gateway, kind="decrease", traffic_cap_gb=traffic_cap_gb, end_date=end_date, bounds=True)
    if resumed is not None:
        return resumed["sub"], resumed["gb_back"], resumed["days_back"]
    subs._ensure_panel(sub.product)
    needs_usage = traffic_cap_gb is not None and qgb(traffic_cap_gb) < qgb(sub.traffic_cap_gb)
    if not needs_usage:
        subs._resolve_decrease(sub, traffic_cap_gb, end_date, now, None)
    v2_user_id = int(sub.v2_user_id)
    _domain, plan_id = subs.panel_identity(sub, sub.product)
    _release_lock(db)
    used_bytes = None
    try:
        if needs_usage:
            info = _call_panel(gateway, "get_user_info", v2_user_id)
            used_bytes = subs.used_bytes_of(info)
        _call_panel(gateway, "get_plan", plan_id)
    except Exception as exc:
        _guard_pre_money(exc)
    sub = subs.lock_subscription(db, sub_id, partner_id)
    resumed = _resume_sub(db, sub.id, gateway, kind="decrease", traffic_cap_gb=traffic_cap_gb, end_date=end_date, bounds=True)
    if resumed is not None:
        return resumed["sub"], resumed["gb_back"], resumed["days_back"]
    if traffic_cap_gb is not None and qgb(traffic_cap_gb) < qgb(sub.traffic_cap_gb) and used_bytes is None:
        info = _call_panel(gateway, "get_user_info", sub.v2_user_id)
        used_bytes = subs.used_bytes_of(info)
    new_cap, new_exp, gb_back, days_back = subs._resolve_decrease(sub, traffic_cap_gb, end_date, now, used_bytes)
    used = subs.bytes_to_gb(used_bytes) if used_bytes is not None else None
    old_cap = qgb(sub.traffic_cap_gb)
    old_exp = int(sub.expired_at)
    partner = subs.lock_partner_row(db, sub.partner_id)
    authorization = consent.check(db, sub.product_id, sub, _refund_terms(gb_back, days_back, used))
    sub.traffic_cap_gb = new_cap
    sub.expired_at = new_exp
    sub.updated_at = utcnow()
    data = _base_payload(
        actor,
        sub.product,
        sub=sub,
        **authorization,
        old_cap=dec_str(old_cap),
        new_cap=dec_str(new_cap),
        old_expired_at=old_exp,
        new_expired_at=int(new_exp),
        refund_deferred=True,
        gb_back=dec_str(gb_back),
        days_back=int(days_back),
        used_gb=dec_str(used) if used is not None else None,
        subscription_id=sub.id,
    )
    op = PanelOperation(
        partner_id=partner.id,
        product_id=sub.product_id,
        subscription_id=sub.id,
        username=sub.username,
        kind="decrease",
        status="pending",
        payload=json.dumps(data, ensure_ascii=False),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    try:
        _open(db, op)
    except IntegrityError:
        resumed = _resume_sub(db, sub.id, gateway, kind="decrease", traffic_cap_gb=traffic_cap_gb, end_date=end_date, bounds=True)
        if resumed is None:
            raise AppError("یک عملیات دیگر روی این اشتراک در جریان است")
        return resumed["sub"], resumed["gb_back"], resumed["days_back"]
    outcome = drive_operation(db, op, gateway)
    return outcome["sub"], outcome["gb_back"], outcome["days_back"]


def _refund_terms(gb, days, used):
    return dict(_dump_charge(_subs().quote_charge(0, 0, 0, 0, 0, 0)),
                gb_refund=dec_str(gb), days_refund=int(days),
                used_gb=dec_str(used) if used is not None else None)


def perform_delete(
    db: Session,
    *,
    sub_id: int,
    partner_id: int | None,
    actor,
    gateway,
    now=None,
):
    subs = _subs()
    sub = subs.lock_subscription(db, sub_id, partner_id)
    resumed = _resume_sub(db, sub.id, gateway, kind="delete")
    if resumed is not None:
        return resumed
    subs._ensure_panel(sub.product)
    v2_user_id = int(sub.v2_user_id)
    _release_lock(db)
    try:
        info = _call_panel(gateway, "get_user_info", v2_user_id)
    except Exception as exc:
        _guard_pre_money(exc)
    sub = subs.lock_subscription(db, sub_id, partner_id)
    resumed = _resume_sub(db, sub.id, gateway, kind="delete")
    if resumed is not None:
        return resumed
    used = subs.used_gb_of(info)
    now_ts = int(subs.now_tehran(now).timestamp())
    left = subs.days_left(int(sub.expired_at), now_ts)
    gb_back, days_back = subs.refund_delete(sub.traffic_cap_gb, used, left)
    partner = subs.lock_partner_row(db, sub.partner_id)
    authorization = consent.check(db, sub.product_id, sub, _refund_terms(gb_back, days_back, used))
    data = _base_payload(
        actor,
        sub.product,
        sub=sub,
        **authorization,
        v2_user_id=int(sub.v2_user_id),
        refund_deferred=True,
        gb_back=dec_str(gb_back),
        days_back=int(days_back),
        used_gb=dec_str(used),
        subscription_id=sub.id,
    )
    op = PanelOperation(
        partner_id=partner.id,
        product_id=sub.product_id,
        subscription_id=sub.id,
        username=sub.username,
        kind="delete",
        status="pending",
        payload=json.dumps(data, ensure_ascii=False),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    try:
        _open(db, op)
    except IntegrityError:
        resumed = _resume_sub(db, sub.id, gateway, kind="delete")
        if resumed is None:
            raise AppError("یک عملیات دیگر روی این اشتراک در حال حذف است")
        return resumed
    return drive_operation(db, op, gateway)


def perform_reset(
    db: Session,
    *,
    sub_id: int,
    partner_id: int | None,
    actor,
    gateway,
):
    subs = _subs()
    sub = subs.lock_subscription(db, sub_id, partner_id)
    resumed = _resume_sub(db, sub.id, gateway, kind="reset_traffic")
    if resumed is not None:
        return resumed
    subs._ensure_panel(sub.product)
    _domain, plan_id = subs.panel_identity(sub, sub.product)
    v2_user_id = int(sub.v2_user_id)
    _release_lock(db)
    try:
        _call_panel(gateway, "get_plan", plan_id)
    except Exception as exc:
        _guard_pre_money(exc)
    for _attempt in range(3):
        sub = subs.lock_subscription(db, sub_id, partner_id)
        resumed = _resume_sub(db, sub.id, gateway, kind="reset_traffic")
        if resumed is not None:
            return resumed
        stamp = _moment(sub.updated_at)
        _release_lock(db)
        try:
            info = _call_panel(gateway, "get_user_info", v2_user_id)
        except Exception as exc:
            _guard_pre_money(exc)
        sub = subs.lock_subscription(db, sub_id, partner_id)
        resumed = _resume_sub(db, sub.id, gateway, kind="reset_traffic")
        if resumed is not None:
            return resumed
        if _moment(sub.updated_at) != stamp:
            _release_lock(db)
            continue
        raw_used = subs.used_bytes_of(info)
        if raw_used <= 0:
            if db.info.get("financial_consent") is not None:
                raise consent.conflict()
            raise AppError("مصرفی برای ریست وجود ندارد")
        used = subs.bytes_to_gb(raw_used)
        cost = reset_cost_toman(raw_used, sub.product.price_per_gb)
        partner = subs.lock_partner_row(db, sub.partner_id)
        if cost > int(partner.balance_toman):
            raise AppError("موجودی تومان برای ریست ترافیک کافی نیست")
        authorization = consent.check(db, sub.product_id, sub, dict(
            _dump_charge(subs.quote_charge(used, 0, 0, 0, 0, 0)),
            toman=cost, used_gb=dec_str(used),
        ))
        partner.balance_toman = int(partner.balance_toman) - cost
        subs._ledger(
            db,
            partner_id=partner.id,
            asset="toman",
            amount=-cost,
            product_id=sub.product_id,
            subscription_id=sub.id,
            reason="reset_traffic",
            actor=actor,
        )
        sub.updated_at = utcnow()
        data = _base_payload(
            actor,
            sub.product,
            sub=sub,
            **authorization,
            v2_user_id=int(sub.v2_user_id),
            cost=int(cost),
            used_gb=dec_str(used),
            subscription_id=sub.id,
            raw_before=int(raw_used),
            reset_sent=False,
        )
        op = PanelOperation(
            partner_id=partner.id,
            product_id=sub.product_id,
            subscription_id=sub.id,
            username=sub.username,
            kind="reset_traffic",
            status="pending",
            payload=json.dumps(data, ensure_ascii=False),
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        try:
            _open(db, op)
        except IntegrityError:
            resumed = _resume_sub(db, sub.id, gateway, kind="reset_traffic")
            if resumed is None:
                raise AppError("یک عملیات دیگر روی این اشتراک در جریان است")
            return resumed
        return drive_operation(db, op, gateway)
    raise AppError("وضعیت اشتراک در حال تغییر بود. ریست را دوباره امتحان کنید.", 503)


def _resend_pending(op: PanelOperation, data: dict) -> bool:
    if op.kind == "create" and not data.get("v2_user_id"):
        return not _never_generated(data)
    if op.kind == "delete":
        return bool(data.get("panel_sent"))
    return _change_maybe_sent(data)


def _retry_kind(op: PanelOperation, data: dict) -> str | None:
    """What `retry` does for this attention row, mirroring resolve_attention."""
    if op.status != "attention" or data.get("foreign_target"):
        return None
    shortfall = "shortfall_gb" in data or "shortfall_days" in data
    if op.kind == "reset_traffic" and not data.get("panel_applied"):
        return "resend_reset"
    if op.kind in {"decrease", "delete"} and _deferred_refund(data):
        return "resend_change"
    if (op.kind in {"create", "increase", "decrease", "delete"} and _resend_pending(op, data)
            and not data.get("panel_applied") and not shortfall):
        return "resend_change"
    if shortfall:
        return "collect_shortfall"
    return None


def _moment_text(value) -> str | None:
    if value is None:
        return None
    # Stored as naive UTC on SQLite; say so, or browsers read it as local time.
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


TECHNICAL_ERROR = "خطای فنی ثبت شد؛ جزئیات آن فقط در لاگ سرور نگه داشته می‌شود."


def _shown_error(op: PanelOperation) -> str | None:
    """Messages written by this module are Persian; anything else is a raw technical text."""
    text = op.last_error
    if not text or re.search("[\u0600-\u06FF]", text):
        return text
    logger.warning("عملیات %s: متن فنی خطا به ادمین نشان داده نشد: %s", op.id, text[:300])
    return TECHNICAL_ERROR


def operation_view(op: PanelOperation) -> dict:
    # Explicit allow-list: payloads also hold subscribe links and panel identifiers.
    data = _loads(op)
    charge = data.get("charge") if isinstance(data.get("charge"), dict) else {}
    refund_state = None
    if _deferred_refund(data):
        refund_state = {"completed": "credited", "failed": "voided", "closed": "voided"}.get(op.status, "reserved")
    return {
        "id": op.id,
        "status": op.status,
        "kind": op.kind,
        "partner_id": op.partner_id,
        "product_id": op.product_id,
        "subscription_id": op.subscription_id,
        "username": op.username,
        "last_error": _shown_error(op),
        "created_at": _moment_text(op.created_at),
        "updated_at": _moment_text(op.updated_at),
        "busy": bool(op.lease_until and int(op.lease_until) > int(time.time())),
        "charge_toman": int(charge.get("toman") or data.get("cost") or 0),
        "charge_gb_from_credit": charge.get("gb_from_credit"),
        "charge_days_from_credit": charge.get("days_from_credit"),
        "gb_back": data.get("gb_back"),
        "days_back": data.get("days_back"),
        "refund_state": refund_state,
        "old_cap_gb": data.get("old_cap"),
        "new_cap_gb": data.get("new_cap") or data.get("cap_gb"),
        "panel_applied": bool(data.get("panel_applied")),
        "panel_sent": bool(data.get("panel_sent") or data.get("reset_sent") or data.get("generate_sent")),
        "shortfall_gb": data.get("shortfall_gb"),
        "shortfall_days": data.get("shortfall_days"),
        "resolution": data.get("resolution"),
        "resolution_note": data.get("resolution_note"),
        "retry_action": _retry_kind(op, data),
    }


def list_operations(db: Session, status: str | None = None, limit: int = 50) -> list[dict]:
    allowed = {"pending", "attention", "failed", "completed", "closed"}
    if status is not None and status not in allowed:
        raise AppError("وضعیت نامعتبر است")
    query = select(PanelOperation).order_by(PanelOperation.id.desc()).limit(min(max(int(limit), 1), 100))
    if status is not None:
        query = query.where(PanelOperation.status == status)
    return [operation_view(row) for row in db.scalars(query).all()]


def operation_panel_state(db: Session, op_id: int, gateway_factory) -> dict:
    """Read-only, non-secret view of the panel user behind an operation."""
    op = db.get(PanelOperation, op_id)
    if op is None:
        raise AppError("عملیات پیدا نشد", 404)
    data = _loads(op)
    if data.get("consent_panel"):
        product = _subs().load_product(db, op.product_id)
        if consent.recovery_target(product.panel) != data["consent_panel"]:
            return {"available": False, "reason": "مقصد پنل این عملیات تغییر کرده است"}
    user_id = data.get("v2_user_id")
    sub = db.get(Subscription, op.subscription_id) if op.subscription_id else None
    if not user_id and sub is not None:
        user_id = sub.v2_user_id
    if not user_id or op.kind == "partner_ban":
        return {"available": False, "no_user": True, "reason": "این عملیات کاربر مشخصی روی پنل ندارد"}
    product = _subs().load_product(db, op.product_id)
    # The same identity _ensure_owner checks before any update, delete or reset.
    if op.kind == "create":
        email = data.get("email")
    elif data.get("email_domain") or sub is not None:
        email = _panel_email(op, data, sub)
    else:
        email = None
    db.commit()
    with gateway_factory(product.panel) as gateway:
        user, state = _read_remote(gateway, user_id=int(user_id))
        if state == "missing":
            return {"available": True, "exists": False}
        if state != "found" or not isinstance(user, dict):
            return {"available": False, "reason": "وضعیت پنل قابل خواندن نیست"}
        if email is None or not _subs().owns_panel_user(user, email):
            return {"available": True, "exists": True, "owned": False}
        try:
            info = gateway.get_user_info(int(user_id))
        except Exception:
            info = user
    used = int(info.get("u") or 0) + int(info.get("d") or 0)
    expired_at = info.get("expired_at")
    return {
        "available": True,
        "exists": True,
        "owned": True,
        **({"refund_target_applied": _same_quota(user, data["new_cap"], data["new_expired_at"])}
           if op.kind == "decrease" and _deferred_refund(data) else {}),
        "transfer_enable_gb": dec_str(D(int(info.get("transfer_enable") or 0)) / D(1073741824)),
        "used_gb": dec_str(D(used) / D(1073741824)),
        "expired_jalali": format_jalali_ts(int(expired_at)) if expired_at is not None else None,
        "banned": bool(int(info.get("banned") or 0)),
        "plan_id": info.get("plan_id"),
    }


PANEL_UNREADABLE = "وضعیت کاربر روی پنل الان قابل خواندن نیست؛ تا خوانده نشود این تصمیم ممکن نیست."


def _panel_category(panel: dict) -> str:
    if not panel.get("available"):
        return "no_user" if panel.get("no_user") else "unknown"
    if not panel.get("exists"):
        return "missing"
    return "ours" if panel.get("owned") else "foreign"


def operation_decision_blocks(op: PanelOperation, panel: dict) -> dict[str, str]:
    """Decisions the current panel state makes unsafe, with the reason shown to the admin."""
    if op.status != "attention":
        return {}
    data = _loads(op)
    action = _retry_kind(op, data)
    where = _panel_category(panel)
    blocks: dict[str, str] = {}
    deferred = _deferred_refund(data)
    confirmed = deferred and bool(data.get("panel_applied"))
    if action in {"resend_reset", "resend_change"} and not confirmed:
        generate = op.kind == "create" and not data.get("v2_user_id")
        allowed = {"no_user"} if generate else ({"ours", "missing"} if op.kind == "delete" else {"ours"})
        if where not in allowed:
            blocks["retry"] = {
                "missing": "کاربر این عملیات روی پنل وجود ندارد؛ ارسال دوباره به پنل ممکن نیست.",
                "foreign": "کاربر این شناسه روی پنل متعلق به این اشتراک نیست؛ ارسال دوباره ممکن نیست.",
            }.get(where, PANEL_UNREADABLE)
    if deferred:
        if confirmed or (op.kind == "delete" and where == "missing") or (
            op.kind == "decrease" and panel.get("refund_target_applied") is True
        ):
            blocks["close"] = "نتیجه روی پنل تأیید شده است؛ برای ثبت اعتبار رزروشده «تلاش دوباره» را انتخاب کنید."
        elif where not in {"foreign", "missing"} and (where != "ours" or _possibly_sent(data)):
            blocks["close"] = (
                "نتیجهٔ درخواست قبلی قطعی نیست؛ برای بررسی نتیجه و جلوگیری از اختلاف مالی «تلاش دوباره» را انتخاب کنید."
                if where == "ours" else PANEL_UNREADABLE
            )
    elif op.kind == "delete" and action == "resend_change" and where != "foreign":
        # The refund for a delete was paid up front; closing would keep it next to a live local row.
        blocks["close"] = {
            "missing": "کاربر روی پنل حذف شده و مبلغ حذف برگشت خورده است؛ «تلاش دوباره» حذف را بدون "
                       "جابه‌جایی مالی کامل می‌کند و بستن، اشتراک را با موجودی ناسازگار می‌گذارد.",
            "ours": "مبلغ حذف برگشت خورده ولی کاربر هنوز روی پنل است؛ بستن، پنل و موجودی را ناسازگار "
                    "می‌گذارد. ارسال دوباره حذف را انتخاب کنید.",
        }.get(where, PANEL_UNREADABLE)
    return blocks


def resolve_attention(
    db: Session,
    op_id: int,
    *,
    decision: str,
    actor,
    note: str | None = None,
    accept_shortfall: bool = False,
    seen_updated_at: str | None = None,
    gateway_factory=None,
) -> dict:
    if decision not in {"close", "retry"}:
        raise AppError("تصمیم نامعتبر است")
    op = db.get(PanelOperation, op_id)
    if op is None:
        raise AppError("عملیات پیدا نشد", 404)
    if op.status != "attention":
        raise AppError("این عملیات در وضعیت بررسی نیست")
    if seen_updated_at is not None and seen_updated_at != _moment_text(op.updated_at):
        raise AppError(STALE_DECISION, 409)
    if not (note or "").strip():
        raise AppError("نوشتن توضیح برای تصمیم ادمین الزامی است")
    panel: dict = {}
    if _deferred_refund(_loads(op)) and gateway_factory is None:
        raise AppError(PANEL_UNREADABLE, 409)
    if gateway_factory is not None:
        # Re-read the panel now: the admin's view may predate a deletion or a change of owner.
        try:
            panel = operation_panel_state(db, op_id, gateway_factory)
        except AppError:
            panel = {"available": False}
        op = _reload(db, op_id)
        if seen_updated_at is not None and seen_updated_at != _moment_text(op.updated_at):
            raise AppError(STALE_DECISION, 409)
        blocked = operation_decision_blocks(op, panel).get(decision)
        if blocked:
            raise AppError(blocked, 409)
    data = _loads(op)
    gap_gb = D(data.get("shortfall_gb") or 0)
    gap_days = int(data.get("shortfall_days") or 0)
    if decision == "close" and (gap_gb > 0 or gap_days > 0) and not accept_shortfall:
        raise AppError(
            f"اختلاف مالی مانده است: {dec_str(gap_gb)} گیگ و {gap_days} روز. "
            "بستن فقط با تایید همین اختلاف ممکن است و موجودی را عوض نمی‌کند."
        )
    token = _claim(db, op_id, ("attention",))
    if token is None:
        raise AppError(BUSY, 503)
    handle = _lease_token.set(token)
    try:
        op = _reload(db, op_id)
        data = _loads(op)
        cleaned = (note or "").strip()
        if cleaned:
            data["resolution_note"] = cleaned[:500]
        data["resolved_by"] = int(actor.id)
        if decision == "close":
            if op.kind == "decrease" and _deferred_refund(data):
                _subs().lock_partner_row(db, op.partner_id, allow_banned=True)
                _revert_cap(db, op, data)
            data["resolution"] = "close"
            data["accepted_shortfall_gb"] = dec_str(gap_gb)
            data["accepted_shortfall_days"] = gap_days
            _mark(db, op, data, "closed", AppError(cleaned or "توسط ادمین با دیدن اختلاف بسته شد"))
            return operation_view(_reload(db, op_id))
        if data.get("foreign_target"):
            _save_owned(db, op.id, {}, release=True)
            raise AppError("کاربر این شناسه در پنل متعلق به این اشتراک نیست؛ ارسال دوباره ممکن نیست و فقط بستن مجاز است.")
        if op.kind == "reset_traffic" and not data.get("panel_applied"):
            # The admin checked the panel and saw no reset; the kept charge pays for the resend.
            data["reset_sent"] = False
            data.setdefault("raw_before", int(data.get("raw_after") or 0))
            data["auto_resends"] = 0
            data["resolution"] = "retry"
            _mark(db, op, data, "pending", AppError(cleaned or "ادمین ارسال دوباره ریست را تایید کرد"))
            return operation_view(_reload(db, op_id))
        if _retry_kind(op, data) == "resend_change":
            # An old snapshot permits an authorized retry, but cannot rule out the first send.
            deferred = _deferred_refund(data)
            target_observed = deferred and panel.get("refund_target_applied") is True
            if not target_observed:
                if deferred and data.get("panel_sent"):
                    data["refund_prior_send"] = True
                data["panel_sent"] = False
            data["auto_resends"] = 0
            data["resolution"] = "retry"
            _mark(db, op, data, "pending", AppError(cleaned or "ادمین ارسال دوباره به پنل را تایید کرد"))
            return operation_view(_reload(db, op_id))
        if "shortfall_gb" not in data and "shortfall_days" not in data:
            _save_owned(db, op.id, {}, release=True)
            raise AppError("تلاش دوباره فقط مانده اعتبار را پس می‌گیرد. بستن بدون اختلاف، موجودی را جابه‌جا نمی‌کند.")
        data["gb_back"] = data.get("shortfall_gb") or "0"
        data["days_back"] = int(data.get("shortfall_days") or 0)
        if _clawback(db, op, data, _subs()):
            data["shortfall_gb"] = "0"
            data["shortfall_days"] = 0
            data["resolution"] = "retry"
            _mark(db, op, data, "failed", AppError("مانده اعتبار برگشت"))
        else:
            data["resolution"] = "retry"
            _mark(db, op, data, "attention", AppError("مانده اعتبار هنوز کامل برنگشت"))
        return operation_view(_reload(db, op_id))
    finally:
        _lease_token.reset(handle)


def reconcile_pending(db: Session, gateway_factory) -> int:
    subs = _subs()
    ids = list(
        db.scalars(select(PanelOperation.id).where(PanelOperation.status == "pending").order_by(PanelOperation.id)).all()
    )
    done = 0
    for op_id in ids:
        op = db.get(PanelOperation, op_id)
        if op is None or op.status != "pending":
            continue
        gateway = None
        if op.kind == "partner_ban":
            from app.services.partner_ban import drive_partner_ban

            try:
                drive_partner_ban(db, op_id, gateway_factory)
                done += 1
            except AppError as exc:
                db.rollback()
                logger.warning("بازیابی عملیات %s: %s", op_id, _err(exc))
            continue
        try:
            product = subs.load_product(db, op.product_id)
        except AppError as exc:
            op.status = "attention"
            op.last_error = exc.message
            op.updated_at = utcnow()
            db.commit()
            logger.warning("بازیابی عملیات %s: %s", op_id, exc.message)
            continue
        try:
            gateway = gateway_factory(product.panel)
            drive_operation(db, op, gateway)
            op = db.get(PanelOperation, op_id)
            if op is not None and op.status == "completed":
                done += 1
        except AppError as exc:
            logger.warning("بازیابی عملیات %s: %s", op_id, _err(exc))
        except Exception as exc:
            logger.error("بازیابی عملیات %s ناموفق بود (%s)", op_id, type(exc).__name__)
        finally:
            closer = getattr(gateway, "close", None)
            if closer:
                closer()
    return done
