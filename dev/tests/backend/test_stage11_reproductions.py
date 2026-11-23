"""Stage 11 reproductions of operational risks R1-R4.

Each test failed against the pre-Stage-11 code (R1/R1b/R2 were xfail(strict)
reproductions, R3/R4 counted sessions/logins) and asserts the fixed behavior.
The guard tests cover behavior that must not change.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.core.errors import AppError, V2BoardError
from app.integrations.v2board import V2BoardClient
from app.models import PanelOperation, Subscription
from app.services.money_sync import reconcile_pending
from app.services.subscriptions import (
    Actor,
    create_subscription,
    increase_subscription,
    reset_traffic,
    set_subscription_ban,
)
from test_billing import GB, NOW, PanelSim, _Cut, _toman_ledgers, gateway, jalali, seed
from test_live_v2board import _http, lab_user
from test_live_v2board import seed as lab_seed
from v2lab.server import ADMIN_EMAIL, ADMIN_PASSWORD, SECURE_PATH


@pytest.fixture()
def sim():
    return PanelSim()


@pytest.fixture()
def db():
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.core.database import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

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


def _make_sub(db, client, *, cap=20, balance=50_000, price_gb=100):
    partner, product = seed(db, balance=balance, price_gb=price_gb)
    sub, _, _ = create_subscription(
        db, partner_id=partner.id, product_id=product.id, username="ali01", traffic_cap_gb=cap,
        end_date=jalali(date(2026, 6, 25)), note=None, actor=Actor("partner", partner.id),
        gateway=client, now=NOW,
    )
    db.commit()
    return partner, product, sub


def _reset_op(db) -> PanelOperation:
    return db.scalar(select(PanelOperation).where(PanelOperation.kind == "reset_traffic"))


# ---------------------------------------------------------------- R2: reset with a lost/ambiguous result

class _ApplyThenFail:
    """Panel applies the reset, new usage arrives, then the acknowledgement is lost."""

    def __init__(self, inner, sim, user_id, *, new_usage: int, error: BaseException):
        self.inner, self.sim, self.user_id = inner, sim, user_id
        self.new_usage, self.error = new_usage, error

    def update_user(self, payload):
        self.inner.update_user(payload)
        self.sim.users[self.user_id]["u"] = self.new_usage
        raise self.error

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_r2_timeout_after_reset_then_new_usage_is_not_reset_again(db, sim):
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    sim.users[sub.v2_user_id]["u"] = 10 * GB
    before = int(partner.balance_toman)
    cut = _ApplyThenFail(client, sim, sub.v2_user_id, new_usage=3 * GB,
                         error=httpx.ReadTimeout("lost response"))
    with pytest.raises(AppError):
        reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id), gateway=cut)
    db.rollback()
    assert _reset_op(db).status == "pending"

    reconcile_pending(db, lambda _panel: gateway(sim))
    db.refresh(partner)
    assert int(partner.balance_toman) == before - 1000
    assert len(_toman_ledgers(db, "reset_traffic")) == 1
    assert sim.users[sub.v2_user_id]["u"] == 3 * GB  # was 0: the reset was sent twice
    # Lower usage does not prove our reset ran (admin or plan resets look the same): held.
    assert _reset_op(db).status == "attention"


def test_r2_uncertain_error_after_reset_then_new_usage_is_not_refunded(db, sim):
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    sim.users[sub.v2_user_id]["u"] = 10 * GB
    before = int(partner.balance_toman)
    cut = _ApplyThenFail(client, sim, sub.v2_user_id, new_usage=3 * GB,
                         error=V2BoardError("پنل عملیات را تأیید نکرد"))
    try:
        reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id), gateway=cut)
    except AppError:
        db.rollback()
    db.refresh(partner)
    assert sim.users[sub.v2_user_id]["u"] == 3 * GB  # the reset did happen on the panel
    assert int(partner.balance_toman) == before - 1000  # was refunded to `before`
    assert len(_toman_ledgers(db, "reset_traffic")) == 1
    assert _reset_op(db).status == "attention"  # 3GB < 10GB is not proof it was our reset


def test_r2_guard_save_failed_reset_is_held_not_refunded(db, sim):
    # 保存失败 comes from the UPDATE itself and can follow a commit: it proves nothing.
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    sim.users[sub.v2_user_id]["u"] = 10 * GB
    before = int(partner.balance_toman)
    sim.fail_update = True
    with pytest.raises(AppError):
        reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id),
                      gateway=client)
    db.rollback()
    db.refresh(partner)
    assert sim.users[sub.v2_user_id]["u"] == 10 * GB
    assert int(partner.balance_toman) == before - 1000
    assert _reset_op(db).status == "attention"


def test_r2_guard_reset_refused_before_the_update_is_refunded(db, sim, monkeypatch):
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    sim.users[sub.v2_user_id]["u"] = 10 * GB
    before = int(partner.balance_toman)

    def refuse(_payload):
        error = V2BoardError("用户不存在")
        error.panel_rejected = True
        raise error

    monkeypatch.setattr(client, "update_user", refuse)
    with pytest.raises(AppError):
        reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id),
                      gateway=client)
    db.rollback()
    db.refresh(partner)
    assert sim.users[sub.v2_user_id]["u"] == 10 * GB
    assert int(partner.balance_toman) == before
    assert _reset_op(db).status == "failed"


def test_r2_guard_reset_lost_before_delivery_is_applied_once_on_reconcile(db, sim):
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    sim.users[sub.v2_user_id]["u"] = 10 * GB
    before = int(partner.balance_toman)
    cut = _Cut(client, "update_user", 1, applied=False)
    with pytest.raises(AppError):
        reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id), gateway=cut)
    db.rollback()
    assert sim.users[sub.v2_user_id]["u"] == 10 * GB
    data = json.loads(_reset_op(db).payload)
    assert (data["reset_sent"], data["raw_before"]) == (False, 10 * GB)
    assert reconcile_pending(db, lambda _panel: gateway(sim)) == 1
    db.refresh(partner)
    assert sim.users[sub.v2_user_id]["u"] == 0
    assert int(partner.balance_toman) == before - 1000
    assert len(_toman_ledgers(db, "reset_traffic")) == 1


# ---------------------------------------------------------------- R1: data PartnerPanel does not own

@pytest.fixture()
def live(lab_url):
    _http().post(f"{lab_url}/_lab/reset", timeout=2)
    client = V2BoardClient(lab_url, SECURE_PATH, ADMIN_EMAIL, ADMIN_PASSWORD)
    yield lab_url, client
    client.close()


def _lab_sub(db, lab_url, client):
    partner, product = lab_seed(db, lab_url)
    sub, _, _ = create_subscription(
        db, partner_id=partner.id, product_id=product.id, username="ali01", traffic_cap_gb=5,
        end_date=jalali(date(2026, 6, 25)), note=None, actor=Actor("partner", partner.id),
        gateway=client, now=NOW,
    )
    db.commit()
    client.generate_user(email_prefix="boss", email_suffix="gold.example.com", plan_id=7,
                         expired_at=1_800_000_000, password="secretpass")
    inviter = client.fetch_by_email("boss@gold.example.com")
    _http().post(f"{lab_url}/_lab/users/{sub.v2_user_id}/inviter", json={"invite_user_id": inviter["id"]})
    return partner, sub, inviter["id"]


def test_r1_increase_keeps_the_panel_inviter(db, live):
    lab_url, client = live
    partner, sub, inviter_id = _lab_sub(db, lab_url, client)
    increase_subscription(db, sub_id=sub.id, partner_id=partner.id, traffic_cap_gb=Decimal(6), end_date=None,
                          actor=Actor("partner", partner.id), gateway=client, now=NOW)
    db.commit()
    assert lab_user(lab_url, sub.v2_user_id)["invite_user_id"] == inviter_id


def test_r1_ban_keeps_the_panel_inviter(db, live):
    lab_url, client = live
    partner, sub, inviter_id = _lab_sub(db, lab_url, client)
    set_subscription_ban(db, sub_id=sub.id, partner_id=partner.id, banned=True,
                         actor=Actor("partner", partner.id), gateway=client)
    db.commit()
    assert lab_user(lab_url, sub.v2_user_id)["invite_user_id"] == inviter_id


def test_r1_documents_plan_owned_limits_overwrite_panel_edits(db, live):
    lab_url, client = live
    partner, sub, _inviter = _lab_sub(db, lab_url, client)
    info = client.get_user_info(sub.v2_user_id)
    client.update_user({"id": sub.v2_user_id, "email": info["email"], "plan_id": 7, "banned": 0, "is_admin": 0,
                        "is_staff": 0, "transfer_enable": info["transfer_enable"],
                        "expired_at": info["expired_at"], "device_limit": 9, "speed_limit": 7})
    increase_subscription(db, sub_id=sub.id, partner_id=partner.id, traffic_cap_gb=Decimal(6), end_date=None,
                          actor=Actor("partner", partner.id), gateway=client, now=NOW)
    db.commit()
    row = lab_user(lab_url, sub.v2_user_id)
    assert (row["device_limit"], row["speed_limit"]) == (2, 100)  # plan values win today


def test_r1b_create_does_not_take_over_a_foreign_panel_user(db, live):
    lab_url, client = live
    partner, product = lab_seed(db, lab_url)
    client.generate_user(email_prefix="ali01", email_suffix="gold.example.com", plan_id=8,
                         expired_at=1_900_000_000, password="secretpass")
    foreign = client.fetch_by_email("ali01@gold.example.com")
    before = int(partner.balance_toman)
    with pytest.raises(AppError):
        create_subscription(
            db, partner_id=partner.id, product_id=product.id, username="ali01", traffic_cap_gb=5,
            end_date=jalali(date(2026, 6, 25)), note=None, actor=Actor("partner", partner.id),
            gateway=client, now=NOW,
        )
    db.rollback()
    db.refresh(partner)
    row = lab_user(lab_url, foreign["id"])
    assert (row["plan_id"], row["expired_at"]) == (8, 1_900_000_000)
    assert int(partner.balance_toman) == before
    assert db.scalar(select(Subscription.id)) is None


# ---------------------------------------------------------------- R3: one admin session per operation

def test_r3_consecutive_operations_reuse_one_admin_session(live):
    lab_url, _client = live
    for _ in range(3):
        with V2BoardClient(lab_url, SECURE_PATH, ADMIN_EMAIL, ADMIN_PASSWORD) as fresh:
            fresh.fetch_plans()
    assert _http().get(f"{lab_url}/_lab/sessions").json()["total"] == 1


# ---------------------------------------------------------------- R4: repeated logins with bad credentials

class _CountingSim(PanelSim):
    def __init__(self):
        super().__init__()
        self.logins = 0
        self.reject_logins = False

    def handler(self, request):
        if request.url.path.endswith("/passport/auth/login"):
            self.logins += 1
            if self.reject_logins:
                return httpx.Response(500, json={"message": "邮箱或密码错误"})
        elif self.reject_logins:
            # The panel password changed: existing sessions are gone as well.
            return httpx.Response(403, json={"message": "未登录或登陆已过期"})
        return super().handler(request)


def test_r4_reconcile_stops_after_one_rejected_login(db):
    sim = _CountingSim()
    client = gateway(sim)
    partner, product = seed(db)
    subs = []
    for name in ("ali01", "ali02", "ali03"):
        sub, _, _ = create_subscription(
            db, partner_id=partner.id, product_id=product.id, username=name, traffic_cap_gb=5,
            end_date=jalali(date(2026, 6, 25)), note=None, actor=Actor("partner", partner.id),
            gateway=client, now=NOW,
        )
        db.commit()
        cut = _Cut(client, "update_user", 1, applied=False)
        with pytest.raises(AppError):
            increase_subscription(db, sub_id=sub.id, partner_id=partner.id, traffic_cap_gb=Decimal(8),
                                  end_date=None, actor=Actor("partner", partner.id), gateway=cut, now=NOW)
        db.rollback()
        subs.append(sub)
    balance = int(partner.balance_toman)
    sim.reject_logins = True
    sim.logins = 0
    assert reconcile_pending(db, lambda _panel: gateway(sim)) == 0
    db.refresh(partner)
    assert int(partner.balance_toman) == balance
    pending = db.scalars(select(PanelOperation).where(PanelOperation.status == "pending")).all()
    assert len(pending) == 3
    assert all(json.loads(op.payload).get("panel_applied") is not True for op in pending)
    assert sim.logins == 1  # was 6 before R4: two per pending operation
