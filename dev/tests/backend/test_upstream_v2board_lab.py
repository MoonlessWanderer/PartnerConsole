"""Contract checks against a real upstream wyx2685/v2board instance (not a simulator).

Opt-in: V2BOARD_UPSTREAM_LAB=<path to lab json> with base_url, secure_path, admin_email,
admin_password and plan_id. Only loopback panels are accepted. Tests that send mutating
requests additionally require V2BOARD_UPSTREAM_LAB_MUTATE=1.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base
from app.core.errors import AppError, PanelCredentialsRejected, V2BoardError
from app.core.outbound import PanelTransport
from app.core.timeutil import format_jalali_date, now_tehran
from app.integrations.v2board import PanelUserNotFound, V2BoardClient
from app.models import Panel, PanelOperation, Partner, Product, Subscription, WalletLedger
from app.services.billing import gb_to_bytes
from app.services.money_sync import reconcile_pending, resolve_attention
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

LAB_FILE = os.environ.get("V2BOARD_UPSTREAM_LAB", "")
MUTATE = os.environ.get("V2BOARD_UPSTREAM_LAB_MUTATE") == "1"
EXPIRED = "未登录或登陆已过期"

pytestmark = pytest.mark.skipif(not LAB_FILE, reason="set V2BOARD_UPSTREAM_LAB to run against a real V2Board")
mutating = pytest.mark.skipif(not MUTATE, reason="set V2BOARD_UPSTREAM_LAB_MUTATE=1 to send mutating requests")


@pytest.fixture(scope="module")
def lab() -> dict:
    config = json.loads(Path(LAB_FILE).read_text(encoding="utf-8"))
    if urlsplit(config["base_url"]).hostname != "127.0.0.1":
        pytest.fail("upstream lab must be a loopback panel")
    return config


@pytest.fixture(autouse=True)
def allow_lab_origin(lab, monkeypatch):
    monkeypatch.setattr(settings, "panel_private_origins", lab["base_url"])


@pytest.fixture()
def client(lab):
    with V2BoardClient(lab["base_url"], lab["secure_path"], lab["admin_email"], lab["admin_password"]) as c:
        yield c


def _raw(lab, method: str, path: str, auth: str | None, **kwargs) -> httpx.Response:
    headers = {"Authorization": auth} if auth else {}
    with httpx.Client(timeout=15, trust_env=False) as http:
        return http.request(method, f"{lab['base_url']}/api/v1/{path}", headers=headers, **kwargs)


def _admin(lab, method: str, path: str, auth: str | None, **kwargs) -> httpx.Response:
    return _raw(lab, method, f"{lab['secure_path']}/{path}", auth, **kwargs)


def test_login_and_plan_contract(client, lab):
    assert client.login()
    plan = client.get_plan(lab["plan_id"])
    assert {"id", "transfer_enable", "device_limit", "speed_limit"} <= plan.keys()


def test_fetch_by_email_returns_subscribe_url(client, lab):
    row = client.fetch_by_email(lab["admin_email"])
    assert row["email"] == lab["admin_email"]
    assert row["subscribe_url"].startswith(lab["base_url"])
    assert row["subscribe_url"].endswith(f"token={row['token']}")
    assert not row.get("subscribe_url_fallback")


def test_validated_empty_fetch_proves_absence(client):
    with pytest.raises(PanelUserNotFound):
        client.fetch_by_email("nobody-pp-lab@v2board-lab.test")
    with pytest.raises(PanelUserNotFound):
        client.fetch_by_id(2_000_000_000)


def test_user_info_exposes_credentials_the_simulator_strips(client, lab):
    admin = client.fetch_by_email(lab["admin_email"])
    info = client.get_user_info(admin["id"])
    assert {"u", "d", "transfer_enable", "expired_at"} <= info.keys()
    # Upstream User has no $hidden: hash, token and uuid come back from getUserInfoById.
    assert {"password", "token", "uuid"} <= info.keys()


def test_user_info_for_missing_user_is_generic_500_not_absence(client, lab):
    auth = client.login()
    response = _admin(lab, "GET", "user/getUserInfoById", auth, params={"id": 2_000_000_000})
    assert response.status_code == 500
    assert "用户不存在" not in response.text
    with pytest.raises(V2BoardError) as exc:
        client.get_user_info(2_000_000_000)
    assert not isinstance(exc.value, PanelUserNotFound)


def test_invalid_auth_is_expired_403_and_client_relogs(client, lab):
    response = _admin(lab, "GET", "plan/fetch", "not-a-token")
    assert response.status_code == 403
    assert response.json()["message"] == EXPIRED
    client._auth = "not-a-token"
    assert client.fetch_plans()
    assert client._auth != "not-a-token"


def _sessions(lab, auth: str) -> int:
    response = _raw(lab, "GET", "user/getActiveSession", auth)
    assert response.status_code == 200
    return len(response.json()["data"])


def test_every_login_adds_a_persistent_admin_session(lab):
    with V2BoardClient(lab["base_url"], lab["secure_path"], lab["admin_email"], lab["admin_password"]) as probe:
        before = _sessions(lab, probe.login())
        after = _sessions(lab, probe.login())
    assert after == before + 1


def test_operations_reuse_one_admin_session(lab):
    with V2BoardClient(lab["base_url"], lab["secure_path"], lab["admin_email"], lab["admin_password"]) as probe:
        before = _sessions(lab, probe.login())
    for _ in range(5):
        with V2BoardClient(lab["base_url"], lab["secure_path"], lab["admin_email"], lab["admin_password"]) as c:
            c.fetch_plans()
            reused = c._auth
    assert _sessions(lab, reused) == before


# ---- Mutating checks: dedicated pp-lab.test users only, removed afterwards. ----

DOMAIN = "pp-lab.test"


class LoseResponse(httpx.BaseTransport):
    """Delivers the first matching request to the panel, then drops its response."""

    def __init__(self, base_url: str, path_suffix: str):
        self.inner = PanelTransport(base_url)
        self.path_suffix = path_suffix
        self.armed = True

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.armed and request.url.path.endswith(self.path_suffix):
            self.armed = False
            response = self.inner.handle_request(request)
            response.read()
            response.close()
            raise httpx.ReadTimeout("lost response", request=request)
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()


@pytest.fixture()
def db():
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
def cleanup(lab):
    emails: list[str] = []
    yield emails
    with V2BoardClient(lab["base_url"], lab["secure_path"], lab["admin_email"], lab["admin_password"]) as c:
        for email in emails:
            try:
                c.delete_user(c.fetch_by_email(email)["id"])
            except PanelUserNotFound:
                pass


def _client(lab, transport: httpx.BaseTransport | None = None) -> V2BoardClient:
    return V2BoardClient(lab["base_url"], lab["secure_path"], lab["admin_email"], lab["admin_password"],
                         transport=transport)


def _seed(db, lab, balance=1_000_000):
    panel = Panel(name="lab", base_url=lab["base_url"], secure_path=lab["secure_path"],
                  admin_email=lab["admin_email"], admin_password_enc="unused", status="active")
    product = Product(panel=panel, plan_id=lab["plan_id"], name="lab", email_domain=DOMAIN,
                      price_per_gb=1000, price_per_day=1000, active=True)
    partner = Partner(username="labshop", password_hash="x", balance_toman=balance, banned=False)
    db.add_all([panel, product, partner])
    db.commit()
    return partner, product


def _username(cleanup) -> str:
    name = "pp" + secrets.token_hex(4)
    cleanup.append(f"{name}@{DOMAIN}")
    return name


def _end(days: int) -> str:
    return format_jalali_date((now_tehran() + timedelta(days=days)).date())


def _create(db, partner, product, gateway, username, cap=20):
    sub, charge, fetched = create_subscription(
        db, partner_id=partner.id, product_id=product.id, username=username, traffic_cap_gb=cap,
        end_date=_end(10), note=None, actor=Actor("partner", partner.id), gateway=gateway,
    )
    db.commit()
    return sub, charge, fetched


def _set_traffic(client: V2BoardClient, user_id: int, u: int) -> None:
    info = client.get_user_info(user_id)
    payload = {
        "id": user_id, "email": info["email"], "plan_id": info["plan_id"], "banned": info["banned"],
        "is_admin": 0, "is_staff": 0, "transfer_enable": info["transfer_enable"],
        "expired_at": info["expired_at"], "device_limit": info["device_limit"],
        "speed_limit": info["speed_limit"], "u": u, "d": 0,
    }
    if isinstance(info.get("invite_user"), dict):
        payload["invite_user_email"] = info["invite_user"]["email"]
    client.update_user(payload)


def _ledgers(db, reason: str) -> list[WalletLedger]:
    return list(db.scalars(select(WalletLedger).where(WalletLedger.asset == "toman", WalletLedger.reason == reason)))


@mutating
def test_full_lifecycle_against_upstream(db, lab, client, cleanup):
    partner, product = _seed(db, lab)
    username = _username(cleanup)
    sub, charge, fetched = _create(db, partner, product, client, username)
    info = client.get_user_info(sub.v2_user_id)
    plan = client.get_plan(lab["plan_id"])
    assert info["email"] == f"{username}@{DOMAIN}"
    assert int(info["transfer_enable"]) == gb_to_bytes(20)
    assert int(info["plan_id"]) == lab["plan_id"]
    assert info["group_id"] == plan["group_id"]
    assert info["device_limit"] == plan["device_limit"]
    assert fetched["subscribe_url"].endswith(f"token={info['token']}")
    assert int(partner.balance_toman) == 1_000_000 - charge.toman

    increase_subscription(db, sub_id=sub.id, partner_id=partner.id, traffic_cap_gb=Decimal(25), end_date=None,
                          actor=Actor("partner", partner.id), gateway=client)
    db.commit()
    assert int(client.get_user_info(sub.v2_user_id)["transfer_enable"]) == gb_to_bytes(25)

    _set_traffic(client, sub.v2_user_id, 3 * 1073741824)
    before = int(partner.balance_toman)
    result = reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id),
                           gateway=client)
    db.commit()
    after_reset = client.get_user_info(sub.v2_user_id)
    assert result["cost_toman"] == 3000
    assert int(partner.balance_toman) == before - 3000
    assert int(after_reset["u"]) == 0 and int(after_reset["d"]) == 0

    decrease_subscription(db, sub_id=sub.id, partner_id=partner.id, traffic_cap_gb=Decimal(15), end_date=None,
                          actor=Actor("partner", partner.id), gateway=client)
    db.commit()
    assert int(client.get_user_info(sub.v2_user_id)["transfer_enable"]) == gb_to_bytes(15)

    renewed = reset_link(db, sub_id=sub.id, partner_id=partner.id, gateway=client)
    db.commit()
    assert renewed["subscribe_url"] != fetched["subscribe_url"]

    set_subscription_ban(db, sub_id=sub.id, partner_id=partner.id, banned=True,
                         actor=Actor("partner", partner.id), gateway=client)
    db.commit()
    assert int(client.get_user_info(sub.v2_user_id)["banned"]) == 1

    delete_subscription(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id),
                        gateway=client)
    db.commit()
    with pytest.raises(PanelUserNotFound):
        client.fetch_by_id(sub.v2_user_id)
    assert db.scalar(select(Subscription.id)) is None


@mutating
def test_duplicate_generate_is_classified_as_existing(lab, client, cleanup):
    username = _username(cleanup)
    kwargs = dict(email_prefix=username, email_suffix=DOMAIN, plan_id=lab["plan_id"],
                  expired_at=2_000_000_000, password="pp-lab-pass-1")
    client.generate_user(**kwargs)
    with pytest.raises(V2BoardError, match="کاربر قبلاً ثبت شده است"):
        client.generate_user(**kwargs)
    assert len(client._fetch("email", f"{username}@{DOMAIN}")) == 1


@mutating
def test_update_validation_is_422_and_changes_nothing(lab, client, cleanup):
    username = _username(cleanup)
    client.generate_user(email_prefix=username, email_suffix=DOMAIN, plan_id=lab["plan_id"],
                         expired_at=2_000_000_000, password="pp-lab-pass-1")
    user = client.fetch_by_email(f"{username}@{DOMAIN}")
    payload = {"id": user["id"], "email": f"{username}..x@{DOMAIN}", "plan_id": lab["plan_id"],
               "banned": 0, "is_admin": 0, "is_staff": 0, "transfer_enable": 1073741824}
    response = _admin(lab, "POST", "user/update", client._auth, json=payload)
    assert response.status_code == 422
    assert response.json()["message"] == "The given data was invalid."
    assert response.json()["errors"]["email"] == ["邮箱格式不正确"]
    with pytest.raises(V2BoardError) as exc:
        client.update_user(payload)
    assert str(exc.value) == "خطای پنل (422)"
    assert exc.value.panel_rejected is True
    assert client.get_user_info(user["id"])["email"] == f"{username}@{DOMAIN}"


@mutating
def test_dot_local_email_passes_upstream_update(lab, client, cleanup):
    username = "pp" + secrets.token_hex(4)
    cleanup.append(f"{username}@gold.local")
    client.generate_user(email_prefix=username, email_suffix="gold.local", plan_id=lab["plan_id"],
                         expired_at=2_000_000_000, password="pp-lab-pass-1")
    user = client.fetch_by_email(f"{username}@gold.local")
    client.update_user({"id": user["id"], "email": user["email"], "plan_id": lab["plan_id"], "banned": 0,
                        "is_admin": 0, "is_staff": 0, "transfer_enable": 2 * 1073741824})
    assert int(client.get_user_info(user["id"])["transfer_enable"]) == 2 * 1073741824


@mutating
def test_app_updates_keep_the_panel_inviter(db, lab, client, cleanup):
    partner, product = _seed(db, lab)
    inviter = _username(cleanup)
    client.generate_user(email_prefix=inviter, email_suffix=DOMAIN, plan_id=lab["plan_id"],
                         expired_at=2_000_000_000, password="pp-lab-pass-1")
    inviter_id = client.fetch_by_email(f"{inviter}@{DOMAIN}")["id"]
    sub, _charge, _fetched = _create(db, partner, product, client, _username(cleanup), cap=5)
    info = client.get_user_info(sub.v2_user_id)
    client.update_user({"id": sub.v2_user_id, "email": info["email"], "plan_id": info["plan_id"], "banned": 0,
                        "is_admin": 0, "is_staff": 0, "transfer_enable": info["transfer_enable"],
                        "expired_at": info["expired_at"], "invite_user_email": f"{inviter}@{DOMAIN}"})
    assert client.get_user_info(sub.v2_user_id)["invite_user_id"] == inviter_id
    actor = Actor("partner", partner.id)
    increase_subscription(db, sub_id=sub.id, partner_id=partner.id, traffic_cap_gb=Decimal(6), end_date=None,
                          actor=actor, gateway=client)
    db.commit()
    assert client.get_user_info(sub.v2_user_id)["invite_user_id"] == inviter_id
    _set_traffic(client, sub.v2_user_id, 1073741824)
    assert client.get_user_info(sub.v2_user_id)["invite_user_id"] == inviter_id
    reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=actor, gateway=client)
    db.commit()
    assert client.get_user_info(sub.v2_user_id)["invite_user_id"] == inviter_id
    for banned in (True, False):
        set_subscription_ban(db, sub_id=sub.id, partner_id=partner.id, banned=banned, actor=actor, gateway=client)
        db.commit()
        assert client.get_user_info(sub.v2_user_id)["invite_user_id"] == inviter_id
    info = client.get_user_info(sub.v2_user_id)
    assert info["invite_user_id"] == inviter_id
    assert int(info["u"]) == 0 and int(info["banned"]) == 0


@mutating
def test_create_refuses_a_panel_user_it_did_not_make(db, lab, client, cleanup):
    partner, product = _seed(db, lab)
    username = _username(cleanup)
    client.generate_user(email_prefix=username, email_suffix=DOMAIN, plan_id=lab["plan_id"],
                         expired_at=2_000_000_000, password="pp-lab-pass-1")
    foreign = client.get_user_info(client.fetch_by_email(f"{username}@{DOMAIN}")["id"])
    with pytest.raises(AppError) as exc:
        _create(db, partner, product, client, username, cap=3)
    assert exc.value.status_code == 409
    db.rollback()
    db.refresh(partner)
    after = client.get_user_info(foreign["id"])
    assert (after["transfer_enable"], after["expired_at"], after["token"]) == (
        foreign["transfer_enable"], foreign["expired_at"], foreign["token"])
    assert int(partner.balance_toman) == 1_000_000
    assert db.scalar(select(Subscription.id)) is None


class DropRequest(httpx.BaseTransport):
    """Swallows the first matching request without delivering it and reports a timeout."""

    def __init__(self, base_url: str, path_suffix: str):
        self.inner = PanelTransport(base_url)
        self.path_suffix = path_suffix
        self.armed = True

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.armed and request.url.path.endswith(self.path_suffix):
            self.armed = False
            raise httpx.ReadTimeout("lost response", request=request)
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()


@mutating
def test_unproven_reset_waits_for_admin_then_resends_once(db, lab, client, cleanup):
    partner, product = _seed(db, lab)
    sub, _charge, _fetched = _create(db, partner, product, client, _username(cleanup), cap=5)
    _set_traffic(client, sub.v2_user_id, 1073741824)
    before = int(partner.balance_toman)
    with _client(lab, DropRequest(lab["base_url"], "/user/update")) as cut, pytest.raises(AppError):
        reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id), gateway=cut)
    db.rollback()
    _set_traffic(client, sub.v2_user_id, 2 * 1073741824)
    assert reconcile_pending(db, lambda _panel: _client(lab)) == 0
    op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "reset_traffic"))
    assert op.status == "attention"
    assert int(client.get_user_info(sub.v2_user_id)["u"]) == 2 * 1073741824
    db.refresh(partner)
    assert int(partner.balance_toman) == before - 1000

    resolve_attention(db, op.id, decision="retry", actor=Actor("admin", 1), note="پنل بررسی شد",
                      gateway_factory=lambda _panel: _client(lab))
    assert reconcile_pending(db, lambda _panel: _client(lab)) == 1
    info = client.get_user_info(sub.v2_user_id)
    assert int(info["u"]) == 0 and int(info["d"]) == 0
    db.refresh(partner)
    assert int(partner.balance_toman) == before - 1000
    assert len(_ledgers(db, "reset_traffic")) == 1


@mutating
def test_lost_reset_while_the_panel_admin_resets_by_hand_is_held(db, lab, client, cleanup):
    partner, product = _seed(db, lab)
    sub, _charge, _fetched = _create(db, partner, product, client, _username(cleanup), cap=5)
    _set_traffic(client, sub.v2_user_id, 1073741824)
    with _client(lab, DropRequest(lab["base_url"], "/user/update")) as cut, pytest.raises(AppError):
        reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id), gateway=cut)
    db.rollback()
    db.refresh(partner)
    charged = int(partner.balance_toman)
    _set_traffic(client, sub.v2_user_id, 0)  # the panel admin's own reset
    sent: list[str] = []
    assert reconcile_pending(db, lambda _panel: _client(lab, CountMutations(lab["base_url"], sent))) == 0
    assert sent == []
    op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "reset_traffic"))
    assert op.status == "attention"
    db.refresh(partner)
    assert int(partner.balance_toman) == charged and _ledgers(db, "operation_rollback") == []


@mutating
def test_undelivered_increase_waits_for_admin_then_is_sent_once(db, lab, client, cleanup):
    partner, product = _seed(db, lab)
    sub, _charge, _fetched = _create(db, partner, product, client, _username(cleanup), cap=5)
    with _client(lab, DropRequest(lab["base_url"], "/user/update")) as cut, pytest.raises(AppError):
        increase_subscription(db, sub_id=sub.id, partner_id=partner.id, traffic_cap_gb=Decimal(8), end_date=None,
                              actor=Actor("partner", partner.id), gateway=cut)
    db.rollback()
    db.refresh(partner)
    charged = int(partner.balance_toman)
    sent: list[str] = []
    assert reconcile_pending(db, lambda _panel: _client(lab, CountMutations(lab["base_url"], sent))) == 0
    op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "increase"))
    assert op.status == "attention" and sent == []
    assert int(client.get_user_info(sub.v2_user_id)["transfer_enable"]) == gb_to_bytes(5)
    resolve_attention(db, op.id, decision="retry", actor=Actor("admin", 1), note="پنل و تغییر هم‌زمان بررسی شد",
                      gateway_factory=lambda _panel: _client(lab))
    assert reconcile_pending(db, lambda _panel: _client(lab, CountMutations(lab["base_url"], sent))) == 1
    assert sent == ["update"]
    assert int(client.get_user_info(sub.v2_user_id)["transfer_enable"]) == gb_to_bytes(8)
    db.refresh(partner)
    assert int(partner.balance_toman) == charged and _ledgers(db, "operation_rollback") == []
    assert len(_ledgers(db, "increase")) == 1


@mutating
def test_lost_generate_response_recovers_without_duplicate(db, lab, cleanup):
    partner, product = _seed(db, lab, balance=100_000)
    username = _username(cleanup)
    email = f"{username}@{DOMAIN}"
    with _client(lab, LoseResponse(lab["base_url"], "/user/generate")) as cut, pytest.raises(AppError):
        _create(db, partner, product, cut, username, cap=2)
    db.rollback()
    db.refresh(partner)
    charged = int(partner.balance_toman)
    assert charged < 100_000
    assert db.scalar(select(PanelOperation).where(PanelOperation.status == "pending")) is not None
    with _client(lab) as c:
        assert len(c._fetch("email", email)) == 1
        assert reconcile_pending(db, lambda _panel: _client(lab)) == 1
        rows = c._fetch("email", email)
        assert len(rows) == 1
        assert int(rows[0]["transfer_enable"]) == gb_to_bytes(2)
    db.refresh(partner)
    assert int(partner.balance_toman) == charged
    assert len(_ledgers(db, "create")) == 1
    assert db.scalar(select(Subscription).where(Subscription.username == username)) is not None


@mutating
@pytest.mark.parametrize("operation", ["increase", "reset", "delete"])
def test_lost_update_or_delete_response_recovers_once(db, lab, client, cleanup, operation):
    partner, product = _seed(db, lab)
    sub, _charge, _fetched = _create(db, partner, product, client, _username(cleanup), cap=5)
    _set_traffic(client, sub.v2_user_id, 1073741824)
    before = int(partner.balance_toman)
    suffix = "/user/delUser" if operation == "delete" else "/user/update"
    with _client(lab, LoseResponse(lab["base_url"], suffix)) as cut, pytest.raises(AppError):
        actor = Actor("partner", partner.id)
        if operation == "increase":
            increase_subscription(db, sub_id=sub.id, partner_id=partner.id, traffic_cap_gb=Decimal(8),
                                  end_date=None, actor=actor, gateway=cut)
        elif operation == "reset":
            reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=actor, gateway=cut)
        else:
            delete_subscription(db, sub_id=sub.id, partner_id=partner.id, actor=actor, gateway=cut)
    db.rollback()
    # A landed reset whose response was lost cannot be told from an admin or plan reset: held.
    assert reconcile_pending(db, lambda _panel: _client(lab)) == (0 if operation == "reset" else 1)
    assert reconcile_pending(db, lambda _panel: _client(lab)) == 0
    db.refresh(partner)
    if operation == "reset":
        op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "reset_traffic"))
        assert op.status == "attention"
    if operation == "increase":
        assert int(client.get_user_info(sub.v2_user_id)["transfer_enable"]) == gb_to_bytes(8)
        assert int(partner.balance_toman) == before - 3000
        assert len(_ledgers(db, "increase")) == 1
    elif operation == "reset":
        info = client.get_user_info(sub.v2_user_id)
        assert int(info["u"]) == 0 and int(info["d"]) == 0
        assert int(partner.balance_toman) == before - 1000
        assert len(_ledgers(db, "reset_traffic")) == 1
    else:
        with pytest.raises(PanelUserNotFound):
            client.fetch_by_id(sub.v2_user_id)
        assert db.scalar(select(Subscription.id)) is None
        assert int(partner.balance_toman) == before


class CountMutations(httpx.BaseTransport):
    """Real transport that records every mutating admin request it delivers."""

    def __init__(self, base_url: str, log: list[str]):
        self.inner = PanelTransport(base_url)
        self.log = log

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith(("/user/update", "/user/delUser", "/user/generate")):
            self.log.append(path.rsplit("/", 1)[-1])
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()


def _db_lost() -> Exception:
    import pymysql
    from sqlalchemy.exc import OperationalError

    return OperationalError("UPDATE panel_operations", {}, pymysql.err.OperationalError(2013, "injected"))


@mutating
@pytest.mark.parametrize("failure", ["db_lost", "crash"])
@pytest.mark.parametrize("operation", ["create", "increase", "decrease", "reset", "delete"])
def test_failure_after_real_panel_success_neither_refunds_nor_resends(
        db, lab, client, cleanup, monkeypatch, operation, failure):
    from app.services import money_sync

    partner, product = _seed(db, lab)
    username = _username(cleanup)
    if operation != "create":
        sub, _charge, _fetched = _create(db, partner, product, client, username, cap=5)
        _set_traffic(client, sub.v2_user_id, 1073741824)
    sent: list[str] = []
    real_write, fired = money_sync._write, []

    def checkpoint_fails(db_, op, data, **kwargs):
        if data.get("panel_applied") and not fired:
            fired.append(True)
            raise _db_lost() if failure == "db_lost" else SystemExit("worker killed")
        return real_write(db_, op, data, **kwargs)

    monkeypatch.setattr(money_sync, "_write", checkpoint_fails)
    expected = pytest.raises(SystemExit) if failure == "crash" else pytest.raises(AppError)
    with _client(lab, CountMutations(lab["base_url"], sent)) as counted, expected:
        actor = Actor("partner", partner.id)
        if operation == "create":
            _create(db, partner, product, counted, username, cap=3)
        elif operation in {"increase", "decrease"}:
            perform = increase_subscription if operation == "increase" else decrease_subscription
            perform(db, sub_id=sub.id, partner_id=partner.id, end_date=None, actor=actor, gateway=counted,
                    traffic_cap_gb=Decimal(8 if operation == "increase" else 3))
        elif operation == "reset":
            reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=actor, gateway=counted)
        else:
            delete_subscription(db, sub_id=sub.id, partner_id=partner.id, actor=actor, gateway=counted)
    monkeypatch.setattr(money_sync, "_write", real_write)
    db.rollback()
    assert fired
    expected_sent = {"create": ["generate", "update"], "delete": ["delUser"]}.get(operation, ["update"])
    assert sent == expected_sent
    db.expire_all()
    charged = int(db.get(Partner, partner.id).balance_toman)
    op = db.scalar(select(PanelOperation).order_by(PanelOperation.id.desc()))
    assert op.status == "pending"
    if failure == "crash":
        op.lease_until = 1
        db.commit()
    done = reconcile_pending(db, lambda _panel: _client(lab, CountMutations(lab["base_url"], sent)))
    assert done == (0 if operation == "reset" else 1)
    assert sent == expected_sent  # recovery read the panel and sent nothing
    db.expire_all()
    assert db.get(PanelOperation, op.id).status == ("attention" if operation == "reset" else "completed")
    assert int(db.get(Partner, partner.id).balance_toman) == charged
    assert _ledgers(db, "operation_rollback") == []
    email = f"{username}@{DOMAIN}"
    if operation == "delete":
        with pytest.raises(PanelUserNotFound):
            client.fetch_by_email(email)
        return
    info = client.get_user_info(client.fetch_by_email(email)["id"])
    if operation == "reset":
        assert int(info["u"]) == 0 and int(info["d"]) == 0
    else:
        assert int(info["transfer_enable"]) == gb_to_bytes({"create": 3, "increase": 8, "decrease": 3}[operation])


@mutating
def test_real_pre_write_aborts_are_the_only_proven_refusals(lab, client, cleanup):
    username = _username(cleanup)
    client.generate_user(email_prefix=username, email_suffix=DOMAIN, plan_id=lab["plan_id"],
                         expired_at=2_000_000_000, password="pp-lab-pass-1")
    user = client.fetch_by_email(f"{username}@{DOMAIN}")
    base = {"email": user["email"], "banned": 0, "is_admin": 0, "is_staff": 0}
    cases = [
        ({**base, "id": 2_000_000_000}, "用户不存在"),
        ({**base, "id": user["id"], "plan_id": 2_000_000_000}, "订阅计划不存在"),
    ]
    for payload, message in cases:
        response = _admin(lab, "POST", "user/update", client._auth, json=payload)
        assert (response.status_code, response.json()) == (500, {"message": message})
        with pytest.raises(V2BoardError) as caught:
            client.update_user(payload)
        assert caught.value.panel_rejected is True
    assert int(client.get_user_info(user["id"])["plan_id"]) == lab["plan_id"]
    # The same refusal wording on another endpoint proves nothing.
    with pytest.raises(V2BoardError) as caught:
        client.reset_secret(2_000_000_000)
    assert caught.value.panel_rejected is False


@mutating
def test_rejected_credentials_reach_the_panel_once(lab, client, cleanup):
    username = _username(cleanup)
    email, password = f"{username}@{DOMAIN}", "pp-lab-pass-1"
    client.generate_user(email_prefix=username, email_suffix=DOMAIN, plan_id=lab["plan_id"],
                         expired_at=2_000_000_000, password=password)
    for _ in range(6):
        with V2BoardClient(lab["base_url"], lab["secure_path"], email, "wrong-password-1") as bad:
            with pytest.raises(PanelCredentialsRejected):
                bad.fetch_plans()
    login = _raw(lab, "POST", "passport/auth/login", None, json={"email": email, "password": password})
    assert login.status_code == 200  # one counted failure, far below the lockout threshold


@mutating
def test_password_lockout_follows_panel_config(lab, client, cleanup):
    username = _username(cleanup)
    email, password = f"{username}@{DOMAIN}", "pp-lab-pass-1"
    client.generate_user(email_prefix=username, email_suffix=DOMAIN, plan_id=lab["plan_id"],
                         expired_at=2_000_000_000, password=password)

    def login(pw: str) -> httpx.Response:
        return _raw(lab, "POST", "passport/auth/login", None, json={"email": email, "password": pw})

    assert login(password).status_code == 200
    for _ in range(5):
        assert login("wrong-password").status_code == 500
    locked = login(password)
    if lab.get("password_limit_enable"):
        assert locked.status_code == 500
    else:
        # Failures are only counted when password_limit_enable is saved in config/v2board.php.
        assert locked.status_code == 200
