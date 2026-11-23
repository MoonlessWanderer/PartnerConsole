"""Admin attention review over real HTTP: API process, its SQLite file and the panel simulator."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import make_engine
from app.models import PanelOperation, Partner, WalletLedger
from local_http import local_server
from test_stage6_http import api_env, create, login, payload, prepare

SECRET_LINK = "https://panel.example.test/api/v1/client/subscribe?token=synthetic-secret-token-91"


def _attention(engine, *, partner_id, product_id, sub_id, v2_user_id, kind, extra) -> int:
    body = {"actor_type": "partner", "actor_id": partner_id, "panel_applied": False, "v2_user_id": v2_user_id,
            "subscribe_url": SECRET_LINK, "email": "stage11_user@old.example.test", **extra}
    with Session(engine) as db:
        op = PanelOperation(partner_id=partner_id, product_id=product_id, subscription_id=sub_id,
                            username="stage11_user", kind=kind, status="attention",
                            payload=json.dumps(body), last_error="نتیجه قابل اثبات نیست")
        db.add(op)
        db.commit()
        return op.id


def _status(engine, op_id) -> str:
    with Session(engine) as db:
        return db.get(PanelOperation, op_id).status


def _balance_and_ledgers(engine, partner_id):
    with Session(engine) as db:
        return (int(db.get(Partner, partner_id).balance_toman),
                len(db.scalars(select(WalletLedger).where(WalletLedger.partner_id == partner_id)).all()))


def test_attention_review_is_admin_only_noted_single_shot_and_secret_free(tmp_path, lab_url):
    env = api_env(tmp_path, lab_url)
    with httpx.Client(base_url=lab_url, trust_env=False, timeout=5) as panel:
        payload(panel.post("/_lab/reset"))
        with local_server(tmp_path, "app.main:app", env=env) as (url, _):
            engine = make_engine(env["DATABASE_URL"])
            try:
                with httpx.Client(base_url=url, trust_env=False, timeout=15) as api:
                    admin, partner, partner_id, product = prepare(api, lab_url)
                    sub = create(api, partner, product, username="stage11_user")
                    remote = payload(panel.get(f"/_lab/users/{sub['v2_user_id']}"))["data"]
                    reset_op = _attention(engine, partner_id=partner_id, product_id=product, sub_id=sub["id"],
                                          v2_user_id=sub["v2_user_id"], kind="reset_traffic",
                                          extra={"cost": 1000, "raw_before": 5, "reset_sent": True})
                    gap_op = _attention(engine, partner_id=partner_id, product_id=product, sub_id=sub["id"],
                                        v2_user_id=sub["v2_user_id"], kind="decrease",
                                        extra={"gb_back": "2", "days_back": 0, "old_cap": "10", "new_cap": "8",
                                               "shortfall_gb": "1.5", "shortfall_days": 0})
                    money = _balance_and_ledgers(engine, partner_id)
                    routes = [("get", "/api/admin/operations?status=attention", None),
                              ("get", f"/api/admin/operations/{reset_op}", None),
                              ("post", f"/api/admin/operations/{reset_op}/resolve",
                               {"decision": "close", "note": "partner attempt"})]

                    # Partners and anonymous callers are refused on every route.
                    for method, route, body in routes:
                        for headers in (partner, {}):
                            response = getattr(api, method)(route, headers=headers,
                                                            **({"json": body} if body else {}))
                            assert response.status_code in (401, 403), (route, response.status_code)
                    assert _status(engine, reset_op) == "attention"

                    listed = api.get("/api/admin/operations?status=attention", headers=admin)
                    items = payload(listed)["items"]
                    assert {item["id"] for item in items} == {reset_op, gap_op}
                    detail_response = api.get(f"/api/admin/operations/{reset_op}", headers=admin)
                    detail = payload(detail_response)
                    assert detail["retry_action"] == "resend_reset" and detail["charge_toman"] == 1000
                    assert detail["updated_at"].endswith("+00:00")
                    state = detail["panel_state"]
                    assert state["available"] and state["exists"] and state["owned"] is True
                    assert detail["decision_blocks"] == {}
                    assert state["transfer_enable_gb"] == "10" and state["banned"] is False
                    for text in (listed.text, detail_response.text):
                        assert "token" not in text and SECRET_LINK not in text
                        assert remote["token"] not in text and "@old.example.test" not in text
                        assert "password" not in text

                    # A note is mandatory.
                    refused = api.post(f"/api/admin/operations/{reset_op}/resolve", headers=admin,
                                       json={"decision": "close", "note": "   "})
                    assert refused.status_code == 400
                    assert _status(engine, reset_op) == "attention"

                    # Two simultaneous submits: exactly one decision is applied.
                    def close():
                        with httpx.Client(base_url=url, trust_env=False, timeout=15) as other:
                            return other.post(f"/api/admin/operations/{reset_op}/resolve", headers=admin,
                                              json={"decision": "close", "note": "پنل بررسی شد"})

                    with ThreadPoolExecutor(max_workers=2) as pool:
                        codes = sorted(r.status_code for r in pool.map(lambda _i: close(), range(2)))
                    assert codes[0] == 200 and codes[1] in (400, 503), codes
                    assert _status(engine, reset_op) == "closed"
                    again = api.post(f"/api/admin/operations/{reset_op}/resolve", headers=admin,
                                     json={"decision": "retry", "note": "دوباره"})
                    assert again.status_code == 400

                    # The shortfall must be acknowledged explicitly; closing moves no money.
                    gap_route = f"/api/admin/operations/{gap_op}/resolve"
                    blocked = api.post(gap_route, headers=admin, json={"decision": "close", "note": "بستن"})
                    assert blocked.status_code == 400 and "اختلاف مالی" in blocked.json()["detail"]
                    closed = payload(api.post(gap_route, headers=admin, json={
                        "decision": "close", "note": "اختلاف پذیرفته شد", "accept_shortfall": True}))
                    assert closed["status"] == "closed" and closed["resolution_note"] == "اختلاف پذیرفته شد"
                    assert _balance_and_ledgers(engine, partner_id) == money
                    assert payload(panel.get(f"/_lab/users/{sub['v2_user_id']}"))["data"] == remote
            finally:
                engine.dispose()
