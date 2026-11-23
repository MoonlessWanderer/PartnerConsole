"""Real HTTP across the API, its SQLite file, and the local panel simulator."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import hmac
import json
import time

import httpx
import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import make_engine
from app.core.timeutil import TZ, format_jalali_date, now_tehran, ts_to_tehran_date
from app.integrations.v2board import V2BoardClient
from app.models import PanelOperation, Partner, PartnerProductCredit, RevokedToken, Subscription, WalletLedger
from app.services.billing import BYTES_PER_GB
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


def advance_lab_user_id(panel):
    auth = payload(panel.post("/api/v1/passport/auth/login", json={
        "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD,
    }))["data"]["auth_data"]
    headers = {"Authorization": auth}
    users = f"/api/v1/{SECURE_PATH}/user"
    assert payload(panel.post(users + "/generate", headers=headers, json={
        "email_prefix": "fixture_only", "email_suffix": "fixture.example.test",
    }))["data"] is True
    disposable, = payload(panel.get("/_lab/users"))["data"]
    assert payload(panel.post(users + "/delUser", headers=headers, json={"id": disposable["id"]}))["data"] is True


def assert_completed_create_link(env, operation_id, created, remote_id):
    engine = make_engine(env["DATABASE_URL"])
    try:
        with Session(engine) as db:
            column_id, raw_payload, status, kind = db.execute(select(
                PanelOperation.subscription_id, PanelOperation.payload, PanelOperation.status, PanelOperation.kind,
            ).where(PanelOperation.id == operation_id)).one()
            local = db.get(Subscription, created["id"])
            assert local is not None and local.username == created["username"]
            recorded = json.loads(raw_payload)
            assert status == "completed" and kind == "create"
            assert local.id != remote_id
            assert column_id == local.id
            assert recorded["subscription_id"] == local.id
            assert recorded["v2_user_id"] == local.v2_user_id == created["v2_user_id"] == remote_id
            print(f"Completed create op={operation_id}: column.subscription_id={column_id}, "
                  f"payload.subscription_id={recorded['subscription_id']}, payload.v2_user_id={recorded['v2_user_id']}, "
                  f"local.id={local.id}, remote.id={remote_id}")
    finally:
        engine.dispose()


def test_http_retries_same_paid_purchase_after_lost_generate_reply(tmp_path):
    with local_server(tmp_path, "financial_panel_lab:app") as (lab_url, _):
        env = api_env(tmp_path, lab_url)
        with local_server(tmp_path, "app.main:app", env=env) as (url, _), \
             httpx.Client(base_url=url, trust_env=False, timeout=15) as api, \
             httpx.Client(base_url=lab_url, trust_env=False, timeout=10) as panel:
            admin, partner, partner_id, product_id = prepare(api, lab_url)
            advance_lab_user_id(panel)
            opening_counts = payload(panel.get("/_lab/consent-writes"))
            adjust = f"/api/admin/partners/{partner_id}/adjust"
            for asset, amount in (("gb", 3), ("days", 5)):
                payload(api.post(adjust, headers=admin,
                                 json={"product_id": product_id, "asset": asset, "amount": amount}))

            subscriptions = "/api/partner/subscriptions"
            ledger_path = "/api/partner/ledger"
            opening_entries = {row["id"] for row in payload(api.get(ledger_path, headers=partner))["items"]}
            username = "lost_paid_reply"
            email = f"{username}@old.example.test"

            def snapshot():
                remote = payload(panel.get("/_lab/users"))["data"]
                local = payload(api.get(subscriptions, headers=partner))["items"]
                product, = payload(api.get("/api/partner/products", headers=partner))["items"]
                ledger = payload(api.get(ledger_path, headers=partner))["items"]
                operations = payload(api.get("/api/admin/operations", headers=admin))["items"]
                counts = payload(panel.get("/_lab/consent-writes"))
                return {
                    "remote": [(row["id"], row["email"]) for row in remote],
                    "local": [(row["id"], row["v2_user_id"], row["username"]) for row in local],
                    "wallet": balance(api, partner),
                    "credit": (Decimal(product["credit_gb"]), product["credit_days"]),
                    "ledger": sorted((row["asset"], Decimal(row["amount"]), row["reason"])
                                     for row in ledger if row["id"] not in opening_entries),
                    "operations": [(row["id"], row["status"], row["subscription_id"]) for row in operations],
                    "generates": counts["generate_count"] - opening_counts["generate_count"],
                    "panel_writes": counts["count"] - opening_counts["count"],
                }

            before = snapshot()
            assert before == {
                "remote": [], "local": [], "wallet": 100_000, "credit": (3, 5), "ledger": [],
                "operations": [], "generates": 0, "panel_writes": 0,
            }
            body = {
                "product_id": product_id, "username": username, "traffic_cap_gb": "10",
                "end_date": format_jalali_date(now_tehran().date() + timedelta(days=31)),
            }
            quote = payload(api.post(subscriptions + "/quote", headers=partner,
                                     json={**body, "action": "create", "note": ""}))
            approved = (Decimal(quote["gb_from_credit"]), quote["days_from_credit"],
                        Decimal(quote["gb_paid"]), quote["days_paid"], quote["toman"])
            assert approved == (3, 5, 7, 26, 9600)
            print(f"Approved (credit GB/days, paid GB/days, toman): {approved}; before: {before}")
            payload(panel.post("/_lab/consent-drop-next-generate?disconnect=true"))

            confirmation = {**body, "note": None, "consent_token": quote["consent_token"]}
            response = api.post(subscriptions, headers=partner, json=confirmation)
            fault = payload(panel.get("/_lab/consent-lost-replies"))
            after_submit = snapshot()
            print(f"After submit (HTTP {response.status_code}): {after_submit}; at drop: {fault}")
            first_error = payload(response, 503)
            (operation_id, _, _), = after_submit["operations"]
            remote_user, = fault["users_at_drop"]
            assert fault["count"] == 1 and remote_user["email"] == email
            assert first_error["code"] == "operation_status"
            assert f"عملیات شماره {operation_id} ذخیره شد" in first_error["detail"]
            assert "تلاش بعدی همان را بدون جابه‌جایی دوباره موجودی ادامه می‌دهد" in first_error["detail"]
            charges = [("days", -5, "create"), ("gb", -3, "create"), ("toman", -9600, "create")]
            pending = {
                "remote": [(remote_user["id"], email)],
                "local": [],
                "wallet": 90_400,
                "credit": (0, 0),
                "ledger": charges,
                "operations": [(operation_id, "pending", None)],
                "generates": 1,
                "panel_writes": 1,
            }
            assert after_submit == pending

            # An operation_status error leaves the quote on the same purchase
            # form and clears pending. The next click submits the retained quote.
            created = payload(api.post(subscriptions, headers=partner, json=confirmation))
            assert created["v2_user_id"] == remote_user["id"]
            assert_completed_create_link(env, operation_id, created, remote_user["id"])
            after_retry = snapshot()
            assert after_retry == {
                **pending,
                "local": [(created["id"], created["v2_user_id"], username)],
                "operations": [(operation_id, "completed", created["id"])],
                "panel_writes": 2,
            }

            # On success the purchase page redirects to the returned ID. The
            # destination loads that subscription with GET and sends no confirmation.
            detail = payload(api.get(f"{subscriptions}/{created['id']}", headers=partner))
            assert detail["username"] == username and detail["v2_user_id"] == created["v2_user_id"]
            assert detail["status"] == "active" and detail["subscribe_url"] and detail["live_error"] is None
            after_detail = snapshot()
            assert after_detail == after_retry
            print(f"After same-form retry and detail load (HTTP 200, active): {after_detail}")


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


def test_http_create_requires_new_consent_after_another_purchase_spends_credit(tmp_path):
    route = "/api/partner/subscriptions"

    def funding(quote):
        return quote["toman"], Decimal(quote["gb_from_credit"]), quote["days_from_credit"]

    def new_debits(before, after):
        assert before["ledger"].items() <= after["ledger"].items()
        return sorted(after["ledger"][key] for key in after["ledger"].keys() - before["ledger"].keys())

    with local_server(tmp_path, "financial_panel_lab:app") as (lab_url, _):
        env = api_env(tmp_path, lab_url)
        with (local_server(tmp_path, "app.main:app", env=env) as (url, _),
              httpx.Client(base_url=url, trust_env=False, timeout=10) as api,
              httpx.Client(base_url=lab_url, trust_env=False, timeout=10) as panel):
            admin, partner, partner_id, product_id = prepare(api, lab_url)
            for asset, amount in (("gb", 7), ("days", 6)):
                payload(api.post(f"/api/admin/partners/{partner_id}/adjust", headers=admin,
                                 json={"asset": asset, "amount": amount, "product_id": product_id}))

            def snapshot():
                product, = payload(api.get("/api/partner/products", headers=partner))["items"]
                ledger = payload(api.get("/api/partner/ledger", headers=partner))["items"]
                subs = payload(api.get(route, headers=partner))["items"]
                ops = payload(api.get("/api/admin/operations", headers=admin))["items"]
                upstream = payload(panel.get("/_lab/users"))["data"]
                return {
                    "wallet": balance(api, partner),
                    "credit": (Decimal(product["credit_gb"]), product["credit_days"]),
                    "rates": (product["price_per_gb"], product["price_per_day"]),
                    "ledger": {row["id"]: (row["asset"], Decimal(row["amount"]), row["reason"]) for row in ledger},
                    "local": sorted((row["username"], row["v2_user_id"], row["status"]) for row in subs),
                    "remote": sorted((row["email"], row["id"], row["banned"]) for row in upstream),
                    "operations": sorted((row["id"], row["username"], row["status"]) for row in ops
                                         if row["kind"] == "create" and row["status"] != "failed"),
                    "panel_writes": payload(panel.get("/_lab/consent-writes"))["count"],
                }

            def quote(body):
                decision = payload(api.post(route + "/quote", headers=partner, json={**body, "action": "create"}))
                assert decision["affordable"] and decision["consent_token"]
                return decision

            today = now_tehran().date()
            a = {"product_id": product_id, "username": "credit_shift_a", "traffic_cap_gb": 10,
                 "end_date": format_jalali_date(today + timedelta(days=10))}
            b = {**a, "username": "credit_shift_b", "traffic_cap_gb": 4,
                 "end_date": format_jalali_date(today + timedelta(days=2))}
            before = snapshot()
            assert (before["wallet"], before["credit"], before["rates"]) == (100_000, (7, 6), (1000, 100))
            assert before["local"] == before["remote"] == before["operations"] == []

            original = quote(a)
            assert funding(original) == (3400, 7, 6)
            secret = (env["SECRET_KEY"] + ":partner").encode()
            key = hmac.digest(secret, b"partner-financial-consent-v1", "sha256")
            old_proof = jwt.decode(original["consent_token"], key, algorithms=["HS256"])
            authorization = jwt.decode(partner["Authorization"].removeprefix("Bearer "),
                                       sha256(secret).hexdigest(), algorithms=["HS256"])
            print(f"A original quote (toman, credit GB, credit days): {funding(original)}")

            b_quote = quote(b)
            assert funding(b_quote) == (0, 4, 2)
            created_b = payload(api.post(route, headers=partner, json={**b, "consent_token": b_quote["consent_token"]}))
            after_b = snapshot()
            assert after_b["wallet"] == before["wallet"]
            assert after_b["credit"] == (3, 4)
            assert after_b["rates"] == before["rates"] == (original["price_per_gb"], original["price_per_day"])
            assert new_debits(before, after_b) == [("days", -2, "create"), ("gb", -4, "create")]
            assert after_b["local"] == [(b["username"], created_b["v2_user_id"], "active")]
            assert after_b["remote"] == [(b["username"] + "@old.example.test", created_b["v2_user_id"], 0)]
            (_, b_username, b_status), = after_b["operations"]
            assert (b_username, b_status) == (b["username"], "completed")

            gb_rate, day_rate = after_b["rates"]
            gb_left, days_left = after_b["credit"]
            assert gb_left < Decimal(original["gb_from_credit"])
            assert days_left < original["days_from_credit"]
            repriced = (a["traffic_cap_gb"] - gb_left) * gb_rate + (original["need_days"] - days_left) * day_rate
            assert original["toman"] < repriced == 7600 < after_b["wallet"]
            assert old_proof["session"] == authorization["jti"] and old_proof["sub"] == authorization["sub"]
            engine = make_engine(env["DATABASE_URL"])
            try:
                with Session(engine) as db:
                    assert db.get(RevokedToken, authorization["jti"]) is None
                    used_refs = set(json.loads(row).get("consent_ref") for row in db.scalars(
                        select(PanelOperation.payload).where(PanelOperation.partner_id == partner_id)))
                    assert old_proof["ref"] not in used_refs
            finally:
                engine.dispose()
            assert old_proof["exp"] > time.time()
            print(f"After B: wallet={after_b['wallet']}, credit={after_b['credit']}, A now costs {repriced}")

            rejected = payload(api.post(route, headers=partner, json={**a, "consent_token": original["consent_token"]}), 409)
            assert rejected["code"] == "financial_consent"
            assert old_proof["exp"] > time.time()
            assert snapshot() == after_b
            print("A original confirmation: HTTP 409 financial_consent, no new ledger, operation, subscription or panel write")

            renewed = quote(a)
            new_proof = jwt.decode(renewed["consent_token"], key, algorithms=["HS256"])
            assert all(new_proof[field] == old_proof[field] for field in ("sub", "session", "inputs", "target"))
            assert new_proof["ref"] != old_proof["ref"] and new_proof["terms"] != old_proof["terms"]
            new_toman, new_gb, new_days = funding(renewed)
            assert (new_toman, new_gb, new_days) == (7600, 3, 4)
            assert (new_toman - original["toman"], Decimal(original["gb_from_credit"]) - new_gb,
                    original["days_from_credit"] - new_days) == (4200, 4, 2)
            print(f"A fresh quote (toman, credit GB, credit days): {(new_toman, new_gb, new_days)}")
            created_a = payload(api.post(route, headers=partner, json={**a, "consent_token": renewed["consent_token"]}))
            after_a = snapshot()
            assert funding(created_a["charge"]) == funding(renewed)
            assert after_b["wallet"] - after_a["wallet"] == new_toman
            assert after_a["credit"] == (gb_left - new_gb, days_left - new_days)
            assert new_debits(after_b, after_a) == [
                ("days", -new_days, "create"), ("gb", -new_gb, "create"), ("toman", -new_toman, "create"),
            ]
            assert after_a["local"] == [(a["username"], created_a["v2_user_id"], "active"), *after_b["local"]]
            assert after_a["remote"] == [
                (a["username"] + "@old.example.test", created_a["v2_user_id"], 0), *after_b["remote"],
            ]
            assert [(name, status) for _, name, status in after_a["operations"]] == [
                (b["username"], "completed"), (a["username"], "completed"),
            ]
            print(f"A fresh confirmation: HTTP 200, debits={new_debits(after_b, after_a)}, "
                  f"wallet={after_a['wallet']}, credit={after_a['credit']}")


@pytest.mark.parametrize("shortage", [0, 1], ids=["exact_balance", "one_toman_short"])
def test_http_purchase_at_wallet_balance_boundary(tmp_path, shortage):
    route = "/api/partner/subscriptions"
    volume_gb, days = 10, 5
    with local_server(tmp_path, "financial_panel_lab:app") as (lab_url, _):
        env = api_env(tmp_path, lab_url)
        with (local_server(tmp_path, "app.main:app", env=env) as (url, _),
              httpx.Client(base_url=url, trust_env=False, timeout=10) as api,
              httpx.Client(base_url=lab_url, trust_env=False, timeout=10) as panel):
            admin, partner, partner_id, product_id = prepare(api, lab_url)
            product, = payload(api.get("/api/partner/products", headers=partner))["items"]
            assert (product["id"], product["price_per_gb"], product["price_per_day"]) == (product_id, 1000, 100)
            price = volume_gb * product["price_per_gb"] + days * product["price_per_day"]
            assert price > 0
            opening = price - shortage
            payload(api.post(f"/api/admin/partners/{partner_id}/adjust", headers=admin, json={
                "asset": "toman", "amount": opening - balance(api, partner),
            }))

            def snapshot():
                engine = make_engine(env["DATABASE_URL"])
                try:
                    with Session(engine) as db:
                        credit = db.scalar(select(PartnerProductCredit).where(
                            PartnerProductCredit.partner_id == partner_id,
                            PartnerProductCredit.product_id == product_id,
                        ))
                        entries = db.scalars(select(WalletLedger).where(
                            WalletLedger.partner_id == partner_id).order_by(WalletLedger.id))
                        local = db.scalars(select(Subscription).where(Subscription.partner_id == partner_id))
                        operations = db.scalars(select(PanelOperation).where(PanelOperation.partner_id == partner_id))
                        state = {
                            "wallet": db.get(Partner, partner_id).balance_toman,
                            "credit": (credit.credit_gb, credit.credit_days) if credit is not None else (0, 0),
                            "ledger": [(row.id, row.asset, row.amount, row.reason) for row in entries],
                            "local": [(row.id, row.username, row.v2_user_id, row.expired_at, row.banned) for row in local],
                            "operations": [(row.kind, row.username, row.status) for row in operations],
                        }
                finally:
                    engine.dispose()
                users = payload(panel.get("/_lab/users"))["data"]
                state["upstream"] = [(row["id"], row["email"], row["expired_at"], row["banned"]) for row in users]
                state["panel_writes"] = payload(panel.get("/_lab/consent-writes"))["count"]
                return state

            body = {"product_id": product_id, "username": "wallet_boundary", "traffic_cap_gb": volume_gb,
                    "end_date": format_jalali_date(now_tehran().date() + timedelta(days=days))}
            before = snapshot()
            assert before["wallet"] == opening and before["credit"] == (0, 0)
            assert sum(amount for _, asset, amount, _ in before["ledger"] if asset == "toman") == opening
            assert before["local"] == before["upstream"] == before["operations"] == []

            quote = payload(api.post(route + "/quote", headers=partner, json={**body, "action": "create"}))
            token = quote.get("consent_token")
            confirmation = api.post(route, headers=partner, json={**body, "consent_token": token}) if token else None
            after = snapshot()
            assert quote["toman"] == price and quote["balance_toman"] == opening
            assert Decimal(quote["need_gb"]) == volume_gb and quote["need_days"] == days
            assert Decimal(quote["gb_from_credit"]) == quote["days_from_credit"] == 0
            assert quote["affordable"] is (shortage == 0) and quote["shortage_toman"] == shortage

            if shortage:
                assert "consent_token" not in quote and confirmation is None
                assert after == before
                print(f"P={price}, opening={opening}, closing={after['wallet']}, "
                      "quote=200/affordable=false/shortage_toman=1/no-consent, debit=0, local=0, upstream=0")
            else:
                assert confirmation is not None
                created = payload(confirmation)
                assert created["charge"]["toman"] == price and created["balance_toman"] == 0
                assert after["wallet"] == 0 and after["credit"] == (0, 0)
                assert after["ledger"][:-1] == before["ledger"]
                assert after["ledger"][-1][1:] == ("toman", -price, "create")
                assert created["status"] == "active" and created["expired_at"] > time.time()
                assert after["local"] == [
                    (created["id"], body["username"], created["v2_user_id"], created["expired_at"], False),
                ]
                assert after["upstream"] == [
                    (created["v2_user_id"], body["username"] + "@" + product["email_domain"], created["expired_at"], 0),
                ]
                assert after["operations"] == [("create", body["username"], "completed")]
                print(f"P={price}, opening={opening}, closing={after['wallet']}, "
                      f"quote=200/affordable=true, confirmation=200, debit={price}, local=1, upstream=1")


@pytest.mark.parametrize("funded_a", [False, True], ids=["zero_credit_a", "full_credit_a"])
def test_http_buying_one_product_does_not_spend_another_products_credit(tmp_path, lab_url, funded_a):
    route = "/api/partner/subscriptions"
    volume_gb, days = 10, 5
    credits_a = (12, 8) if funded_a else (0, 0)
    env = api_env(tmp_path, lab_url)
    with (local_server(tmp_path, "app.main:app", env=env) as (url, _),
          httpx.Client(base_url=url, trust_env=False, timeout=10) as api,
          httpx.Client(base_url=lab_url, trust_env=False, timeout=10) as panel):
        payload(panel.post("/_lab/reset"))
        admin, partner, partner_id, a_id = prepare(api, lab_url)
        panel_record, = payload(api.get("/api/admin/panels", headers=admin))["items"]
        panel_id = panel_record["id"]
        gb_rate, day_rate = 1200, 150
        product_b = payload(api.post("/api/admin/products", headers=admin, json={
            "panel_id": panel_id, "plan_id": 8, "name": "synthetic product B", "email_domain": "b.example.test",
            "price_per_gb": gb_rate, "price_per_day": day_rate,
        }))["product"]
        b_id = product_b["id"]
        assert a_id != b_id
        catalog = {row["id"]: row for row in payload(api.get("/api/partner/products", headers=partner))["items"]}
        assert set(catalog) == {a_id, b_id} and catalog[a_id]["active"] and catalog[b_id]["active"]
        assert (catalog[a_id]["plan_id"], catalog[b_id]["plan_id"]) == (7, 8)
        price = volume_gb * gb_rate + days * day_rate
        assert price == 12_750

        # Configure both cases through the usual adjustments, including the zero-credit row for A.
        adjust = f"/api/admin/partners/{partner_id}/adjust"
        for asset, grant, target in zip(("gb", "days"), (24, 16), credits_a):
            for amount in (grant, target - grant):
                payload(api.post(adjust, headers=admin, json={
                    "asset": asset, "amount": amount, "product_id": a_id,
                }))

        def snapshot():
            engine = make_engine(env["DATABASE_URL"])
            try:
                with Session(engine) as db:
                    credits = db.scalars(select(PartnerProductCredit).where(
                        PartnerProductCredit.partner_id == partner_id,
                    ))
                    entries = db.scalars(select(WalletLedger).where(
                        WalletLedger.partner_id == partner_id,
                    ).order_by(WalletLedger.id))
                    subs = db.scalars(select(Subscription).where(Subscription.partner_id == partner_id))
                    balances = {row.product_id: (row.credit_gb, row.credit_days) for row in credits}
                    state = {
                        "wallet": db.get(Partner, partner_id).balance_toman,
                        "credit_a": balances.get(a_id, (0, 0)),
                        "credit_b": balances.get(b_id, (0, 0)),
                        "ledger": [(row.id, row.asset, row.amount, row.product_id, row.reason) for row in entries],
                        "subscriptions": [
                            (sub.id, sub.product_id, sub.product.panel_id, sub.product.panel.base_url,
                             sub.v2_user_id, sub.plan_id) for sub in subs
                        ],
                    }
            finally:
                engine.dispose()
            state["upstream"] = [(row["id"], row["plan_id"], row["email"], row["banned"])
                                 for row in payload(panel.get("/_lab/users"))["data"]]
            return state

        body = {"product_id": b_id, "username": "credit_isolation_b", "traffic_cap_gb": volume_gb,
                "end_date": format_jalali_date(now_tehran().date() + timedelta(days=days))}
        before = snapshot()
        assert before["wallet"] == 100_000 > price
        assert before["credit_a"] == credits_a
        assert before["credit_b"] == (0, 0)
        assert before["subscriptions"] == before["upstream"] == []

        quote = payload(api.post(route + "/quote", headers=partner, json={**body, "action": "create"}))
        assert quote["affordable"] and quote["consent_token"]
        assert (quote["price_per_gb"], quote["price_per_day"]) == (gb_rate, day_rate)
        assert Decimal(quote["need_gb"]) == volume_gb and quote["need_days"] == days
        assert quote["toman"] == price
        assert Decimal(quote["gb_from_credit"]) == quote["days_from_credit"] == 0
        assert snapshot() == before

        created = payload(api.post(route, headers=partner, json={**body, "consent_token": quote["consent_token"]}))
        after = snapshot()
        assert (created["product_id"], created["status"]) == (b_id, "active")
        assert created["charge"]["toman"] == price
        assert Decimal(created["charge"]["gb_from_credit"]) == created["charge"]["days_from_credit"] == 0
        assert before["wallet"] - after["wallet"] == price
        assert after["credit_a"] == before["credit_a"] and after["credit_b"] == before["credit_b"]
        assert after["ledger"][:-1] == before["ledger"]
        assert after["ledger"][-1][1:] == ("toman", -price, b_id, "create")
        (local_id, stored_product, stored_panel, stored_origin, remote_id, stored_plan), = after["subscriptions"]
        assert (local_id, stored_product, stored_panel, stored_origin, remote_id, stored_plan) == (
            created["id"], b_id, panel_id, lab_url, created["v2_user_id"], 8,
        )
        assert after["upstream"] == [(remote_id, stored_plan, body["username"] + "@" + product_b["email_domain"], 0)]
        print(f"A={a_id}, B={b_id}, A credit={before['credit_a']} -> {after['credit_a']}, "
              f"B credit={before['credit_b']} -> {after['credit_b']}, quote={quote['toman']}, "
              f"debit={price}, wallet={after['wallet']}, product={stored_product}, "
              f"panel={stored_panel}, V2Board user={remote_id}, plan={stored_plan}")


@pytest.mark.parametrize("days_shorter", [0, 3], ids=["volume_only", "duration_only"])
def test_http_decrease_credits_only_the_subscription_product(tmp_path, lab_url, days_shorter):
    fixed_now = datetime(2026, 11, 23, 12, tzinfo=TZ)
    env = {**api_env(tmp_path, lab_url), "STAGE6_FIXED_TEHRAN_NOW": fixed_now.isoformat()}
    old_cap, used = 10, 2
    new_cap = old_cap if days_shorter else 7
    old_end = (fixed_now + timedelta(days=15)).replace(hour=23, minute=59, second=59)
    target_end = old_end - timedelta(days=days_shorter)
    old_expiry, target_expiry = int(old_end.timestamp()), int(target_end.timestamp())
    old_jalali, target_jalali = format_jalali_date(old_end.date()), format_jalali_date(target_end.date())
    assert fixed_now.date() < target_end.date() <= old_end.date()
    assert 0 < used < new_cap <= old_cap
    # Compute expectations independently from the documented refund and full-day rules.
    now_ts = int(fixed_now.timestamp())
    old_days = max(0, (old_expiry - now_ts) // 86_400)
    target_days = max(0, (target_expiry - now_ts) // 86_400)
    refund_gb = old_cap - new_cap
    refund_days = max(0, old_days - target_days)
    assert (old_days, target_days, refund_days) == (15, 15 - days_shorter, days_shorter)
    assert BYTES_PER_GB == GB == 1024 ** 3
    upload, download = GB // 2, 3 * GB // 2
    route = "/api/partner/subscriptions"
    with (local_server(tmp_path, "stage6_fixed_clock_api:app", env=env) as (url, _),
          httpx.Client(base_url=url, trust_env=False, timeout=10) as api,
          httpx.Client(base_url=lab_url, trust_env=False, timeout=10) as panel):
        payload(panel.post("/_lab/reset"))
        admin, partner, partner_id, b_id = prepare(api, lab_url)
        panel_record, = payload(api.get("/api/admin/panels", headers=admin))["items"]
        panel_id = panel_record["id"]
        a_id = payload(api.post("/api/admin/products", headers=admin, json={
            "panel_id": panel_id, "plan_id": 8, "name": "synthetic product A", "email_domain": "a.example.test",
            "price_per_gb": 1200, "price_per_day": 150,
        }))["product"]["id"]
        assert a_id != b_id
        created = payload(confirmed(api, partner, route, "create", {
            "product_id": b_id, "username": "decrease_product_b", "traffic_cap_gb": old_cap, "end_date": old_jalali,
        }))
        assert created["status"] == "active" and Decimal(created["traffic_cap_gb"]) == old_cap
        assert (created["days_left"], created["expired_at"], created["expired_jalali"]) == (old_days, old_expiry, old_jalali)
        sub_id, user_id = created["id"], created["v2_user_id"]
        payload(panel.post(f"/_lab/users/{user_id}/traffic", json={"u": upload, "d": download}))
        for product_id, amounts in ((a_id, (11, 9)), (b_id, (4, 6))):
            for asset, amount in zip(("gb", "days"), amounts):
                payload(api.post(f"/api/admin/partners/{partner_id}/adjust", headers=admin, json={
                    "product_id": product_id, "asset": asset, "amount": amount,
                }))

        def snapshot():
            engine = make_engine(env["DATABASE_URL"])
            try:
                with Session(engine) as db:
                    credits = db.scalars(select(PartnerProductCredit).where(
                        PartnerProductCredit.partner_id == partner_id))
                    entries = db.scalars(select(WalletLedger).where(
                        WalletLedger.partner_id == partner_id).order_by(WalletLedger.id))
                    subs = db.scalars(select(Subscription).where(Subscription.partner_id == partner_id))
                    ops = db.scalars(select(PanelOperation).where(
                        PanelOperation.partner_id == partner_id).order_by(PanelOperation.id))
                    state = {
                        "wallet": db.get(Partner, partner_id).balance_toman,
                        "credits": {row.product_id: (row.credit_gb, row.credit_days) for row in credits},
                        "ledger": [(row.id, row.asset, row.amount, row.product_id,
                                    row.subscription_id, row.reason) for row in entries],
                        "subscriptions": [
                            {"id": sub.id, "product_id": sub.product_id, "panel_id": sub.product.panel_id,
                             "panel_url": sub.product.panel.base_url, "v2_user_id": sub.v2_user_id,
                             "plan_id": sub.plan_id, "cap": sub.traffic_cap_gb, "expiry": sub.expired_at}
                            for sub in subs
                        ],
                        "operations": [(row.id, row.kind, row.status, row.product_id,
                                        row.subscription_id) for row in ops],
                    }
            finally:
                engine.dispose()
            fields = ("id", "plan_id", "email", "transfer_enable", "u", "d", "expired_at", "banned")
            state["upstream"] = [{key: row[key] for key in fields}
                                 for row in payload(panel.get("/_lab/users"))["data"]]
            return state

        before = snapshot()
        assert before["wallet"] == 88_500 and before["credits"] == {a_id: (11, 9), b_id: (4, 6)}
        local, = before["subscriptions"]
        assert local == {"id": sub_id, "product_id": b_id, "panel_id": panel_id, "panel_url": lab_url,
                         "v2_user_id": user_id, "plan_id": 7, "cap": old_cap, "expiry": old_expiry}
        upstream, = before["upstream"]
        assert upstream == {"id": user_id, "plan_id": 7, "email": "decrease_product_b@old.example.test",
                            "transfer_enable": old_cap * GB, "u": upload, "d": download,
                            "expired_at": local["expiry"], "banned": 0}

        change = {"end_date": target_jalali} if days_shorter else {"traffic_cap_gb": new_cap}
        quote = payload(api.post(route + "/quote", headers=partner,
                                 json={**change, "action": "decrease", "subscription_id": sub_id}))
        assert quote["affordable"] and quote["consent_token"]
        assert quote["used_gb"] == (None if days_shorter else str(used))
        assert (Decimal(quote["gb_refund"]), quote["days_refund"], quote["toman"]) == (refund_gb, refund_days, 0)
        assert snapshot() == before

        result = payload(api.post(f"{route}/{sub_id}/decrease", headers=partner,
                                  json={**change, "consent_token": quote["consent_token"]}))
        after = snapshot()
        assert (result["id"], result["v2_user_id"], result["status"]) == (sub_id, user_id, "active")
        assert (Decimal(result["gb_refund"]), result["days_refund"]) == (refund_gb, refund_days)
        assert result["balance_toman"] == after["wallet"] == before["wallet"]
        assert after["credits"] == {a_id: before["credits"][a_id], b_id: (4 + refund_gb, 6 + refund_days)}
        asset, refunded = ("days", refund_days) if days_shorter else ("gb", refund_gb)
        assert after["ledger"][:-1] == before["ledger"]
        assert after["ledger"][-1][1:] == (asset, refunded, b_id, sub_id, "decrease")
        assert after["operations"][:-1] == before["operations"]
        assert after["operations"][-1][1:] == ("decrease", "completed", b_id, sub_id)
        assert Decimal(result["traffic_cap_gb"]) == new_cap
        assert (result["days_left"], result["expired_at"], result["expired_jalali"]) == (
            target_days, target_expiry, target_jalali,
        )
        assert after["subscriptions"] == [{**local, "cap": new_cap, "expiry": target_expiry}]
        assert after["upstream"] == [{**upstream, "transfer_enable": new_cap * GB, "expired_at": target_expiry}]

        detail = payload(api.get(f"{route}/{sub_id}", headers=partner))
        assert detail["live_error"] is None
        live, = after["upstream"]
        live_cap = Decimal(live["transfer_enable"]) / BYTES_PER_GB
        live_used = Decimal(live["u"] + live["d"]) / BYTES_PER_GB
        assert Decimal(detail["traffic_cap_gb"]) == live_cap == new_cap
        assert Decimal(detail["used_gb"]) == live_used == used
        panel_expiry = int(live["expired_at"])
        panel_jalali = format_jalali_date(ts_to_tehran_date(panel_expiry))
        assert panel_jalali == target_jalali and detail["expired_at"] == panel_expiry == target_expiry
        print(f"Tehran clock={fixed_now.isoformat()}, expiry before={old_jalali} ({old_expiry}), "
              f"target={target_jalali}, final local={result['expired_at']}, "
              f"final panel={panel_expiry} ({panel_jalali}), days_left={old_days} -> {target_days}, "
              f"quoted days={quote['days_refund']}, recorded days={result['days_refund']}, "
              f"ledger=+{refunded} {asset} for B={b_id}, wallet={before['wallet']} -> {after['wallet']}, "
              f"credits={before['credits']} -> {after['credits']}, cap={old_cap} -> {live_cap} GB, "
              f"used={live_used} GB, subscription={sub_id}, panel={panel_id}, V2Board user={user_id}")


@pytest.mark.parametrize(("extra_at_quote", "extra_at_confirmation"), [
    pytest.param(0, 0, id="equal_to_cap"),
    pytest.param(1, 1, id="one_byte_over"),
    pytest.param(0, 1, id="one_byte_over_after_quote"),
])
def test_http_decreasing_volume_at_panel_usage_boundary(tmp_path, lab_url, extra_at_quote, extra_at_confirmation):
    env = api_env(tmp_path, lab_url)
    old_cap, new_cap = 10, 7
    upload, download = 3 * GB, 4 * GB
    with (local_server(tmp_path, "app.main:app", env=env) as (url, _),
          httpx.Client(base_url=url, trust_env=False, timeout=10) as api,
          httpx.Client(base_url=lab_url, trust_env=False, timeout=10) as panel,
          V2BoardClient(lab_url, SECURE_PATH, ADMIN_EMAIL, ADMIN_PASSWORD) as gateway):
        payload(panel.post("/_lab/reset"))
        admin, partner, partner_id, product_b = prepare(api, lab_url)
        created = create(api, partner, product_b, "decrease_usage_boundary")
        sub_id, v2_id = created["id"], created["v2_user_id"]
        assert created["status"] == "active"
        payload(api.post(f"/api/admin/partners/{partner_id}/adjust", headers=admin,
                         json={"asset": "gb", "amount": 4, "product_id": product_b}))
        traffic_path = f"/_lab/users/{v2_id}/traffic"
        payload(panel.post(traffic_path, json={"u": upload, "d": download + extra_at_quote}))

        def snapshot():
            # Use the same getUserInfoById route as the API to read the raw counters.
            remote = gateway.get_user_info(v2_id)
            engine = make_engine(env["DATABASE_URL"])
            try:
                with Session(engine) as db:
                    credit = db.scalar(select(PartnerProductCredit).where(
                        PartnerProductCredit.partner_id == partner_id,
                        PartnerProductCredit.product_id == product_b,
                    ))
                    ledger = db.scalars(select(WalletLedger).where(
                        WalletLedger.partner_id == partner_id).order_by(WalletLedger.id))
                    ops = db.scalars(select(PanelOperation).where(
                        PanelOperation.partner_id == partner_id).order_by(PanelOperation.id))
                    sub = db.get(Subscription, sub_id)
                    return {
                        "wallet": db.get(Partner, partner_id).balance_toman,
                        "credit": (credit.credit_gb, credit.credit_days),
                        "local_cap": sub.traffic_cap_gb,
                        "identity": (sub.id, sub.product_id, sub.v2_user_id, int(remote["id"])),
                        "rates": (sub.product.price_per_gb, sub.product.price_per_day),
                        "panel_cap": int(remote["transfer_enable"]),
                        "usage": (int(remote["u"]), int(remote["d"])),
                        "ledger": [(row.id, row.asset, row.amount, row.product_id,
                                    row.subscription_id, row.reason) for row in ledger],
                        "operations": [(row.id, row.kind, row.status, row.product_id,
                                        row.subscription_id) for row in ops],
                    }
            finally:
                engine.dispose()

        before = snapshot()
        assert before["wallet"] == 88_500 and before["credit"] == (4, 0)
        assert before["local_cap"] == old_cap and before["panel_cap"] == old_cap * GB
        assert before["identity"] == (sub_id, product_b, v2_id, v2_id)
        assert [row[1:3] for row in before["operations"]] == [("create", "completed")]
        assert before["rates"] == (1000, 100)
        assert before["usage"] == (upload, download + extra_at_quote)
        assert sum(before["usage"]) == new_cap * GB + extra_at_quote
        route = "/api/partner/subscriptions"
        change = {"traffic_cap_gb": new_cap}
        quote_response = api.post(route + "/quote", headers=partner, json={
            **change, "action": "decrease", "subscription_id": sub_id,
        })
        quote = payload(quote_response)
        assert snapshot() == before
        token = quote.get("consent_token")
        if not extra_at_quote:
            assert quote["affordable"] and token
            assert Decimal(quote["gb_refund"]) == old_cap - new_cap
            assert Decimal(quote["used_gb"]) == new_cap
        if extra_at_confirmation != extra_at_quote:
            payload(panel.post(traffic_path, json={"u": upload, "d": download + extra_at_confirmation}))
            secret = (env["SECRET_KEY"] + ":partner").encode()
            key = hmac.digest(secret, b"partner-financial-consent-v1", "sha256")
            proof = jwt.decode(token, key, algorithms=["HS256"])
            session = jwt.decode(partner["Authorization"].removeprefix("Bearer "),
                                 sha256(secret).hexdigest(), algorithms=["HS256"])
            assert proof["session"] == session["jti"] and proof["exp"] > time.time()

        before_confirmation = snapshot()
        assert before_confirmation == {**before, "usage": (upload, download + extra_at_confirmation)}
        confirmation = None
        if quote["affordable"] and token:
            confirmation = api.post(f"{route}/{sub_id}/decrease", headers=partner,
                                    json={**change, "consent_token": token})
        after = snapshot()
        ledger_delta = after["ledger"][len(before["ledger"]):]
        print(f"raw at quote={sum(before['usage'])} bytes; "
              f"raw before confirmation phase={sum(before_confirmation['usage'])} bytes; "
              f"quote={quote_response.status_code}, affordable={quote['affordable']}, "
              f"quote_refund={quote['gb_refund']}, "
              f"confirmation={confirmation.status_code if confirmation is not None else 'not sent'}, "
              f"panel_cap={after['panel_cap']} bytes, wallet={after['wallet']}, "
              f"B credit={before['credit']} -> {after['credit']}, ledger_delta={ledger_delta}")
        assert after["usage"] == before_confirmation["usage"]
        assert after["wallet"] == before["wallet"]
        if extra_at_quote:
            assert quote["affordable"] is False
            assert quote["message"] == "مصرف فعلی بیشتر از سقف جدید است"
            assert "consent_token" not in quote and confirmation is None
            assert Decimal(quote["gb_refund"]) == 0
            assert after == before
        elif extra_at_confirmation:
            assert confirmation is not None
            assert payload(confirmation, 400) == {
                "code": "request_rejected",
                "detail": "مصرف فعلی بیشتر از سقف جدید است",
            }
            assert after == before_confirmation
            print("Confirmation rejected for excess usage; no new operation or financial entry.")
        else:
            refund = old_cap - new_cap
            assert quote["affordable"] is True and confirmation is not None
            assert Decimal(quote["gb_refund"]) == refund and quote["days_refund"] == quote["toman"] == 0
            result = payload(confirmation)
            assert Decimal(result["gb_refund"]) == refund and result["days_refund"] == 0
            assert after["local_cap"] == new_cap and after["panel_cap"] == new_cap * GB
            assert after["credit"] == (before["credit"][0] + refund, before["credit"][1])
            assert after["ledger"][:-1] == before["ledger"]
            assert [row[1:] for row in ledger_delta] == [("gb", refund, product_b, sub_id, "decrease")]
            assert after["operations"][:-1] == before["operations"]
            assert after["operations"][-1][1:] == ("decrease", "completed", product_b, sub_id)


@pytest.fixture
def deletion_panel(tmp_path):
    with (local_server(tmp_path, "stage6_delete_panel_lab:app") as (url, _),
          httpx.Client(base_url=url, trust_env=False, timeout=10) as panel):
        yield url, panel


@pytest.mark.parametrize(("lose_reply", "lookup_fault"), [
    pytest.param(False, None, id="success"),
    pytest.param(True, None, id="lost_reply"),
    pytest.param(True, "unavailable", id="lost_reply_lookup_503"),
    pytest.param(True, "malformed", id="lost_reply_lookup_malformed"),
])
def test_http_delete_returns_unused_volume_and_remaining_days_to_subscription_product(
    tmp_path, deletion_panel, lose_reply, lookup_fault,
):
    lab_url, panel = deletion_panel
    deletion_time = datetime(2026, 11, 23, 12, tzinfo=TZ)
    purchase_time = deletion_time - timedelta(days=5)
    end = (deletion_time + timedelta(days=10)).replace(hour=23, minute=59, second=59)
    end_jalali, expiry = format_jalali_date(end.date()), int(end.timestamp())
    purchased_days = (end.date() - purchase_time.date()).days
    cap = 10
    upload, download = GB // 2, 3 * GB // 2
    used = Decimal(upload + download) / BYTES_PER_GB
    expected_gb = max(0, cap - used)
    expected_days = max(0, (expiry - int(deletion_time.timestamp())) // 86_400)
    assert (used, expected_gb, purchased_days, expected_days) == (2, 8, 15, 10)
    api_target = "stage6_fixed_clock_api:app"
    env = api_env(tmp_path, lab_url)
    route = "/api/partner/subscriptions"

    with (local_server(tmp_path, api_target,
                       env={**env, "STAGE6_FIXED_TEHRAN_NOW": purchase_time.isoformat()}) as (url, _),
          httpx.Client(base_url=url, trust_env=False, timeout=10) as api):
        payload(panel.post("/_lab/reset"))
        admin, partner, partner_id, b_id = prepare(api, lab_url)
        panel_record, = payload(api.get("/api/admin/panels", headers=admin))["items"]
        panel_id = panel_record["id"]
        a_id = payload(api.post("/api/admin/products", headers=admin, json={
            "panel_id": panel_id, "plan_id": 8, "name": "synthetic product A", "email_domain": "a.example.test",
            "price_per_gb": 1200, "price_per_day": 150,
        }))["product"]["id"]
        assert a_id != b_id
        created = payload(confirmed(api, partner, route, "create", {
            "product_id": b_id, "username": "delete_product_b", "traffic_cap_gb": cap, "end_date": end_jalali,
        }))
        assert created["status"] == "active" and created["days_left"] == purchased_days
        assert created["charge"]["need_days"] == purchased_days and created["expired_at"] == expiry
        sub_id, user_id = created["id"], created["v2_user_id"]
        payload(panel.post(f"/_lab/users/{user_id}/traffic", json={"u": upload, "d": download}))

    delete_env = {**env, "STAGE6_FIXED_TEHRAN_NOW": deletion_time.isoformat()}
    delete_path = f"{route}/{sub_id}/delete"
    with (local_server(tmp_path, api_target, env=delete_env) as (url, _),
          httpx.Client(base_url=url, trust_env=False, timeout=10) as api):
        for product_id, amounts in ((a_id, (11, 9)), (b_id, (4, 6))):
            for asset, amount in zip(("gb", "days"), amounts):
                payload(api.post(f"/api/admin/partners/{partner_id}/adjust", headers=admin, json={
                    "product_id": product_id, "asset": asset, "amount": amount,
                }))

        def snapshot():
            engine = make_engine(env["DATABASE_URL"])
            try:
                with Session(engine) as db:
                    credits = db.scalars(select(PartnerProductCredit).where(
                        PartnerProductCredit.partner_id == partner_id))
                    rows = db.scalars(select(WalletLedger).where(
                        WalletLedger.partner_id == partner_id).order_by(WalletLedger.id))
                    subs = db.scalars(select(Subscription).where(Subscription.partner_id == partner_id))
                    ops = db.scalars(select(PanelOperation).where(
                        PanelOperation.partner_id == partner_id).order_by(PanelOperation.id))
                    state = {
                        "wallet": db.get(Partner, partner_id).balance_toman,
                        "credits": {row.product_id: (row.credit_gb, row.credit_days) for row in credits},
                        "ledger": [(row.id, row.asset, row.amount, row.product_id,
                                    row.subscription_id, row.reason) for row in rows],
                        "subscriptions": [
                            (sub.id, sub.product_id, sub.product.panel_id, sub.v2_user_id, sub.traffic_cap_gb,
                             sub.expired_at, sub.created_at) for sub in subs
                        ],
                        "operations": [(op.id, op.kind, op.status, op.product_id, op.subscription_id,
                                        json.loads(op.payload)) for op in ops],
                    }
            finally:
                engine.dispose()
            fields = ("id", "plan_id", "email", "transfer_enable", "u", "d", "expired_at")
            state["upstream"] = [{key: row[key] for key in fields}
                                 for row in payload(panel.get("/_lab/users"))["data"]]
            return state

        before = snapshot()
        assert before["wallet"] == 88_500 and before["credits"] == {a_id: (11, 9), b_id: (4, 6)}
        (local_id, product_id, stored_panel_id, remote_id, local_cap, local_expiry, started_at), = before["subscriptions"]
        assert (local_id, product_id, stored_panel_id, remote_id, local_cap, local_expiry) == (
            sub_id, b_id, panel_id, user_id, cap, expiry,
        )
        assert started_at.replace(tzinfo=timezone.utc).astimezone(TZ) == purchase_time < deletion_time < end
        assert before["upstream"] == [{
            "id": user_id, "plan_id": 7, "email": "delete_product_b@old.example.test",
            "transfer_enable": cap * GB, "u": upload, "d": download, "expired_at": expiry,
        }]
        detail = payload(api.get(f"{route}/{sub_id}", headers=partner))
        assert detail["status"] == "active" and detail["days_left"] == expected_days
        assert Decimal(detail["used_gb"]) == used and detail["live_error"] is None
        quote = payload(api.post(route + "/quote", headers=partner,
                                 json={"action": "delete", "subscription_id": sub_id}))
        assert quote["affordable"] and quote["consent_token"] and quote["toman"] == 0
        assert (Decimal(quote["gb_refund"]), quote["days_refund"], Decimal(quote["used_gb"])) == (
            expected_gb, expected_days, used,
        )
        assert snapshot() == before

        if lose_reply:
            payload(panel.post(f"/_lab/drop-delete-reply/{user_id}"))
        response = api.post(delete_path, headers=partner, json={"consent_token": quote["consent_token"]})
        after = snapshot()
        if lose_reply:
            pending = after
            error = payload(response, 503)
            pending_id, kind, status, product, linked_sub, audit = pending["operations"][-1]
            assert error["code"] == "operation_status" and f"عملیات شماره {pending_id} ذخیره شد" in error["detail"]
            assert (kind, status, product, linked_sub) == ("delete", "pending", b_id, sub_id)
            assert audit["panel_sent"] is True and audit["panel_applied"] is False
            assert (audit["subscription_id"], audit["v2_user_id"]) == (sub_id, user_id)
            assert (Decimal(audit["used_gb"]), Decimal(audit["gb_back"]), audit["days_back"]) == (
                used, expected_gb, expected_days,
            )
            assert pending["subscriptions"] == before["subscriptions"] and pending["upstream"] == []
            assert pending["wallet"] == before["wallet"]
            assert pending["credits"] == {a_id: before["credits"][a_id], b_id: (4 + expected_gb, 6 + expected_days)}
            assert pending["ledger"][:-2] == before["ledger"]
            assert sorted(row[1:] for row in pending["ledger"][-2:]) == [
                ("days", expected_days, b_id, sub_id, "delete_refund"),
                ("gb", expected_gb, b_id, sub_id, "delete_refund"),
            ]
            drop_evidence = {"delete_ids": [user_id], "dropped_id": user_id, "lookups": []}
            assert payload(panel.get("/_lab/delete-evidence")) == drop_evidence
            print(f"After dropped reply: HTTP {response.status_code} {error['code']}, "
                  f"local subscription={sub_id}, empty panel, operation={pending_id}/{status}, "
                  f"B={pending['credits'][b_id]}, refund={expected_gb} GB/{expected_days} days")
        else:
            result = payload(response)
            assert (Decimal(result["gb_refund"]), result["days_refund"]) == (expected_gb, expected_days)
            assert result["balance_toman"] == after["wallet"]
            payload(api.get(f"{route}/{sub_id}", headers=partner), 404)

    if lose_reply:
        failed_lookups = []
        if lookup_fault is not None:
            payload(panel.put(f"/_lab/delete-lookup-fault/{lookup_fault}"))
            with local_server(tmp_path, api_target, env=delete_env):
                blocked = snapshot()
                observed = payload(panel.get("/_lab/delete-evidence"))
                failed_lookups = observed["lookups"]
                assert observed == {**drop_evidence, "lookups": failed_lookups}
                expected_status = 503 if lookup_fault == "unavailable" else 200
                assert failed_lookups and all(row == {
                    "user_id": user_id, "status": expected_status, "ids": None, "fault": lookup_fault,
                } for row in failed_lookups)
                assert blocked == pending
                print(f"Invalid lookup ({lookup_fault}): responses={[row['status'] for row in failed_lookups]}, "
                      f"operation={pending_id}/{blocked['operations'][-1][2]}, "
                      f"local subscription={sub_id}, panel user absent, "
                      f"B={blocked['credits'][b_id]}, refund still {expected_gb} GB/{expected_days} days")
            payload(panel.delete("/_lab/delete-lookup-fault"))

        # Run the normal startup reconciler on the same temporary DB. No repeat confirmation.
        with (local_server(tmp_path, api_target, env=delete_env) as (url, _),
              httpx.Client(base_url=url, trust_env=False, timeout=10) as api):
            after = snapshot()
            payload(api.get(f"{route}/{sub_id}", headers=partner), 404)
            proof = payload(panel.get("/_lab/delete-evidence"))
            assert proof == {
                **drop_evidence, "lookups": [*failed_lookups, {"user_id": user_id, "status": 200, "ids": []}],
            }
            assert [row[0] for row in after["ledger"]] == [row[0] for row in pending["ledger"]]
            assert after["operations"][-1][0] == pending_id
            print(f"Startup recovery evidence: {proof}")

    assert after["wallet"] == before["wallet"]
    assert after["credits"] == {a_id: before["credits"][a_id], b_id: (4 + expected_gb, 6 + expected_days)}
    assert after["ledger"][:-2] == [
        (entry_id, asset, amount, pid, None if linked_sub == sub_id else linked_sub, reason)
        for entry_id, asset, amount, pid, linked_sub, reason in before["ledger"]
    ]
    assert sorted(row[1:] for row in after["ledger"][-2:]) == [
        ("days", expected_days, b_id, None, "delete_refund"),
        ("gb", expected_gb, b_id, None, "delete_refund"),
    ]
    assert after["subscriptions"] == after["upstream"] == []
    assert [op[0] for op in after["operations"][:-1]] == [op[0] for op in before["operations"]]
    (op_id, kind, status, op_product, op_fk, audit) = after["operations"][-1]
    assert (kind, status, op_product, op_fk) == ("delete", "completed", b_id, None)
    assert (audit["subscription_id"], audit["v2_user_id"]) == (sub_id, user_id)
    assert audit["panel_applied"] is True and audit["panel_sent"] is True
    print(f"purchase={purchase_time.isoformat()}, delete={deletion_time.isoformat()}, expiry={end_jalali}; "
          f"duration={purchased_days} days, full days left={expected_days}; "
          f"quote=+{quote['gb_refund']} GB/+{quote['days_refund']} days; "
          f"B {before['credits'][b_id]} -> {after['credits'][b_id]}, A={after['credits'][a_id]}, "
          f"wallet={after['wallet']}; ledger=+{expected_gb} GB/+{expected_days} days; "
          f"subscription={sub_id}, panel={panel_id}, remote user={user_id} removed, operation={op_id} completed")


def test_http_volume_increase_spends_product_credit_before_wallet(tmp_path, lab_url):
    env = api_env(tmp_path, lab_url)
    route = "/api/partner/subscriptions"
    old_cap, new_cap, used = 10, 15, 2
    initial_credit_gb, initial_credit_days = 3, 6
    gb_rate = 1000
    # START.md charges only the added volume, using the subscription's product credit first.
    added_gb = new_cap - old_cap
    paid_gb = added_gb - initial_credit_gb
    expected_toman = paid_gb * gb_rate
    expected_charge = {
        "need_gb": str(added_gb), "need_days": 0,
        "gb_from_credit": str(initial_credit_gb), "days_from_credit": 0,
        "gb_paid": str(paid_gb), "days_paid": 0, "toman": expected_toman,
    }

    with (local_server(tmp_path, "app.main:app", env=env) as (url, _),
          httpx.Client(base_url=url, trust_env=False, timeout=10) as api,
          httpx.Client(base_url=lab_url, trust_env=False, timeout=10) as panel):
        payload(panel.post("/_lab/reset"))
        admin, partner, partner_id, b_id = prepare(api, lab_url)
        panel_record, = payload(api.get("/api/admin/panels", headers=admin))["items"]
        panel_id = panel_record["id"]
        a_id = payload(api.post("/api/admin/products", headers=admin, json={
            "panel_id": panel_id, "plan_id": 8, "name": "synthetic product A", "email_domain": "a.example.test",
            "price_per_gb": 1200, "price_per_day": 150,
        }))["product"]["id"]
        assert a_id != b_id
        created = create(api, partner, b_id, "volume_upgrade_b")
        sub_id, v2_id = created["id"], created["v2_user_id"]
        assert created["status"] == "active" and Decimal(created["traffic_cap_gb"]) == old_cap
        upload, download = GB // 2, 3 * GB // 2
        assert upload + download == used * GB
        payload(panel.post(f"/_lab/users/{v2_id}/traffic", json={"u": upload, "d": download}))
        for product_id, credit in ((a_id, (11, 9)), (b_id, (initial_credit_gb, initial_credit_days))):
            for asset, amount in zip(("gb", "days"), credit):
                payload(api.post(f"/api/admin/partners/{partner_id}/adjust", headers=admin, json={
                    "product_id": product_id, "asset": asset, "amount": amount,
                }))

        def snapshot():
            engine = make_engine(env["DATABASE_URL"])
            try:
                with Session(engine) as db:
                    credit = db.scalars(select(PartnerProductCredit).where(
                        PartnerProductCredit.partner_id == partner_id))
                    ledger = db.scalars(select(WalletLedger).where(
                        WalletLedger.partner_id == partner_id).order_by(WalletLedger.id))
                    subs = db.scalars(select(Subscription).where(Subscription.partner_id == partner_id))
                    operations = db.scalars(select(PanelOperation).where(
                        PanelOperation.partner_id == partner_id).order_by(PanelOperation.id))
                    state = {
                        "wallet": db.get(Partner, partner_id).balance_toman,
                        "credits": {row.product_id: (row.credit_gb, row.credit_days) for row in credit},
                        "ledger": [(row.id, row.asset, row.amount, row.product_id,
                                    row.subscription_id, row.reason) for row in ledger],
                        "subscriptions": [
                            {"id": sub.id, "product_id": sub.product_id, "panel_id": sub.product.panel_id,
                             "v2_user_id": sub.v2_user_id, "cap": sub.traffic_cap_gb, "expiry": sub.expired_at,
                             "rates": (sub.product.price_per_gb, sub.product.price_per_day)} for sub in subs
                        ],
                        "operations": [(row.id, row.kind, row.status, row.product_id,
                                        row.subscription_id) for row in operations],
                    }
            finally:
                engine.dispose()
            keys = ("id", "plan_id", "email", "transfer_enable", "u", "d", "expired_at", "banned")
            state["upstream"] = [{key: row[key] for key in keys}
                                 for row in payload(panel.get("/_lab/users"))["data"]]
            return state

        before = snapshot()
        assert before["wallet"] == 88_500 > expected_toman
        assert before["credits"] == {a_id: (11, 9), b_id: (initial_credit_gb, initial_credit_days)}
        local, = before["subscriptions"]
        assert local == {"id": sub_id, "product_id": b_id, "panel_id": panel_id, "v2_user_id": v2_id,
                         "cap": old_cap, "expiry": created["expired_at"], "rates": (gb_rate, 100)}
        upstream, = before["upstream"]
        assert upstream == {"id": v2_id, "plan_id": 7, "email": "volume_upgrade_b@old.example.test",
                            "transfer_enable": old_cap * GB, "u": upload, "d": download,
                            "expired_at": local["expiry"], "banned": 0}

        change = {"traffic_cap_gb": new_cap}
        quote = payload(api.post(route + "/quote", headers=partner,
                                 json={**change, "action": "increase", "subscription_id": sub_id}))
        assert quote["affordable"] and quote["consent_token"] and quote["price_per_gb"] == gb_rate
        assert {field: quote[field] for field in expected_charge} == expected_charge
        assert snapshot() == before

        result = payload(api.post(f"{route}/{sub_id}/increase", headers=partner,
                                  json={**change, "consent_token": quote["consent_token"]}))
        after = snapshot()
        assert result["charge"] == expected_charge
        assert (result["id"], result["v2_user_id"], result["status"]) == (sub_id, v2_id, "active")
        assert Decimal(result["traffic_cap_gb"]) == new_cap and result["expired_at"] == local["expiry"]
        assert result["balance_toman"] == after["wallet"] == before["wallet"] - expected_toman
        assert after["credits"] == {a_id: before["credits"][a_id], b_id: (0, initial_credit_days)}
        assert after["ledger"][:-2] == before["ledger"]
        assert sorted(row[1:] for row in after["ledger"][-2:]) == [
            ("gb", -initial_credit_gb, b_id, sub_id, "increase"),
            ("toman", -expected_toman, b_id, sub_id, "increase"),
        ]
        assert after["operations"][:-1] == before["operations"]
        assert after["operations"][-1][1:] == ("increase", "completed", b_id, sub_id)
        assert after["subscriptions"] == [{**local, "cap": new_cap}]
        assert after["upstream"] == [{**upstream, "transfer_enable": new_cap * GB}]
        print(f"B rate={gb_rate} toman/GB; quote={quote['need_gb']} GB with {quote['gb_from_credit']} GB credit "
              f"and {quote['toman']} toman; ledger=-{initial_credit_gb} GB/-{expected_toman} toman; "
              f"wallet={before['wallet']} -> {after['wallet']}; panel={new_cap} GB ({new_cap * GB} bytes), "
              f"usage={used} GB and expiry={created['expired_jalali']} unchanged; subscription={sub_id}, user={v2_id}")


@pytest.mark.parametrize("increase_usage", [False, True], ids=["fixed_usage", "usage_increases_before_confirmation"])
def test_http_traffic_reset_prices_raw_bytes_and_requires_fresh_consent(tmp_path, increase_usage):
    route = "/api/partner/subscriptions"
    rate = 1000
    upload = 536_870_912
    starting_total = 1_610_612_735
    final_total = 2_147_483_648 if increase_usage else starting_total
    starting_toman = starting_total * rate // 1_073_741_824
    expected_toman = final_total * rate // 1_073_741_824
    assert starting_toman == 1499 and expected_toman == (2000 if increase_usage else 1499)

    with local_server(tmp_path, "financial_panel_lab:app") as (lab_url, _):
        env = api_env(tmp_path, lab_url)
        with (local_server(tmp_path, "app.main:app", env=env) as (url, _),
              httpx.Client(base_url=url, trust_env=False, timeout=10) as api,
              httpx.Client(base_url=lab_url, trust_env=False, timeout=10) as panel):
            admin, partner, partner_id, b_id = prepare(api, lab_url)
            created = create(api, partner, b_id, "traffic_reset_b")
            sub_id, user_id = created["id"], created["v2_user_id"]
            assert created["status"] == "active"
            adjust = f"/api/admin/partners/{partner_id}/adjust"
            for asset, amount in (("gb", 3), ("days", 6)):
                payload(api.post(adjust, headers=admin, json={"product_id": b_id, "asset": asset, "amount": amount}))

            def set_usage(total):
                payload(panel.post(f"/_lab/users/{user_id}/traffic",
                                   json={"u": upload, "d": total - upload}))

            def snapshot():
                engine = make_engine(env["DATABASE_URL"])
                try:
                    with Session(engine) as db:
                        credit = db.scalar(select(PartnerProductCredit).where(
                            PartnerProductCredit.partner_id == partner_id, PartnerProductCredit.product_id == b_id))
                        rows = db.scalars(select(WalletLedger).where(
                            WalletLedger.partner_id == partner_id).order_by(WalletLedger.id))
                        ops = db.scalars(select(PanelOperation).where(
                            PanelOperation.partner_id == partner_id).order_by(PanelOperation.id))
                        sub = db.get(Subscription, sub_id)
                        state = {
                            "wallet": db.get(Partner, partner_id).balance_toman,
                            "credit": (credit.credit_gb, credit.credit_days),
                            "ledger": [(row.id, row.asset, row.amount, row.product_id, row.subscription_id, row.reason)
                                       for row in rows],
                            "operations": [(op.id, op.kind, op.status, op.subscription_id) for op in ops],
                            "subscription": (sub.id, sub.product_id, sub.v2_user_id, sub.traffic_cap_gb,
                                             sub.expired_at, sub.product.price_per_gb),
                        }
                finally:
                    engine.dispose()
                user, = payload(panel.get("/_lab/users"))["data"]
                state["panel"] = {field: user[field] for field in ("id", "u", "d", "transfer_enable", "expired_at")}
                state["reset_requests"] = payload(panel.get("/_lab/traffic-resets"))["requests"]
                return state

            def get_quote():
                quote = payload(api.post(route + "/quote", headers=partner,
                                         json={"action": "reset_traffic", "subscription_id": sub_id}))
                assert quote["affordable"] and quote["consent_token"] and quote["price_per_gb"] == rate
                assert Decimal(quote["gb_from_credit"]) == quote["days_from_credit"] == 0
                return quote

            set_usage(starting_total)
            before = snapshot()
            assert before["wallet"] == 88_500 > expected_toman and before["credit"] == (3, 6)
            assert before["subscription"] == (sub_id, b_id, user_id, 10, created["expired_at"], rate)
            assert before["panel"] == {"id": user_id, "u": upload, "d": starting_total - upload,
                                       "transfer_enable": 10 * GB, "expired_at": created["expired_at"]}
            assert before["reset_requests"] == []
            original = get_quote()
            assert snapshot() == before
            quote_to_confirm = original
            reset_path = f"{route}/{sub_id}/reset-traffic"

            if increase_usage:
                set_usage(final_total)
                changed = snapshot()
                assert changed == {**before, "panel": {**before["panel"], "d": final_total - upload}}
                secret = (env["SECRET_KEY"] + ":partner").encode()
                proof = jwt.decode(original["consent_token"],
                                   hmac.digest(secret, b"partner-financial-consent-v1", "sha256"),
                                   algorithms=["HS256"])
                session = jwt.decode(partner["Authorization"].removeprefix("Bearer "),
                                     sha256(secret).hexdigest(), algorithms=["HS256"])
                assert proof["session"] == session["jti"] and proof["exp"] > time.time()
                rejected = api.post(reset_path, headers=partner, json={"consent_token": original["consent_token"]})
                unchanged = snapshot()
                print(f"Old quote: bytes={final_total}, proposed={original['toman']}, "
                      f"HTTP {rejected.status_code}, debit={before['wallet'] - unchanged['wallet']}, "
                      f"resets={len(unchanged['reset_requests'])}, "
                      f"remaining={unchanged['panel']['u'] + unchanged['panel']['d']}")
                assert payload(rejected, 409)["code"] == "financial_consent"
                assert unchanged == changed
                quote_to_confirm = get_quote()
                assert quote_to_confirm["toman"] == expected_toman
                assert snapshot() == changed

            response = api.post(reset_path, headers=partner, json={"consent_token": quote_to_confirm["consent_token"]})
            after = snapshot()
            print(f"Confirmed quote: bytes={final_total}, proposed={quote_to_confirm['toman']}, "
                  f"HTTP {response.status_code}, debit={before['wallet'] - after['wallet']}, "
                  f"resets={len(after['reset_requests'])}, u={after['panel']['u']}, d={after['panel']['d']}")
            result = payload(response)
            assert original["toman"] == starting_toman
            assert quote_to_confirm["toman"] == result["cost_toman"] == expected_toman
            assert after["wallet"] == before["wallet"] - expected_toman and after["credit"] == before["credit"]
            assert after["ledger"][:-1] == before["ledger"]
            assert after["ledger"][-1][1:] == ("toman", -expected_toman, b_id, sub_id, "reset_traffic")
            assert after["operations"][:-1] == before["operations"]
            assert after["operations"][-1][1:] == ("reset_traffic", "completed", sub_id)
            assert after["subscription"] == before["subscription"]
            assert after["panel"] == {**before["panel"], "u": 0, "d": 0}
            assert after["reset_requests"] == [{"id": user_id, "u": 0, "d": 0}]


def test_http_reset_link_returns_fresh_link_on_reload_without_traffic_or_wallet_effect(tmp_path):
    with local_server(tmp_path, "financial_panel_lab:app") as (lab_url, _):
        env = api_env(tmp_path, lab_url)
        with (local_server(tmp_path, "app.main:app", env=env) as (url, _),
              httpx.Client(base_url=url, trust_env=False, timeout=10) as api,
              httpx.Client(base_url=lab_url, trust_env=False, timeout=10) as panel):
            admin, partner, partner_id, product_id = prepare(api, lab_url)
            created = create(api, partner, product_id, "reset_link_fixture")
            sub_id, user_id = created["id"], created["v2_user_id"]
            route = f"/api/partner/subscriptions/{sub_id}"
            upload, download = GB // 2, 3 * GB // 2
            payload(panel.post(f"/_lab/users/{user_id}/traffic", json={"u": upload, "d": download}))
            for asset, amount in (("gb", 3), ("days", 6)):
                payload(api.post(f"/api/admin/partners/{partner_id}/adjust", headers=admin,
                                 json={"product_id": product_id, "asset": asset, "amount": amount}))

            login_result = payload(panel.post("/api/v1/passport/auth/login",
                                              json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}))
            panel_headers = {"Authorization": login_result["data"]["auth_data"]}

            def fetch_user():
                data = payload(panel.get(f"/api/v1/{SECURE_PATH}/user/fetch", headers=panel_headers, params={
                    "filter[0][key]": "id", "filter[0][condition]": "=", "filter[0][value]": str(user_id),
                }))
                user, = data["data"]
                assert user["id"] == user_id
                return user

            def use_subscription_link(link):
                assert link.startswith(f"{lab_url}/api/v1/client/subscribe?"), "Expected a link from the isolated panel"
                return panel.get(link.removeprefix(lab_url))

            def local_state():
                engine = make_engine(env["DATABASE_URL"])
                try:
                    with Session(engine) as db:
                        sub = db.scalars(select(Subscription).where(Subscription.partner_id == partner_id)).one()
                        credit = db.scalar(select(PartnerProductCredit).where(
                            PartnerProductCredit.partner_id == partner_id,
                            PartnerProductCredit.product_id == product_id,
                        ))
                        rows = db.scalars(select(WalletLedger).where(
                            WalletLedger.partner_id == partner_id).order_by(WalletLedger.id))
                        return {
                            "wallet": db.get(Partner, partner_id).balance_toman,
                            "credit": (credit.credit_gb, credit.credit_days),
                            "ledger": [(row.id, row.asset, row.amount, row.product_id,
                                        row.subscription_id, row.reason) for row in rows],
                            "subscription": (sub.id, sub.product_id, sub.v2_user_id, sub.traffic_cap_gb, sub.expired_at),
                        }
                finally:
                    engine.dispose()

            before = local_state()
            assert before["wallet"] == 88_500 and before["credit"] == (3, 6)
            assert before["subscription"] == (sub_id, product_id, user_id, 10, created["expired_at"])
            first_detail = payload(api.get(route, headers=partner))
            panel_before = fetch_user()
            old_link = first_detail["subscribe_url"]
            assert old_link and old_link == panel_before["subscribe_url"]
            assert first_detail["status"] == "active" and first_detail["live_error"] is None
            assert first_detail["subscribe_url_fallback"] is False
            assert (panel_before["u"], panel_before["d"], panel_before["transfer_enable"],
                    panel_before["expired_at"]) == (upload, download, 10 * GB, created["expired_at"])
            expected_content = "lab-subscribe reset_link_fixture@old.example.test"
            assert payload(use_subscription_link(old_link))["data"] == expected_content
            assert payload(panel.get("/_lab/link-resets"))["ids"] == []
            assert payload(panel.get("/_lab/traffic-resets"))["requests"] == []
            opening_writes = payload(panel.get("/_lab/consent-writes"))["count"]

            reset = payload(api.post(route + "/reset-link", headers=partner))
            detail = payload(api.get(route, headers=partner))
            panel_after = fetch_user()
            new_link = detail["subscribe_url"]
            assert new_link and new_link != old_link
            assert new_link == reset["subscribe_url"] == panel_after["subscribe_url"]
            assert reset["subscribe_url_fallback"] is detail["subscribe_url_fallback"] is False
            assert detail["live_error"] is None and detail["status"] == "active"
            assert (detail["id"], detail["v2_user_id"], Decimal(detail["traffic_cap_gb"]),
                    detail["expired_at"], Decimal(detail["upload_gb"]), Decimal(detail["download_gb"])) == (
                sub_id, user_id, 10, created["expired_at"], Decimal("0.5"), Decimal("1.5"),
            )
            assert local_state() == before
            for key in ("id", "u", "d", "transfer_enable", "expired_at", "plan_id", "banned"):
                assert panel_after[key] == panel_before[key]
            assert payload(panel.get("/_lab/link-resets"))["ids"] == [user_id]
            assert payload(panel.get("/_lab/traffic-resets"))["requests"] == []
            assert payload(panel.get("/_lab/consent-writes"))["count"] == opening_writes + 1

            invalid = use_subscription_link(old_link)
            assert payload(invalid, 500)["message"] == "token is error"
            valid = use_subscription_link(new_link)
            assert payload(valid)["data"] == expected_content
            print(f"Reset link for subscription={sub_id}, panel user={user_id}: POST and GET returned the panel's "
                  f"fresh link, old link HTTP {invalid.status_code} invalid, new link HTTP {valid.status_code}; "
                  f"link resets=1, traffic resets=0, u={upload}, d={download}, wallet and ledger unchanged")


def test_http_financial_lifecycle_and_old_panel_identity(tmp_path, lab_url):
    env = api_env(tmp_path, lab_url)
    with httpx.Client(base_url=lab_url, trust_env=False, timeout=5) as panel:
        payload(panel.post("/_lab/reset"))
        advance_lab_user_id(panel)
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
                operation, = payload(api.get("/api/admin/operations", headers=admin))["items"]
                remote_id = payload(panel.get(remote_path))["data"]["id"]
                assert_completed_create_link(env, operation["id"], sub, remote_id)
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
