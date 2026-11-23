from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.errors import AppError, V2BoardError
from app.core.timeutil import (
    days_left,
    end_of_day_ts,
    extend_base_date,
    format_jalali_date,
    need_days,
    now_tehran,
    parse_jalali_to_date,
    ts_to_tehran_date,
    utcnow,
)
from app.models import Partner, PartnerProductCredit, Product, Subscription, WalletLedger
from app.services.billing import (
    D,
    bytes_to_gb,
    dec_str,
    floor_money,
    gb_to_bytes,
    qgb,
    quote_charge,
    refund_decrease,
    refund_delete,
)

logger = logging.getLogger("partner.billing")

USERNAME_RE = re.compile(r"^[a-z0-9_]{3,32}$")
MAX_GB = Decimal(1_000_000)
MAX_DAYS = 3650


@dataclass
class Actor:
    type: str
    id: int


def normalize_username(value: str) -> str:
    username = value.strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise AppError("نام کاربری فقط a-z، عدد و _ است و باید ۳ تا ۳۲ نویسه باشد")
    return username


def _has_row_locks(db: Session) -> bool:
    bind = db.get_bind()
    return bind is not None and bind.dialect.name in {"postgresql", "mysql"}


def _parse_end(value: str) -> date:
    try:
        return parse_jalali_to_date(value)
    except ValueError as exc:
        raise AppError("تاریخ شمسی نامعتبر است") from exc


def _check_cap(value) -> Decimal:
    cap = qgb(value)
    if cap < 1:
        raise AppError("حجم باید حداقل ۱ گیگ باشد")
    if cap > MAX_GB:
        raise AppError("سقف حجم بیش از حد بزرگ است")
    return cap


def _opt_int(value):
    if value is None or value == "":
        return None
    return int(value)


def load_product(db: Session, product_id: int) -> Product:
    product = db.scalar(
        select(Product).where(Product.id == product_id).options(joinedload(Product.panel))
    )
    if product is None:
        raise AppError("محصول پیدا نشد", 404)
    return product


def lock_partner_row(db: Session, partner_id: int, *, allow_banned: bool = False) -> Partner:
    query = select(Partner).where(Partner.id == partner_id).execution_options(populate_existing=True)
    if _has_row_locks(db):
        query = query.with_for_update()
    partner = db.scalar(query)
    if partner is None:
        raise AppError("همکار پیدا نشد", 404)
    if not allow_banned:
        from app.services.partner_ban import ensure_no_partner_ban

        ensure_no_partner_ban(db, partner_id)
    if partner.banned and not allow_banned:
        raise AppError("حساب همکار مسدود است", 403)
    return partner


def get_subscription(db: Session, sub_id: int, partner_id: int | None = None) -> Subscription:
    query = (
        select(Subscription)
        .where(Subscription.id == sub_id)
        .options(
            joinedload(Subscription.product).joinedload(Product.panel),
            joinedload(Subscription.partner),
        )
    )
    if partner_id is not None:
        query = query.where(Subscription.partner_id == partner_id)
    sub = db.execute(query).unique().scalar_one_or_none()
    if sub is None:
        raise AppError("اشتراک پیدا نشد", 404)
    return sub


def lock_subscription(db: Session, sub_id: int, partner_id: int | None = None) -> Subscription:
    from app.services.partner_ban import ensure_no_partner_ban

    # All mutations serialize on the partner before taking a subscription lock.
    owner_query = select(Subscription.partner_id).where(Subscription.id == sub_id)
    if partner_id is not None:
        owner_query = owner_query.where(Subscription.partner_id == partner_id)
    owner_id = db.scalar(owner_query)
    if owner_id is None:
        raise AppError("اشتراک پیدا نشد", 404)
    # Refresh eligibility under the parent lock, after checking ownership.
    lock_partner_row(db, owner_id, allow_banned=partner_id is None)
    if partner_id is None:
        ensure_no_partner_ban(db, owner_id)
    query = (
        select(Subscription)
        .where(Subscription.id == sub_id)
        .options(
            joinedload(Subscription.product).joinedload(Product.panel),
            joinedload(Subscription.partner),
        )
    )
    if partner_id is not None:
        query = query.where(Subscription.partner_id == partner_id)
    if _has_row_locks(db):
        if db.get_bind().dialect.name == "mysql":
            # MySQL has no FOR UPDATE OF. Lock only the subscription, avoiding
            # joined product/panel locks that would change the lock order.
            query = select(Subscription).where(Subscription.id == sub_id)
            if partner_id is not None:
                query = query.where(Subscription.partner_id == partner_id)
            query = query.with_for_update()
        else:
            query = query.with_for_update(of=Subscription)
    query = query.execution_options(populate_existing=True)
    sub = db.execute(query).unique().scalar_one_or_none()
    if sub is None:
        raise AppError("اشتراک پیدا نشد", 404)
    return sub


def lock_credit(db: Session, partner_id: int, product_id: int) -> PartnerProductCredit:
    query = select(PartnerProductCredit).where(
        PartnerProductCredit.partner_id == partner_id,
        PartnerProductCredit.product_id == product_id,
    )
    if db.get_bind().dialect.name == "mysql":
        query = query.execution_options(populate_existing=True)
    if _has_row_locks(db):
        query = query.with_for_update()
    row = db.scalar(query)
    if row is None:
        row = PartnerProductCredit(
            partner_id=partner_id,
            product_id=product_id,
            credit_gb=Decimal(0),
            credit_days=0,
        )
        db.add(row)
        db.flush()
    return row


def read_credit(db: Session, partner_id: int, product_id: int) -> tuple[Decimal, int]:
    row = db.scalar(
        select(PartnerProductCredit).where(
            PartnerProductCredit.partner_id == partner_id,
            PartnerProductCredit.product_id == product_id,
        )
    )
    if row is None:
        return Decimal(0), 0
    return D(row.credit_gb), int(row.credit_days)


def _ledger(
    db: Session,
    *,
    partner_id: int,
    asset: str,
    amount,
    product_id: int | None,
    subscription_id: int | None,
    reason: str,
    actor: Actor,
) -> None:
    if D(amount) == 0:
        return
    stored = Decimal(int(amount)) if asset in {"days", "toman"} else qgb(amount)
    db.add(
        WalletLedger(
            partner_id=partner_id,
            asset=asset,
            amount=stored,
            product_id=product_id,
            subscription_id=subscription_id,
            reason=reason,
            actor_type=actor.type,
            actor_id=actor.id,
        )
    )


def _take_charge(partner: Partner, credit: PartnerProductCredit, charge) -> None:
    if charge.toman > int(partner.balance_toman):
        raise AppError("موجودی تومان یا اعتبار این محصول کافی نیست")
    credit.credit_gb = qgb(D(credit.credit_gb) - charge.gb_from_credit)
    credit.credit_days = int(credit.credit_days) - int(charge.days_from_credit)
    partner.balance_toman = int(partner.balance_toman) - int(charge.toman)
    if D(credit.credit_gb) < 0 or credit.credit_days < 0 or partner.balance_toman < 0:
        raise AppError("موجودی منفی مجاز نیست")


def _charge_ledgers(db, partner_id, product_id, subscription_id, charge, reason: str, actor: Actor) -> None:
    _ledger(
        db,
        partner_id=partner_id,
        asset="gb",
        amount=-charge.gb_from_credit,
        product_id=product_id,
        subscription_id=subscription_id,
        reason=reason,
        actor=actor,
    )
    _ledger(
        db,
        partner_id=partner_id,
        asset="days",
        amount=-charge.days_from_credit,
        product_id=product_id,
        subscription_id=subscription_id,
        reason=reason,
        actor=actor,
    )
    _ledger(
        db,
        partner_id=partner_id,
        asset="toman",
        amount=-charge.toman,
        product_id=product_id,
        subscription_id=subscription_id,
        reason=reason,
        actor=actor,
    )


def _give_credit(db, credit, partner_id, product_id, subscription_id, gb, days, reason: str, actor: Actor) -> None:
    if D(gb) > 0:
        credit.credit_gb = qgb(D(credit.credit_gb) + D(gb))
        _ledger(
            db,
            partner_id=partner_id,
            asset="gb",
            amount=gb,
            product_id=product_id,
            subscription_id=subscription_id,
            reason=reason,
            actor=actor,
        )
    if int(days) > 0:
        credit.credit_days = int(credit.credit_days) + int(days)
        _ledger(
            db,
            partner_id=partner_id,
            asset="days",
            amount=days,
            product_id=product_id,
            subscription_id=subscription_id,
            reason=reason,
            actor=actor,
        )


def panel_identity(sub, product) -> tuple[str, int]:
    """Domain and plan frozen on the subscription. Product edits apply to new creates."""
    domain = getattr(sub, "email_domain", None) or product.email_domain
    plan = getattr(sub, "plan_id", None)
    if plan is None:
        plan = product.plan_id
    return str(domain), int(plan)


FOREIGN_TARGET = "کاربر این شناسه در پنل دیگر متعلق به این اشتراک نیست؛ هیچ تغییری روی آن اعمال نشد."


def panel_email(sub, product) -> str:
    return f"{sub.username}@{panel_identity(sub, product)[0]}"


def owns_panel_user(user, email: str) -> bool:
    """Panel ids can be reused after a panel restore; the email is the identity we created."""
    return isinstance(user, dict) and str(user.get("email") or "").strip().lower() == email.strip().lower()


def build_update(
    sub: Subscription,
    product: Product,
    plan: dict,
    *,
    banned: bool,
    transfer_gb=None,
    expired_at: int | None = None,
    reset_traffic: bool = False,
    current: dict | None = None,
) -> dict:
    """`current` is the panel's getUserInfoById row; it carries data we do not own."""
    domain, plan_id = panel_identity(sub, product)
    payload = {
        "id": int(sub.v2_user_id),
        "email": f"{sub.username}@{domain}",
        "plan_id": plan_id,
        "banned": 1 if banned else 0,
        "is_admin": 0,
        "is_staff": 0,
        "transfer_enable": gb_to_bytes(sub.traffic_cap_gb if transfer_gb is None else transfer_gb),
        "expired_at": int(sub.expired_at if expired_at is None else expired_at),
        "device_limit": _opt_int(plan.get("device_limit")),
        "speed_limit": _opt_int(plan.get("speed_limit")),
    }
    if reset_traffic:
        payload["u"] = 0
        payload["d"] = 0
    # Upstream clears invite_user_id on every update unless the inviter's email is sent.
    inviter = (current or {}).get("invite_user")
    if isinstance(inviter, dict) and isinstance(inviter.get("email"), str) and inviter["email"]:
        payload["invite_user_email"] = inviter["email"]
    return payload


def used_gb_of(info: dict) -> Decimal:
    return bytes_to_gb(int(info.get("u") or 0) + int(info.get("d") or 0))


def _fetch_new_user(gateway, email: str) -> dict:
    last: Exception | None = None
    for attempt in range(3):
        try:
            return gateway.fetch_by_email(email)
        except V2BoardError as exc:
            last = exc
            if attempt < 2:
                time.sleep(0.2)
    raise AppError(f"کاربر ساخته شد ولی در پنل پیدا نشد: {last}", 502)


def _ensure_panel(product: Product) -> None:
    if product.panel.status == "disabled":
        raise AppError("پنل این محصول غیرفعال است")


def charge_view(charge) -> dict:
    return {
        "need_gb": dec_str(charge.need_gb),
        "need_days": charge.need_days,
        "gb_from_credit": dec_str(charge.gb_from_credit),
        "days_from_credit": charge.days_from_credit,
        "gb_paid": dec_str(charge.gb_paid),
        "days_paid": charge.days_paid,
        "toman": charge.toman,
    }


def create_subscription(
    db: Session,
    *,
    partner_id: int,
    product_id: int,
    username: str,
    traffic_cap_gb,
    end_date: str,
    note: str | None,
    actor: Actor,
    gateway,
    now: datetime | None = None,
):
    from app.services.money_sync import perform_create

    return perform_create(
        db,
        partner_id=partner_id,
        product_id=product_id,
        username=username,
        traffic_cap_gb=traffic_cap_gb,
        end_date=end_date,
        note=note,
        actor=actor,
        gateway=gateway,
        now=now,
    )


def _resolve_increase(sub: Subscription, traffic_cap_gb, end_date: str | None, now: datetime | None):
    current = now_tehran(now)
    old_cap = qgb(sub.traffic_cap_gb)
    new_cap = old_cap
    need_gb = Decimal(0)
    if traffic_cap_gb is not None:
        new_cap = _check_cap(traffic_cap_gb)
        if new_cap < old_cap:
            raise AppError("برای کم کردن حجم از کاهش استفاده کنید")
        need_gb = qgb(new_cap - old_cap)
    base = extend_base_date(sub.expired_at, current)
    new_exp = int(sub.expired_at)
    days = 0
    end = None
    if end_date:
        end = _parse_end(end_date)
        if end <= base:
            raise AppError("تاریخ پایان باید بعد از مبدأ باشد")
        days = need_days(end, base)
        if days <= 0 or days > MAX_DAYS:
            raise AppError("بازه زمانی نامعتبر است")
        new_exp = end_of_day_ts(end)
    if need_gb == 0 and days == 0:
        raise AppError("تغییری برای اعمال نیست")
    return new_cap, new_exp, need_gb, days, base, end


def increase_subscription(
    db: Session,
    *,
    sub_id: int,
    partner_id: int | None,
    traffic_cap_gb,
    end_date: str | None,
    actor: Actor,
    gateway,
    now: datetime | None = None,
):
    from app.services.money_sync import perform_increase

    return perform_increase(
        db,
        sub_id=sub_id,
        partner_id=partner_id,
        traffic_cap_gb=traffic_cap_gb,
        end_date=end_date,
        actor=actor,
        gateway=gateway,
        now=now,
    )


def _resolve_decrease(sub: Subscription, traffic_cap_gb, end_date: str | None, now: datetime | None, used: Decimal | None):
    current = now_tehran(now)
    now_ts = int(current.timestamp())
    old_cap = qgb(sub.traffic_cap_gb)
    new_cap = old_cap
    if traffic_cap_gb is not None:
        new_cap = _check_cap(traffic_cap_gb)
        if new_cap > old_cap:
            raise AppError("برای زیاد کردن حجم از افزایش استفاده کنید")
        if new_cap < old_cap:
            if used is None:
                raise AppError("مصرف فعلی از پنل خوانده نشد", 502)
            if used > new_cap:
                raise AppError("مصرف فعلی بیشتر از سقف جدید است")
    new_exp = int(sub.expired_at)
    if end_date:
        if int(sub.expired_at) <= now_ts:
            raise AppError("اشتراک منقضی شده است؛ برای زمان از تمدید استفاده کنید")
        end = _parse_end(end_date)
        if end <= current.date() or end >= ts_to_tehran_date(int(sub.expired_at)):
            raise AppError("تاریخ جدید باید زودتر از انقضای فعلی و بعد از امروز باشد")
        new_exp = end_of_day_ts(end)
    if new_cap == old_cap and new_exp == int(sub.expired_at):
        raise AppError("تغییری برای اعمال نیست")
    gb_back, days_back = refund_decrease(
        old_cap,
        new_cap,
        days_left(int(sub.expired_at), now_ts),
        days_left(new_exp, now_ts),
    )
    if gb_back == 0 and days_back == 0:
        raise AppError("تغییری برای اعمال نیست")
    return new_cap, new_exp, gb_back, days_back


def decrease_subscription(
    db: Session,
    *,
    sub_id: int,
    partner_id: int | None,
    traffic_cap_gb,
    end_date: str | None,
    actor: Actor,
    gateway,
    now: datetime | None = None,
):
    from app.services.money_sync import perform_decrease

    return perform_decrease(
        db,
        sub_id=sub_id,
        partner_id=partner_id,
        traffic_cap_gb=traffic_cap_gb,
        end_date=end_date,
        actor=actor,
        gateway=gateway,
        now=now,
    )


def delete_subscription(
    db: Session,
    *,
    sub_id: int,
    partner_id: int | None,
    actor: Actor,
    gateway,
    now: datetime | None = None,
):
    from app.services.money_sync import perform_delete

    return perform_delete(
        db,
        sub_id=sub_id,
        partner_id=partner_id,
        actor=actor,
        gateway=gateway,
        now=now,
    )


def reset_link(db: Session, *, sub_id: int, partner_id: int | None, gateway):
    sub = lock_subscription(db, sub_id, partner_id) if partner_id is not None else get_subscription(db, sub_id)
    _ensure_panel(sub.product)
    email = panel_email(sub, sub.product)
    try:
        if not owns_panel_user(gateway.fetch_by_id(sub.v2_user_id), email):
            raise AppError(FOREIGN_TARGET, 409)
        gateway.reset_secret(sub.v2_user_id)
        fetched = gateway.fetch_by_id(sub.v2_user_id)
    except Exception as exc:
        if isinstance(exc, AppError):
            raise
        raise AppError(f"ریست لینک ناموفق بود: {exc}", 502) from exc
    sub.updated_at = utcnow()
    return {
        "subscribe_url": fetched.get("subscribe_url") or "",
        "subscribe_url_fallback": bool(fetched.get("subscribe_url_fallback")),
    }


def reset_traffic(
    db: Session,
    *,
    sub_id: int,
    partner_id: int | None,
    actor: Actor,
    gateway,
):
    from app.services.money_sync import perform_reset

    return perform_reset(
        db,
        sub_id=sub_id,
        partner_id=partner_id,
        actor=actor,
        gateway=gateway,
    )


def set_subscription_ban(
    db: Session,
    *,
    sub_id: int,
    partner_id: int | None,
    banned: bool,
    actor: Actor,
    gateway,
):
    sub = lock_subscription(db, sub_id, partner_id)
    if not banned and sub.blocked_by_partner_ban and actor.type == "partner":
        raise AppError("این مسدودی به‌خاطر مسدود بودن همکار است و فقط با رفع مسدودیت همکار باز می‌شود")
    _ensure_panel(sub.product)
    _domain, plan_id = panel_identity(sub, sub.product)
    plan = gateway.get_plan(plan_id)
    current = gateway.get_user_info(sub.v2_user_id)
    if not owns_panel_user(current, panel_email(sub, sub.product)):
        raise AppError(FOREIGN_TARGET, 409)
    try:
        gateway.update_user(build_update(sub, sub.product, plan, banned=banned, current=current))
    except Exception as exc:
        if isinstance(exc, AppError):
            raise
        raise AppError(f"تغییر وضعیت مسدودی روی پنل ناموفق بود: {exc}", 502) from exc
    sub.banned = banned
    if not banned:
        sub.blocked_by_partner_ban = False
    sub.updated_at = utcnow()
    return sub


def _ban_holder(sub: Subscription):
    domain, plan_id = panel_identity(sub, sub.product)
    holder = type("Sub", (), {})()
    holder.username = sub.username
    holder.v2_user_id = int(sub.v2_user_id)
    holder.traffic_cap_gb = sub.traffic_cap_gb
    holder.expired_at = int(sub.expired_at)
    holder.email_domain = domain
    holder.plan_id = plan_id
    view = type("Product", (), {"email_domain": domain, "plan_id": plan_id})()
    return holder, view, plan_id, bool(sub.banned)


def set_partner_ban(db: Session, *, partner_id: int, banned: bool, actor: Actor, gateway_factory) -> Partner:
    from app.services.partner_ban import perform_partner_ban

    return perform_partner_ban(
        db, partner_id=partner_id, banned=banned, actor=actor, gateway_factory=gateway_factory
    )


def update_note(db: Session, *, sub_id: int, partner_id: int | None, note: str | None) -> Subscription:
    sub = lock_subscription(db, sub_id, partner_id) if partner_id is not None else get_subscription(db, sub_id)
    cleaned = (note or "").strip()
    if len(cleaned) > 2000:
        raise AppError("یادداشت طولانی است")
    sub.note = cleaned or None
    sub.updated_at = utcnow()
    return sub


def admin_adjust(
    db: Session,
    *,
    partner_id: int,
    asset: str,
    amount,
    product_id: int | None,
    actor: Actor,
) -> Partner:
    partner = lock_partner_row(db, partner_id, allow_banned=True)
    delta = D(amount)
    if delta == 0:
        raise AppError("مقدار تعدیل نمی‌تواند صفر باشد")
    if abs(delta) > Decimal("1000000000"):
        raise AppError("مقدار تعدیل بیش از حد بزرگ است")
    if asset == "toman":
        if delta != delta.to_integral_value():
            raise AppError("مبلغ تومان باید عدد صحیح باشد")
        updated = int(partner.balance_toman) + int(delta)
        if updated < 0:
            raise AppError("موجودی تومان منفی نمی‌شود")
        partner.balance_toman = updated
        _ledger(
            db,
            partner_id=partner.id,
            asset="toman",
            amount=int(delta),
            product_id=None,
            subscription_id=None,
            reason="admin_adjust",
            actor=actor,
        )
        return partner
    if asset not in {"gb", "days"}:
        raise AppError("دارایی نامعتبر است")
    if not product_id:
        raise AppError("برای اعتبار گیگ یا روز باید محصول انتخاب شود")
    load_product(db, product_id)
    credit = lock_credit(db, partner.id, product_id)
    if asset == "gb":
        updated_gb = qgb(D(credit.credit_gb) + delta)
        if updated_gb < 0:
            raise AppError("اعتبار گیگ منفی نمی‌شود")
        credit.credit_gb = updated_gb
        stored = delta
    else:
        if delta != delta.to_integral_value():
            raise AppError("اعتبار روز باید عدد صحیح باشد")
        updated_days = int(credit.credit_days) + int(delta)
        if updated_days < 0:
            raise AppError("اعتبار روز منفی نمی‌شود")
        credit.credit_days = updated_days
        stored = int(delta)
    _ledger(
        db,
        partner_id=partner.id,
        asset=asset,
        amount=stored,
        product_id=product_id,
        subscription_id=None,
        reason="admin_adjust",
        actor=actor,
    )
    return partner


def _affordable(toman: int, balance: int) -> tuple[bool, int]:
    shortage = max(0, int(toman) - int(balance))
    return shortage == 0, shortage


def quote_operation(
    db: Session,
    *,
    partner_id: int,
    action: str,
    product_id: int | None,
    subscription_id: int | None,
    traffic_cap_gb,
    end_date: str | None,
    gateway,
    now: datetime | None = None,
) -> dict:
    current = now_tehran(now)
    partner = db.get(Partner, partner_id)
    if partner is None:
        raise AppError("همکار پیدا نشد", 404)
    message = None
    gb_refund = Decimal(0)
    days_refund = 0
    used = None
    base = current.date()
    end = None
    charge = quote_charge(0, 0, 0, 0, 0, 0)
    product: Product
    if action == "create":
        if not product_id:
            raise AppError("محصول را انتخاب کنید")
        product = load_product(db, product_id)
        if not product.active:
            raise AppError("این محصول خاموش است و ساخت اشتراک جدید ممکن نیست")
        if traffic_cap_gb is None or not end_date:
            raise AppError("حجم و تاریخ پایان لازم است")
        cap = _check_cap(traffic_cap_gb)
        end = _parse_end(end_date)
        if end <= base:
            raise AppError("تاریخ پایان باید بعد از امروز باشد")
        days = need_days(end, base)
        credit_gb, credit_days = read_credit(db, partner.id, product.id)
        charge = quote_charge(cap, days, credit_gb, credit_days, product.price_per_gb, product.price_per_day)
    else:
        if not subscription_id:
            raise AppError("اشتراک مشخص نیست")
        sub = get_subscription(db, subscription_id, partner.id)
        product = sub.product
        credit_gb, credit_days = read_credit(db, partner.id, product.id)
        if action == "increase":
            _new_cap, _new_exp, need_gb, days, base, end = _resolve_increase(sub, traffic_cap_gb, end_date, current)
            charge = quote_charge(
                need_gb, days, credit_gb, credit_days, product.price_per_gb, product.price_per_day
            )
        elif action == "decrease":
            live_used = None
            if traffic_cap_gb is not None and qgb(traffic_cap_gb) < qgb(sub.traffic_cap_gb):
                if gateway is None:
                    raise AppError("اتصال پنل برای خواندن مصرف لازم است", 502)
                live_used = used_gb_of(gateway.get_user_info(sub.v2_user_id))
                used = live_used
            try:
                _cap, _exp, gb_refund, days_refund = _resolve_decrease(
                    sub, traffic_cap_gb, end_date, current, live_used
                )
            except AppError as exc:
                if exc.message == "مصرف فعلی بیشتر از سقف جدید است":
                    message = exc.message
                    gb_refund = Decimal(0)
                    days_refund = 0
                else:
                    raise
            if end_date:
                end = _parse_end(end_date)
            base = ts_to_tehran_date(int(sub.expired_at))
        elif action == "delete":
            if gateway is None:
                raise AppError("اتصال پنل برای خواندن مصرف لازم است", 502)
            info = gateway.get_user_info(sub.v2_user_id)
            used = used_gb_of(info)
            left = days_left(int(sub.expired_at), int(current.timestamp()))
            gb_refund, days_refund = refund_delete(sub.traffic_cap_gb, used, left)
            base = current.date()
            end = ts_to_tehran_date(int(sub.expired_at))
        elif action == "reset_traffic":
            if gateway is None:
                raise AppError("اتصال پنل برای خواندن مصرف لازم است", 502)
            info = gateway.get_user_info(sub.v2_user_id)
            raw_used = int(info.get("u") or 0) + int(info.get("d") or 0)
            used = bytes_to_gb(raw_used)
            if raw_used <= 0:
                message = "مصرفی برای ریست وجود ندارد"
                cost = 0
            else:
                cost = floor_money(used, product.price_per_gb)
            charge = quote_charge(used or 0, 0, 0, 0, product.price_per_gb, product.price_per_day)
            charge = type(charge)(
                need_gb=qgb(used or 0),
                need_days=0,
                gb_from_credit=Decimal(0),
                days_from_credit=0,
                gb_paid=qgb(used or 0),
                days_paid=0,
                toman=cost,
            )
        else:
            raise AppError("عملیات نامعتبر است")
    affordable, shortage = _affordable(charge.toman, int(partner.balance_toman))
    if message:
        affordable = False
    if action in {"decrease", "delete"}:
        affordable = message is None
        shortage = 0
    return {
        "action": action,
        "affordable": affordable,
        "shortage_toman": shortage,
        "message": message,
        "base_jalali": format_jalali_date(base),
        "end_jalali": format_jalali_date(end) if end else None,
        **charge_view(charge),
        "gb_refund": dec_str(gb_refund),
        "days_refund": days_refund,
        "used_gb": dec_str(used) if used is not None else None,
        "balance_toman": int(partner.balance_toman),
        "credit_gb": dec_str(credit_gb),
        "credit_days": credit_days,
        "price_per_gb": int(product.price_per_gb),
        "price_per_day": int(product.price_per_day),
        "product_name": product.name,
    }
