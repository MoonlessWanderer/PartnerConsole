"""Security boundaries exercised with dummy users and temporary databases only."""
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import httpcore
import httpx
import jwt
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from local_http import BACKEND

from app.core.config import Settings, settings
from app.core.database import Base, make_engine
from app.core.deps import get_db
from app.core.errors import AppError, V2BoardError
from app.core.outbound import PanelBackend, check_address, panel_origin
from app.core.security import (
    _jwt_key, check_password, create_token, decode_token,
    ensure_login_allowed, hash_password, login_key, verify_password,
)
from app.integrations.v2board import V2BoardClient
from app.api import admin as admin_routes, partner as partner_routes
from app.main import app, lifespan
from app.models import AdminUser, LoginThrottle, Partner, RevokedToken, Subscription, WalletLedger
from test_billing import PanelSim, gateway, seed
from test_stage35 import setup

PASSWORD = "Only-for-synthetic-test-872!"
TEST_KEY = "synthetic-test-key-not-a-real-secret-872!"


@pytest.fixture
def security_api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "secret_key", TEST_KEY)
    monkeypatch.setattr(settings, "admin_jwt_secret", "")
    monkeypatch.setattr(settings, "partner_jwt_secret", "")
    engine = make_engine(f"sqlite:///{(tmp_path / 'security.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        partner, product = seed(db)
        password_hash = hash_password(PASSWORD)
        partner.password_hash = password_hash
        other = Partner(username="other_shop", password_hash=password_hash, balance_toman=5000)
        admin = AdminUser(username="admin", password_hash=password_hash)
        db.add_all([other, admin])
        db.flush()
        own = Subscription(partner_id=partner.id, product_id=product.id, username="own_sub",
                           v2_user_id=101, traffic_cap_gb=10, expired_at=1900000000)
        foreign = Subscription(partner_id=other.id, product_id=product.id, username="other_sub",
                               v2_user_id=102, traffic_cap_gb=10, expired_at=1900000000)
        db.add_all([own, foreign])
        db.flush()
        for owner in (partner, other):
            db.add(WalletLedger(partner_id=owner.id, asset="toman", amount=100,
                                reason="admin_adjust", actor_type="admin", actor_id=admin.id))
        db.commit()
        ids = dict(partner=partner.id, other=other.id, admin=admin.id,
                   own=own.id, foreign=foreign.id, balance=partner.balance_toman)

    def dependency():
        with sessions() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    app.dependency_overrides[get_db] = dependency
    client = TestClient(app)  # no context manager: never run real lifespan/migrations
    yield client, sessions, ids
    client.close()
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()


def bearer(role, uid):
    return {"Authorization": f"Bearer {create_token(role, uid, role)}"}


PROTECTED = [
    (next(iter(route.methods)), "/api" + route.path)
    for router in (admin_routes.router, partner_routes.router)
    for route in router.routes if isinstance(route, APIRoute)
    and not route.path.endswith("/auth/login")
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_every_protected_endpoint_requires_authentication(security_api, method, path):
    client, _, _ids = security_api
    import re
    path = re.sub(r"\{[^}]+\}", "1", path)
    response = client.request(method, path, json={})
    assert response.status_code == 401


@pytest.mark.parametrize("role,target", [("partner", "admin"), ("admin", "partner")])
def test_roles_cannot_be_swapped(security_api, role, target):
    client, _, ids = security_api
    assert client.get(f"/api/{target}/me", headers=bearer(role, ids[role])).status_code == 401


@pytest.mark.parametrize("method,suffix,body", [
    ("GET", "", None), ("POST", "/note", {"note": "stolen"}),
    ("POST", "/increase", {"traffic_cap_gb": 12}),
    ("POST", "/decrease", {"traffic_cap_gb": 5}),
    ("POST", "/delete", {}), ("POST", "/reset-link", {}),
    ("POST", "/reset-traffic", {}), ("POST", "/ban", {}), ("POST", "/unban", {}),
])
def test_partner_cannot_touch_other_subscription(security_api, monkeypatch, method, suffix, body):
    from app.api import partner as routes
    client, sessions, ids = security_api
    monkeypatch.setattr(routes, "client_for_panel", lambda *a: pytest.fail("authorization must precede panel contact"))
    response = client.request(method, f"/api/partner/subscriptions/{ids['foreign']}{suffix}",
                              json=body, headers=bearer("partner", ids["partner"]))
    assert response.status_code == 404
    with sessions() as db:
        assert db.get(Partner, ids["partner"]).balance_toman == ids["balance"]
        assert db.get(Subscription, ids["foreign"]).note is None


@pytest.mark.parametrize("action", ["increase", "decrease", "delete", "reset_traffic"])
def test_quote_enforces_subscription_ownership(security_api, action):
    client, _, ids = security_api
    response = client.post("/api/partner/subscriptions/quote", headers=bearer("partner", ids["partner"]),
                           json={"action": action, "subscription_id": ids["foreign"], "traffic_cap_gb": 5})
    assert response.status_code == 404


def test_partner_subscription_reads_are_scoped_to_the_session_owner(security_api, monkeypatch):
    client, sessions, ids = security_api
    route = "/api/partner/subscriptions"
    sim = PanelSim()
    with sessions() as db:
        first = db.get(Subscription, ids["own"])
        extra = Subscription(
            partner_id=ids["partner"], product_id=first.product_id, username="own_sub_extra",
            v2_user_id=103, traffic_cap_gb=12, expired_at=first.expired_at,
        )
        db.add(extra)
        db.flush()
        owners = {
            "A": ("shop1", ids["partner"], {first.id, extra.id}),
            "B": ("other_shop", ids["other"], {ids["foreign"]}),
        }
        subscriptions = {}
        for sub in (first, extra, db.get(Subscription, ids["foreign"])):
            assert sub.product_id == first.product_id
            sub.note = f"test-private-note-{sub.v2_user_id}"
            link = f"https://panel.test/sub/fixture-{sub.v2_user_id}"
            sim.users[sub.v2_user_id] = {
                "id": sub.v2_user_id, "email": f"{sub.username}@{sub.product.email_domain}",
                "u": 0, "d": 0, "subscribe_url": link,
            }
            subscriptions[sub.id] = (sub.v2_user_id, sub.username, sub.note, link)
        assert len({item[3] for item in subscriptions.values()}) == 3
        shared_panel_id = first.product.panel_id
        db.commit()

    opened_panels = []

    def simulated_client(panel):
        assert panel.id == shared_panel_id
        opened_panels.append(panel.id)
        return gateway(sim)

    monkeypatch.setattr(partner_routes, "client_for_panel", simulated_client)
    for label, (username, partner_id, owned_ids) in owners.items():
        login = client.post("/api/partner/auth/login", json={"username": username, "password": PASSWORD})
        assert login.status_code == 200
        session = login.json()
        assert session["role"] == "partner"
        headers = {"Authorization": "Bearer " + session["access_token"]}
        foreign_ids = subscriptions.keys() - owned_ids
        foreign_links = [subscriptions[sub_id][3] for sub_id in foreign_ids]

        full_list = client.get(route, headers=headers)
        assert full_list.status_code == 200
        assert all(link not in full_list.text for link in foreign_links)
        result = full_list.json()
        assert result["total"] == len(result["items"]) == len(owned_ids)
        assert {row["id"] for row in result["items"]} == owned_ids
        assert all(row["partner_id"] == partner_id for row in result["items"])

        paged_ids = []
        for offset in range(len(owned_ids) + 1):
            page = client.get(route, headers=headers, params={"limit": 1, "offset": offset})
            assert page.status_code == 200
            assert all(link not in page.text for link in foreign_links)
            result = page.json()
            assert result["total"] == len(owned_ids)
            assert len(result["items"]) == (1 if offset < len(owned_ids) else 0)
            assert all(row["partner_id"] == partner_id for row in result["items"])
            paged_ids.extend(row["id"] for row in result["items"])
        assert len(paged_ids) == len(owned_ids) and set(paged_ids) == owned_ids

        for sub_id in sorted(owned_ids):
            response = client.get(f"{route}/{sub_id}", headers=headers)
            assert response.status_code == 200
            assert all(link not in response.text for link in foreign_links)
            data = response.json()
            assert (data["id"], data["partner_id"], data["product_id"]) == (sub_id, partner_id, first.product_id)
            assert (data["v2_user_id"], data["username"], data["note"], data["subscribe_url"]) == subscriptions[sub_id]
            assert data["live_error"] is None and data["subscribe_url_fallback"] is False

        before_denials = len(opened_panels)
        for sub_id in sorted(foreign_ids):
            response = client.get(f"{route}/{sub_id}", headers=headers)
            assert response.status_code == 404
            assert response.json() == {"code": "request_rejected", "detail": "اشتراک پیدا نشد"}
        assert len(opened_panels) == before_denials
        print(f"{label}: list={len(owned_ids)}, page totals={len(owned_ids)}, "
              f"own details={len(owned_ids)}, denied foreign details={len(foreign_ids)}")

    assert opened_panels == [shared_panel_id] * 3


@pytest.mark.parametrize(("role", "expected_status", "error_detail"), [
    pytest.param("admin", 200, None, id="admin"),
    pytest.param("partner", 401, "نشست نامعتبر است", id="partner_own_wallet"),
    pytest.param(None, 401, "وارد نشده‌اید", id="no_session"),
])
def test_manual_wallet_adjustment_requires_an_admin(security_api, role, expected_status, error_detail):
    client, sessions, ids = security_api
    partner_id = ids["partner"]
    headers = {}
    if role is not None:
        username = "admin" if role == "admin" else "shop1"
        login = client.post(f"/api/{role}/auth/login", json={"username": username, "password": PASSWORD})
        assert login.status_code == 200
        session = login.json()
        assert session["role"] == role
        headers = {"Authorization": "Bearer " + session["access_token"]}
        if role == "partner":
            me = client.get("/api/partner/me", headers=headers)
            assert me.status_code == 200 and me.json()["id"] == partner_id

    def wallet_state():
        with sessions() as db:
            ledger = db.scalars(select(WalletLedger).where(
                WalletLedger.partner_id == partner_id,
            ).order_by(WalletLedger.id))
            return db.get(Partner, partner_id).balance_toman, [
                (row.id, row.asset, row.amount, row.product_id, row.subscription_id, row.reason,
                 row.actor_type, row.actor_id) for row in ledger
            ]

    opening, old_ledger = wallet_state()
    assert opening == ids["balance"]
    amount = 1250
    response = client.post(f"/api/admin/partners/{partner_id}/adjust", headers=headers,
                           json={"asset": "toman", "amount": amount, "product_id": None})
    closing, new_ledger = wallet_state()

    assert response.status_code == expected_status
    if role == "admin":
        assert (response.json()["id"], response.json()["balance_toman"]) == (partner_id, opening + amount)
        assert closing == opening + amount
        assert new_ledger[:-1] == old_ledger
        assert new_ledger[-1][1:] == ("toman", amount, None, None, "admin_adjust", "admin", ids["admin"])
    else:
        assert response.json() == {"code": "request_rejected", "detail": error_detail}
        assert (closing, new_ledger) == (opening, old_ledger)
    print(f"role={role or 'no_session'}, HTTP={response.status_code}, "
          f"balance={opening}->{closing}, ledger delta={len(new_ledger) - len(old_ledger)}")


def test_lists_and_ledger_ignore_forged_partner_filter(security_api):
    client, _, ids = security_api
    headers = bearer("partner", ids["partner"])
    for resource in ("subscriptions", "ledger"):
        result = client.get(f"/api/partner/{resource}?partner_id={ids['other']}", headers=headers)
        assert result.status_code == 200
        assert result.json()["items"]
        assert all(row["partner_id"] == ids["partner"] for row in result.json()["items"])


@pytest.mark.parametrize("role", ["admin", "partner"])
def test_logout_revokes_only_that_session_persistently(security_api, role):
    client, sessions, ids = security_api
    first = bearer(role, ids[role])
    second = bearer(role, ids[role])
    assert client.get(f"/api/{role}/me", headers=first).status_code == 200
    assert client.post(f"/api/{role}/auth/logout", headers=first).status_code == 200
    assert client.get(f"/api/{role}/me", headers=first).status_code == 401
    assert client.get(f"/api/{role}/me", headers=second).status_code == 200
    with sessions() as fresh:
        payload = decode_token(first["Authorization"][7:], role)
        assert fresh.get(RevokedToken, payload["jti"]) is not None


def test_partner_session_logout_and_expiry(security_api, monkeypatch):
    client, sessions, ids = security_api
    route = "/api/partner/me"

    def login():
        response = client.post("/api/partner/auth/login", json={"username": "shop1", "password": PASSWORD})
        assert response.status_code == 200
        token = response.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}, decode_token(token, "partner")

    def assert_allowed(headers):
        response = client.get(route, headers=headers)
        assert response.status_code == 200
        assert response.json()["id"] == ids["partner"]

    def assert_denied(headers):
        response = client.get(route, headers=headers)
        assert response.status_code == 401
        assert response.json() == {"code": "request_rejected", "detail": "نشست نامعتبر است"}

    headers_a, claims_a = login()
    headers_b, claims_b = login()
    assert claims_a["sub"] == claims_b["sub"] == str(ids["partner"])
    assert claims_a["jti"] != claims_b["jti"]
    assert_allowed(headers_a)
    assert_allowed(headers_b)

    logout = client.post("/api/partner/auth/logout", headers=headers_a)
    assert logout.status_code == 200
    assert logout.json() == {"ok": True}
    assert_denied(headers_a)
    assert_allowed(headers_b)
    with sessions() as fresh:
        assert fresh.get(RevokedToken, claims_a["jti"]).expires_at == claims_a["exp"]
        assert fresh.get(RevokedToken, claims_b["jti"]) is None

    expiry = datetime.fromtimestamp(claims_b["exp"], timezone.utc)
    now = expiry - timedelta(seconds=1)

    class VerificationClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(jwt.api_jwt, "datetime", VerificationClock)
    assert_allowed(headers_b)
    now = expiry
    assert_denied(headers_b)
    now = expiry + timedelta(seconds=1)
    assert_denied(headers_b)


@pytest.mark.parametrize("kind", ["garbage", "wrong_signature", "expired", "missing_exp", "missing_jti", "wrong_role", "unknown_user"])
def test_invalid_tokens_are_rejected(security_api, kind):
    client, _, ids = security_api
    payload = decode_token(create_token("partner", ids["partner"], "shop1"), "partner")
    key = _jwt_key("partner")
    if kind == "expired":
        payload["exp"] = int(time.time()) - 1
    if kind == "missing_exp":
        del payload["exp"]
    if kind == "missing_jti":
        del payload["jti"]
    if kind == "wrong_role":
        payload["role"] = "admin"
    if kind == "unknown_user":
        payload["sub"] = "999999"
    if kind == "wrong_signature":
        key = "wrong-synthetic-key-of-at-least-thirty-two-characters"
    token = "garbage" if kind == "garbage" else jwt.encode(payload, key, algorithm="HS256")
    assert client.get("/api/partner/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_banned_partner_cannot_login_or_use_existing_token(security_api):
    client, sessions, ids = security_api
    headers = bearer("partner", ids["partner"])
    with sessions() as db:
        db.get(Partner, ids["partner"]).banned = True
        db.commit()
    assert client.get("/api/partner/me", headers=headers).status_code == 403
    assert client.post("/api/partner/auth/login", json={"username": "shop1", "password": PASSWORD}).status_code == 403


@pytest.mark.parametrize("role,username", [("admin", "admin"), ("partner", "shop1")])
def test_failed_login_limit_survives_request_rollback_and_new_client(security_api, role, username):
    client, sessions, _ids = security_api
    for _ in range(8):
        result = client.post(f"/api/{role}/auth/login", json={"username": username, "password": "bad"})
        assert result.status_code == 401
    fresh_client = TestClient(app)  # intentionally do not enter lifespan
    try:
        blocked = fresh_client.post(f"/api/{role}/auth/login", json={"username": username, "password": PASSWORD})
        assert blocked.status_code == 429
    finally:
        fresh_client.close()
    with sessions() as db:
        assert db.scalars(select(LoginThrottle)).all()


def test_login_account_limit_ignores_rotating_ips(security_api):
    _client, sessions, _ids = security_api
    for index in range(8):
        with sessions() as db:
            ensure_login_allowed(login_key(f"192.0.2.{index}", "someone", "admin"), db)
    with sessions() as db, pytest.raises(AppError) as caught:
        ensure_login_allowed(login_key("192.0.2.200", "someone", "admin"), db)
    assert caught.value.status_code == 429


def test_password_hashes_are_salted_and_tamper_resistant():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second
    assert verify_password(PASSWORD, first)
    assert not verify_password("bad", first)
    assert not check_password(PASSWORD, None)
    assert not verify_password(PASSWORD, "pbkdf2_sha256$9999999999$bad$bad")


def test_login_ip_quota_stops_username_spraying(security_api):
    _client, sessions, _ids = security_api
    for index in range(40):
        with sessions() as db:
            ensure_login_allowed(login_key("192.0.2.1", f"account{index}", "partner"), db)
    with sessions() as db, pytest.raises(AppError) as caught:
        ensure_login_allowed(login_key("192.0.2.1", "newaccount", "partner"), db)
    assert caught.value.status_code == 429


def test_login_throttle_is_atomic_across_processes(security_api):
    _client, sessions, _ids = security_api
    with sessions() as db:
        url = str(db.get_bind().url)
    script = """
import sys
from sqlalchemy.orm import sessionmaker
from app.core.database import make_engine
from app.core.security import ensure_login_allowed, login_key
from app.core.errors import AppError
engine = make_engine(sys.argv[1])
sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
accepted = 0
for n in range(3):
    with sessions() as db:
        try:
            ensure_login_allowed(login_key(sys.argv[2], 'shared_account', 'partner'), db)
            accepted += 1
        except AppError as exc:
            assert exc.status_code == 429
print(accepted)
engine.dispose()
"""
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "PATH", "TEMP", "TMP") if key in os.environ}
    processes = [subprocess.Popen([sys.executable, "-c", script, url, f"192.0.2.{index}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                 for index in range(4)]
    counts = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            counts.append(int(stdout.strip()))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert sum(counts) == 8


def test_login_window_expires_and_roles_are_separate(security_api):
    _client, sessions, _ids = security_api
    for _ in range(8):
        with sessions() as db:
            ensure_login_allowed(login_key("192.0.2.1", "same", "partner"), db)
    with sessions() as db:
        ensure_login_allowed(login_key("192.0.2.1", "same", "admin"), db)
        for row in db.scalars(select(LoginThrottle)):
            row.reset_at = int(time.time()) - 1
        db.commit()
    with sessions() as db:
        ensure_login_allowed(login_key("192.0.2.1", "same", "partner"), db)


def test_login_success_resets_account_limit_without_resetting_ip(security_api):
    client, sessions, _ids = security_api
    url = "/api/partner/auth/login"
    for _ in range(3):
        assert client.post(url, json={"username": "shop1", "password": "bad"}).status_code == 401
    response = client.post(url, json={"username": "shop1", "password": PASSWORD})
    assert response.status_code == 200
    assert client.get("/api/partner/me", headers={"Authorization": "Bearer " + response.json()["access_token"]}).status_code == 200
    with sessions() as db:
        assert max(row.attempts for row in db.scalars(select(LoginThrottle))) == 4


def test_authenticated_api_quota_spans_new_tokens(security_api, monkeypatch):
    client, sessions, ids = security_api
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 2)
    for _ in range(2):
        assert client.get("/api/partner/me", headers=bearer("partner", ids["partner"])).status_code == 200
    assert client.get("/api/partner/me", headers=bearer("partner", ids["partner"])).status_code == 429
    assert client.get("/api/partner/me", headers=bearer("partner", ids["other"])).status_code == 200


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://user:pass@example.com", "http://example.com:bad",
    "http://example.com:70000", "http://example.com:0", "http://example.com\\@127.0.0.1",
    "http://exam\tple.com", "http://example.com?q=secret", "http://example.com/#x",
    "http://example.com/path", "http://[fe80::1%25eth0]",
])
def test_panel_url_validation_rejects_ambiguous_destinations(url):
    with pytest.raises(AppError):
        panel_origin(url)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1", "::1", "fc00::1",
    "::ffff:127.0.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "100.100.100.200",
])
def test_panel_private_destinations_are_denied_by_default(address):
    with pytest.raises(AppError):
        check_address(address, allow_private=False)


@pytest.mark.parametrize("address", ["169.254.169.254", "100.100.100.200", "fd00:ec2::254", "0.0.0.0", "224.0.0.1"])
def test_metadata_and_unsafe_ranges_remain_blocked_even_with_exception(address):
    with pytest.raises(AppError):
        check_address(address, allow_private=True)


def test_private_panel_exception_is_exact_origin_only(monkeypatch):
    monkeypatch.setattr(settings, "panel_private_origins", "http://127.0.0.1:8788")
    assert PanelBackend(panel_origin("http://127.0.0.1:8788/")).allow_private
    assert not PanelBackend("http://127.0.0.1:8789").allow_private
    assert not PanelBackend("https://127.0.0.1:8788").allow_private
    assert not PanelBackend("http://localhost:8788").allow_private


def test_dns_is_validated_and_connection_uses_pinned_address(monkeypatch):
    monkeypatch.setattr(settings, "panel_private_origins", "")
    backend = PanelBackend("https://panel.example.com")
    answers = ["93.184.216.34", "127.0.0.1"]
    connected = []
    def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers.pop(0), 443))]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", lambda self, host, *a: connected.append(host))
    backend.connect_tcp("panel.example.com", 443)
    assert connected == ["93.184.216.34"]
    with pytest.raises(AppError):
        backend.connect_tcp("panel.example.com", 443)
    assert connected == ["93.184.216.34"]


def test_mixed_public_private_dns_answer_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "panel_private_origins", "")
    records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
               for ip in ("93.184.216.34", "10.0.0.1")]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: records)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", lambda *a, **kw: pytest.fail("must not connect"))
    with pytest.raises(AppError):
        PanelBackend("https://panel.example.com").connect_tcp("panel.example.com", 443)


def secure_settings(**overrides):
    values = dict(_env_file=None, secret_key=TEST_KEY, admin_jwt_secret="", partner_jwt_secret="",
                  panel_encryption_key="", environment="development", api_rate_limit_per_minute=120,
                  cors_origins="http://127.0.0.1:3741", access_token_expire_minutes=720)
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize("changes", [
    {"secret_key": "dev-only-change-me"}, {"secret_key": "change-me-to-a-long-random-string"},
    {"secret_key": "a" * 64}, {"admin_jwt_secret": "short"},
    {"panel_encryption_key": "change-me-panel-key"},
    {"admin_jwt_secret": TEST_KEY, "partner_jwt_secret": TEST_KEY},
    {"cors_origins": "*"}, {"cors_origins": "https://example.com/path"},
    {"environment": "production", "cors_origins": "http://example.com"},
    {"access_token_expire_minutes": 0},
])
def test_runtime_rejects_unsafe_security_configuration(changes):
    with pytest.raises(RuntimeError):
        secure_settings(**changes).validate_security()


@pytest.mark.parametrize("origin", [
    "http://127.0.0.1:3741",
    "http://localhost:3741",
    "http://[::1]:3741",
])
def test_production_accepts_an_explicit_tunnel_origin(origin):
    secure_settings(environment="production", cors_origins=origin).validate_security()


@pytest.mark.parametrize("origin", [
    "http://127.0.0.1",
    "http://127.0.0.1:80",
    "http://127.0.0.1:8741",
    "http://127.0.0.1:3741/",
    "http://127.0.0.1.evil.com:3741",
    "http://localhost.evil:3741",
    "http://0.0.0.0:3741",
    "http://[::ffff:127.0.0.1]:3741",
    "http://2130706433:3741",
    "http://10.0.0.8:3741",
    "http://example.com",
    "HTTP://127.0.0.1:3741",
    "null",
    "*",
])
def test_production_rejects_inexact_or_public_http(origin):
    with pytest.raises(RuntimeError):
        secure_settings(environment="production", cors_origins=origin).validate_security()


def test_production_does_not_allow_http_without_an_explicit_origin():
    values = dict(_env_file=None, secret_key=TEST_KEY, admin_jwt_secret="", partner_jwt_secret="",
                  panel_encryption_key="", environment="production", api_rate_limit_per_minute=120,
                  access_token_expire_minutes=720)
    with pytest.raises(RuntimeError):
        Settings(**values).validate_security()


def test_tunnel_origin_is_echoed_and_other_origins_are_not():
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:3741"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health():
        return {"ok": True}

    client = TestClient(app)
    allowed = client.get("/api/health", headers={"Origin": "http://127.0.0.1:3741"})
    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:3741"
    blocked = client.get("/api/health", headers={"Origin": "https://127.0.0.1"})
    assert "access-control-allow-origin" not in blocked.headers


def test_strong_config_and_verified_https_are_supported(monkeypatch):
    secure_settings().validate_security()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "panel_private_origins", "")
    with pytest.raises(AppError):
        PanelBackend("http://panel.example.com")
    assert PanelBackend("https://panel.example.com").port == 443


def test_unsafe_startup_fails_before_any_database_action(monkeypatch):
    import app.main as main
    monkeypatch.setattr(settings, "secret_key", "dev-only-change-me")
    monkeypatch.setattr(main, "upgrade_database", lambda: pytest.fail("must not migrate"))
    monkeypatch.setattr(main, "seed_admin", lambda: pytest.fail("must not seed"))
    async def start():
        async with lifespan(app):
            pytest.fail("must not start")
    with pytest.raises(RuntimeError):
        asyncio.run(start())


def test_error_response_and_journal_do_not_expose_remote_secrets(security_api, monkeypatch, caplog):
    from app.services.money_sync import _err
    client, _, ids = security_api
    secret = "SYNTHETIC-PANEL-CREDENTIAL-DO-NOT-EXPOSE"
    def handler(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"data": {"auth_data": "dummy", "is_admin": 1}})
        return httpx.Response(500, json={"message": f"SQL error password={secret}", "errors": {"token": secret}})
    with V2BoardClient("http://panel.test", "safe", "dummy", "dummy", transport=httpx.MockTransport(handler)) as gateway:
        with pytest.raises(V2BoardError) as caught:
            gateway.get_user_info(1)
    assert secret not in str(caught.value)
    assert secret not in _err(httpx.ConnectError(secret))
    assert secret not in caplog.text
    class Broken:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get_user_info(self, uid):
            raise RuntimeError(secret)
    monkeypatch.setattr(partner_routes, "client_for_panel", lambda *a: Broken())
    response = client.get(f"/api/partner/subscriptions/{ids['own']}", headers=bearer("partner", ids["partner"]))
    assert response.status_code == 200
    assert response.json()["live_error"]
    assert secret not in response.text


def test_cors_and_security_headers_and_no_cookie_auth(security_api):
    client, _, ids = security_api
    response = client.get("/api/partner/me", headers={**bearer("partner", ids["partner"]), "Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    token = create_token("partner", ids["partner"], "shop1")
    assert client.get("/api/partner/me", headers={"Cookie": f"token={token}"}).status_code == 401


def test_large_or_streamed_body_rejected_before_parsing(security_api):
    client, _, _ids = security_api
    assert client.post("/api/partner/auth/login", content=b"x" * 65537).status_code == 413
    def chunks():
        yield b"x" * 40000
        yield b"x" * 40000
    assert client.post("/api/partner/auth/login", content=chunks()).status_code == 413


@pytest.mark.parametrize("role", ["admin", "partner"])
def test_logout_remains_available_when_api_quota_exhausted(security_api, monkeypatch, role):
    client, _, ids = security_api
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 1)
    headers = bearer(role, ids[role])
    assert client.get(f"/api/{role}/me", headers=headers).status_code == 200
    assert client.get(f"/api/{role}/me", headers=headers).status_code == 429
    assert client.post(f"/api/{role}/auth/logout", headers=headers).status_code == 200
    assert client.get(f"/api/{role}/me", headers=headers).status_code == 401


def test_banned_partner_can_revoke_token_before_unban(security_api):
    client, sessions, ids = security_api
    headers = bearer("partner", ids["partner"])
    with sessions() as db:
        db.get(Partner, ids["partner"]).banned = True
        db.commit()
    assert client.post("/api/partner/auth/logout", headers=headers).status_code == 200
    with sessions() as db:
        db.get(Partner, ids["partner"]).banned = False
        db.commit()
    assert client.get("/api/partner/me", headers=headers).status_code == 401


def test_dns_fallback_uses_only_validated_addresses_and_remaining_timeout(monkeypatch):
    monkeypatch.setattr(settings, "panel_private_origins", "")
    records = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443, 0, 0)),
               (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: records)
    connected = []
    sentinel = object()
    def connect(self, address, port, timeout, *args):
        connected.append((address, timeout))
        if len(connected) == 1:
            raise httpcore.ConnectError("unreachable IPv6")
        return sentinel
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    assert PanelBackend("https://panel.example.com").connect_tcp("panel.example.com", 443, timeout=4) is sentinel
    assert [ip for ip, _ in connected] == ["2606:4700:4700::1111", "93.184.216.34"]
    assert 0 < connected[1][1] <= connected[0][1] <= 4


def panel_client_with_response(response):
    def handler(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"data": {"auth_data": "dummy-auth", "is_admin": 1}})
        return response
    return V2BoardClient("http://panel.test", "safe", "dummy", "dummy", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="not json"), httpx.Response(200, json={"data": False}),
    httpx.Response(200, json={"data": None}), httpx.Response(302, headers={"Location": "http://127.0.0.1"}, json={}),
    httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)),
])
def test_unconfirmed_panel_mutations_are_not_success(response):
    with panel_client_with_response(response) as client, pytest.raises(V2BoardError):
        client.update_user({"id": 1})


@pytest.mark.parametrize("info", [
    {}, {"u": -1, "d": 0}, {"u": 1.5, "d": 0}, {"u": 2**64, "d": 0},
    {"u": True, "d": 0}, {"id": 2, "u": 0, "d": 0},
])
def test_panel_usage_and_identity_must_be_valid(info):
    with panel_client_with_response(httpx.Response(200, json={"data": info})) as client, pytest.raises(V2BoardError):
        client.get_user_info(1)


def test_existing_default_admin_is_rejected_and_interactive_reset_helper_is_scoped(security_api, monkeypatch):
    from app.services import bootstrap
    from app.reset_admin_password import set_password
    _, sessions, ids = security_api
    with sessions() as db:
        db.get(AdminUser, ids["admin"]).password_hash = hash_password("admin12345")
        db.commit()
    monkeypatch.setattr(bootstrap, "SessionLocal", sessions)
    with pytest.raises(RuntimeError, match="default password"):
        bootstrap.seed_admin()
    with sessions() as db:
        with pytest.raises(ValueError):
            set_password(db, "admin", "short")
        with pytest.raises(ValueError, match="not found"):
            set_password(db, "missing", PASSWORD)
        set_password(db, "admin", PASSWORD)
        assert verify_password(PASSWORD, db.get(AdminUser, ids["admin"]).password_hash)
        assert db.get(Partner, ids["partner"]).balance_toman == ids["balance"]
    bootstrap.seed_admin()


def test_security_migration_on_temporary_database_preserves_stage4_data(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    path = (tmp_path / "migration-only.db").resolve()
    assert path.is_relative_to(tmp_path.resolve())
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{path.as_posix()}")
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(cfg, "0004_subscription_identity")
    engine = make_engine(settings.database_url)
    with sessionmaker(bind=engine)() as db:
        partner, _ = seed(db, balance=123456)
        pid = partner.id
        db.commit()
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    with sessionmaker(bind=engine)() as db:
        assert db.get(Partner, pid).balance_toman == 123456
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0005_security_state"
        assert db.scalars(select(LoginThrottle)).all() == []
        assert db.scalars(select(RevokedToken)).all() == []
    engine.dispose()


@pytest.mark.parametrize("body", [
    {"data": {}}, {"data": {"data": False}}, {"data": {"total": 0}},
    {"message": "not a user listing"}, {"data": None}, {"data": False},
])
def test_malformed_lookup_is_unknown_and_never_proof_of_absence(body):
    from app.services.money_sync import _read_remote
    with panel_client_with_response(httpx.Response(200, json=body)) as client:
        assert _read_remote(client, user_id=1) == (None, "unknown")


def test_valid_empty_user_list_is_proof_of_absence():
    from app.services.money_sync import _read_remote
    with panel_client_with_response(httpx.Response(200, json={"data": []})) as client:
        assert _read_remote(client, user_id=1) == (None, "missing")


def test_malformed_recovery_response_cannot_refund_money(setup):
    from app.models import PanelOperation
    from app.services.subscriptions import Actor, increase_subscription
    from test_billing import gateway, NOW
    sessions, sim, pid, ids, balance, _path = setup
    original = sim.handler
    def damaged(request):
        if request.url.path.endswith("/user/update"):
            return httpx.Response(500, json={"message": "unknown result"})
        if request.url.path.endswith("/user/fetch"):
            return httpx.Response(200, json={"data": {}})
        return original(request)
    sim.handler = damaged
    with sessions() as db:
        client = gateway(sim)
        with pytest.raises(AppError):
            increase_subscription(db, sub_id=ids[0], partner_id=pid, traffic_cap_gb=12,
                                  end_date=None, actor=Actor("partner", pid), gateway=client, now=NOW)
        client.close()
        db.rollback()
        assert db.get(Partner, pid).balance_toman == balance - 2000
        assert db.scalar(select(PanelOperation).where(PanelOperation.status == "pending")) is not None
        assert db.scalars(select(WalletLedger).where(WalletLedger.reason == "operation_rollback")).all() == []
        sim.handler = original
        client = gateway(sim)
        updates = len(sim.updates)
        # The unknown result may have been applied: the old panel value is no licence to resend.
        with pytest.raises(AppError) as held:
            increase_subscription(db, sub_id=ids[0], partner_id=pid, traffic_cap_gb=12,
                                  end_date=None, actor=Actor("partner", pid), gateway=client, now=NOW)
        client.close()
        assert held.value.status_code == 409
        assert db.scalar(select(PanelOperation).where(PanelOperation.status == "attention")) is not None
        assert len(sim.updates) == updates
        assert db.get(Partner, pid).balance_toman == balance - 2000
        assert db.scalars(select(WalletLedger).where(WalletLedger.reason == "operation_rollback")).all() == []


def test_compressed_panel_response_is_rejected_without_decompression():
    class NeverRead(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail("compressed stream must never be consumed")
            yield b""
    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=NeverRead())
    with V2BoardClient("http://panel.test", "safe", "dummy", "dummy", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(V2BoardError, match="فشرده"):
            client.login()
