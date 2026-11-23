import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import AppError, V2BoardError
from app.integrations.v2board import V2BoardClient
from app.main import app
from app.models import PanelOperation, Partner, Subscription
from app.services import money_sync, subscriptions
from test_billing import NOW, gateway
from test_stage35 import setup


def test_generic_forbidden_response_never_replays_mutation():
    requests = []

    def panel(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"data": {"auth_data": "dummy", "is_admin": 1}})
        return httpx.Response(403, json={"message": "forbidden"})

    with V2BoardClient("https://panel.test", "private", "dummy", "dummy",
                       transport=httpx.MockTransport(panel)) as client:
        with pytest.raises(V2BoardError):
            client.update_user({"id": 1})
    assert len(requests) == 2
    assert requests[-1].endswith("/user/update")


@pytest.mark.parametrize("identifier", [None, True, 1.2, "malformed"])
def test_malformed_panel_identity_cannot_be_used_as_evidence(identifier):
    def panel(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"data": {"auth_data": "dummy", "is_admin": 1}})
        return httpx.Response(200, json={"data": [{"id": identifier, "email": "a@example.test"}]})

    with V2BoardClient("https://panel.test", "private", "dummy", "dummy",
                       transport=httpx.MockTransport(panel)) as client:
        assert money_sync._read_remote(client, user_id=1) == (None, "unknown")
        assert money_sync._read_remote(client, email="a@example.test") == (None, "unknown")


def test_generic_error_wording_is_not_proof_of_absence():
    class Gateway:
        def fetch_by_id(self, _identifier):
            raise V2BoardError("upstream route not found")

    assert money_sync._read_remote(Gateway(), user_id=1) == (None, "unknown")


def test_invalid_plan_response_does_not_leave_partial_cache():
    plan_responses = iter([[{"id": 1}, {"id": "bad"}], [{"id": 2}]])

    def panel(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"data": {"auth_data": "dummy", "is_admin": 1}})
        return httpx.Response(200, json={"data": next(plan_responses)})

    with V2BoardClient("https://panel.test", "private", "dummy", "dummy",
                       transport=httpx.MockTransport(panel)) as client:
        with pytest.raises(V2BoardError):
            client.get_plan(1)
        assert client.get_plan(2)["id"] == 2


def test_overly_nested_json_is_a_panel_error():
    response = httpx.Response(200, content="[" * 1500 + "0" + "]" * 1500)
    with V2BoardClient("https://panel.test", "private", "dummy", "dummy",
                       transport=httpx.MockTransport(lambda _request: response)) as client:
        with pytest.raises(V2BoardError):
            client.login()


def test_uncertain_forbidden_mutation_keeps_one_charge_and_recovers(setup):
    sessions, sim, pid, ids, balance, _ = setup
    original = sim.handler
    updates = 0

    def uncertain(request):
        nonlocal updates
        if request.url.path.endswith("/user/update"):
            updates += 1
            original(request)
            return httpx.Response(403, json={"message": "unconfirmed"})
        if request.url.path.endswith("/user/fetch"):
            return httpx.Response(200, json={"data": [{"id": False}]})
        return original(request)

    sim.handler = uncertain
    with sessions() as db, gateway(sim) as client:
        with pytest.raises(AppError) as failure:
            subscriptions.increase_subscription(
                db, sub_id=ids[0], partner_id=pid, traffic_cap_gb=12,
                end_date=None, actor=subscriptions.Actor("partner", pid), gateway=client, now=NOW,
            )
        assert failure.value.status_code == 503
        db.rollback()
        op = db.scalar(select(PanelOperation).where(PanelOperation.status == "pending"))
        assert op is not None
        assert updates == 1
        assert db.get(Partner, pid).balance_toman == balance - 2000
        sim.handler = original
        assert money_sync.reconcile_pending(db, lambda _panel: gateway(sim)) == 1
        assert money_sync.reconcile_pending(db, lambda _panel: gateway(sim)) == 0
        db.expire_all()
        assert db.get(PanelOperation, op.id).status == "completed"
        assert db.get(Subscription, ids[0]).traffic_cap_gb == 12
        assert db.get(Partner, pid).balance_toman == balance - 2000


@pytest.mark.parametrize("failure,status,code", [
    (ValueError, 500, "internal_error"),
    (httpx.ReadTimeout, 504, "panel_timeout"),
    (httpx.ConnectError, 502, "panel_unavailable"),
])
def test_api_returns_safe_json_and_cors_on_failure(failure, status, code, monkeypatch, caplog):
    marker = "synthetic-sensitive-exception"

    async def broken():
        raise failure(marker)

    monkeypatch.setattr(app.router, "routes", list(app.router.routes))
    app.add_api_route("/api/test-stage5-error", broken)
    client = TestClient(app)
    origin = settings.cors_origin_list[0]
    try:
        response = client.get("/api/test-stage5-error", headers={"Origin": origin})
    finally:
        client.close()
    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["cache-control"] == "no-store"
    assert marker not in response.text and marker not in caplog.text
