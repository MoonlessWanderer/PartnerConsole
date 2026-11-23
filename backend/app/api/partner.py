from contextlib import nullcontext

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import current_partner
from app.core.deps import _bearer, get_db
from app.core.errors import AppError, public_error
from app.core.search import contains_text
from app.core.security import (
    check_password,
    clear_login_failures,
    create_token,
    decode_token,
    ensure_login_allowed,
    login_key,
    revoke_token,
)
from app.models import Partner, PartnerProductCredit, Product, Subscription, WalletLedger
from app.schemas.dto import ChangeSubIn, ConfirmationIn, CreateSubIn, LoginIn, NoteIn, QuoteIn
from app.services import consent
from app.services.money_sync import drive_operation
from app.services.billing import dec_str, qgb
from app.services.panels import client_for_panel
from app.services.present import present_ledger, present_live, present_product, present_subscription
from app.services.subscriptions import (
    Actor,
    charge_view,
    create_subscription,
    decrease_subscription,
    delete_subscription,
    get_subscription,
    increase_subscription,
    load_product,
    lock_partner_row,
    normalize_username,
    quote_operation,
    reset_link,
    reset_traffic,
    set_subscription_ban,
    update_note,
)

router = APIRouter(prefix="/partner", tags=["partner"])


def _actor(partner: Partner) -> Actor:
    return Actor("partner", partner.id)


def _catalog(db: Session, partner_id: int) -> list[dict]:
    products = (
        db.execute(select(Product).options(joinedload(Product.panel)).order_by(Product.id))
        .unique()
        .scalars()
        .all()
    )
    credits = {
        row.product_id: row
        for row in db.scalars(
            select(PartnerProductCredit).where(PartnerProductCredit.partner_id == partner_id)
        ).all()
    }
    owned = set(
        db.scalars(select(Subscription.product_id).where(Subscription.partner_id == partner_id)).all()
    )
    items = []
    for product in products:
        credit = credits.get(product.id)
        if not product.active and credit is None and product.id not in owned:
            continue
        items.append(
            {
                **present_product(product),
                "credit_gb": dec_str(credit.credit_gb) if credit else "0",
                "credit_days": int(credit.credit_days) if credit else 0,
            }
        )
    return items


@router.post("/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    key = login_key(request.client.host if request.client else "-", body.username, "partner")
    ensure_login_allowed(key, db)
    partner = db.scalar(select(Partner).where(Partner.username == body.username.strip()))
    if partner is not None:
        db.expunge(partner)
    db.commit()  # release SQLite before the expensive password hash
    if not check_password(body.password, partner.password_hash if partner else None):
        raise AppError("نام کاربری یا رمز نادرست است", 401)
    if partner is None or partner.banned:
        raise AppError("حساب شما مسدود است", 403)
    clear_login_failures(key, db)
    return {
        "access_token": create_token("partner", partner.id, partner.username),
        "token_type": "bearer",
        "username": partner.username,
        "role": "partner",
        "balance_toman": int(partner.balance_toman),
    }


@router.post("/auth/logout")
def logout(request: Request, db: Session = Depends(get_db), partner: Partner = Depends(current_partner)):
    # Serialize revocation with the final consent check and debit.
    lock_partner_row(db, partner.id, allow_banned=True)
    revoke_token(_bearer(request), "partner", db)
    return {"ok": True}


@router.get("/me")
def me(partner: Partner = Depends(current_partner)):
    return {
        "id": partner.id,
        "username": partner.username,
        "role": "partner",
        "balance_toman": int(partner.balance_toman),
        "banned": bool(partner.banned),
    }


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), partner: Partner = Depends(current_partner)):
    count = db.scalar(
        select(func.count()).select_from(Subscription).where(Subscription.partner_id == partner.id)
    ) or 0
    return {
        "username": partner.username,
        "balance_toman": int(partner.balance_toman),
        "subscription_count": count,
        "credits": _catalog(db, partner.id),
    }


@router.get("/products")
def products(db: Session = Depends(get_db), partner: Partner = Depends(current_partner)):
    return {"items": _catalog(db, partner.id)}


def _list_subs(db: Session, partner_id: int, q: str | None, limit: int, offset: int):
    query = (
        select(Subscription)
        .where(Subscription.partner_id == partner_id)
        .options(
            joinedload(Subscription.partner),
            joinedload(Subscription.product).joinedload(Product.panel),
        )
    )
    count_query = select(func.count()).select_from(Subscription).where(Subscription.partner_id == partner_id)
    if q:
        text = q.strip().lower()
        query = query.where(contains_text(Subscription.username, text))
        count_query = count_query.where(contains_text(Subscription.username, text))
    total = db.scalar(count_query) or 0
    rows = (
        db.execute(query.order_by(Subscription.id.desc()).limit(limit).offset(offset))
        .unique()
        .scalars()
        .all()
    )
    return {"items": [present_subscription(row) for row in rows], "total": total}


@router.get("/subscriptions")
def list_subscriptions(
    db: Session = Depends(get_db),
    partner: Partner = Depends(current_partner),
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return _list_subs(db, partner.id, q, limit, offset)


def _session(request: Request) -> dict:
    try:
        return decode_token(_bearer(request), "partner")
    except ValueError:
        raise AppError("نشست نامعتبر است", 401) from None


def _gateway_for_confirmation(db: Session, product: Product):
    existing = consent.existing(db)
    if existing is not None and existing.status == "completed":
        return nullcontext()
    if existing is None and consent.panel_version(product.panel) != db.info["financial_consent"]["panel"]:
        raise consent.conflict()
    return client_for_panel(product.panel)


@router.post("/subscriptions/quote")
def quote(body: QuoteIn, request: Request, db: Session = Depends(get_db),
          partner: Partner = Depends(current_partner)):
    sub = None
    if body.action == "create":
        if not body.product_id:
            raise AppError("محصول را انتخاب کنید")
        product = load_product(db, body.product_id)
    else:
        if not body.subscription_id:
            raise AppError("اشتراک مشخص نیست")
        sub = get_subscription(db, body.subscription_id, partner.id)
        product = sub.product
    needs_live = body.action in {"delete", "reset_traffic"} or (
        body.action == "decrease" and body.traffic_cap_gb is not None
        and sub is not None and qgb(body.traffic_cap_gb) < qgb(sub.traffic_cap_gb)
    )
    args = dict(partner_id=partner.id, action=body.action, product_id=body.product_id,
                subscription_id=body.subscription_id, traffic_cap_gb=body.traffic_cap_gb,
                end_date=body.end_date)
    if needs_live:
        with client_for_panel(product.panel) as gateway:
            result = quote_operation(db, gateway=gateway, **args)
    else:
        result = quote_operation(db, gateway=None, **args)
    if result["affordable"] and (body.action != "create" or body.username is not None):
        username = normalize_username(body.username) if body.action == "create" else None
        inputs = consent.parameters(product.id if sub is None else None, sub.id if sub else None,
                                    body.traffic_cap_gb, body.end_date, username, body.note)
        result["consent_token"] = consent.issue(_session(request), body.action, inputs, product, sub, result)
    return result


@router.post("/subscriptions")
def create(
    body: CreateSubIn,
    request: Request,
    db: Session = Depends(get_db),
    partner: Partner = Depends(current_partner),
):
    inputs = consent.parameters(body.product_id, None, body.traffic_cap_gb, body.end_date,
                                normalize_username(body.username), body.note)
    consent.authorize(db, body.consent_token, _session(request), "create", inputs)
    product = load_product(db, body.product_id)
    with _gateway_for_confirmation(db, product) as gateway:
        sub, charge, fetched = create_subscription(
            db,
            partner_id=partner.id,
            product_id=product.id,
            username=body.username,
            traffic_cap_gb=body.traffic_cap_gb,
            end_date=body.end_date,
            note=body.note,
            actor=_actor(partner),
            gateway=gateway,
        )
    payload = present_subscription(sub)
    payload["subscribe_url"] = fetched.get("subscribe_url") or ""
    payload["subscribe_url_fallback"] = bool(fetched.get("subscribe_url_fallback"))
    payload["charge"] = charge_view(charge)
    payload["balance_toman"] = int(partner.balance_toman)
    return payload


@router.get("/subscriptions/{sub_id}")
def detail(sub_id: int, db: Session = Depends(get_db), partner: Partner = Depends(current_partner)):
    sub = get_subscription(db, sub_id, partner.id)
    try:
        with client_for_panel(sub.product.panel) as gateway:
            data = present_live(sub, gateway)
    except AppError as exc:
        data = present_subscription(sub)
        data.update(
            used_gb=None,
            upload_gb=None,
            download_gb=None,
            subscribe_url=None,
            subscribe_url_fallback=False,
            live_error=public_error(exc),
        )
    data["balance_toman"] = int(partner.balance_toman)
    return data


@router.post("/subscriptions/{sub_id}/increase")
def increase(
    sub_id: int,
    body: ChangeSubIn,
    request: Request,
    db: Session = Depends(get_db),
    partner: Partner = Depends(current_partner),
):
    sub = get_subscription(db, sub_id, partner.id)
    inputs = consent.parameters(None, sub_id, body.traffic_cap_gb, body.end_date)
    consent.authorize(db, body.consent_token, _session(request), "increase", inputs)
    with _gateway_for_confirmation(db, sub.product) as gateway:
        updated, charge = increase_subscription(
            db,
            sub_id=sub_id,
            partner_id=partner.id,
            traffic_cap_gb=body.traffic_cap_gb,
            end_date=body.end_date,
            actor=_actor(partner),
            gateway=gateway,
        )
    payload = present_subscription(updated)
    payload["charge"] = charge_view(charge)
    payload["balance_toman"] = int(partner.balance_toman)
    return payload


@router.post("/subscriptions/{sub_id}/decrease")
def decrease(
    sub_id: int,
    body: ChangeSubIn,
    request: Request,
    db: Session = Depends(get_db),
    partner: Partner = Depends(current_partner),
):
    sub = get_subscription(db, sub_id, partner.id)
    inputs = consent.parameters(None, sub_id, body.traffic_cap_gb, body.end_date)
    consent.authorize(db, body.consent_token, _session(request), "decrease", inputs)
    with _gateway_for_confirmation(db, sub.product) as gateway:
        updated, gb_back, days_back = decrease_subscription(
            db,
            sub_id=sub_id,
            partner_id=partner.id,
            traffic_cap_gb=body.traffic_cap_gb,
            end_date=body.end_date,
            actor=_actor(partner),
            gateway=gateway,
        )
    payload = present_subscription(updated)
    payload["gb_refund"] = dec_str(gb_back)
    payload["days_refund"] = days_back
    payload["balance_toman"] = int(partner.balance_toman)
    return payload


@router.post("/subscriptions/{sub_id}/delete")
def remove(sub_id: int, request: Request, body: ConfirmationIn | None = None,
           db: Session = Depends(get_db), partner: Partner = Depends(current_partner)):
    if body is None or not body.consent_token:
        get_subscription(db, sub_id, partner.id)
        raise consent.conflict()
    consent.authorize(db, body.consent_token, _session(request), "delete",
                      consent.parameters(None, sub_id, None, None))
    lock_partner_row(db, partner.id)
    existing = consent.existing(db)
    if existing is not None and existing.status == "completed":
        return {**drive_operation(db, existing, None), "balance_toman": int(partner.balance_toman)}
    sub = get_subscription(db, sub_id, partner.id)
    with _gateway_for_confirmation(db, sub.product) as gateway:
        result = delete_subscription(
            db,
            sub_id=sub_id,
            partner_id=partner.id,
            actor=_actor(partner),
            gateway=gateway,
        )
    result["balance_toman"] = int(partner.balance_toman)
    return result


@router.post("/subscriptions/{sub_id}/reset-link")
def reset_sub_link(sub_id: int, db: Session = Depends(get_db), partner: Partner = Depends(current_partner)):
    sub = get_subscription(db, sub_id, partner.id)
    with client_for_panel(sub.product.panel) as gateway:
        return reset_link(db, sub_id=sub_id, partner_id=partner.id, gateway=gateway)


@router.post("/subscriptions/{sub_id}/reset-traffic")
def reset_sub_traffic(sub_id: int, request: Request, body: ConfirmationIn | None = None,
                      db: Session = Depends(get_db), partner: Partner = Depends(current_partner)):
    sub = get_subscription(db, sub_id, partner.id)
    consent.authorize(db, body.consent_token if body else "", _session(request), "reset_traffic",
                      consent.parameters(None, sub_id, None, None))
    with _gateway_for_confirmation(db, sub.product) as gateway:
        return reset_traffic(
            db,
            sub_id=sub_id,
            partner_id=partner.id,
            actor=_actor(partner),
            gateway=gateway,
        )


@router.post("/subscriptions/{sub_id}/ban")
def ban(sub_id: int, db: Session = Depends(get_db), partner: Partner = Depends(current_partner)):
    sub = get_subscription(db, sub_id, partner.id)
    with client_for_panel(sub.product.panel) as gateway:
        updated = set_subscription_ban(
            db,
            sub_id=sub_id,
            partner_id=partner.id,
            banned=True,
            actor=_actor(partner),
            gateway=gateway,
        )
    return present_subscription(updated)


@router.post("/subscriptions/{sub_id}/unban")
def unban(sub_id: int, db: Session = Depends(get_db), partner: Partner = Depends(current_partner)):
    sub = get_subscription(db, sub_id, partner.id)
    with client_for_panel(sub.product.panel) as gateway:
        updated = set_subscription_ban(
            db,
            sub_id=sub_id,
            partner_id=partner.id,
            banned=False,
            actor=_actor(partner),
            gateway=gateway,
        )
    return present_subscription(updated)


@router.post("/subscriptions/{sub_id}/note")
def note(
    sub_id: int,
    body: NoteIn,
    db: Session = Depends(get_db),
    partner: Partner = Depends(current_partner),
):
    sub = update_note(db, sub_id=sub_id, partner_id=partner.id, note=body.note)
    return present_subscription(sub)


@router.get("/ledger")
def ledger(
    db: Session = Depends(get_db),
    partner: Partner = Depends(current_partner),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    query = (
        select(WalletLedger)
        .where(WalletLedger.partner_id == partner.id)
        .options(joinedload(WalletLedger.partner), joinedload(WalletLedger.product))
    )
    total = db.scalar(
        select(func.count()).select_from(WalletLedger).where(WalletLedger.partner_id == partner.id)
    ) or 0
    rows = db.execute(query.order_by(WalletLedger.id.desc()).limit(limit).offset(offset)).unique().scalars().all()
    return {"items": [present_ledger(row) for row in rows], "total": total}
