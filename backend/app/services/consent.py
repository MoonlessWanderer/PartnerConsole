"""Short-lived, session-bound financial decisions and their durable operation identity."""

import hashlib
import hmac
import json
import secrets
import time

import jwt
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import AppError
from app.models import Panel, PanelOperation, Product, RevokedToken


class ConsentConflict(AppError):
    def __init__(self):
        super().__init__("شرایط یا نشست تغییر کرده است. برآورد تازه را بررسی و دوباره تأیید کنید.", 409)


def conflict() -> ConsentConflict:
    return ConsentConflict()


def _key() -> bytes:
    return hmac.digest(settings.jwt_secret_for("partner").encode(), b"partner-financial-consent-v1", "sha256")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def parameters(product_id, sub_id, cap, end, username=None, note=None):
    return [product_id, sub_id, str(cap.normalize()) if cap is not None else None,
            end or None, username, (note or "").strip() or None]


def panel_version(panel) -> str:
    return _digest([panel.id, panel.base_url, panel.secure_path, panel.admin_email,
                    panel.admin_password_enc, panel.status == "disabled"])


def recovery_target(panel) -> str:
    return _digest([panel.id, panel.base_url, panel.secure_path])


def _target(product, panel, sub) -> str:
    subscription = None if sub is None else [
        sub.id, sub.username, sub.v2_user_id, str(sub.traffic_cap_gb.normalize()), sub.expired_at,
        sub.email_domain, sub.plan_id, sub.banned, sub.blocked_by_partner_ban,
    ]
    return _digest([product.id, product.panel_id, product.plan_id, product.email_domain,
                    product.active, product.price_per_gb, product.price_per_day,
                    panel_version(panel), subscription])


def terms(quote: dict) -> list:
    keys = ("need_gb", "need_days", "gb_from_credit", "days_from_credit", "gb_paid", "days_paid",
            "toman", "gb_refund", "days_refund", "used_gb")
    return [quote.get(key, "0" if key == "gb_refund" else 0 if key == "days_refund" else None) for key in keys]


def issue(session, kind, inputs, product, sub, quote) -> str:
    now = int(time.time())
    return jwt.encode({
        "sub": session["sub"], "session": session["jti"], "kind": kind,
        "inputs": _digest(inputs), "target": _target(product, product.panel, sub),
        "terms": terms(quote), "currency": "toman", "product_id": product.id,
        "panel": panel_version(product.panel), "ref": secrets.token_hex(16),
        "iat": now, "exp": min(now + 300, int(session["exp"])),
    }, _key(), algorithm="HS256")


def authorize(db, token, session, kind, inputs):
    try:
        proof = jwt.decode(token, _key(), algorithms=["HS256"],
                           options={"verify_exp": False, "require": ["exp", "iat", "sub"]})
    except jwt.PyJWTError:
        raise conflict() from None
    if (proof["sub"] != session["sub"] or proof["session"] != session["jti"]
            or proof["kind"] != kind or proof["inputs"] != _digest(inputs)):
        raise conflict()
    db.info["financial_consent"] = proof
    return proof


def existing(db):
    proof = db.info.get("financial_consent")
    if proof is None:
        return None
    # UUID is persisted in the existing operation payload in the same commit as
    # the debit. Callers repeat this lookup under the existing partner row lock.
    candidates = db.scalars(select(PanelOperation).where(
        PanelOperation.partner_id == int(proof["sub"]),
        PanelOperation.kind == proof["kind"],
        PanelOperation.payload.contains(proof["ref"], autoescape=True),
    ).execution_options(populate_existing=True)).all()
    return next((op for op in candidates if json.loads(op.payload).get("consent_ref") == proof["ref"]), None)


def check(db, product_id, sub, outcome):
    proof = db.info.get("financial_consent")
    if proof is None:
        return {}
    product = db.scalar(select(Product).where(Product.id == product_id)
                        .with_for_update().execution_options(populate_existing=True))
    panel = db.scalar(select(Panel).where(Panel.id == product.panel_id)
                      .with_for_update().execution_options(populate_existing=True))
    if (proof["exp"] <= int(time.time()) or db.get(RevokedToken, proof["session"]) is not None
            or proof["target"] != _target(product, panel, sub)
            or proof["terms"] != terms(outcome)):
        raise conflict()
    return {"consent_ref": proof["ref"], "consent_panel": recovery_target(panel)}
