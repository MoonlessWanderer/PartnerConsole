"""Billing against a real HTTP V2Board lab, not an in-process mock."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.errors import AppError, V2BoardError
from app.core.timeutil import format_jalali_date
from app.integrations.v2board import PanelUserNotFound, V2BoardClient
from app.models import Panel, Partner, PartnerProductCredit, Product, Subscription, WalletLedger
from app.services.billing import gb_to_bytes
from app.services.subscriptions import (
    Actor,
    create_subscription,
    decrease_subscription,
    delete_subscription,
    increase_subscription,
    reset_link,
    reset_traffic,
    set_subscription_ban,
)
from v2lab.server import ADMIN_EMAIL, ADMIN_PASSWORD, GB, SECURE_PATH

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=ZoneInfo("Asia/Tehran"))


def _http() -> httpx.Client:
    return httpx.Client(timeout=5, trust_env=False)


@pytest.fixture()
def db():
    from sqlalchemy import create_engine, event

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


@pytest.fixture()
def live(lab_url):
    _http().post(f"{lab_url}/_lab/reset", timeout=2)
    client = V2BoardClient(lab_url, SECURE_PATH, ADMIN_EMAIL, ADMIN_PASSWORD)
    yield lab_url, client
    client.close()


def seed(db, lab_url, balance=1_000_000):
    panel = Panel(
        name="آزمایش",
        base_url=lab_url,
        secure_path=SECURE_PATH,
        admin_email=ADMIN_EMAIL,
        admin_password_enc="unused",
        status="active",
    )
    product = Product(
        panel=panel,
        plan_id=7,
        name="طلایی",
        email_domain="gold.example.com",
        price_per_gb=1000,
        price_per_day=1000,
        active=True,
    )
    partner = Partner(username="shop1", password_hash="x", balance_toman=balance, banned=False)
    db.add_all([panel, product, partner])
    db.commit()
    return partner, product


def jalali(day: date) -> str:
    return format_jalali_date(day)


def lab_user(lab_url: str, user_id: int) -> dict:
    response = _http().get(f"{lab_url}/_lab/users/{user_id}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_login_rejects_non_admin_over_http(live):
    lab_url, _client = live
    guest = V2BoardClient(lab_url, SECURE_PATH, "staff@panel.test", ADMIN_PASSWORD)
    with pytest.raises(V2BoardError):
        guest.login()
    guest.close()


def test_generate_hides_id_and_our_update_overrides_plan_quota(db, live):
    lab_url, client = live
    partner, product = seed(db, live[0])
    sub, _charge, fetched = create_subscription(
        db,
        partner_id=partner.id,
        product_id=product.id,
        username="Ali01",
        traffic_cap_gb=20,
        end_date=jalali(date(2026, 6, 25)),
        note=None,
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    db.commit()
    raw = _http().post(
        f"{lab_url}/api/v1/{SECURE_PATH}/user/generate",
        headers={"Authorization": "nope"},
        json={"email_prefix": "x", "email_suffix": "gold.example.com", "plan_id": 7},
        timeout=2,
    )
    assert raw.status_code == 403
    panel_user = lab_user(lab_url, sub.v2_user_id)
    info = _http().get(
        f"{lab_url}/api/v1/{SECURE_PATH}/user/getUserInfoById",
        params={"id": sub.v2_user_id},
        headers={"Authorization": client._auth},
        timeout=2,
    ).json()["data"]
    assert "email" not in Subscription.__table__.columns
    assert sub.username == "ali01"
    assert panel_user["email"] == "ali01@gold.example.com"
    assert panel_user["transfer_enable"] == gb_to_bytes(20)
    assert panel_user["transfer_enable"] != 50 * GB
    assert panel_user["speed_limit"] == 100
    assert panel_user["device_limit"] == 2
    assert panel_user["plan_id"] == 7
    assert panel_user["group_id"] == 3
    assert "subscribe_url" not in info
    assert fetched["subscribe_url"].endswith(f"token={panel_user['token']}")
    direct = _http().get(fetched["subscribe_url"], timeout=2)
    assert direct.status_code == 200
    assert "ali01@gold.example.com" in direct.text


def test_missing_plan_id_clears_group_but_our_client_sends_it(live):
    lab_url, client = live
    client.generate_user(
        email_prefix="solo",
        email_suffix="gold.example.com",
        plan_id=7,
        expired_at=1_800_000_000,
        password="secretpass",
    )
    created = client.fetch_by_email("solo@gold.example.com")
    bare = _http().post(
        f"{lab_url}/api/v1/{SECURE_PATH}/user/update",
        headers={"Authorization": client._auth},
        json={
            "id": created["id"],
            "email": "solo@gold.example.com",
            "banned": 0,
            "is_admin": 0,
            "is_staff": 0,
            "transfer_enable": GB,
        },
        timeout=2,
    )
    assert bare.status_code == 200
    assert lab_user(lab_url, created["id"])["group_id"] is None
    client.update_user(
        {
            "id": created["id"],
            "email": "solo@gold.example.com",
            "plan_id": 7,
            "banned": 0,
            "is_admin": 0,
            "is_staff": 0,
            "transfer_enable": 4 * GB,
            "expired_at": 1_800_000_000,
            "device_limit": 2,
            "speed_limit": 100,
        }
    )
    fixed = lab_user(lab_url, created["id"])
    assert fixed["group_id"] == 3
    assert fixed["transfer_enable"] == 4 * GB


def test_expired_auth_is_renewed_against_live_server(live):
    lab_url, client = live
    client.login()
    first = client._auth
    _http().post(f"{lab_url}/_lab/expire", timeout=2)
    plans = client.fetch_plans()
    assert client._auth != first
    assert any(plan["id"] == 7 and plan["reset_traffic_method"] == 2 for plan in plans)


def test_live_reset_decrease_and_delete(db, live):
    lab_url, client = live
    partner, product = seed(db, live[0], balance=100_000)
    sub, _charge, fetched = create_subscription(
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
    old_url = fetched["subscribe_url"]
    _http().post(f"{lab_url}/_lab/users/{sub.v2_user_id}/traffic", json={"u": 4 * GB, "d": 0}, timeout=2)
    before = int(partner.balance_toman)
    result = reset_traffic(
        db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id), gateway=client
    )
    db.commit()
    assert result["cost_toman"] == 4000
    assert int(partner.balance_toman) == before - 4000
    assert lab_user(lab_url, sub.v2_user_id)["u"] == 0
    assert lab_user(lab_url, sub.v2_user_id)["d"] == 0

    _http().post(f"{lab_url}/_lab/users/{sub.v2_user_id}/traffic", json={"u": 6 * GB, "d": 0}, timeout=2)
    with pytest.raises(AppError, match="مصرف فعلی"):
        decrease_subscription(
            db,
            sub_id=sub.id,
            partner_id=partner.id,
            traffic_cap_gb=Decimal(5),
            end_date=None,
            actor=Actor("partner", partner.id),
            gateway=client,
            now=NOW,
        )
    db.rollback()
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
    db.commit()
    assert lab_user(lab_url, sub.v2_user_id)["transfer_enable"] == gb_to_bytes(15)
    credit = db.scalar(select(PartnerProductCredit))
    assert credit.credit_gb == 5

    renewed = reset_link(db, sub_id=sub.id, partner_id=partner.id, gateway=client)
    assert renewed["subscribe_url"] != old_url
    assert _http().get(renewed["subscribe_url"], timeout=2).status_code == 200

    set_subscription_ban(
        db, sub_id=sub.id, partner_id=partner.id, banned=True, actor=Actor("partner", partner.id), gateway=client
    )
    db.commit()
    assert lab_user(lab_url, sub.v2_user_id)["banned"] == 1

    balance_before_delete = int(partner.balance_toman)
    _http().post(f"{lab_url}/_lab/users/{sub.v2_user_id}/traffic", json={"u": 2 * GB, "d": 0}, timeout=2)
    deleted = delete_subscription(
        db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id), gateway=client, now=NOW
    )
    db.commit()
    assert deleted["gb_refund"] == "13"
    assert int(partner.balance_toman) == balance_before_delete
    assert _http().get(f"{lab_url}/_lab/users/{sub.v2_user_id}", timeout=2).status_code == 500
    assert db.scalar(select(Subscription.id)) is None
    ledger = db.scalar(select(WalletLedger).where(WalletLedger.reason == "delete_refund", WalletLedger.asset == "gb"))
    assert ledger.subscription_id is None


def test_live_increase_uses_expiry_as_base(db, live):
    _lab_url, client = live
    partner, product = seed(db, live[0])
    sub, _, _ = create_subscription(
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
    db.commit()
    _sub, charge = increase_subscription(
        db,
        sub_id=sub.id,
        partner_id=partner.id,
        traffic_cap_gb=None,
        end_date=jalali(date(2026, 6, 27)),
        actor=Actor("partner", partner.id),
        gateway=client,
        now=NOW,
    )
    db.commit()
    assert charge.need_days == 2
    assert charge.toman == 2000
    assert lab_user(client.base_url, sub.v2_user_id)["plan_id"] == 7


def _generate(client, prefix="ali", suffix="gold.example.com") -> dict:
    client.generate_user(
        email_prefix=prefix,
        email_suffix=suffix,
        plan_id=7,
        expired_at=1_800_000_000,
        password="secretpass",
    )
    return client.fetch_by_email(f"{prefix}@{suffix}")


def _update_payload(user: dict, **overrides) -> dict:
    payload = {
        "id": user["id"],
        "email": user["email"],
        "plan_id": 7,
        "banned": 0,
        "is_admin": 0,
        "is_staff": 0,
        "transfer_enable": 4 * GB,
        "expired_at": 1_800_000_000,
    }
    payload.update(overrides)
    return payload


def test_dot_local_domain_is_accepted_like_upstream_email_strict(live):
    _lab_url, client = live
    created = _generate(client, suffix="gold.local")
    client.update_user(_update_payload(created))
    assert client.get_user_info(created["id"])["transfer_enable"] == 4 * GB


def test_update_validation_is_unclassified_422(live):
    lab_url, client = live
    created = _generate(client)
    with pytest.raises(V2BoardError) as exc:
        client.update_user(_update_payload(created, email="ali..x@gold.example.com"))
    assert str(exc.value) == "خطای پنل (422)"
    assert lab_user(lab_url, created["id"])["email"] == "ali@gold.example.com"


def test_user_info_returns_raw_model_and_generic_500_for_unknown_id(live):
    lab_url, client = live
    created = _generate(client)
    assert {"password", "token", "uuid"} <= client.get_user_info(created["id"]).keys()
    with pytest.raises(V2BoardError) as exc:
        client.get_user_info(999)
    assert not isinstance(exc.value, PanelUserNotFound)
    raw = _http().get(
        f"{lab_url}/api/v1/{SECURE_PATH}/user/getUserInfoById",
        params={"id": 999},
        headers={"Authorization": client._auth},
    )
    assert raw.status_code == 500
    assert "用户不存在" not in raw.text


def test_every_update_clears_the_inviter(live):
    lab_url, client = live
    inviter = _generate(client, prefix="boss")
    created = _generate(client)
    _http().post(f"{lab_url}/_lab/users/{created['id']}/inviter", json={"invite_user_id": inviter["id"]})
    assert client.get_user_info(created["id"])["invite_user"]["id"] == inviter["id"]
    client.update_user(_update_payload(created))
    assert lab_user(lab_url, created["id"])["invite_user_id"] is None


def test_wrong_admin_password_locks_login_after_five_attempts(live):
    lab_url, _client = live
    for _ in range(5):
        with V2BoardClient(lab_url, SECURE_PATH, ADMIN_EMAIL, "wrong") as bad, pytest.raises(V2BoardError):
            bad.login()
    with V2BoardClient(lab_url, SECURE_PATH, ADMIN_EMAIL, ADMIN_PASSWORD) as good, pytest.raises(V2BoardError):
        good.login()


def test_every_explicit_login_adds_a_session_that_is_never_pruned(live):
    lab_url, _client = live
    for _ in range(3):
        with V2BoardClient(lab_url, SECURE_PATH, ADMIN_EMAIL, ADMIN_PASSWORD) as fresh:
            fresh.login()
    assert _http().get(f"{lab_url}/_lab/sessions").json()["total"] == 3
