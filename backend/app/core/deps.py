from collections.abc import Generator

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.errors import AppError
from app.core.security import decode_token, ensure_api_allowed
from app.models import AdminUser, Partner, RevokedToken


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise AppError("وارد نشده‌اید", 401)
    token = header.split(" ", 1)[1].strip()
    if not token:
        raise AppError("وارد نشده‌اید", 401)
    return token


def get_current_admin(request: Request, db: Session) -> AdminUser:
    try:
        payload = decode_token(_bearer(request), "admin")
        admin_id = int(payload["sub"])
    except (ValueError, KeyError) as exc:
        raise AppError("نشست نامعتبر است", 401) from exc
    if db.get(RevokedToken, payload["jti"]) is not None:
        raise AppError("نشست نامعتبر است", 401)
    admin = db.get(AdminUser, admin_id)
    if admin is None:
        raise AppError("نشست نامعتبر است", 401)
    if not request.url.path.endswith("/auth/logout"):
        ensure_api_allowed("admin", admin_id, db)
    return admin


def get_current_partner(request: Request, db: Session) -> Partner:
    try:
        payload = decode_token(_bearer(request), "partner")
        partner_id = int(payload["sub"])
    except (ValueError, KeyError) as exc:
        raise AppError("نشست نامعتبر است", 401) from exc
    if db.get(RevokedToken, payload["jti"]) is not None:
        raise AppError("نشست نامعتبر است", 401)
    partner = db.scalar(select(Partner).where(Partner.id == partner_id))
    if partner is None:
        raise AppError("نشست نامعتبر است", 401)
    if partner.banned and not request.url.path.endswith("/auth/logout"):
        raise AppError("حساب شما مسدود است", 403)
    if not request.url.path.endswith("/auth/logout"):
        ensure_api_allowed("partner", partner_id, db)
    return partner
