import hashlib
import hmac
import secrets
import json
import time
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import case, delete, update

from app.core.config import settings
from app.core.errors import AppError
from app.models import LoginThrottle, RevokedToken

_DUMMY_HASH: str | None = None
_LOGIN_WINDOW = 600
_LOGIN_LIMIT = 8


def hash_password(password: str) -> str:
    rounds = 200_000
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), rounds)
    return f"pbkdf2_sha256${rounds}${salt}${digest.hex()}"


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("not-a-real-password")
    return _DUMMY_HASH


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, rounds, salt, hexhash = encoded.split("$", 3)
        rounds_i = int(rounds)
        if algo != "pbkdf2_sha256" or rounds_i < 100_000 or rounds_i > 500_000:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), rounds_i)
        return hmac.compare_digest(digest.hex(), hexhash)
    except Exception:
        return False


def check_password(password: str, encoded: str | None) -> bool:
    return verify_password(password, encoded or _dummy_hash()) and encoded is not None


def login_key(ip: str, username: str, role: str) -> str:
    return json.dumps([role, ip, username.strip().lower()])


def _insert(db, model):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif db.get_bind().dialect.name == "mysql":
        from sqlalchemy.dialects.mysql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(model)


def _bucket(*parts) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _reserve_bucket(db, bucket: str, limit: int, window: int, now: int) -> bool:
    if db.get_bind().dialect.name == "mysql":
        # The no-op upsert creates or exclusively locks the bucket until the
        # checkpoint commits. Never use INSERT IGNORE: other errors must surface.
        stmt = _insert(db, LoginThrottle).values(key=bucket, attempts=0, reset_at=now + window)
        db.execute(stmt.on_duplicate_key_update(key=LoginThrottle.key))
        result = db.execute(
            update(LoginThrottle)
            .where(LoginThrottle.key == bucket,
                   (LoginThrottle.reset_at <= now) | (LoginThrottle.attempts < limit))
            .values(
                attempts=case((LoginThrottle.reset_at <= now, 1), else_=LoginThrottle.attempts + 1),
                reset_at=case((LoginThrottle.reset_at <= now, now + window), else_=LoginThrottle.reset_at),
            ).execution_options(synchronize_session=False)
        )
        # Accepted reservations always change a value; denied ones match no row.
        return result.rowcount == 1
    stmt = _insert(db, LoginThrottle).values(key=bucket, attempts=1, reset_at=now + window)
    stmt = stmt.on_conflict_do_update(
        index_elements=[LoginThrottle.key],
        set_={
            "attempts": case((LoginThrottle.reset_at <= now, 1), else_=LoginThrottle.attempts + 1),
            "reset_at": case((LoginThrottle.reset_at <= now, now + window), else_=LoginThrottle.reset_at),
        },
        where=(LoginThrottle.reset_at <= now) | (LoginThrottle.attempts < limit),
    ).returning(LoginThrottle.key)
    return db.scalar(stmt) is not None


def ensure_login_allowed(key: str, db) -> None:
    """Reserve attempts atomically *before* password verification, across workers.

    A failed HTTP request rolls back its request session, so this security
    checkpoint deliberately commits before authentication work begins.
    Only login routes call it, before any business mutation.
    """
    role, ip, username = json.loads(key)
    now = int(time.time())
    db.execute(delete(LoginThrottle).where(LoginThrottle.reset_at <= now))
    db.execute(delete(RevokedToken).where(RevokedToken.expires_at <= now))
    for bucket, limit in ((_bucket("ip", role, ip), 40), (_bucket("account", role, username), _LOGIN_LIMIT)):
        if not _reserve_bucket(db, bucket, limit, _LOGIN_WINDOW, now):
            db.commit()
            raise AppError("تلاش‌های ورود زیاد است. چند دقیقه بعد دوباره امتحان کنید", 429)
    db.commit()


def ensure_api_allowed(role: str, user_id: int, db) -> None:
    """Account quota spans tokens/workers; checked before route side effects."""
    allowed = _reserve_bucket(db, _bucket("api", role, user_id),
                              settings.api_rate_limit_per_minute, 60, int(time.time()))
    db.commit()
    if not allowed:
        raise AppError("تعداد درخواست‌ها زیاد است؛ یک دقیقه بعد دوباره تلاش کنید", 429)


def clear_login_failures(key: str, db) -> None:
    role, _ip, username = json.loads(key)
    db.execute(delete(LoginThrottle).where(LoginThrottle.key == _bucket("account", role, username)))
    db.commit()


def revoke_token(token: str, role: str, db) -> None:
    payload = decode_token(token, role)
    stmt = _insert(db, RevokedToken).values(jti=payload["jti"], expires_at=int(payload["exp"]))
    if db.get_bind().dialect.name == "mysql":
        db.execute(stmt.on_duplicate_key_update(jti=RevokedToken.jti))
    else:
        db.execute(stmt.on_conflict_do_nothing(index_elements=[RevokedToken.jti]))
    db.commit()


def create_token(role: str, user_id: int, username: str) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "jti": secrets.token_hex(16),
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, _jwt_key(role), algorithm="HS256")


def decode_token(token: str, role: str) -> dict:
    try:
        payload = jwt.decode(token, _jwt_key(role), algorithms=["HS256"],
                             options={"require": ["exp", "iat", "sub", "role", "jti"]})
    except jwt.PyJWTError as exc:
        raise ValueError("invalid") from exc
    if payload.get("role") != role:
        raise ValueError("role")
    if not isinstance(payload["jti"], str) or len(payload["jti"]) != 32:
        raise ValueError("jti")
    return payload


def _jwt_key(role: str) -> str:
    raw = settings.jwt_secret_for(role)
    return hashlib.sha256(raw.encode()).hexdigest()
