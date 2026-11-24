import re

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import current_admin
from app.core.crypto import encrypt_secret
from app.core.deps import get_db
from app.core.errors import AppError, public_error
from app.core.search import contains_text
from app.core.security import (
    check_password,
    clear_login_failures,
    create_token,
    ensure_login_allowed,
    hash_password,
    login_key,
    revoke_token,
)
from app.core.timeutil import utcnow
from app.models import AdminUser, Panel, Partner, PartnerProductCredit, Product, Subscription, WalletLedger
from app.schemas.dto import (
    AdjustIn,
    LoginIn,
    NoteIn,
    PanelIn,
    PanelUpdate,
    PartnerIn,
    ProductIn,
    ProductUpdate,
    ResolveOperationIn,
)
from app.services.panels import (
    client_for_panel,
    normalize_base_url,
    normalize_domain,
    normalize_secure_path,
    plan_warnings,
)
from app.services.billing import dec_str
from app.services.present import present_ledger, present_panel, present_product, present_subscription
from app.services.present import present_live
from app.services.subscriptions import (
    Actor,
    admin_adjust,
    get_subscription,
    set_partner_ban,
    set_subscription_ban,
    update_note,
)

router = APIRouter(prefix="/admin", tags=["admin"])


def _admin_actor(admin: AdminUser) -> Actor:
    return Actor("admin", admin.id)


@router.post("/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    key = login_key(request.client.host if request.client else "-", body.username, "admin")
    ensure_login_allowed(key, db)
    admin = db.scalar(select(AdminUser).where(AdminUser.username == body.username.strip()))
    if admin is not None:
        db.expunge(admin)
    db.commit()  # release SQLite before the expensive password hash
    if not check_password(body.password, admin.password_hash if admin else None):
        raise AppError("نام کاربری یا رمز نادرست است", 401)
    if admin is None:
        raise AppError("نام کاربری یا رمز نادرست است", 401)
    clear_login_failures(key, db)
    return {
        "access_token": create_token("admin", admin.id, admin.username),
        "token_type": "bearer",
        "username": admin.username,
        "role": "admin",
    }


@router.post("/auth/logout")
def logout(request: Request, db: Session = Depends(get_db), _: AdminUser = Depends(current_admin)):
    from app.core.deps import _bearer

    revoke_token(_bearer(request), "admin", db)
    return {"ok": True}


@router.get("/me")
def me(admin: AdminUser = Depends(current_admin)):
    return {"id": admin.id, "username": admin.username, "role": "admin"}


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), _: AdminUser = Depends(current_admin)):
    return {
        "panels": db.scalar(select(func.count()).select_from(Panel)) or 0,
        "products": db.scalar(select(func.count()).select_from(Product)) or 0,
        "partners": db.scalar(select(func.count()).select_from(Partner)) or 0,
        "subscriptions": db.scalar(select(func.count()).select_from(Subscription)) or 0,
        "banned_partners": db.scalar(select(func.count()).select_from(Partner).where(Partner.banned.is_(True))) or 0,
    }


@router.get("/panels")
def list_panels(db: Session = Depends(get_db), _: AdminUser = Depends(current_admin)):
    rows = db.scalars(select(Panel).order_by(Panel.id.desc())).all()
    return {"items": [present_panel(row) for row in rows]}


@router.post("/panels")
def create_panel(body: PanelIn, db: Session = Depends(get_db), _: AdminUser = Depends(current_admin)):
    panel = Panel(
        name=body.name.strip(),
        base_url=normalize_base_url(body.base_url),
        secure_path=normalize_secure_path(body.secure_path),
        admin_email=body.admin_email.strip(),
        admin_password_enc=encrypt_secret(body.admin_password),
        status=body.status,
    )
    db.add(panel)
    db.flush()
    return present_panel(panel)


@router.put("/panels/{panel_id}")
def update_panel(
    panel_id: int,
    body: PanelUpdate,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(current_admin),
):
    panel = db.get(Panel, panel_id)
    if panel is None:
        raise AppError("پنل پیدا نشد", 404)
    panel.name = body.name.strip()
    panel.base_url = normalize_base_url(body.base_url)
    panel.secure_path = normalize_secure_path(body.secure_path)
    panel.admin_email = body.admin_email.strip()
    panel.status = body.status
    if body.admin_password:
        panel.admin_password_enc = encrypt_secret(body.admin_password)
        panel.last_ok_at = None
    panel.updated_at = utcnow()
    return present_panel(panel)


@router.post("/panels/{panel_id}/test")
def test_panel(panel_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(current_admin)):
    panel = db.get(Panel, panel_id)
    if panel is None:
        raise AppError("پنل پیدا نشد", 404)
    if panel.status == "disabled":
        raise AppError("پنل غیرفعال است. اول آن را فعال کنید")
    try:
        with client_for_panel(panel) as gateway:
            gateway.login()
            plans = gateway.fetch_plans()
    except AppError as exc:
        panel.status = "error"
        panel.updated_at = utcnow()
        db.commit()
        raise AppError(exc.message, exc.status_code) from exc
    panel.status = "active"
    panel.last_ok_at = utcnow()
    panel.updated_at = utcnow()
    return {"ok": True, "plan_count": len(plans), "panel": present_panel(panel)}


@router.get("/panels/{panel_id}/plans")
def panel_plans(panel_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(current_admin)):
    panel = db.get(Panel, panel_id)
    if panel is None:
        raise AppError("پنل پیدا نشد", 404)
    try:
        with client_for_panel(panel) as gateway:
            plans = gateway.fetch_plans()
    except AppError as exc:
        panel.status = "error"
        panel.updated_at = utcnow()
        db.commit()
        raise AppError(exc.message, exc.status_code) from exc
    panel.status = "active"
    panel.last_ok_at = utcnow()
    items = []
    for plan in plans:
        warnings = plan_warnings(plan, "example.com")
        items.append(
            {
                "id": int(plan["id"]),
                "name": plan.get("name"),
                "transfer_enable": plan.get("transfer_enable"),
                "device_limit": plan.get("device_limit"),
                "speed_limit": plan.get("speed_limit"),
                "group_id": plan.get("group_id"),
                "reset_traffic_method": plan.get("reset_traffic_method"),
                "warnings": [item for item in warnings if "reset_traffic" in item or "ریست" in item],
            }
        )
    return {"items": items}


@router.get("/products")
def list_products(db: Session = Depends(get_db), _: AdminUser = Depends(current_admin)):
    rows = (
        db.execute(select(Product).options(joinedload(Product.panel)).order_by(Product.id.desc()))
        .unique()
        .scalars()
        .all()
    )
    return {"items": [present_product(row) for row in rows]}


@router.post("/products")
def create_product(body: ProductIn, db: Session = Depends(get_db), _: AdminUser = Depends(current_admin)):
    panel = db.get(Panel, body.panel_id)
    if panel is None:
        raise AppError("پنل پیدا نشد", 404)
    domain = normalize_domain(body.email_domain)
    exists = db.scalar(
        select(Product.id).where(Product.panel_id == panel.id, Product.plan_id == body.plan_id)
    )
    if exists:
        raise AppError("این پلن قبلاً روی همین پنل به محصول وصل شده")
    try:
        with client_for_panel(panel) as gateway:
            plan = gateway.get_plan(body.plan_id)
    except AppError as exc:
        panel.status = "error"
        panel.updated_at = utcnow()
        db.commit()
        raise AppError(exc.message, exc.status_code) from exc
    panel.status = "active"
    panel.last_ok_at = utcnow()
    product = Product(
        panel_id=panel.id,
        plan_id=body.plan_id,
        name=body.name.strip(),
        email_domain=domain,
        price_per_gb=body.price_per_gb,
        price_per_day=body.price_per_day,
        active=body.active,
    )
    db.add(product)
    db.flush()
    db.refresh(product)
    return {"product": present_product(product), "warnings": plan_warnings(plan, domain)}


@router.put("/products/{product_id}")
def update_product(
    product_id: int,
    body: ProductUpdate,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(current_admin),
):
    product = db.scalar(
        select(Product).where(Product.id == product_id).options(joinedload(Product.panel))
    )
    if product is None:
        raise AppError("محصول پیدا نشد", 404)
    product.name = body.name.strip()
    domain = normalize_domain(body.email_domain)
    domain_changed = domain != product.email_domain
    product.email_domain = domain
    product.price_per_gb = body.price_per_gb
    product.price_per_day = body.price_per_day
    product.active = body.active
    product.updated_at = utcnow()
    warnings: list[str] = []
    if domain_changed:
        existing = db.scalar(
            select(func.count()).select_from(Subscription).where(Subscription.product_id == product.id)
        )
        if existing:
            warnings.append("دامنه اشتراک‌های قبلی ثابت می‌ماند. دامنه جدید فقط برای ساخت اشتراک بعدی است.")
    if product.panel.status != "disabled":
        try:
            with client_for_panel(product.panel) as gateway:
                plan = gateway.get_plan(product.plan_id)
            warnings = plan_warnings(plan, product.email_domain)
        except AppError as exc:
            warnings = [exc.message]
    return {"product": present_product(product), "warnings": warnings}


def _partner_username(value: str) -> str:
    username = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{3,64}", username):
        raise AppError("نام کاربری همکار فقط حروف انگلیسی، عدد، _ ، . و - است")
    return username


def _partner_credits(db: Session, partner: Partner) -> list[dict]:
    products = (
        db.execute(select(Product).options(joinedload(Product.panel)).order_by(Product.id))
        .unique()
        .scalars()
        .all()
    )
    credits = {
        row.product_id: row
        for row in db.scalars(
            select(PartnerProductCredit).where(PartnerProductCredit.partner_id == partner.id)
        ).all()
    }
    items = []
    for product in products:
        credit = credits.get(product.id)
        items.append(
            {
                **present_product(product),
                "credit_gb": dec_str(credit.credit_gb) if credit else "0",
                "credit_days": int(credit.credit_days) if credit else 0,
            }
        )
    return items


@router.get("/partners")
def list_partners(db: Session = Depends(get_db), _: AdminUser = Depends(current_admin)):
    rows = db.scalars(select(Partner).order_by(Partner.id.desc())).all()
    return {
        "items": [
            {
                "id": row.id,
                "username": row.username,
                "balance_toman": int(row.balance_toman),
                "banned": bool(row.banned),
                "created_jalali": present_ledger_created(row),
            }
            for row in rows
        ]
    }


def present_ledger_created(partner: Partner) -> str | None:
    from app.core.timeutil import format_datetime_jalali

    return format_datetime_jalali(partner.created_at)


@router.post("/partners")
def create_partner(body: PartnerIn, db: Session = Depends(get_db), _: AdminUser = Depends(current_admin)):
    username = _partner_username(body.username)
    if db.scalar(select(Partner.id).where(Partner.username == username)):
        raise AppError("این نام کاربری قبلاً ثبت شده")
    partner = Partner(username=username, password_hash=hash_password(body.password), balance_toman=0, banned=False)
    db.add(partner)
    db.flush()
    return {"id": partner.id, "username": partner.username, "balance_toman": 0, "banned": False}


@router.get("/partners/{partner_id}")
def get_partner(partner_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(current_admin)):
    partner = db.get(Partner, partner_id)
    if partner is None:
        raise AppError("همکار پیدا نشد", 404)
    return {
        "id": partner.id,
        "username": partner.username,
        "balance_toman": int(partner.balance_toman),
        "banned": bool(partner.banned),
        "credits": _partner_credits(db, partner),
    }


@router.post("/partners/{partner_id}/adjust")
def adjust_partner(
    partner_id: int,
    body: AdjustIn,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(current_admin),
):
    partner = admin_adjust(
        db,
        partner_id=partner_id,
        asset=body.asset,
        amount=body.amount,
        product_id=body.product_id,
        actor=_admin_actor(admin),
    )
    result = {
        "id": partner.id,
        "balance_toman": int(partner.balance_toman),
        "credits": _partner_credits(db, partner),
    }
    # Commit the credit and its ledger entry before acknowledging the adjustment.
    db.commit()
    return result


@router.post("/partners/{partner_id}/ban")
def ban_partner(partner_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(current_admin)):
    partner = set_partner_ban(
        db,
        partner_id=partner_id,
        banned=True,
        actor=_admin_actor(admin),
        gateway_factory=client_for_panel,
    )
    return {"id": partner.id, "banned": True}


@router.post("/partners/{partner_id}/unban")
def unban_partner(partner_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(current_admin)):
    partner = set_partner_ban(
        db,
        partner_id=partner_id,
        banned=False,
        actor=_admin_actor(admin),
        gateway_factory=client_for_panel,
    )
    return {"id": partner.id, "banned": False}


@router.get("/subscriptions")
def list_subscriptions(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(current_admin),
    q: str | None = None,
    partner_id: int | None = None,
    product_id: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    query = select(Subscription).options(
        joinedload(Subscription.partner),
        joinedload(Subscription.product).joinedload(Product.panel),
    )
    count_query = select(func.count()).select_from(Subscription)
    if partner_id:
        query = query.where(Subscription.partner_id == partner_id)
        count_query = count_query.where(Subscription.partner_id == partner_id)
    if product_id:
        query = query.where(Subscription.product_id == product_id)
        count_query = count_query.where(Subscription.product_id == product_id)
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


@router.get("/subscriptions/{sub_id}")
def subscription_detail(sub_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(current_admin)):
    sub = get_subscription(db, sub_id)
    try:
        with client_for_panel(sub.product.panel) as gateway:
            return present_live(sub, gateway)
    except AppError as exc:
        data = present_subscription(sub)
        data.update(used_gb=None, upload_gb=None, download_gb=None, subscribe_url=None, live_error=public_error(exc))
        return data


@router.post("/subscriptions/{sub_id}/ban")
def ban_subscription(sub_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(current_admin)):
    sub = get_subscription(db, sub_id)
    with client_for_panel(sub.product.panel) as gateway:
        updated = set_subscription_ban(
            db, sub_id=sub_id, partner_id=None, banned=True, actor=_admin_actor(admin), gateway=gateway
        )
    return present_subscription(updated)


@router.post("/subscriptions/{sub_id}/unban")
def unban_subscription(sub_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(current_admin)):
    sub = get_subscription(db, sub_id)
    with client_for_panel(sub.product.panel) as gateway:
        updated = set_subscription_ban(
            db, sub_id=sub_id, partner_id=None, banned=False, actor=_admin_actor(admin), gateway=gateway
        )
    return present_subscription(updated)


@router.post("/subscriptions/{sub_id}/note")
def note_subscription(
    sub_id: int,
    body: NoteIn,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(current_admin),
):
    sub = update_note(db, sub_id=sub_id, partner_id=None, note=body.note)
    return present_subscription(sub)


@router.get("/operations")
def operations(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(current_admin),
    status: str | None = None,
    limit: int = Query(50, ge=1, le=100),
):
    from app.services.money_sync import list_operations

    return {"items": list_operations(db, status, limit)}


@router.get("/operations/{op_id}")
def operation_detail(op_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(current_admin)):
    from app.models import PanelOperation
    from app.services.money_sync import operation_decision_blocks, operation_panel_state, operation_view

    op = db.get(PanelOperation, op_id)
    if op is None:
        raise AppError("عملیات پیدا نشد", 404)
    view = operation_view(op)
    try:
        view["panel_state"] = operation_panel_state(db, op_id, client_for_panel)
    except AppError as exc:
        view["panel_state"] = {"available": False, "reason": public_error(exc)}
    view["decision_blocks"] = operation_decision_blocks(db.get(PanelOperation, op_id), view["panel_state"])
    return view


@router.post("/operations/{op_id}/resolve")
def resolve_operation(
    op_id: int,
    body: ResolveOperationIn,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(current_admin),
):
    from app.services.money_sync import resolve_attention

    return resolve_attention(
        db,
        op_id,
        decision=body.decision,
        actor=_admin_actor(admin),
        note=body.note,
        accept_shortfall=body.accept_shortfall,
        seen_updated_at=body.seen_updated_at,
        gateway_factory=client_for_panel,
    )


@router.get("/ledger")
def ledger(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(current_admin),
    partner_id: int | None = None,
    asset: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    query = select(WalletLedger).options(
        joinedload(WalletLedger.partner),
        joinedload(WalletLedger.product),
    )
    count_query = select(func.count()).select_from(WalletLedger)
    if partner_id:
        query = query.where(WalletLedger.partner_id == partner_id)
        count_query = count_query.where(WalletLedger.partner_id == partner_id)
    if asset:
        query = query.where(WalletLedger.asset == asset)
        count_query = count_query.where(WalletLedger.asset == asset)
    total = db.scalar(count_query) or 0
    rows = db.execute(query.order_by(WalletLedger.id.desc()).limit(limit).offset(offset)).unique().scalars().all()
    return {"items": [present_ledger(row) for row in rows], "total": total}
