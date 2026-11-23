import hashlib
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.security import hash_password, verify_password
from app.models import AdminUser

logger = logging.getLogger("partner.bootstrap")


def _ensure_sqlite_parent() -> None:
    url = settings.database_url
    if url.startswith("sqlite:///"):
        raw = url.removeprefix("sqlite:///")
        if raw.startswith("./") or (raw and not raw.startswith(":memory:")):
            Path(raw).parent.mkdir(parents=True, exist_ok=True)


def _alembic_config() -> Config:
    ini = Path(__file__).resolve().parents[2] / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "alembic"))
    return cfg


def upgrade_database(connection=None) -> None:
    _ensure_sqlite_parent()
    cfg = _alembic_config()
    # env.py reads DATABASE_URL directly from settings. Passing it through
    # ConfigParser would interpolate percent-encoded credentials.
    if connection is not None:
        cfg.attributes["connection"] = connection
    command.upgrade(cfg, "head")


def _has_valid_admin(db) -> bool:
    hashes = db.scalars(select(AdminUser.password_hash)).all()
    if any(verify_password("admin12345", password_hash) for password_hash in hashes):
        raise RuntimeError("An existing admin still uses the published default password; reset it before starting")
    return bool(hashes)


def _mysql_bootstrap_lock(connection) -> str:
    database = connection.engine.url.database
    if not database:
        raise RuntimeError("An explicit MySQL database is required")
    key = hashlib.sha256(database.lower().encode()).hexdigest()[:48]
    return f"partnerpanel:{key}"


def _require_explicit_database() -> None:
    if settings.database_url == type(settings).model_fields["database_url"].default:
        raise RuntimeError("Set DATABASE_URL to the intended database explicitly")
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite" and (
        url.database in (None, "", ":memory:", "file::memory:") or url.query.get("mode") == "memory"
    ):
        raise RuntimeError("Maintenance and production require a persistent database")


def require_database_ready() -> None:
    """Check schema and admin eligibility before a production worker starts."""
    _require_explicit_database()
    expected = set(ScriptDirectory.from_config(_alembic_config()).get_heads())
    try:
        with SessionLocal() as db:
            connection = db.connection()
            if connection.dialect.name == "mysql":
                lock_name = _mysql_bootstrap_lock(connection)
                if connection.scalar(text("SELECT IS_FREE_LOCK(:name)"), {"name": lock_name}) != 1:
                    raise RuntimeError("Database maintenance is active or its lock cannot be verified")
            actual = set(MigrationContext.configure(connection).get_current_heads())
            if not expected or actual != expected:
                raise RuntimeError("Database revision does not match this release; run the maintenance bootstrap")
            if not _has_valid_admin(db):
                raise RuntimeError("No administrator is configured; run the maintenance bootstrap")
    except SQLAlchemyError:
        raise RuntimeError("Database readiness check failed; verify connectivity and schema") from None


def initialize_database() -> None:
    """Run migrations and seed in one serialized maintenance invocation."""
    settings.validate_security()
    _require_explicit_database()
    _ensure_sqlite_parent()
    with engine.connect() as connection:
        dialect = connection.dialect.name
        if dialect not in {"sqlite", "mysql"}:
            raise RuntimeError("Maintenance bootstrap supports SQLite and MySQL")

        lock_name = _mysql_bootstrap_lock(connection) if dialect == "mysql" else None
        locked = False
        try:
            if lock_name is not None:
                # The migration connection keeps the named lock across
                # MySQL's implicit DDL commits.
                locked = connection.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": lock_name}) == 1
                if not locked:
                    raise RuntimeError("Another database bootstrap is running or the MySQL lock is unavailable")
                connection.commit()
            # The SQLite engine uses BEGIN IMMEDIATE to serialize peer
            # invocations before either one inspects the Alembic revision.
            with connection.begin():
                upgrade_database(connection)
                seed_admin(connection)
        finally:
            if locked and not connection.invalidated:
                try:
                    released = connection.scalar(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})
                    if released != 1:
                        connection.invalidate()
                except SQLAlchemyError:
                    # Avoid handing a connection with a named lock to the pool.
                    connection.invalidate()


def recover_pending_operations() -> None:
    from app.services.money_sync import reconcile_pending
    from app.services.panels import client_for_panel

    db = SessionLocal()
    try:
        done = reconcile_pending(db, client_for_panel)
        if done:
            logger.info("عملیات ناتمام پنل ادامه پیدا کرد: %s", done)
    except Exception as exc:
        logger.error("بازیابی عملیات پنل ناموفق بود (%s)", type(exc).__name__)
        db.rollback()
    finally:
        db.close()


def seed_admin(connection=None) -> None:
    if settings.secret_key == "dev-only-change-me":
        logger.warning("SECRET_KEY پیش‌فرض است. قبل از استفاده واقعی آن را عوض کنید.")
    db = SessionLocal(bind=connection) if connection is not None else SessionLocal()
    try:
        if _has_valid_admin(db):
            return
        if len(settings.admin_password) < 12 or settings.admin_password == "admin12345":
            raise RuntimeError("Set a non-default ADMIN_PASSWORD of at least 12 characters before initial bootstrap")
        db.add(
            AdminUser(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
            )
        )
        db.commit()
    finally:
        db.close()
