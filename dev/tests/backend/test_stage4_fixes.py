"""Regression coverage for stage 4 response confirmation and account-ban races."""
import json
import httpx
import pytest
from sqlalchemy import select
from app.core.errors import AppError, V2BoardError
from app.integrations.v2board import V2BoardClient
from app.models import Subscription, PanelOperation, PartnerProductCredit
from app.services.money_sync import _read_remote, reconcile_pending
from app.services.subscriptions import Actor, delete_subscription, increase_subscription, set_subscription_ban, reset_link, update_note
from test_billing import gateway, NOW
from test_stage35 import setup
ACKS = [0, '', [], {}, {'success': False}, 1, 'true']

def make_client(reply):

    def handler(request):
        if request.url.path.endswith('/login'):
            return httpx.Response(200, json={'data': {'auth_data': 'dummy', 'is_admin': 1}})
        return reply
    return V2BoardClient('http://panel.test', 'safe', 'dummy', 'dummy', transport=httpx.MockTransport(handler))

@pytest.mark.parametrize('ack', ACKS)
def test_unconfirmed_ack_must_not_be_success(ack):
    with make_client(httpx.Response(200, json={'data': ack})) as client:
        with pytest.raises(V2BoardError):
            client.update_user({'id': 1})

def test_bare_error_object_must_not_be_success():
    with make_client(httpx.Response(200, json={'message': 'error'})) as client:
        with pytest.raises(V2BoardError):
            client.update_user({'id': 1})

@pytest.mark.parametrize('status,message', [(404, 'Route not found'), (500, 'No such route')])
def test_routing_error_must_remain_unknown(status, message):
    with make_client(httpx.Response(status, json={'message': message})) as client:
        assert _read_remote(client, user_id=1)[1] == 'unknown'

@pytest.mark.parametrize('kind', ['zero_ack', 'routing_404'])
def test_remote_user_survives_so_local_delete_must_not_complete(setup, kind):
    sessions, sim, pid, ids, balance, path = setup
    original = sim.handler

    def damaged(request):
        if request.url.path.endswith('/user/delUser'):
            if kind == 'zero_ack':
                return httpx.Response(200, json={'data': 0})
            return httpx.Response(404, json={'message': 'Route not found'})
        return original(request)
    sim.handler = damaged
    with sessions() as db:
        client = gateway(sim)
        try:
            try:
                delete_subscription(db, sub_id=ids[0], partner_id=pid, actor=Actor('partner', pid), gateway=client, now=NOW)
            except AppError:
                db.rollback()
            sub_exists = db.get(Subscription, ids[0]) is not None
            op = db.scalar(select(PanelOperation).where(PanelOperation.kind == 'delete'))
            credits = db.scalar(select(PartnerProductCredit).where(PartnerProductCredit.partner_id == pid))
            observed = {'kind': kind, 'remote_user_exists': 1 in sim.users, 'local_subscription_exists': sub_exists, 'operation_status': op.status if op else None, 'credit_gb': str(credits.credit_gb)}
            assert observed['remote_user_exists']
            assert observed['local_subscription_exists']
            assert observed['operation_status'] in ('pending', 'failed')
        finally:
            client.close()
from test_security import security_api, bearer
from app.api import partner as routes
from app.models import Partner, Subscription
from app.services.subscriptions import set_partner_ban, Actor

def test_authenticated_unban_cannot_bypass_completed_admin_ban(security_api, monkeypatch):
    client, sessions, ids = security_api
    with sessions() as db:
        sub = db.get(Subscription, ids['own'])
        sub.banned = True
        sub.blocked_by_partner_ban = False
        db.commit()
    original = routes.get_subscription
    triggered = False
    remote = {'banned': True}

    class Gateway:

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get_plan(self, plan_id):
            return {'id': plan_id}

        def update_user(self, payload):
            remote['banned'] = bool(payload['banned'])

    def interleaved(db, sub_id, partner_id):
        nonlocal triggered
        if not triggered:
            triggered = True
            with sessions() as admin_db:
                set_partner_ban(admin_db, partner_id=ids['partner'], banned=True, actor=Actor('admin', 1), gateway_factory=lambda panel: Gateway())
        return original(db, sub_id, partner_id)
    monkeypatch.setattr(routes, 'get_subscription', interleaved)
    monkeypatch.setattr(routes, 'client_for_panel', lambda panel: Gateway())
    response = client.post(f"/api/partner/subscriptions/{ids['own']}/unban", headers=bearer('partner', ids['partner']))
    with sessions() as db:
        observed = {'http_status': response.status_code, 'partner_banned': db.get(Partner, ids['partner']).banned, 'local_sub_banned': db.get(Subscription, ids['own']).banned, 'remote_sub_banned': remote['banned'], 'interleaving': 'authenticated request; completed administrator partner ban; route mutation'}
    assert response.status_code in (403, 409) and observed['local_sub_banned'] and observed['remote_sub_banned']


@pytest.mark.parametrize("method", ["user/generate", "user/update", "user/resetSecret", "user/delUser"])
def test_documented_mutation_acknowledgement_is_accepted(method):
    with make_client(httpx.Response(200, json={"data": True})) as client:
        assert client._request("POST", method, json={}) is True


@pytest.mark.parametrize("applied", [False, True])
def test_uncertain_batch_ack_stays_pending_and_resumes(setup, applied):
    sessions, sim, pid, ids, balance, _path = setup
    original = sim.handler

    def damaged(request):
        if request.url.path.endswith("/user/update"):
            if applied:
                original(request)
            return httpx.Response(200, json={"data": {"success": False}})
        return original(request)

    sim.handler = damaged
    with sessions() as db:
        with pytest.raises(AppError):
            set_partner_ban(db, partner_id=pid, banned=True,
                            actor=Actor("admin", 1), gateway_factory=lambda panel: gateway(sim))
        db.rollback()
        operation = db.scalar(select(PanelOperation).where(PanelOperation.kind == "partner_ban"))
        assert operation.status == "pending"
        assert operation.lease_token is None
        assert not any(target["done"] for target in json.loads(operation.payload)["targets"])
        assert not db.get(Subscription, ids[0]).blocked_by_partner_ban
        assert db.get(Partner, pid).balance_toman == balance
        sim.handler = original
        assert reconcile_pending(db, lambda panel: gateway(sim)) == 1
        db.expire_all()
        assert db.get(PanelOperation, operation.id).status == "completed"
        assert all(db.get(Subscription, sid).blocked_by_partner_ban for sid in ids)
        assert all(user["banned"] == 1 for user in sim.users.values())
        assert db.get(Partner, pid).balance_toman == balance
        assert reconcile_pending(db, lambda panel: gateway(sim)) == 0


def test_unknown_financial_result_resumes_without_second_charge(setup):
    sessions, sim, pid, ids, balance, _path = setup
    original = sim.handler

    def damaged(request):
        if request.url.path.endswith("/user/update"):
            return httpx.Response(200, json={"data": 0})
        if request.url.path.endswith("/user/fetch"):
            return httpx.Response(404, json={"message": "Route not found"})
        return original(request)

    sim.handler = damaged
    with sessions() as db:
        client = gateway(sim)
        try:
            with pytest.raises(AppError):
                increase_subscription(db, sub_id=ids[0], partner_id=pid, traffic_cap_gb=12,
                                      end_date=None, actor=Actor("partner", pid), gateway=client, now=NOW)
        finally:
            client.close()
        db.rollback()
        operation = db.scalar(select(PanelOperation).where(PanelOperation.status == "pending"))
        assert operation is not None
        assert db.get(Partner, pid).balance_toman == balance - 2000
        sim.handler = original
        updates = len(sim.updates)
        # The unknown result may have been applied: held for the admin, never resent or refunded.
        assert reconcile_pending(db, lambda panel: gateway(sim)) == 0
        db.expire_all()
        assert db.get(PanelOperation, operation.id).status == "attention"
        assert len(sim.updates) == updates
        assert db.get(Partner, pid).balance_toman == balance - 2000
        assert reconcile_pending(db, lambda panel: gateway(sim)) == 0
        assert len(sim.updates) == updates


def test_admin_retains_unban_permission_for_banned_account(setup):
    sessions, sim, pid, ids, _balance, _path = setup
    with sessions() as db:
        db.get(Partner, pid).banned = True
        sub = db.get(Subscription, ids[0])
        sub.banned = True
        sub.blocked_by_partner_ban = True
        sim.users[1]["banned"] = 1
        db.commit()
        client = gateway(sim)
        try:
            updated = set_subscription_ban(db, sub_id=ids[0], partner_id=None, banned=False,
                                           actor=Actor("admin", 1), gateway=client)
            db.commit()
        finally:
            client.close()
        assert not updated.banned
        assert not updated.blocked_by_partner_ban
        assert db.get(Partner, pid).banned
        assert sim.users[1]["banned"] == 0


@pytest.mark.parametrize("action", ["reset_link", "note"])
def test_banned_partner_cannot_mutate_other_subscription_fields(setup, action):
    sessions, sim, pid, ids, _balance, _path = setup
    with sessions() as db:
        db.get(Partner, pid).banned = True
        db.commit()
        class NoPanelCalls:
            def __getattr__(self, name):
                pytest.fail("Banned account must be rejected before panel contact")
        with pytest.raises(AppError) as caught:
            if action == "reset_link":
                reset_link(db, sub_id=ids[0], partner_id=pid, gateway=NoPanelCalls())
            else:
                update_note(db, sub_id=ids[0], partner_id=pid, note="not authorized")
        assert caught.value.status_code == 403
        db.rollback()
        assert db.get(Subscription, ids[0]).note is None



def test_delete_retry_accepts_only_independently_proven_absence():
    def handler(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"data": {"auth_data": "dummy", "is_admin": 1}})
        if request.url.path.endswith("/user/fetch"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(500, json={"message": "generic failure"})
    with V2BoardClient("http://panel.test", "safe", "dummy", "dummy",
                       transport=httpx.MockTransport(handler)) as client:
        client.delete_user(1)


@pytest.mark.parametrize("lookup", ["id", "email"])
def test_nonempty_mismatched_lookup_cannot_prove_absence(lookup):
    with make_client(httpx.Response(200, json={"data": [{"id": 2, "email": "other@example.test"}]})) as client:
        kwargs = {"user_id": 1} if lookup == "id" else {"email": "target@example.test"}
        assert _read_remote(client, **kwargs)[1] == "unknown"
