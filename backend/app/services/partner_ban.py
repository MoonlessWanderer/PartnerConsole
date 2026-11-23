"""Durable, forward-only partner ban batches using the existing operation journal.

Keep login disabled until every unban has completed. A failed or ambiguous
remote call stays pending; retry repeats only an idempotent target update.
No wallet changes or compensating remote calls are made by this service.
"""

import json
import logging

from sqlalchemy import select

from app.core.errors import AppError
from app.core.timeutil import utcnow
from app.models import Panel, PanelOperation, Partner, Subscription
from app.services import money_sync as sync

logger = logging.getLogger("partner.billing")


def pending_batch(db, partner_id):
    return db.scalar(select(PanelOperation).where(
        PanelOperation.partner_id == partner_id,
        PanelOperation.kind == "partner_ban",
        PanelOperation.status == "pending",
    ))


def ensure_no_partner_ban(db, partner_id):
    if pending_batch(db, partner_id) is not None:
        raise AppError("تغییر مسدودی همکار ناتمام است؛ ابتدا همان عملیات را ادامه دهید", 409)


def perform_partner_ban(db, *, partner_id, banned, actor, gateway_factory):
    from app.services import subscriptions as subs

    partner = subs.lock_partner_row(db, partner_id, allow_banned=True)
    pending = pending_batch(db, partner_id)
    if pending is not None:
        if bool(json.loads(pending.payload)["banned"]) != banned:
            raise AppError("ابتدا عملیات ناتمام مسدودی همکار را تکمیل کنید", 409)
        op_id = pending.id
        sync._release_lock(db)
        return drive_partner_ban(db, op_id, gateway_factory)
    # A money operation already in flight must settle before taking snapshots.
    if db.scalar(select(PanelOperation.id).where(
        PanelOperation.partner_id == partner_id,
        PanelOperation.status.in_(("pending", "attention")),
    ).limit(1)) is not None:
        raise AppError("ابتدا عملیات ناتمام همکار را تعیین تکلیف کنید", 409)
    rows = db.scalars(select(Subscription).where(
        Subscription.partner_id == partner_id
    ).order_by(Subscription.id)).all()
    targets = []
    for sub in rows:
        if (banned and sub.banned) or (not banned and not sub.blocked_by_partner_ban):
            continue
        subs._ensure_panel(sub.product)
        holder, view, plan_id, _previous = subs._ban_holder(sub)
        targets.append(dict(
            sub_id=sub.id, panel_id=sub.product.panel_id,
            username=holder.username, v2_user_id=holder.v2_user_id,
            traffic_cap_gb=str(holder.traffic_cap_gb), expired_at=holder.expired_at,
            email_domain=view.email_domain, plan_id=plan_id, done=False,
        ))
    if not targets:
        partner.banned = banned
        partner.updated_at = utcnow()
        db.commit()
        return partner
    op = PanelOperation(
        partner_id=partner_id, product_id=rows[0].product_id,
        subscription_id=None, kind="partner_ban", status="pending",
        payload=json.dumps(dict(banned=banned, targets=targets,
                                actor_type=actor.type, actor_id=actor.id), ensure_ascii=False),
    )
    db.add(op)
    partner.banned = True
    partner.updated_at = utcnow()
    db.commit()
    op_id = op.id
    sync._release_lock(db)
    return drive_partner_ban(db, op_id, gateway_factory)


def drive_partner_ban(db, op_id, gateway_factory):
    from types import SimpleNamespace
    from app.services import subscriptions as subs

    token = sync._claim(db, op_id)
    if token is None:
        db.rollback()
        raise AppError(sync.BUSY, 503)
    handle = sync._lease_token.set(token)
    cache = {}
    try:
        op = sync._reload(db, op_id)
        partner_id = op.partner_id
        data = json.loads(op.payload)
        banned = bool(data["banned"])
        sync._release_lock(db)
        for target in data["targets"]:
            if target["done"]:
                continue
            panel_id = target["panel_id"]
            if panel_id not in cache:
                panel = db.get(Panel, panel_id)
                if panel is None or panel.status == "disabled":
                    raise AppError("پنل عملیات مسدودی در دسترس نیست", 503)
                # Detach a complete snapshot: the factory may perform I/O too.
                db.expunge(panel)
                sync._release_lock(db)
                cache[panel_id] = gateway_factory(panel)
            gateway = cache[panel_id]
            sync._save_owned(db, op_id, {})
            plan = gateway.get_plan(target["plan_id"])
            current = gateway.get_user_info(int(target["v2_user_id"]))
            sync._save_owned(db, op_id, {})
            if not subs.owns_panel_user(current, f"{target['username']}@{target['email_domain']}"):
                target["foreign"] = True
                logger.error("مسدودی همکار %s: کاربر پنل اشتراک %s متعلق به آن نیست و تغییر نکرد",
                             partner_id, target["sub_id"])
            # After a crash or failed checkpoint the earlier update may have landed; do not resend it.
            elif not (isinstance(current, dict) and current.get("banned") is not None
                      and bool(int(current["banned"])) == banned):
                payload = subs.build_update(
                    SimpleNamespace(**target), SimpleNamespace(**target), plan, banned=banned, current=current
                )
                gateway.update_user(payload)
            # The checkpoint and local flags commit together, fenced by the lease.
            sub = db.get(Subscription, target["sub_id"])
            if sub is None:
                raise AppError("اشتراک عملیات مسدودی پیدا نشد", 409)
            sub.banned = banned
            sub.blocked_by_partner_ban = banned
            sub.updated_at = utcnow()
            target["done"] = True
            sync._save_owned(db, op_id, {"payload": json.dumps(data, ensure_ascii=False)})
        partner = subs.lock_partner_row(db, partner_id, allow_banned=True)
        partner.banned = banned
        partner.updated_at = utcnow()
        sync._save_owned(db, op_id, {"status": "completed", "last_error": None})
        return partner
    except sync.LostLease as exc:
        db.rollback()
        raise AppError(sync.BUSY, 503) from exc
    except Exception as exc:
        sync._keep_for_recovery(db, op_id, exc)
    finally:
        sync._lease_token.reset(handle)
        for gateway in cache.values():
            try:
                closer = getattr(gateway, "close", None)
                if closer:
                    closer()
            except Exception as exc:
                logger.error("بستن اتصال پنل مسدودی ناموفق بود (%s)", type(exc).__name__)
