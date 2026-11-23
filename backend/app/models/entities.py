from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.db_types import MYSQL_TABLE_OPTIONS, utc_datetime, money_integer, operation_text
from app.core.timeutil import utcnow


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), default=utcnow)


class Panel(Base):
    __tablename__ = "panels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    base_url: Mapped[str] = mapped_column(String(300))
    secure_path: Mapped[str] = mapped_column(String(120))
    admin_email: Mapped[str] = mapped_column(String(200))
    admin_password_enc: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="active")
    last_ok_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(utc_datetime(), default=utcnow, onupdate=utcnow)

    products: Mapped[list["Product"]] = relationship(back_populates="panel")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("panel_id", "plan_id", name="uq_product_panel_plan"), MYSQL_TABLE_OPTIONS)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id"), index=True)
    plan_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(120))
    email_domain: Mapped[str] = mapped_column(String(200))
    price_per_gb: Mapped[int] = mapped_column(money_integer())
    price_per_day: Mapped[int] = mapped_column(money_integer())
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(utc_datetime(), default=utcnow, onupdate=utcnow)

    panel: Mapped[Panel] = relationship(back_populates="products")
    credits: Mapped[list["PartnerProductCredit"]] = relationship(back_populates="product")
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="product")


class Partner(Base):
    __tablename__ = "partners"
    __table_args__ = (CheckConstraint("balance_toman >= 0", name="ck_partner_toman_nonneg"), MYSQL_TABLE_OPTIONS)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    balance_toman: Mapped[int] = mapped_column(money_integer(), default=0)
    banned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(utc_datetime(), default=utcnow, onupdate=utcnow)

    credits: Mapped[list["PartnerProductCredit"]] = relationship(back_populates="partner")
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="partner")
    ledgers: Mapped[list["WalletLedger"]] = relationship(back_populates="partner")


class PartnerProductCredit(Base):
    __tablename__ = "partner_product_credits"
    __table_args__ = (
        UniqueConstraint("partner_id", "product_id", name="uq_partner_product_credit"),
        CheckConstraint("credit_gb >= 0", name="ck_credit_gb_nonneg"),
        CheckConstraint("credit_days >= 0", name="ck_credit_days_nonneg"),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    credit_gb: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0)
    credit_days: Mapped[int] = mapped_column(Integer, default=0)

    partner: Mapped[Partner] = relationship(back_populates="credits")
    product: Mapped[Product] = relationship(back_populates="credits")


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("product_id", "username", name="uq_subscription_product_username"),
        UniqueConstraint("product_id", "v2_user_id", name="uq_subscription_product_v2_user"),
        CheckConstraint("traffic_cap_gb >= 0", name="ck_cap_nonneg"),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    username: Mapped[str] = mapped_column(String(32))
    v2_user_id: Mapped[int] = mapped_column(Integer)
    email_domain: Mapped[str | None] = mapped_column(String(200), nullable=True)
    plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    traffic_cap_gb: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    expired_at: Mapped[int] = mapped_column(BigInteger)
    banned: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_by_partner_ban: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(utc_datetime(), default=utcnow, onupdate=utcnow)

    partner: Mapped[Partner] = relationship(back_populates="subscriptions")
    product: Mapped[Product] = relationship(back_populates="subscriptions")


class PanelOperation(Base):
    """Committed intent for a panel change. Survives a crash between the money
    movement and the remote call so the same operation can be finished once."""

    __tablename__ = "panel_operations"
    __table_args__ = (
        Index(
            "uq_panel_op_pending_create",
            "product_id",
            "username",
            unique=True,
            sqlite_where=text("status = 'pending' AND kind = 'create'"),
            postgresql_where=text("status = 'pending' AND kind = 'create'"),
        ).ddl_if(dialect=("sqlite", "postgresql")),
        Index(
            "uq_panel_op_pending_sub",
            "subscription_id",
            unique=True,
            sqlite_where=text("status = 'pending' AND subscription_id IS NOT NULL"),
            postgresql_where=text("status = 'pending' AND subscription_id IS NOT NULL"),
        ).ddl_if(dialect=("sqlite", "postgresql")),
        Index("uq_panel_op_pending_create_mysql", "product_id", "username",
              text("(CASE WHEN status = 'pending' AND kind = 'create' THEN 1 ELSE NULL END)"),
              unique=True).ddl_if(dialect="mysql"),
        Index("uq_panel_op_pending_sub_mysql", "subscription_id",
              text("(CASE WHEN status = 'pending' THEN 1 ELSE NULL END)"),
              unique=True).ddl_if(dialect="mysql"),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    username: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    payload: Mapped[str] = mapped_column(operation_text())
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_until: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(utc_datetime(), default=utcnow, onupdate=utcnow)


class WalletLedger(Base):
    __tablename__ = "wallet_ledgers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"), index=True)
    asset: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str] = mapped_column(String(64))
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), default=utcnow, index=True)

    partner: Mapped[Partner] = relationship(back_populates="ledgers")
    product: Mapped[Product | None] = relationship()
    subscription: Mapped[Subscription | None] = relationship()
