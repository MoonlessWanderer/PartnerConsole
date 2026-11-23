from __future__ import annotations

from datetime import datetime
from app.core.errors import AppError, public_error

from app.core.timeutil import (
    days_left,
    format_datetime_jalali,
    gregorian_to_jalali,
    now_tehran,
    ts_to_tehran_date,
)
from app.models import Product, Subscription, WalletLedger
from app.services.billing import bytes_to_gb, dec_str


def subscription_status(sub: Subscription, now_ts: int) -> str:
    if sub.banned and sub.blocked_by_partner_ban:
        return "banned_partner"
    if sub.banned:
        return "banned"
    if int(sub.expired_at) <= now_ts:
        return "expired"
    return "active"


def present_subscription(sub: Subscription, now: datetime | None = None) -> dict:
    current = now_tehran(now)
    now_ts = int(current.timestamp())
    end = ts_to_tehran_date(int(sub.expired_at))
    return {
        "id": sub.id,
        "partner_id": sub.partner_id,
        "partner_username": sub.partner.username if sub.partner else None,
        "product_id": sub.product_id,
        "product_name": sub.product.name if sub.product else None,
        "panel_name": sub.product.panel.name if sub.product and sub.product.panel else None,
        "username": sub.username,
        "v2_user_id": sub.v2_user_id,
        "note": sub.note,
        "traffic_cap_gb": dec_str(sub.traffic_cap_gb),
        "expired_at": int(sub.expired_at),
        "expired_jalali": gregorian_to_jalali(end).iso(),
        "days_left": days_left(int(sub.expired_at), now_ts),
        "banned": bool(sub.banned),
        "blocked_by_partner_ban": bool(sub.blocked_by_partner_ban),
        "status": subscription_status(sub, now_ts),
        "created_jalali": format_datetime_jalali(sub.created_at),
    }


def present_live(sub: Subscription, gateway, now: datetime | None = None) -> dict:
    data = present_subscription(sub, now)
    from app.services.subscriptions import FOREIGN_TARGET, owns_panel_user, panel_email

    try:
        info = gateway.get_user_info(sub.v2_user_id)
        fetched = gateway.fetch_by_id(sub.v2_user_id)
        if not owns_panel_user(fetched, panel_email(sub, sub.product)):
            raise AppError(FOREIGN_TARGET, 409)
    except Exception as exc:
        data.update(
            {
                "used_gb": None,
                "upload_gb": None,
                "download_gb": None,
                "subscribe_url": None,
                "subscribe_url_fallback": False,
                "live_error": public_error(exc),
            }
        )
        return data
    data["used_gb"] = dec_str(bytes_to_gb(int(info.get("u") or 0) + int(info.get("d") or 0)))
    data["upload_gb"] = dec_str(bytes_to_gb(int(info.get("u") or 0)))
    data["download_gb"] = dec_str(bytes_to_gb(int(info.get("d") or 0)))
    data["subscribe_url"] = fetched.get("subscribe_url") or ""
    data["subscribe_url_fallback"] = bool(fetched.get("subscribe_url_fallback"))
    data["live_error"] = None
    return data


def present_ledger(row: WalletLedger) -> dict:
    return {
        "id": row.id,
        "partner_id": row.partner_id,
        "partner_username": row.partner.username if row.partner else None,
        "asset": row.asset,
        "amount": dec_str(row.amount),
        "product_id": row.product_id,
        "product_name": row.product.name if row.product else None,
        "subscription_id": row.subscription_id,
        "reason": row.reason,
        "actor_type": row.actor_type,
        "actor_id": row.actor_id,
        "created_jalali": format_datetime_jalali(row.created_at),
    }


def present_product(product: Product) -> dict:
    return {
        "id": product.id,
        "panel_id": product.panel_id,
        "panel_name": product.panel.name if product.panel else None,
        "plan_id": product.plan_id,
        "name": product.name,
        "email_domain": product.email_domain,
        "price_per_gb": product.price_per_gb,
        "price_per_day": product.price_per_day,
        "active": bool(product.active),
    }


def present_panel(panel) -> dict:
    return {
        "id": panel.id,
        "name": panel.name,
        "base_url": panel.base_url,
        "secure_path": panel.secure_path,
        "admin_email": panel.admin_email,
        "status": panel.status,
        "last_ok_at": panel.last_ok_at.isoformat() if panel.last_ok_at else None,
        "last_ok_jalali": format_datetime_jalali(panel.last_ok_at),
    }
