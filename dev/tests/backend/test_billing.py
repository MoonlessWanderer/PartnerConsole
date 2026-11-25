import json
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from local_http import BACKEND

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.database import Base, make_engine
from app.core.errors import AppError, V2BoardError
from app.core.security import create_token, decode_token
from app.core.timeutil import (
    days_left,
    end_of_day_ts,
    extend_base_date,
    format_jalali_date,
    gregorian_to_jalali,
    jalali_to_gregorian,
    need_days,
    ts_to_tehran_date,
)
from app.integrations.v2board import V2BoardClient
from app.models import Panel, PanelOperation, Partner, PartnerProductCredit, Product, Subscription, WalletLedger
from app.services.billing import gb_to_bytes, quote_charge
from app.services.money_sync import drive_operation, reconcile_pending, resolve_attention
from app.services.panels import plan_warnings
from app.services.subscriptions import (
    Actor,
    admin_adjust,
    create_subscription,
    decrease_subscription,
    delete_subscription,
    increase_subscription,
    quote_operation,
    reset_traffic,
    set_partner_ban,
    set_subscription_ban,
)

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=ZoneInfo("Asia/Tehran"))
GB = 1073741824


class PanelSim:
    def __init__(self):
        self.users: dict[int, dict] = {}
        self.next_id = 1
        self.fail_update = False
        self.updates: list[dict] = []
        self.deleted: list[int] = []
        self.generates = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/passport/auth/login"):
            body = json.loads(request.content)
            if body.get("email") != "admin@panel.test":
                return httpx.Response(500, json={"message": "bad login"})
            return httpx.Response(
                200,
                json={"data": {"auth_data": "auth-token", "is_admin": 1, "token": "login"}},
            )
        if request.headers.get("authorization") != "auth-token":
            return httpx.Response(403, json={"message": "expired"})
        if path.endswith("/plan/fetch"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": 7,
                            "name": "طلایی",
                            "transfer_enable": 50,
                            "device_limit": 2,
                            "speed_limit": 100,
                            "reset_traffic_method": 2,
                            "group_id": 1,
                        }
                    ]
                },
            )
        if path.endswith("/user/generate"):
            body = json.loads(request.content)
            self.generates += 1
            email = f"{body['email_prefix']}@{body['email_suffix']}"
            uid = self.next_id
            self.next_id += 1
            self.users[uid] = {
                "id": uid,
                "email": email,
                "plan_id": body.get("plan_id"),
                "expired_at": body.get("expired_at"),
                "u": 0,
                "d": 0,
                "transfer_enable": 50 * GB,
                "banned": 0,
                "token": f"tok{uid}",
                "subscribe_url": f"https://panel.test/sub/{uid}",
                "device_limit": None,
                "speed_limit": None,
            }
            return httpx.Response(200, json={"data": True})
        if path.endswith("/user/fetch"):
            params = request.url.params
            key = params.get("filter[0][key]")
            value = params.get("filter[0][value]")
            rows = []
            for user in self.users.values():
                if key == "email" and user["email"] == value:
                    rows.append(user)
                elif key == "id" and str(user["id"]) == str(value):
                    rows.append(user)
            return httpx.Response(200, json={"data": rows, "total": len(rows)})
        if path.endswith("/user/getUserInfoById"):
            uid = int(request.url.params.get("id"))
            user = self.users.get(uid)
            if user is None:
                # Upstream crashes on a null model; production hides the cause.
                return httpx.Response(500, json={"message": "Uh-oh, we've had some problems, we're working on it."})
            return httpx.Response(200, json={"data": user})
        if path.endswith("/user/update"):
            if self.fail_update:
                return httpx.Response(500, json={"message": "保存失败"})
            body = json.loads(request.content)
            self.updates.append(body)
            user = self.users.get(int(body["id"]))
            if user is None:
                return httpx.Response(500, json={"message": "用户不存在"})
            for key, value in body.items():
                if key != "id":
                    user[key] = value
            return httpx.Response(200, json={"data": True})
        if path.endswith("/user/resetSecret"):
            body = json.loads(request.content)
            user = self.users[int(body["id"])]
            user["token"] = "reset"
            user["subscribe_url"] = f"https://panel.test/sub/{user['id']}?n=1"
            return httpx.Response(200, json={"data": True})
        if path.endswith("/user/delUser"):
            body = json.loads(request.content)
            uid = int(body["id"])
            self.deleted.append(uid)
            self.users.pop(uid, None)
            return httpx.Response(200, json={"data": True})
        return httpx.Response(500, json={"message": f"unknown {path} {dict(request.url.params)}"})


@pytest.fixture()
def db():
    engine = make_engine("sqlite://")
    engine.dispose()
    engine = make_engine("sqlite://")
    # Static pool so the in-memory database survives across connections.
    from sqlalchemy import create_engine, event

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def sim():
    return PanelSim()


def gateway(sim: PanelSim) -> V2BoardClient:
    return V2BoardClient(
        "http://panel.test",
        "secret",
        "admin@panel.test",
        "secret",
        transport=httpx.MockTransport(sim.handler),
    )


def seed(db, *, balance=1_000_000, credit_gb=0, credit_days=0, active=True, price_gb=1000, price_day=1000):
    panel = Panel(
        name="پنل",
        base_url="http://panel.test",
        secure_path="secret",
        admin_email="admin@panel.test",
        admin_password_enc="unused",
        status="active",
    )
    product = Product(
        panel=panel,
        plan_id=7,
        name="طلایی",
        email_domain="gold.example.com",
        price_per_gb=price_gb,
        price_per_day=price_day,
        active=active,
    )
    partner = Partner(username="shop1", password_hash="x", balance_toman=balance, banned=False)
    db.add_all([panel, product, partner])
    db.flush()
    if credit_gb or credit_days:
        db.add(
            PartnerProductCredit(
                partner_id=partner.id,
                product_id=product.id,
                credit_gb=credit_gb,
                credit_days=credit_days,
            )
        )
    db.commit()
    return partner, product


def jalali(day: date) -> str:
    return format_jalali_date(day)


def test_charge_uses_credit_then_floors_toman():
    full = quote_charge(Decimal("5"), 4, Decimal("5"), 4, 1000, 800)
    assert full.toman == 0
    assert full.gb_paid == 0
    partial = quote_charge(Decimal("1.5"), 2, Decimal("1"), 0, 10, 7)
    assert partial.gb_from_credit == Decimal("1.000000") or partial.gb_paid == Decimal("0.5")
    assert partial.gb_paid == Decimal("0.500000") or partial.toman == 19
    assert partial.days_paid == 2
    assert partial.toman == 19
    tiny = quote_charge(Decimal("0.2"), 0, 0, 0, 3, 10)
    assert tiny.toman == 0


def test_time_formulas():
    assert need_days(date(2026, 11, 23), date(2026, 11, 20)) == 3
    assert days_left(1_000 + 2 * 86400 + 10, 1_000) == 2
    assert days_left(1_000, 1_000) == 0
    assert days_left(999, 1_000) == 0
    sample = date(2026, 11, 20)
    assert jalali_to_gregorian(gregorian_to_jalali(sample)) == sample
    assert ts_to_tehran_date(end_of_day_ts(sample)) == sample
    assert extend_base_date(end_of_day_ts(date(2026, 6, 10)), NOW) == date(2026, 6, 15)
    assert extend_base_date(end_of_day_ts(date(2026, 6, 25)), NOW) == date(2026, 6, 25)


def test_plan_warning_when_reset_is_not_off():
    assert plan_warnings({"reset_traffic_method": None}, "a.example.com")
    assert plan_warnings({"reset_traffic_method": 2}, "a.example.com") == []
    assert any(".local" in item for item in plan_warnings({"reset_traffic_method": 2}, "x.local"))


def test_secrets_and_separate_jwt():
    token = encrypt_secret("panel-pass")
    assert decrypt_secret(token) == "panel-pass"
    admin = create_token("admin", 1, "admin")
    with pytest.raises(ValueError):
        decode_token(admin, "partner")
    assert decode_token(admin, "admin")["role"] == "admin"


def test_create_overrides_cap_and_does_not_store_email(db, sim):
    partner, product = seed(db, credit_gb=1, credit_days=1)
    client = gateway(sim)
    sub, charge, fetched = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="Ali01",
        traffic_cap_gb=20,
        end_date=jalali(date(2026, 6, 25)),
        note="مشتری الف",
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    db.commit()
    assert "email" not in Subscription.__table__.columns
    assert sub.username == "ali01"
    assert sub.v2_user_id == 1
    assert fetched["subscribe_url"].endswith("/1")
    assert charge.need_days == 10
    assert charge.gb_from_credit == Decimal("1")
    assert charge.days_from_credit == 1
    assert charge.toman == 19 * 1000 + 9 * 1000
    db.refresh(partner)
    credit = db.scalar(select(PartnerProductCredit))
    assert int(partner.balance_toman) == 1_000_000 - charge.toman
    assert credit.credit_gb == 0
    assert credit.credit_days == 0
    payload = sim.updates[-1]
    assert payload["plan_id"] == 7
    assert payload["email"] == "ali01@gold.example.com"
    assert payload["is_admin"] == 0
    assert payload["is_staff"] == 0
    assert payload["banned"] == 0
    assert payload["transfer_enable"] == gb_to_bytes(20)
    assert payload["device_limit"] == 2
    assert payload["speed_limit"] == 100
    assert "u" not in payload
    client.close()


def test_remote_failure_rolls_back_and_deletes_panel_user(db, sim):
    partner, product = seed(db, balance=50_000)
    sim.fail_update = True
    client = gateway(sim)
    with pytest.raises(AppError):
        create_subscription(
            db,
            partner_id=partner.id,
            product_id=product.id,
            username="ali01",
            traffic_cap_gb=2,
            end_date=jalali(date(2026, 6, 20)),
            note=None,
            actor=Actor("partner", partner.id),
            gateway=client,
            now=NOW,
        )
    db.rollback()
    db.refresh(partner)
    assert partner.balance_toman == 50_000
    assert db.scalar(select(Subscription.id)) is None
    assert sim.users == {}
    assert sim.deleted
    client.close()


def test_insufficient_balance_does_not_call_panel(db, sim):
    partner, product = seed(db, balance=10, price_gb=100, price_day=100)
    client = gateway(sim)
    with pytest.raises(AppError):
        create_subscription(
            db,
            partner_id=partner.id,
            product_id=product.id,
            username="ali01",
            traffic_cap_gb=1,
            end_date=jalali(date(2026, 6, 16)),
            note=None,
            actor=Actor("partner", partner.id),
            gateway=client,
            now=NOW,
        )
    db.rollback()
    assert sim.generates == 0
    db.refresh(partner)
    assert partner.balance_toman == 10
    client.close()


def test_inactive_product_and_bad_username(db, sim):
    partner, product = seed(db, active=False)
    client = gateway(sim)
    with pytest.raises(AppError):
        create_subscription(
            db,
            partner_id=partner.id,
            product_id=product.id,
            username="ali01",
            traffic_cap_gb=1,
            end_date=jalali(date(2026, 6, 16)),
            note=None,
            actor=Actor("partner", partner.id),
            gateway=client,
            now=NOW,
        )
    product.active = True
    db.commit()
    with pytest.raises(AppError):
        create_subscription(
            db,
            partner_id=partner.id,
            product_id=product.id,
            username="AB",
            traffic_cap_gb=1,
            end_date=jalali(date(2026, 6, 16)),
            note=None,
            actor=Actor("partner", partner.id),
            gateway=client,
            now=NOW,
        )
    assert sim.generates == 0
    client.close()


def test_increase_expired_uses_today_and_active_uses_expiry(db, sim):
    partner, product = seed(db)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=5,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    db.commit()
    _, charge = increase_subscription(
        db,
        sub_id=sub.id,
        partner_id=partner.id,
        traffic_cap_gb=None,
        end_date=jalali(date(2026, 6, 27)),
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    assert charge.need_days == 2
    assert charge.need_gb == 0
    assert charge.toman == 2000
    sub.expired_at = end_of_day_ts(date(2026, 6, 14))
    db.commit()
    _, charge = increase_subscription(
        db,
        sub_id=sub.id,
        partner_id=partner.id,
        traffic_cap_gb=Decimal(8),
        end_date=jalali(date(2026, 6, 20)),
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    assert charge.need_days == 5
    assert charge.need_gb == 3
    assert charge.toman == 8000
    client.close()


def test_decrease_refunds_credit_not_toman_and_blocks_overuse(db, sim):
    partner, product = seed(db, balance=100_000)
    client = gateway(sim)
    sub, charge, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=20,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    db.commit()
    paid = int(partner.balance_toman)
    sim.users[sub.v2_user_id]["u"] = 16 * GB
    with pytest.raises(AppError, match="مصرف فعلی"):
        decrease_subscription(
            db,
            sub_id=sub.id,
            partner_id=partner.id,
            traffic_cap_gb=Decimal(15),
            end_date=None,
            actor=Actor("partner", partner.id),
            gateway=client,
            now=NOW,
        )
    db.rollback()
    sim.users[sub.v2_user_id]["u"] = 4 * GB
    decrease_subscription(
        db,
        sub_id=sub.id,
        partner_id=partner.id,
        traffic_cap_gb=Decimal(15),
        end_date=jalali(date(2026, 6, 20)),
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    db.commit()
    db.refresh(partner)
    credit = db.scalar(select(PartnerProductCredit))
    assert int(partner.balance_toman) == paid
    assert credit.credit_gb == 5
    assert credit.credit_days > 0
    assert db.scalar(
        select(WalletLedger).where(WalletLedger.reason == "decrease", WalletLedger.asset == "toman")
    ) is None
    client.close()
    _ = charge


def test_delete_refunds_unused_credit_without_toman(db, sim):
    partner, product = seed(db)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=20,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    db.commit()
    balance_after_create = int(partner.balance_toman)
    sim.users[sub.v2_user_id]["u"] = 4 * GB
    result = delete_subscription(
        db,
        sub_id=sub.id,
        partner_id=partner.id,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    db.commit()
    db.refresh(partner)
    credit = db.scalar(select(PartnerProductCredit))
    assert result["gb_refund"] == "16"
    assert result["days_refund"] == 10
    assert credit.credit_gb == 16
    assert credit.credit_days == 10
    assert int(partner.balance_toman) == balance_after_create
    assert db.scalar(select(Subscription.id)) is None
    assert sim.users == {}
    ledger = db.scalar(select(WalletLedger).where(WalletLedger.reason == "delete_refund"))
    assert ledger.subscription_id is None
    client.close()


def test_reset_traffic_charges_used_times_price(db, sim):
    partner, product = seed(db, balance=50_000, price_gb=100)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=20,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    db.commit()
    cap = sub.traffic_cap_gb
    sim.users[sub.v2_user_id]["u"] = 10 * GB
    sim.users[sub.v2_user_id]["d"] = 0
    before = int(partner.balance_toman)
    result = reset_traffic(
        db,
        sub_id=sub.id,
        partner_id=partner.id,
        actor=Actor("partner", partner.id),
        gateway=client,
    )
    db.commit()
    db.refresh(sub)
    db.refresh(partner)
    assert result["cost_toman"] == 1000
    assert int(partner.balance_toman) == before - 1000
    assert sub.traffic_cap_gb == cap
    assert sim.users[sub.v2_user_id]["u"] == 0
    assert sim.users[sub.v2_user_id]["d"] == 0
    credit = db.scalar(select(PartnerProductCredit))
    assert credit.credit_gb == 0
    sim.users[sub.v2_user_id]["u"] = 0
    with pytest.raises(AppError, match="مصرفی"):
        reset_traffic(
            db,
            sub_id=sub.id,
            partner_id=partner.id,
            actor=Actor("partner", partner.id),
            gateway=client,
        )
    client.close()


def test_partner_ban_only_reopens_flagged_subscriptions(db, sim):
    partner, product = seed(db)
    client = gateway(sim)
    first, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=2,
        end_date=jalali(date(2026, 6, 20)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    second, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali02",
        traffic_cap_gb=2,
        end_date=jalali(date(2026, 6, 20)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    second.banned = True
    sim.users[second.v2_user_id]["banned"] = 1
    db.commit()
    client.close()

    def factory(_panel):
        return gateway(sim)

    set_partner_ban(db, partner_id=partner.id, banned=True, actor=Actor("admin", 1), gateway_factory=factory)
    db.commit()
    db.refresh(first)
    db.refresh(second)
    db.refresh(partner)
    assert partner.banned is True
    assert first.banned is True and first.blocked_by_partner_ban is True
    assert second.banned is True and second.blocked_by_partner_ban is False
    assert sim.users[first.v2_user_id]["banned"] == 1

    set_partner_ban(db, partner_id=partner.id, banned=False, actor=Actor("admin", 1), gateway_factory=factory)
    db.commit()
    db.refresh(first)
    db.refresh(second)
    db.refresh(partner)
    assert partner.banned is False
    assert first.banned is False and first.blocked_by_partner_ban is False
    assert second.banned is True
    assert sim.users[second.v2_user_id]["banned"] == 1
    assert sim.users[first.v2_user_id]["banned"] == 0


def test_quote_does_not_spend(db, sim):
    partner, product = seed(db, balance=100)
    preview = quote_operation(
        db,
        partner_id=partner.id,
        action="create",
        product_id=product.id,
        subscription_id=None,
        traffic_cap_gb=Decimal(2),
        end_date=jalali(date(2026, 6, 18)),
        gateway=None,
        now=NOW,
    )
    db.commit()
    assert preview["affordable"] is False
    assert preview["toman"] > 100
    db.refresh(partner)
    assert partner.balance_toman == 100
    assert sim.generates == 0


def test_admin_adjust_cannot_go_negative(db):
    partner, product = seed(db, balance=1000)
    admin_adjust(
        db,
        partner_id=partner.id,
        asset="gb",
        amount=Decimal("2.5"),
        product_id=product.id,
        actor=Actor("admin", 1),
    )
    credit = db.scalar(select(PartnerProductCredit))
    assert credit.credit_gb == Decimal("2.500000")
    with pytest.raises(AppError):
        admin_adjust(
            db,
            partner_id=partner.id,
            asset="toman",
            amount=-5000,
            product_id=None,
            actor=Actor("admin", 1),
        )
    db.rollback()


def test_client_relogin_and_rejects_non_admin():
    state = {"login": 0, "update": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/passport/auth/login"):
            state["login"] += 1
            return httpx.Response(
                200, json={"data": {"auth_data": f"tok{state['login']}", "is_admin": 1, "token": "t"}}
            )
        state["update"] += 1
        if state["update"] == 1:
            return httpx.Response(403, json={"message": "expired"})
        assert request.headers["authorization"] == "tok2"
        body = json.loads(request.content)
        assert body["plan_id"] == 3
        assert body["is_admin"] == 0
        assert body["is_staff"] == 0
        return httpx.Response(200, json={"data": True})

    client = V2BoardClient(
        "http://panel.test", "secret", "a@b.c", "pw", transport=httpx.MockTransport(handler)
    )
    client.update_user({"id": 1, "plan_id": 3, "email": "a@b.c", "banned": 0, "is_admin": 0, "is_staff": 0})
    assert state["login"] == 2
    client.close()

    def bad_login(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"auth_data": "x", "is_admin": 0, "token": "t"}})

    guest = V2BoardClient(
        "http://panel.test", "secret", "a@b.c", "pw", transport=httpx.MockTransport(bad_login)
    )
    with pytest.raises(V2BoardError):
        guest.login()
    guest.close()


class _Cut:
    def __init__(self, inner, method: str, times: int, *, applied: bool = False):
        self.inner = inner
        self.method = method
        self.times = times
        self.applied = applied
        self.calls = 0

    def __getattr__(self, name):
        if name == self.method:
            def wrapped(*args, **kwargs):
                self.calls += 1
                if self.calls <= self.times:
                    if self.applied:
                        getattr(self.inner, self.method)(*args, **kwargs)
                    raise httpx.ConnectError("network down")
                return getattr(self.inner, self.method)(*args, **kwargs)

            return wrapped
        return getattr(self.inner, name)


def _pending(db):
    return db.scalar(select(PanelOperation).where(PanelOperation.status == "pending"))


def _refused() -> V2BoardError:
    """A strict pre-write abort (e.g. 订阅计划不存在): the only update error that proves nothing landed."""
    error = V2BoardError("ذخیره نشد")
    error.panel_rejected = True
    return error


def _toman_ledgers(db, reason: str):
    return list(
        db.scalars(
            select(WalletLedger).where(WalletLedger.asset == "toman", WalletLedger.reason == reason)
        ).all()
    )


def _refund_ledgers(db, kind):
    reason = "delete_refund" if kind == "delete" else "decrease"
    return db.scalars(select(WalletLedger).where(WalletLedger.reason == reason).order_by(WalletLedger.asset)).all()


def _credit_amounts(db):
    return db.execute(select(PartnerProductCredit.credit_gb, PartnerProductCredit.credit_days)).one()


def _legacy_refund(db, sub, kind, *, status="pending"):
    """Represent a journal written before refunds were deferred."""
    credit = db.scalar(select(PartnerProductCredit))
    gb, days = (16, 10) if kind == "delete" else (5, 0)
    data = {
        "actor_type": "partner", "actor_id": sub.partner_id, "panel_sent": True, "panel_applied": False,
        "email_domain": sub.email_domain, "plan_id": sub.plan_id, "v2_user_id": sub.v2_user_id,
        "subscription_id": sub.id, "gb_back": str(gb), "days_back": days, "used_gb": "4",
    }
    if kind == "decrease":
        data.update(old_cap=str(sub.traffic_cap_gb), new_cap=str(sub.traffic_cap_gb - gb),
                    old_expired_at=sub.expired_at, new_expired_at=sub.expired_at)
    if status == "pending":
        credit.credit_gb, credit.credit_days = gb, days
        if kind == "decrease":
            sub.traffic_cap_gb = Decimal(data["new_cap"])
    else:
        data.update(shortfall_gb=str(gb), shortfall_days=days)
    for asset, amount in (("gb", gb), ("days", days)):
        if amount:
            db.add(WalletLedger(partner_id=sub.partner_id, product_id=sub.product_id, subscription_id=sub.id,
                                asset=asset, amount=amount, reason="delete_refund" if kind == "delete" else "decrease",
                                actor_type="partner", actor_id=sub.partner_id))
    op = PanelOperation(partner_id=sub.partner_id, product_id=sub.product_id, subscription_id=sub.id,
                        username=sub.username, kind=kind, status=status, payload=json.dumps(data))
    db.add(op)
    db.commit()
    return op


def test_increase_timeout_is_resumed_without_a_second_charge(db, sim):
    partner, product = seed(db)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=5,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    before = int(partner.balance_toman)
    cut = _Cut(client, "update_user", 1, applied=False)
    with pytest.raises(AppError) as caught:
        increase_subscription(
            db,
            sub_id=sub.id,
            partner_id=partner.id,
            traffic_cap_gb=Decimal(8),
            end_date=None,
            actor=Actor("partner", partner.id),
            gateway=cut,
            now=NOW,
        )
    assert caught.value.status_code == 503
    db.rollback()
    db.refresh(partner)
    db.refresh(sub)
    assert int(partner.balance_toman) == before - 3000
    assert sub.traffic_cap_gb == 8
    assert _pending(db) is not None
    assert sim.users[sub.v2_user_id]["transfer_enable"] == gb_to_bytes(5)
    _, charge = increase_subscription(
        db,
        sub_id=sub.id,
        partner_id=partner.id,
        traffic_cap_gb=Decimal(8),
        end_date=None,
        actor=Actor("partner", partner.id),
        gateway=cut,
        now=NOW,
    )
    db.refresh(partner)
    assert charge.toman == 3000
    assert int(partner.balance_toman) == before - 3000
    assert len(_toman_ledgers(db, "increase")) == 1
    assert _pending(db) is None
    assert sim.users[sub.v2_user_id]["transfer_enable"] == gb_to_bytes(8)
    client.close()


def test_panel_error_refunds_increase_once_and_a_later_increase_charges_once(db, sim):
    partner, product = seed(db)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=5,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    before = int(partner.balance_toman)

    class Reject:
        def __init__(self, inner):
            self.inner = inner

        def update_user(self, _payload):
            raise _refused()

        def __getattr__(self, name):
            return getattr(self.inner, name)

    with pytest.raises(V2BoardError):
        increase_subscription(
            db,
            sub_id=sub.id,
            partner_id=partner.id,
            traffic_cap_gb=Decimal(8),
            end_date=None,
            actor=Actor("partner", partner.id),
            gateway=Reject(client),
            now=NOW,
        )
    db.rollback()
    db.refresh(partner)
    db.refresh(sub)
    assert int(partner.balance_toman) == before
    assert sub.traffic_cap_gb == 5
    failed = db.scalar(select(PanelOperation).where(PanelOperation.kind == "increase"))
    assert failed.status == "failed"
    _, charge = increase_subscription(
        db,
        sub_id=sub.id,
        partner_id=partner.id,
        traffic_cap_gb=Decimal(8),
        end_date=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    db.refresh(partner)
    assert charge.toman == 3000
    assert int(partner.balance_toman) == before - 3000
    charged = sum(int(row.amount) for row in _toman_ledgers(db, "increase"))
    restored = sum(int(row.amount) for row in _toman_ledgers(db, "operation_rollback"))
    assert charged == -6000
    assert restored == 3000
    client.close()


def test_create_timeout_after_panel_insert_does_not_generate_twice(db, sim):
    partner, product = seed(db, balance=50_000)
    client = gateway(sim)
    cut = _Cut(client, "generate_user", 1, applied=True)
    with pytest.raises(AppError) as caught:
        create_subscription(
            db,
            partner_id=partner.id,
            product_id=product.id,
            username="ali01",
            traffic_cap_gb=2,
            end_date=jalali(date(2026, 6, 20)),
            note=None,
            actor=Actor("partner", partner.id),
            gateway=cut,
            now=NOW,
        )
    assert caught.value.status_code == 503
    db.rollback()
    db.refresh(partner)
    charged = int(partner.balance_toman)
    assert charged < 50_000
    assert db.scalar(select(Subscription.id)) is None
    assert sim.generates == 1
    assert _pending(db) is not None
    sub, _charge, fetched = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=2,
        end_date=jalali(date(2026, 6, 20)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=cut,
        now=NOW,
    )
    db.refresh(partner)
    assert sub.v2_user_id == 1
    assert fetched["subscribe_url"].endswith("/1")
    assert sim.generates == 1
    assert int(partner.balance_toman) == charged
    assert len(_toman_ledgers(db, "create")) == 1
    assert _pending(db) is None
    client.close()


def test_delete_timeout_holds_refund_and_retry_settles_once(db, sim):
    partner, product = seed(db)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=20,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    sim.users[sub.v2_user_id]["u"] = 4 * GB
    balance = int(partner.balance_toman)
    cut = _Cut(client, "delete_user", 1, applied=True)
    with pytest.raises(AppError) as caught:
        delete_subscription(
            db,
            sub_id=sub.id,
            partner_id=partner.id,
            actor=Actor("partner", partner.id),
            gateway=cut,
            now=NOW,
        )
    assert caught.value.status_code == 503
    db.rollback()
    db.refresh(partner)
    op = _pending(db)
    assert json.loads(op.payload)["refund_deferred"] is True
    assert _credit_amounts(db) == (0, 0)
    assert _refund_ledgers(db, "delete") == []
    assert int(partner.balance_toman) == balance
    assert db.get(Subscription, sub.id) is not None
    assert sim.users == {}
    result = delete_subscription(
        db,
        sub_id=sub.id,
        partner_id=partner.id,
        actor=Actor("partner", partner.id),
        gateway=cut,
        now=NOW,
    )
    db.refresh(partner)
    assert result["gb_refund"] == "16"
    assert result["days_refund"] == 10
    assert _credit_amounts(db) == (16, 10)
    assert int(partner.balance_toman) == balance
    assert db.scalar(select(Subscription.id)) is None
    assert drive_operation(db, op, cut) == result
    rows = _refund_ledgers(db, "delete")
    assert [(row.asset, row.amount) for row in rows] == [("days", 10), ("gb", 16)]
    assert all(row.subscription_id is None for row in rows)
    assert cut.calls == 1
    client.close()


def test_decrease_timeout_holds_refund_and_retry_settles_once(db, sim):
    partner, product = seed(db)
    with gateway(sim) as client:
        actor = Actor("partner", partner.id)
        sub, _, _ = create_subscription(
            db, partner_id=partner.id, product_id=product.id, username="ali01", traffic_cap_gb=20,
            end_date=jalali(date(2026, 6, 25)), note=None, actor=actor, gateway=client, now=NOW,
        )
        balance = int(partner.balance_toman)
        updates_before = len(sim.updates)

        class ApplyThenTimeout:
            def update_user(self, payload):
                client.update_user(payload)
                raise httpx.ReadTimeout("confirmation interrupted")

            def __getattr__(self, name):
                return getattr(client, name)

        end_date = jalali(date(2026, 6, 20))
        with pytest.raises(AppError) as caught:
            decrease_subscription(db, sub_id=sub.id, partner_id=partner.id, traffic_cap_gb=Decimal(15),
                                  end_date=end_date, actor=actor, gateway=ApplyThenTimeout(), now=NOW)
        assert caught.value.status_code == 503
        db.rollback()
        op = _pending(db)
        assert json.loads(op.payload)["refund_deferred"] is True
        assert _credit_amounts(db) == (0, 0)
        assert _refund_ledgers(db, "decrease") == []
        offer = quote_operation(db, partner_id=partner.id, action="create", product_id=product.id,
                                subscription_id=None, traffic_cap_gb=Decimal(5), end_date=end_date,
                                gateway=None, now=NOW)
        assert Decimal(offer["credit_gb"]) == offer["credit_days"] == 0
        assert offer["toman"] == 10_000
        assert sim.users[sub.v2_user_id]["transfer_enable"] == 15 * GB

        outcome = drive_operation(db, op, client)
        assert (outcome["gb_back"], outcome["days_back"]) == (5, 5)
        repeated = drive_operation(db, op, client)
        assert (repeated["gb_back"], repeated["days_back"]) == (5, 5)
        db.refresh(sub)
        db.refresh(partner)
        assert sub.traffic_cap_gb == 15 and sub.expired_at == end_of_day_ts(date(2026, 6, 20))
        assert _credit_amounts(db) == (5, 5)
        assert partner.balance_toman == balance
        rows = _refund_ledgers(db, "decrease")
        assert [(row.asset, row.amount, row.subscription_id, row.actor_type, row.actor_id) for row in rows] == [
            ("days", 5, sub.id, "partner", partner.id), ("gb", 5, sub.id, "partner", partner.id),
        ]
        assert len(sim.updates) - updates_before == 1


def test_reset_timeout_after_panel_zero_does_not_charge_again(db, sim):
    partner, product = seed(db, balance=50_000, price_gb=100)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=20,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    sim.users[sub.v2_user_id]["u"] = 10 * GB
    before = int(partner.balance_toman)
    cut = _Cut(client, "update_user", 1, applied=True)
    with pytest.raises(AppError) as caught:
        reset_traffic(
            db,
            sub_id=sub.id,
            partner_id=partner.id,
            actor=Actor("partner", partner.id),
            gateway=cut,
        )
    assert caught.value.status_code == 503
    db.rollback()
    db.refresh(partner)
    assert int(partner.balance_toman) == before - 1000
    assert sim.users[sub.v2_user_id]["u"] == 0
    result = reset_traffic(
        db,
        sub_id=sub.id,
        partner_id=partner.id,
        actor=Actor("partner", partner.id),
        gateway=cut,
    )
    db.refresh(partner)
    assert result["cost_toman"] == 1000
    assert int(partner.balance_toman) == before - 1000
    assert len(_toman_ledgers(db, "reset_traffic")) == 1
    assert sim.users[sub.v2_user_id]["u"] == 0
    client.close()


def test_reconcile_finishes_a_pending_increase_without_charging_again(db, sim):
    partner, product = seed(db)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=5,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    before = int(partner.balance_toman)
    cut = _Cut(client, "update_user", 1, applied=False)
    with pytest.raises(AppError):
        increase_subscription(
            db,
            sub_id=sub.id,
            partner_id=partner.id,
            traffic_cap_gb=Decimal(8),
            end_date=None,
            actor=Actor("partner", partner.id),
            gateway=cut,
            now=NOW,
        )
    from app.services.money_sync import reconcile_pending

    fresh = gateway(sim)
    done = reconcile_pending(db, lambda _panel: fresh)
    db.refresh(partner)
    assert done == 1
    assert int(partner.balance_toman) == before - 3000
    assert sim.users[sub.v2_user_id]["transfer_enable"] == gb_to_bytes(8)
    assert _pending(db) is None
    client.close()
    fresh.close()


def test_legacy_decrease_shortfall_remains_in_review_and_blocks_the_next_change(db, sim):
    partner, product = seed(db, balance=100_000)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=20,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    paid = int(partner.balance_toman)

    op = _legacy_refund(db, sub, "decrease", status="attention")
    assert reconcile_pending(db, lambda _panel: gateway(sim)) == 0
    assert op.status == "attention"
    assert sub.traffic_cap_gb == 20
    assert _credit_amounts(db) == (0, 0)
    assert int(partner.balance_toman) == paid
    assert sim.users[sub.v2_user_id]["transfer_enable"] == gb_to_bytes(20)
    with pytest.raises(AppError) as blocked:
        increase_subscription(
            db,
            sub_id=sub.id,
            partner_id=partner.id,
            traffic_cap_gb=Decimal(21),
            end_date=None,
            actor=Actor("partner", partner.id),
            gateway=client,
            now=NOW,
        )
    assert blocked.value.status_code == 409
    client.close()


@pytest.mark.parametrize("snapshot", ["old_bounds", "wrong_expiry", "foreign_owner"])
def test_decrease_acknowledgement_without_verified_target_holds_refund(db, sim, snapshot):
    partner, product = seed(db)
    with gateway(sim) as client:
        actor = Actor("partner", partner.id)
        sub, _, _ = create_subscription(
            db, partner_id=partner.id, product_id=product.id, username="ali01", traffic_cap_gb=20,
            end_date=jalali(date(2026, 6, 25)), note=None, actor=actor, gateway=client, now=NOW,
        )
        old_expiry = sub.expired_at
        balance = int(partner.balance_toman)

        class AcknowledgedChange:
            def update_user(self, payload):
                if snapshot == "old_bounds":
                    return
                if snapshot == "wrong_expiry":
                    payload = dict(payload, expired_at=old_expiry)
                client.update_user(payload)
                if snapshot == "foreign_owner":
                    sim.users[payload["id"]]["email"] = "other-owner@gold.example.com"

            def __getattr__(self, name):
                return getattr(client, name)

        with pytest.raises(AppError) as caught:
            decrease_subscription(db, sub_id=sub.id, partner_id=partner.id, traffic_cap_gb=Decimal(15),
                                  end_date=jalali(date(2026, 6, 20)), actor=actor,
                                  gateway=AcknowledgedChange(), now=NOW)
        assert caught.value.status_code == 409
        db.rollback()
        op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "decrease"))
        data = json.loads(op.payload)
        assert op.status == "attention" and data["refund_deferred"] is True and not data["panel_applied"]
        assert "shortfall_gb" not in data and "shortfall_days" not in data
        assert _credit_amounts(db) == (0, 0)
        assert _refund_ledgers(db, "decrease") == []
        assert reconcile_pending(db, lambda _panel: gateway(sim)) == 0
        db.refresh(partner)
        assert partner.balance_toman == balance
        if snapshot == "foreign_owner":
            closed = resolve_attention(db, op.id, decision="close", actor=Actor("admin", 1),
                                       note="مالک پنل بررسی شد", gateway_factory=lambda _panel: gateway(sim))
            assert closed["status"] == "closed"
            db.refresh(sub)
            assert (sub.traffic_cap_gb, sub.expired_at) == (20, old_expiry)
            assert sim.users[sub.v2_user_id]["email"] == "other-owner@gold.example.com"
            assert _credit_amounts(db) == (0, 0)
            assert _refund_ledgers(db, "decrease") == []
            assert db.scalars(select(WalletLedger).where(WalletLedger.reason == "operation_rollback")).all() == []


def test_decrease_attention_keeps_reservation_when_retry_is_refused_then_settles_late_confirmation(db, sim):
    partner, product = seed(db)
    with gateway(sim) as client:
        actor = Actor("partner", partner.id)
        admin = Actor("admin", 1)
        sub, _, _ = create_subscription(
            db, partner_id=partner.id, product_id=product.id, username="ali01", traffic_cap_gb=20,
            end_date=jalali(date(2026, 6, 25)), note=None, actor=actor, gateway=client, now=NOW,
        )
        original = []

        class DelayedUpdate:
            def update_user(self, payload):
                original.append(payload)

            def __getattr__(self, name):
                return getattr(client, name)

        class RejectRetry(DelayedUpdate):
            def update_user(self, _payload):
                raise _refused()

        with pytest.raises(AppError) as first:
            decrease_subscription(db, sub_id=sub.id, partner_id=partner.id, traffic_cap_gb=Decimal(15),
                                  end_date=None, actor=actor, gateway=DelayedUpdate(), now=NOW)
        assert first.value.status_code == 409
        db.rollback()
        op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "decrease"))
        assert op.status == "attention"
        def panel_factory(_panel):
            return gateway(sim)

        with pytest.raises(AppError) as close_block:
            resolve_attention(db, op.id, decision="close", actor=admin,
                              note="هنوز روی پنل دیده نشد", gateway_factory=panel_factory)
        assert close_block.value.status_code == 409
        db.rollback()
        pending = resolve_attention(db, op.id, decision="retry", actor=admin,
                                    note="یک بار دیگر بررسی شود", gateway_factory=panel_factory)
        assert pending["status"] == "pending"
        with pytest.raises(AppError) as refused:
            drive_operation(db, op, RejectRetry())
        assert refused.value.status_code == 409
        db.rollback()
        db.refresh(op)
        assert op.status == "attention" and json.loads(op.payload)["refund_prior_send"] is True
        assert _credit_amounts(db) == (0, 0) and _refund_ledgers(db, "decrease") == []

        client.update_user(original[0])
        confirmed_update_count = len(sim.updates)
        pending = resolve_attention(db, op.id, decision="retry", actor=admin,
                                    note="نتیجه روی پنل دیده شد", gateway_factory=panel_factory)
        assert pending["status"] == "pending"
        assert reconcile_pending(db, panel_factory) == 1
        assert reconcile_pending(db, panel_factory) == 0
        assert len(sim.updates) == confirmed_update_count
        db.refresh(op)
        assert op.status == "completed" and _credit_amounts(db) == (5, 0)
        rows = _refund_ledgers(db, "decrease")
        assert [(row.asset, row.amount) for row in rows] == [("gb", 5)]


@pytest.mark.parametrize("rejected", [False, True], ids=["acknowledged_with_user_present", "rejected"])
def test_delete_requires_authoritative_absence_to_release_refund(db, sim, rejected):
    partner, product = seed(db)
    with gateway(sim) as client:
        actor = Actor("partner", partner.id)
        sub, _, _ = create_subscription(
            db, partner_id=partner.id, product_id=product.id, username="ali01", traffic_cap_gb=20,
            end_date=jalali(date(2026, 6, 25)), note=None, actor=actor, gateway=client, now=NOW,
        )
        sim.users[sub.v2_user_id]["u"] = 4 * GB
        balance = int(partner.balance_toman)

        class UnconfirmedDelete:
            def delete_user(self, _user_id):
                if rejected:
                    raise _refused()

            def __getattr__(self, name):
                return getattr(client, name)

        with pytest.raises(AppError) as caught:
            delete_subscription(db, sub_id=sub.id, partner_id=partner.id, actor=actor,
                                gateway=UnconfirmedDelete(), now=NOW)
        db.rollback()
        op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "delete"))
        assert op.status == ("failed" if rejected else "pending")
        if rejected:
            assert isinstance(caught.value, V2BoardError)
        else:
            assert caught.value.status_code == 503
        assert sub.v2_user_id in sim.users and db.get(Subscription, sub.id) is not None
        assert _credit_amounts(db) == (0, 0) and _refund_ledgers(db, "delete") == []
        assert db.scalars(select(WalletLedger).where(WalletLedger.reason == "operation_rollback")).all() == []
        db.refresh(partner)
        assert partner.balance_toman == balance

        if not rejected:
            client.delete_user(sub.v2_user_id)
            assert reconcile_pending(db, lambda _panel: gateway(sim)) == 1
            assert reconcile_pending(db, lambda _panel: gateway(sim)) == 0
            assert _credit_amounts(db) == (16, 10)
            assert [(row.asset, row.amount) for row in _refund_ledgers(db, "delete")] == [("days", 10), ("gb", 16)]
            assert len(sim.deleted) == 1


@pytest.mark.parametrize("kind", ["decrease", "delete"])
def test_old_pending_refund_is_not_paid_again_on_recovery(db, sim, kind):
    partner, product = seed(db)
    with gateway(sim) as client:
        sub, _, _ = create_subscription(
            db, partner_id=partner.id, product_id=product.id, username="ali01", traffic_cap_gb=20,
            end_date=jalali(date(2026, 6, 25)), note=None, actor=Actor("partner", partner.id),
            gateway=client, now=NOW,
        )
        balance = int(partner.balance_toman)
        op = _legacy_refund(db, sub, kind)
        amounts = _credit_amounts(db)
        ids = [row.id for row in _refund_ledgers(db, kind)]
        if kind == "decrease":
            sim.users[sub.v2_user_id]["transfer_enable"] = 15 * GB
        else:
            del sim.users[sub.v2_user_id]
        start = (len(sim.updates), len(sim.deleted))
        assert reconcile_pending(db, lambda _panel: gateway(sim)) == 1
        assert reconcile_pending(db, lambda _panel: gateway(sim)) == 0
        db.refresh(op)
        assert op.status == "completed" and "refund_deferred" not in json.loads(op.payload)
        assert (len(sim.updates), len(sim.deleted)) == start
        assert _credit_amounts(db) == amounts
        assert [row.id for row in _refund_ledgers(db, kind)] == ids
        db.refresh(partner)
        assert partner.balance_toman == balance


def test_definite_decrease_and_reset_errors_reverse_once(db, sim):
    partner, product = seed(db, balance=50_000, price_gb=100)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=20,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    paid = int(partner.balance_toman)

    class Reject:
        def __init__(self, inner):
            self.inner = inner
            self.updates = 0

        def update_user(self, _payload):
            self.updates += 1
            error = V2BoardError("ذخیره نشد")
            error.panel_rejected = True
            raise error

        def __getattr__(self, name):
            return getattr(self.inner, name)

    reject = Reject(client)
    with pytest.raises(V2BoardError):
        decrease_subscription(
            db,
            sub_id=sub.id,
            partner_id=partner.id,
            traffic_cap_gb=Decimal(15),
            end_date=None,
            actor=Actor("partner", partner.id),
            gateway=reject,
            now=NOW,
        )
    db.rollback()
    db.refresh(sub)
    credit = db.scalar(select(PartnerProductCredit))
    assert sub.traffic_cap_gb == 20
    assert credit.credit_gb == 0
    op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "decrease"))
    assert op.status == "failed"
    assert _refund_ledgers(db, "decrease") == []
    assert db.scalars(select(WalletLedger).where(WalletLedger.reason == "operation_rollback")).all() == []
    assert sim.users[sub.v2_user_id]["transfer_enable"] == gb_to_bytes(20)
    decrease_subscription(
        db,
        sub_id=sub.id,
        partner_id=partner.id,
        traffic_cap_gb=Decimal(15),
        end_date=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    credit = db.scalar(select(PartnerProductCredit))
    assert credit.credit_gb == 5
    assert sim.users[sub.v2_user_id]["transfer_enable"] == gb_to_bytes(15)

    sim.users[sub.v2_user_id]["u"] = 10 * GB
    with pytest.raises(V2BoardError):
        reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id), gateway=reject)
    db.rollback()
    db.refresh(partner)
    assert int(partner.balance_toman) == paid
    assert sim.users[sub.v2_user_id]["u"] == 10 * GB
    result = reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id), gateway=client)
    db.refresh(partner)
    assert result["cost_toman"] == 1000
    assert int(partner.balance_toman) == paid - 1000
    assert sim.users[sub.v2_user_id]["u"] == 0
    assert sum(int(row.amount) for row in _toman_ledgers(db, "reset_traffic")) == -2000
    assert sum(int(row.amount) for row in _toman_ledgers(db, "operation_rollback")) == 1000
    client.close()
    _ = reject


def test_concurrent_increase_charges_once(tmp_path, sim):
    engine = make_engine(f"sqlite:///{tmp_path.as_posix()}/race.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    partner, product = seed(db)
    real = sim.handler
    panel_lock = threading.Lock()

    def safe(request):
        with panel_lock:
            return real(request)

    sim.handler = safe
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=5,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    sub_id = sub.id
    partner_id = partner.id
    v2_id = sub.v2_user_id
    before = int(partner.balance_toman)
    db.close()
    client.close()
    barrier = threading.Barrier(2)
    errors: list[str] = []

    def worker():
        session = Session()
        local = gateway(sim)
        try:
            barrier.wait(5)
            increase_subscription(
                session,
                sub_id=sub_id,
                partner_id=partner_id,
                traffic_cap_gb=Decimal(8),
                end_date=None,
                actor=Actor("partner", partner_id),
                gateway=local,
                now=NOW,
            )
        except AppError as exc:
            errors.append(exc.message)
        finally:
            local.close()
            session.close()

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert not any(thread.is_alive() for thread in threads)
    check = Session()
    try:
        partner = check.get(Partner, partner_id)
        assert int(partner.balance_toman) == before - 3000
        completed = list(
            check.scalars(
                select(PanelOperation).where(PanelOperation.kind == "increase", PanelOperation.status == "completed")
            ).all()
        )
        assert len(completed) == 1
        assert sim.users[v2_id]["transfer_enable"] == gb_to_bytes(8)
        allowed = ("تغییری برای اعمال نیست", "در حال انجام")
        assert all(any(bit in item for bit in allowed) for item in errors)
    finally:
        check.close()
        engine.dispose()


def test_http_error_after_panel_applied_does_not_refund(db, sim):
    partner, product = seed(db)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=5,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    before = int(partner.balance_toman)

    class ApplyThenFail:
        def __init__(self, inner):
            self.inner = inner

        def update_user(self, payload):
            self.inner.update_user(payload)
            raise V2BoardError("ذخیره نشد")

        def __getattr__(self, name):
            return getattr(self.inner, name)

    _sub, charge = increase_subscription(
        db,
        sub_id=sub.id,
        partner_id=partner.id,
        traffic_cap_gb=Decimal(8),
        end_date=None,
        actor=Actor("partner", partner.id),
        gateway=ApplyThenFail(client),
        now=NOW,
    )
    db.refresh(partner)
    op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "increase"))
    assert charge.toman == 3000
    assert int(partner.balance_toman) == before - 3000
    assert op.status == "completed"
    assert sim.users[sub.v2_user_id]["transfer_enable"] == gb_to_bytes(8)
    assert len(_toman_ledgers(db, "operation_rollback")) == 0
    client.close()


def test_http_error_with_unreadable_panel_does_not_refund(db, sim):
    partner, product = seed(db)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=5,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    before = int(partner.balance_toman)

    class Unreadable:
        def __init__(self, inner):
            self.inner = inner

        def update_user(self, _payload):
            raise V2BoardError("ذخیره نشد")

        def fetch_by_id(self, _user_id):
            raise V2BoardError("خطای پنل (500)")

        def fetch_by_email(self, _email):
            raise V2BoardError("خطای پنل (500)")

        def get_user_info(self, _user_id):
            raise V2BoardError("خطای پنل (500)")

        def __getattr__(self, name):
            return getattr(self.inner, name)

    with pytest.raises(AppError) as caught:
        increase_subscription(
            db,
            sub_id=sub.id,
            partner_id=partner.id,
            traffic_cap_gb=Decimal(8),
            end_date=None,
            actor=Actor("partner", partner.id),
            gateway=Unreadable(client),
            now=NOW,
        )
    assert caught.value.status_code == 503
    db.rollback()
    db.refresh(partner)
    assert int(partner.balance_toman) == before - 3000
    assert _pending(db) is not None
    assert sim.users[sub.v2_user_id]["transfer_enable"] == gb_to_bytes(5)
    increase_subscription(
        db,
        sub_id=sub.id,
        partner_id=partner.id,
        traffic_cap_gb=Decimal(8),
        end_date=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    db.refresh(partner)
    assert int(partner.balance_toman) == before - 3000
    assert sim.users[sub.v2_user_id]["transfer_enable"] == gb_to_bytes(8)
    client.close()


def test_delete_http_error_follows_what_the_panel_shows(db, sim):
    partner, product = seed(db)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=20,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    sim.users[sub.v2_user_id]["u"] = 4 * GB

    class DeleteThenFail:
        def __init__(self, inner):
            self.inner = inner

        def delete_user(self, user_id):
            self.inner.delete_user(user_id)
            raise V2BoardError("ذخیره نشد")

        def __getattr__(self, name):
            return getattr(self.inner, name)

    result = delete_subscription(
        db,
        sub_id=sub.id,
        partner_id=partner.id,
        actor=Actor("partner", partner.id),
        gateway=DeleteThenFail(client),
        now=NOW,
    )
    credit = db.scalar(select(PartnerProductCredit))
    assert result["gb_refund"] == "16"
    assert credit.credit_gb == 16
    assert db.scalar(select(Subscription.id)) is None
    assert sim.users == {}
    client.close()


def test_leased_operation_is_not_run_twice(tmp_path, sim):
    engine = make_engine(f"sqlite:///{tmp_path.as_posix()}/lease.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    partner, product = seed(db)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=5,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    sub_id = sub.id
    partner_id = partner.id
    before = int(partner.balance_toman)
    updates_before = len(sim.updates)
    db.close()
    entered = threading.Event()
    release = threading.Event()

    class Hold:
        def __init__(self, inner):
            self.inner = inner

        def update_user(self, payload):
            entered.set()
            assert release.wait(5)
            return self.inner.update_user(payload)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    errors: list[str] = []

    def worker():
        session = Session()
        try:
            increase_subscription(
                session,
                sub_id=sub_id,
                partner_id=partner_id,
                traffic_cap_gb=Decimal(8),
                end_date=None,
                actor=Actor("partner", partner_id),
                gateway=Hold(client),
                now=NOW,
            )
        except Exception as exc:
            errors.append(repr(exc))
        finally:
            session.close()

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(5)
    other = Session()
    try:
        with pytest.raises(AppError) as caught:
            increase_subscription(
                other,
                sub_id=sub_id,
                partner_id=partner_id,
                traffic_cap_gb=Decimal(8),
                end_date=None,
                actor=Actor("partner", partner_id),
                gateway=gateway(sim),
                now=NOW,
            )
        assert caught.value.status_code == 503
        assert "در حال انجام" in caught.value.message
        assert len(sim.updates) == updates_before
    finally:
        release.set()
        other.close()
        thread.join(5)
    assert errors == []
    assert not thread.is_alive()
    check = Session()
    try:
        assert int(check.get(Partner, partner_id).balance_toman) == before - 3000
        assert len(sim.updates) == updates_before + 1
        assert sim.users[sub.v2_user_id]["transfer_enable"] == gb_to_bytes(8)
    finally:
        check.close()
        client.close()
        engine.dispose()


def test_legacy_attention_can_be_closed_or_collected_without_touching_the_panel(db, sim):
    from app.services.money_sync import list_operations

    partner, product = seed(db, balance=100_000)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=20,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    paid = int(partner.balance_toman)

    op = _legacy_refund(db, sub, "decrease", status="attention")
    assert op.status == "attention"
    assert list_operations(db, "attention")[0]["id"] == op.id
    panel_before = sim.users[sub.v2_user_id]["transfer_enable"]
    with pytest.raises(AppError, match="اختلاف مالی"):
        resolve_attention(db, op.id, decision="close", actor=Actor("admin", 1), note="بدون دیدن اختلاف")
    db.refresh(op)
    db.refresh(partner)
    assert op.status == "attention"
    assert int(partner.balance_toman) == paid
    assert sim.users[sub.v2_user_id]["transfer_enable"] == panel_before
    closed = resolve_attention(
        db,
        op.id,
        decision="close",
        actor=Actor("admin", 1),
        note="مانده پذیرفته شد",
        accept_shortfall=True,
    )
    db.refresh(partner)
    db.refresh(sub)
    assert closed["status"] == "closed"
    assert closed["resolution"] == "close"
    assert int(partner.balance_toman) == paid
    assert db.scalar(select(PartnerProductCredit)).credit_gb == 0
    assert sim.users[sub.v2_user_id]["transfer_enable"] == panel_before
    increase_subscription(
        db,
        sub_id=sub.id,
        partner_id=partner.id,
        traffic_cap_gb=Decimal(21),
        end_date=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    db.refresh(sub)
    assert sub.traffic_cap_gb == 21

    stuck = _legacy_refund(db, sub, "decrease", status="attention")
    credit = db.scalar(select(PartnerProductCredit))
    credit.credit_gb = Decimal(5)
    db.commit()
    panel_again = sim.users[sub.v2_user_id]["transfer_enable"]
    resolved = resolve_attention(db, stuck.id, decision="retry", actor=Actor("admin", 1),
                                 note="مانده اعتبار پس گرفته شود")
    credit = db.scalar(select(PartnerProductCredit))
    assert resolved["status"] == "failed"
    assert credit.credit_gb == 0
    assert sim.users[sub.v2_user_id]["transfer_enable"] == panel_again
    reversal = db.scalars(select(WalletLedger).where(WalletLedger.reason == "operation_rollback")).one()
    assert (reversal.asset, reversal.amount) == ("gb", -5)
    with pytest.raises(AppError):
        resolve_attention(db, stuck.id, decision="retry", actor=Actor("admin", 1), note="تکرار")
    assert db.scalars(select(WalletLedger).where(WalletLedger.reason == "operation_rollback")).one().id == reversal.id
    client.close()


def test_product_changes_keep_existing_subscription_identity(db, sim):
    partner, product = seed(db)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=5,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    assert sub.email_domain == "gold.example.com"
    assert sub.plan_id == 7
    product.email_domain = "other.example.com"
    product.plan_id = 99
    product.price_per_gb = 100
    product.active = False
    db.commit()
    with pytest.raises(AppError, match="خاموش"):
        create_subscription(
            db,
            partner_id=partner.id,
            product_id=product.id,
            username="ali02",
            traffic_cap_gb=1,
            end_date=jalali(date(2026, 6, 16)),
            note=None,
            actor=Actor("partner", partner.id),
            gateway=client,
            now=NOW,
        )
    product.active = True
    db.commit()
    before = int(partner.balance_toman)
    _, charge = increase_subscription(
        db,
        sub_id=sub.id,
        partner_id=partner.id,
        traffic_cap_gb=Decimal(6),
        end_date=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    db.refresh(sub)
    assert charge.toman == 100
    assert int(partner.balance_toman) == before - 100
    assert sub.username == "ali01"
    assert sub.v2_user_id == 1
    assert sub.email_domain == "gold.example.com"
    assert sub.plan_id == 7
    assert sim.updates[-1]["email"] == "ali01@gold.example.com"
    assert sim.updates[-1]["plan_id"] == 7
    assert sim.updates[-1]["id"] == 1
    set_subscription_ban(
        db, sub_id=sub.id, partner_id=partner.id, banned=True, actor=Actor("partner", partner.id), gateway=client
    )
    assert sim.updates[-1]["email"] == "ali01@gold.example.com"
    assert sim.updates[-1]["plan_id"] == 7
    assert sim.users[1]["email"] == "ali01@gold.example.com"
    product.plan_id = 7
    db.commit()
    sub2, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali02",
        traffic_cap_gb=1,
        end_date=jalali(date(2026, 6, 16)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    assert sub2.email_domain == "other.example.com"
    assert sub2.plan_id == 7
    assert sim.updates[-1]["email"] == "ali02@other.example.com"
    assert sub.email_domain == "gold.example.com"
    client.close()


def test_product_rename_and_price_do_not_touch_panel_identity(db, sim):
    partner, product = seed(db, price_gb=1000)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=5,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    product.name = "نام جدید"
    product.price_per_day = 50
    db.commit()
    db.refresh(sub)
    assert sub.traffic_cap_gb == 5
    assert sub.v2_user_id == 1
    assert sim.users[1]["email"] == "ali01@gold.example.com"
    assert sim.users[1]["plan_id"] == 7
    client.close()
    _ = partner


def test_expired_lease_does_not_refund_or_charge_twice(tmp_path, sim):
    from app.services.money_sync import _claim

    engine = make_engine(f"sqlite:///{tmp_path.as_posix()}/expire.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    partner, product = seed(db)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=5,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    sub_id = sub.id
    partner_id = partner.id
    v2_id = sub.v2_user_id
    before = int(partner.balance_toman)
    db.close()

    class Steal:
        def __init__(self, inner):
            self.inner = inner

        def update_user(self, payload):
            self.inner.update_user(payload)
            other = Session()
            try:
                op = other.scalar(
                    select(PanelOperation).where(
                        PanelOperation.kind == "increase", PanelOperation.status == "pending"
                    )
                )
                op.lease_until = 1
                other.commit()
                assert _claim(other, op.id)
            finally:
                other.close()

        def __getattr__(self, name):
            return getattr(self.inner, name)

    holder = Session()
    try:
        with pytest.raises(AppError) as caught:
            increase_subscription(
                holder,
                sub_id=sub_id,
                partner_id=partner_id,
                traffic_cap_gb=Decimal(8),
                end_date=None,
                actor=Actor("partner", partner_id),
                gateway=Steal(client),
                now=NOW,
            )
        assert caught.value.status_code == 503
        assert "در حال انجام" in caught.value.message
    finally:
        holder.close()

    check = Session()
    try:
        assert int(check.get(Partner, partner_id).balance_toman) == before - 3000
        assert len(_toman_ledgers(check, "operation_rollback")) == 0
        op = check.scalar(select(PanelOperation).where(PanelOperation.kind == "increase"))
        assert op.status == "pending"
        op.lease_until = 1
        check.commit()
        increase_subscription(
            check,
            sub_id=sub_id,
            partner_id=partner_id,
            traffic_cap_gb=Decimal(8),
            end_date=None,
            actor=Actor("partner", partner_id),
            gateway=client,
            now=NOW,
        )
        check.refresh(check.get(Partner, partner_id))
        assert int(check.get(Partner, partner_id).balance_toman) == before - 3000
        assert len(_toman_ledgers(check, "increase")) == 1
        assert len(_toman_ledgers(check, "operation_rollback")) == 0
        finished = check.scalar(select(PanelOperation).where(PanelOperation.kind == "increase"))
        assert finished.status == "completed"
        assert sim.users[v2_id]["transfer_enable"] == gb_to_bytes(8)
    finally:
        check.close()
        client.close()
        engine.dispose()


def test_slow_panel_read_does_not_block_another_connection(tmp_path, sim):
    engine = make_engine(f"sqlite:///{tmp_path.as_posix()}/slow.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    partner, product = seed(db)
    real = sim.handler
    panel_lock = threading.Lock()

    def safe(request):
        with panel_lock:
            return real(request)

    sim.handler = safe
    client = gateway(sim)
    first, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=5,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    second, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali02",
        traffic_cap_gb=5,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    partner_id = partner.id
    first_id = first.id
    second_id = second.id
    before = int(partner.balance_toman)
    db.close()

    class SlowPlan:
        def __init__(self, inner):
            self.inner = inner
            self.calls = 0

        def get_plan(self, plan_id):
            self.calls += 1
            if self.calls == 1:
                time.sleep(6)
            return self.inner.get_plan(plan_id)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    errors: list[str] = []
    finished: dict[int, float] = {}

    def run(sub_id, slow):
        session = Session()
        local = SlowPlan(gateway(sim)) if slow else gateway(sim)
        try:
            increase_subscription(
                session,
                sub_id=sub_id,
                partner_id=partner_id,
                traffic_cap_gb=Decimal(6),
                end_date=None,
                actor=Actor("partner", partner_id),
                gateway=local,
                now=NOW,
            )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            finished[sub_id] = time.time()
            local.close()
            session.close()

    started = time.time()
    threads = [
        threading.Thread(target=run, args=(first_id, True)),
        threading.Thread(target=run, args=(second_id, False)),
    ]
    threads[0].start()
    time.sleep(0.4)
    threads[1].start()
    threads[1].join(4)
    assert not threads[1].is_alive()
    assert finished[second_id] - started < 4
    threads[0].join(15)
    assert not threads[0].is_alive()
    check = Session()
    try:
        assert errors == []
        assert int(check.get(Partner, partner_id).balance_toman) == before - 2000
        assert len(_toman_ledgers(check, "increase")) == 2
    finally:
        check.close()
        client.close()
        engine.dispose()


def test_two_processes_cannot_lose_a_balance_update(tmp_path):
    import subprocess
    import sys

    engine = make_engine(f"sqlite:///{tmp_path.as_posix()}/procs.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    partner, _product = seed(db, balance=0)
    partner_id = partner.id
    db.close()
    engine.dispose()
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from sqlalchemy.orm import sessionmaker
from app.core.database import make_engine
from app.services.subscriptions import Actor, admin_adjust
engine = make_engine("sqlite:///" + sys.argv[2])
db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
admin_adjust(db, partner_id=int(sys.argv[3]), asset="toman", amount=1000, product_id=None, actor=Actor("admin", 1))
db.commit()
db.close()
engine.dispose()
"""
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(BACKEND), str(tmp_path / "procs.db"), str(partner_id)])
        for _ in range(2)
    ]
    codes = [proc.wait(20) for proc in procs]
    assert codes == [0, 0]
    engine = make_engine(f"sqlite:///{tmp_path.as_posix()}/procs.db")
    check = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        assert int(check.get(Partner, partner_id).balance_toman) == 2000
        assert len(_toman_ledgers(check, "admin_adjust")) == 2
    finally:
        check.close()
        engine.dispose()


def test_reconcile_does_not_touch_a_leased_operation(tmp_path, sim):
    from app.services.money_sync import reconcile_pending

    engine = make_engine(f"sqlite:///{tmp_path.as_posix()}/recon.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    partner, product = seed(db)
    client = gateway(sim)
    sub, _, _ = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="ali01",
        traffic_cap_gb=5,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    partner_id = partner.id
    sub_id = sub.id
    before = int(partner.balance_toman)
    db.close()
    entered = threading.Event()
    release = threading.Event()

    class Hold:
        def __init__(self, inner):
            self.inner = inner

        def update_user(self, payload):
            entered.set()
            assert release.wait(5)
            return self.inner.update_user(payload)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    errors: list[str] = []

    def worker():
        session = Session()
        try:
            increase_subscription(
                session,
                sub_id=sub_id,
                partner_id=partner_id,
                traffic_cap_gb=Decimal(8),
                end_date=None,
                actor=Actor("partner", partner_id),
                gateway=Hold(client),
                now=NOW,
            )
        except Exception as exc:
            errors.append(repr(exc))
        finally:
            session.close()

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(5)
    recovery = Session()
    try:
        assert reconcile_pending(recovery, lambda _panel: gateway(sim)) == 0
        assert int(recovery.get(Partner, partner_id).balance_toman) == before - 3000
        assert _pending(recovery) is not None
    finally:
        release.set()
        recovery.close()
        thread.join(5)
    assert errors == []
    check = Session()
    try:
        assert int(check.get(Partner, partner_id).balance_toman) == before - 3000
        assert len(_toman_ledgers(check, "increase")) == 1
    finally:
        check.close()
        client.close()
        engine.dispose()
