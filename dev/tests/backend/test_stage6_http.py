"""Real HTTP across the API, its SQLite file, and the local panel simulator."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import hmac
import time

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import make_engine
from app.core.timeutil import format_jalali_date, now_tehran, ts_to_tehran_date
from app.models import PanelOperation, Partner, PartnerProductCredit, WalletLedger
from local_http import local_server
from v2lab.server import ADMIN_EMAIL, ADMIN_PASSWORD, GB, SECURE_PATH


PASSWORD = "stage6-local-password-72"
ADMIN = "stage6_admin"


def api_env(tmp_path, lab_url):
    return {
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'isolated.db').as_posix()}",
        "SECRET_KEY": "stage6-synthetic-test-signing-key-2874",
        "ADMIN_USERNAME": ADMIN,
        "ADMIN_PASSWORD": PASSWORD,
        "PANEL_PRIVATE_ORIGINS": lab_url,
    }


def payload(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def login(api, role, username):
    result = payload(api.post(f"/api/{role}/auth/login", json={"username": username, "password": PASSWORD}))
    return {"Authorization": "Bearer " + result["access_token"]}


def prepare(api, lab_url):
    admin = login(api, "admin", ADMIN)
    panel = payload(api.post("/api/admin/panels", headers=admin, json={
        "name": "synthetic lab", "base_url": lab_url, "secure_path": SECURE_PATH,
        "admin_email": ADMIN_EMAIL, "admin_password": ADMIN_PASSWORD,
    }))
    product = payload(api.post("/api/admin/products", headers=admin, json={
        "panel_id": panel["id"], "plan_id": 7, "name": "synthetic product", "email_domain": "old.example.test",
        "price_per_gb": 1000, "price_per_day": 100,
    }))
    account = payload(api.post("/api/admin/partners", headers=admin, json={
        "username": "shop_stage6", "password": PASSWORD,
    }))
    payload(api.post(f"/api/admin/partners/{account['id']}/adjust", headers=admin,
                     json={"asset": "toman", "amount": 100000}))
    return admin, login(api, "partner", "shop_stage6"), account["id"], product["product"]["id"]


def confirmed(api, partner, route, action, body=None, sub_id=None):
    body = dict(body or {})
    quote = api.post("/api/partner/subscriptions/quote", headers=partner,
                     json={**body, "action": action, "subscription_id": sub_id})
    # Invalid actions remain visible in the quote response.
    if quote.status_code != 200 or not quote.json().get("consent_token"):
        return quote
    return api.post(route, headers=partner, json={**body, "consent_token": quote.json()["consent_token"]})


def create(api, partner, product, username="stage6_user"):
    return payload(confirmed(api, partner, "/api/partner/subscriptions", "create", {
        "product_id": product, "username": username, "traffic_cap_gb": 10,
        "end_date": format_jalali_date(now_tehran().date() + timedelta(days=15)),
    }))


def balance(api, partner):
    return payload(api.get("/api/partner/me", headers=partner))["balance_toman"]


def test_http_consent_rejects_context_changes_and_replays_once(tmp_path, lab_url):
    env = api_env(tmp_path, lab_url)
    with local_server(tmp_path, "app.main:app", env=env) as (url, _):
        with httpx.Client(base_url=url, trust_env=False, timeout=10) as api, httpx.Client(
            base_url=lab_url, trust_env=False, timeout=10
        ) as panel:
            payload(panel.post("/_lab/reset"))
            admin, first, _partner_id, product = prepare(api, lab_url)
            sub = create(api, first, product)
            base = f"/api/partner/subscriptions/{sub['id']}"
            mock = f"/_lab/users/{sub['v2_user_id']}"
            quote_path = "/api/partner/subscriptions/quote"
            payload(panel.post(mock + "/traffic", json={"u": GB}))
            original = payload(api.post(quote_path, headers=first, json={
                "action": "reset_traffic", "subscription_id": sub["id"],
            }))
            assert original["toman"] == 1000
            proof = original["consent_token"]
            before = balance(api, first)
            ledger = payload(api.get("/api/partner/ledger", headers=first))
            ops = payload(api.get("/api/admin/operations", headers=admin))
            reissued = login(api, "partner", "shop_stage6")
            key = hmac.digest((env["SECRET_KEY"] + ":partner").encode(),
                              b"partner-financial-consent-v1", "sha256")
            expired = jwt.decode(proof, key, algorithms=["HS256"])
            expired.update(iat=int(time.time()) - 360, exp=int(time.time()) - 60)
            stale = jwt.encode(expired, key, algorithm="HS256")
            for header, token in ((reissued, proof), (first, stale), (first, proof + "x"), (first, "")):
                result = payload(api.post(base + "/reset-traffic", headers=header,
                                          json={"consent_token": token}), 409)
                assert result["code"] == "financial_consent"
            for usage in (3 * GB, GB // 2, 0):
                payload(panel.post(mock + "/traffic", json={"u": usage}))
                payload(api.post(base + "/reset-traffic", headers=first, json={"consent_token": proof}), 409)
            assert balance(api, first) == before
            assert payload(api.get("/api/partner/ledger", headers=first)) == ledger
            assert payload(api.get("/api/admin/operations", headers=admin)) == ops

            payload(panel.post(mock + "/traffic", json={"u": GB}))
            confirmation = {"consent_token": proof}
            with ThreadPoolExecutor(max_workers=2) as executor:
                calls = [executor.submit(api.post, base + "/reset-traffic", headers=first,
                                         json=confirmation) for _ in range(2)]
                statuses = [call.result().status_code for call in calls]
            assert 200 in statuses and set(statuses) <= {200, 503}
            assert payload(api.post(base + "/reset-traffic", headers=first,
                                    json=confirmation))["cost_toman"] == 1000
            assert payload(api.post(base + "/reset-traffic", headers=first,
                                    json={"consent_token": stale}))["cost_toman"] == 1000
            assert balance(api, first) == before - 1000
            assert len(payload(api.get("/api/admin/operations", headers=admin))["items"]) == len(ops["items"]) + 1

            new = {"product_id": product, "username": "funding_check", "traffic_cap_gb": 3,
                   "end_date": format_jalali_date(now_tehran().date() + timedelta(days=5))}
            approved = payload(api.post(quote_path, headers=first, json={**new, "action": "create"}))
            payload(api.post("/api/partner/subscriptions", headers=first,
                             json={**new, "traffic_cap_gb": 4, "consent_token": approved["consent_token"]}), 409)
            payload(api.post("/api/partner/subscriptions", headers=first,
                             json={**new, "product_id": product + 1, "consent_token": approved["consent_token"]}), 409)
            tariffs = {"name": "synthetic product", "email_domain": "old.example.test",
                       "price_per_gb": 2000, "price_per_day": 100, "active": True}
            payload(api.put(f"/api/admin/products/{product}", headers=admin, json=tariffs))
            payload(api.post("/api/partner/subscriptions", headers=first,
                             json={**new, "consent_token": approved["consent_token"]}), 409)
            payload(api.put(f"/api/admin/products/{product}", headers=admin,
                            json={**tariffs, "price_per_gb": 1000}))
            owner = payload(api.get("/api/partner/me", headers=first))
            payload(api.post(f"/api/admin/partners/{owner['id']}/adjust", headers=admin,
                             json={"asset": "gb", "product_id": product, "amount": 1}))
            payload(api.post("/api/partner/subscriptions", headers=first,
                             json={**new, "consent_token": approved["consent_token"]}), 409)
            assert balance(api, first) == before - 1000

            panel_id = payload(api.get("/api/admin/panels", headers=admin))["items"][0]["id"]
            other = payload(api.post("/api/admin/products", headers=admin, json={
                **tariffs, "panel_id": panel_id, "plan_id": 8,
            }))["product"]["id"]
            request = {**new, "product_id": other}
            approved = payload(api.post(quote_path, headers=first, json={**request, "action": "create"}))
            payload(api.put(f"/api/admin/products/{other}", headers=admin,
                            json={**tariffs, "price_per_gb": 3000}))
            payload(api.post("/api/partner/subscriptions", headers=first,
                             json={**request, "consent_token": approved["consent_token"]}), 409)
            engine = make_engine(env["DATABASE_URL"])
            try:
                with Session(engine) as db:
                    assert db.scalar(select(PartnerProductCredit).where(
                        PartnerProductCredit.partner_id == owner["id"],
                        PartnerProductCredit.product_id == other,
                    )) is None
            finally:
                engine.dispose()


def test_http_financial_lifecycle_and_old_panel_identity(tmp_path, lab_url):
    env = api_env(tmp_path, lab_url)
    with httpx.Client(base_url=lab_url, trust_env=False, timeout=5) as panel:
        payload(panel.post("/_lab/reset"))
        with local_server(tmp_path, "app.main:app", env=env) as (url, _):
            with httpx.Client(base_url=url, trust_env=False, timeout=10) as api:
                admin, partner, partner_id, product = prepare(api, lab_url)
                adjust = f"/api/admin/partners/{partner_id}/adjust"
                payload(api.post(adjust, headers=partner, json={"asset": "toman", "amount": 1}), 401)
                payload(api.post(adjust, headers=admin, json={"asset": "toman", "amount": -100001}), 400)
                assert balance(api, partner) == 100000

                sub = create(api, partner, product)
                after_create = balance(api, partner)
                assert after_create < 100000
                route = f"/api/partner/subscriptions/{sub['id']}"
                remote_path = f"/_lab/users/{sub['v2_user_id']}"
                payload(api.post("/api/admin/partners", headers=admin, json={
                    "username": "other_stage6", "password": PASSWORD,
                }))
                other = login(api, "partner", "other_stage6")
                payload(api.post(route + "/note", headers=other, json={"note": "foreign"}), 404)

                payload(api.put(f"/api/admin/products/{product}", headers=admin, json={
                    "name": "renamed", "email_domain": "new.example.test", "price_per_gb": 1000,
                    "price_per_day": 100, "active": True,
                }))
                renewed_until = format_jalali_date(ts_to_tehran_date(sub["expired_at"]) + timedelta(days=3))
                payload(confirmed(api, partner, route + "/increase", "increase", {
                    "traffic_cap_gb": 12, "end_date": renewed_until,
                }, sub["id"]))
                assert balance(api, partner) == after_create - 2300
                remote = payload(panel.get(remote_path))["data"]
                assert remote["email"] == "stage6_user@old.example.test"
                assert remote["plan_id"] == 7 and remote["transfer_enable"] == 12 * GB
                payload(confirmed(api, partner, route + "/increase", "increase", {"traffic_cap_gb": 12}, sub["id"]), 400)

                payload(panel.post(remote_path + "/traffic", json={"u": 8 * GB}))
                assert not confirmed(api, partner, route + "/decrease", "decrease", {"traffic_cap_gb": 7}, sub["id"]).json()["affordable"]
                decreased = payload(confirmed(api, partner, route + "/decrease", "decrease", {"traffic_cap_gb": 10}, sub["id"]))
                assert Decimal(decreased["gb_refund"]) == 2
                assert balance(api, partner) == after_create - 2300
                payload(confirmed(api, partner, route + "/reset-traffic", "reset_traffic", sub_id=sub["id"]))
                assert payload(panel.get(remote_path))["data"]["u"] == 0
                after_reset = balance(api, partner)
                assert after_reset == after_create - 10300
                assert not confirmed(api, partner, route + "/reset-traffic", "reset_traffic", sub_id=sub["id"]).json()["affordable"]

                payload(api.post(f"/api/admin/partners/{partner_id}/ban", headers=admin))
                payload(api.get(route, headers=partner), 403)
                assert payload(panel.get(remote_path))["data"]["banned"] == 1
                payload(api.post(f"/api/admin/partners/{partner_id}/unban", headers=admin))
                assert payload(panel.get(remote_path))["data"]["banned"] == 0
                payload(confirmed(api, partner, route + "/delete", "delete", sub_id=sub["id"]))
                payload(api.post(route + "/delete", headers=partner), 404)
                assert not payload(panel.get("/_lab/users"))["data"]
                assert balance(api, partner) == after_reset
                payload(api.post("/api/partner/auth/logout", headers=partner))
                payload(api.get("/api/partner/me", headers=partner), 401)


def test_http_worker_crash_and_restart_preserve_one_charge(tmp_path):
    with local_server(tmp_path, "stage6_panel_lab:app",
                      env={"STAGE6_CHECKPOINT": str(tmp_path)}) as (lab_url, _):
        env = api_env(tmp_path, lab_url)
        increase = {"traffic_cap_gb": 12}
        with local_server(tmp_path, "app.main:app", env=env) as (url, worker):
            with httpx.Client(base_url=url, trust_env=False, timeout=10) as api:
                _, partner, partner_id, product = prepare(api, lab_url)
                sub = create(api, partner, product)
                before = balance(api, partner)
                quote = payload(api.post("/api/partner/subscriptions/quote", headers=partner,
                                         json={**increase, "action": "increase", "subscription_id": sub["id"]}))
                increase["consent_token"] = quote["consent_token"]
            route = f"/api/partner/subscriptions/{sub['id']}/increase"
            (tmp_path / "armed").touch()

            def send_increase():
                with httpx.Client(base_url=url, trust_env=False, timeout=50) as api:
                    return api.post(route, headers=partner, json=increase)

            with ThreadPoolExecutor(max_workers=1) as executor:
                response = executor.submit(send_increase)
                try:
                    deadline = time.monotonic() + 8
                    while not (tmp_path / "applied").exists():
                        assert not response.done(), "mutation ended before the panel checkpoint"
                        assert time.monotonic() < deadline, "panel checkpoint not reached"
                        time.sleep(0.02)
                    with local_server(tmp_path, "app.main:app", env=env) as (second_url, _):
                        with httpx.Client(base_url=second_url, trust_env=False, timeout=7) as second:
                            busy = payload(second.post(route, headers=partner, json=increase), 503)
                            assert busy["code"] == "operation_status"
                            assert balance(second, partner) == before - 2000
                    worker.kill()
                    worker.wait(timeout=5)
                    try:
                        response.result(timeout=5)
                    except httpx.TransportError:
                        pass
                    else:
                        raise AssertionError("the killed worker returned a response")
                finally:
                    (tmp_path / "release").touch()

        engine = make_engine(env["DATABASE_URL"])
        try:
            with Session(engine) as db:
                op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "increase"))
                assert op.status == "pending" and op.lease_token is not None
                assert db.get(Partner, partner_id).balance_toman == before - 2000
                # Advance this abandoned lease deterministically in the disposable DB.
                op.lease_until = int(time.time()) - 1
                db.commit()

            with local_server(tmp_path, "app.main:app", env=env) as (restarted, _):
                with httpx.Client(base_url=restarted, trust_env=False, timeout=10) as api:
                    assert balance(api, partner) == before - 2000
                    result = payload(api.get(f"/api/partner/subscriptions/{sub['id']}", headers=partner))
                    assert Decimal(result["traffic_cap_gb"]) == 12 and result["live_error"] is None
            with Session(engine) as db:
                operations = db.scalars(select(PanelOperation).where(PanelOperation.kind == "increase")).all()
                assert len(operations) == 1 and operations[0].status == "completed"
                rows = db.scalars(select(WalletLedger).where(WalletLedger.subscription_id == sub["id"])).all()
                assert [entry.amount for entry in rows
                        if entry.reason == "increase" and entry.asset == "toman"] == [Decimal(-2000)]
        finally:
            engine.dispose()
