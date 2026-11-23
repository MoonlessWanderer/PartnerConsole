"""Stage 11 regression tests for the R1/R1b/R2 fixes (simulated panel only)."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from sqlalchemy import select

from app.core.errors import AppError, V2BoardError
from app.integrations.v2board import V2BoardClient
from app.models import PanelOperation, Subscription
from app.services.money_sync import reconcile_pending, resolve_attention
from app.services.subscriptions import Actor, create_subscription, reset_traffic, set_partner_ban
from test_billing import GB, NOW, PanelSim, _Cut, _toman_ledgers, gateway, jalali, seed
from test_stage11_reproductions import _make_sub, _reset_op, db  # noqa: F401


@pytest.fixture()
def sim():
    return PanelSim()


def _resets(sim) -> int:
    return sum(1 for body in sim.updates if body.get("u") == 0 and body.get("d") == 0)


class _LoseAck:
    """The update never takes effect, usage keeps growing, and the answer is lost."""

    def __init__(self, inner, sim, user_id, *, new_usage: int, error: BaseException):
        self.inner, self.sim, self.user_id = inner, sim, user_id
        self.new_usage, self.error = new_usage, error

    def update_user(self, _payload):
        self.sim.users[self.user_id]["u"] = self.new_usage
        raise self.error

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _unproven_reset(db, sim, error):
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    sim.users[sub.v2_user_id]["u"] = 10 * GB
    before = int(partner.balance_toman)
    cut = _LoseAck(client, sim, sub.v2_user_id, new_usage=12 * GB, error=error)
    with pytest.raises(AppError):
        reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id), gateway=cut)
    db.rollback()
    return partner, sub, before


# ---------------------------------------------------------------- R2: unproven reset goes to attention

def test_timeout_without_proof_is_held_not_resent_or_refunded(db, sim):
    partner, sub, before = _unproven_reset(db, sim, httpx.ReadTimeout("lost response"))
    assert _reset_op(db).status == "pending"
    assert reconcile_pending(db, lambda _panel: gateway(sim)) == 0
    db.refresh(partner)
    op = _reset_op(db)
    assert op.status == "attention"
    assert json.loads(op.payload)["raw_after"] == 12 * GB
    assert sim.users[sub.v2_user_id]["u"] == 12 * GB
    assert _resets(sim) == 0
    assert int(partner.balance_toman) == before - 1000
    assert not _toman_ledgers(db, "operation_rollback")


@pytest.mark.parametrize("error", [
    V2BoardError("پنل عملیات را تأیید نکرد"),  # 200 without data:true
    V2BoardError("پاسخ JSON پنل نامعتبر است"),
    V2BoardError("خطای پنل (502)"),
])
def test_unclassified_panel_error_without_proof_is_held_not_refunded(db, sim, error):
    partner, sub, before = _unproven_reset(db, sim, error)
    db.refresh(partner)
    assert _reset_op(db).status == "attention"
    assert sim.users[sub.v2_user_id]["u"] == 12 * GB
    assert int(partner.balance_toman) == before - 1000
    assert not _toman_ledgers(db, "operation_rollback")


def test_explicit_panel_rejection_is_still_refunded_despite_new_usage(db, sim):
    error = V2BoardError("خطای پنل (500)")
    error.panel_rejected = True
    partner, sub, before = _unproven_reset(db, sim, error)
    db.refresh(partner)
    assert _reset_op(db).status == "failed"
    assert sim.users[sub.v2_user_id]["u"] == 12 * GB
    assert int(partner.balance_toman) == before


def test_attention_retry_resends_once_and_keeps_the_single_charge(db, sim):
    partner, sub, before = _unproven_reset(db, sim, httpx.ReadTimeout("lost response"))
    reconcile_pending(db, lambda _panel: gateway(sim))
    op_id = _reset_op(db).id
    view = resolve_attention(db, op_id, decision="retry", actor=Actor("admin", 1), note="پنل بررسی شد")
    assert view["status"] == "pending"
    data = json.loads(_reset_op(db).payload)
    assert data["reset_sent"] is False
    assert reconcile_pending(db, lambda _panel: gateway(sim)) == 1
    db.refresh(partner)
    assert _reset_op(db).status == "completed"
    assert sim.users[sub.v2_user_id]["u"] == 0
    assert _resets(sim) == 1
    assert int(partner.balance_toman) == before - 1000
    assert len(_toman_ledgers(db, "reset_traffic")) == 1


def test_attention_close_keeps_money_and_panel_untouched(db, sim):
    partner, sub, before = _unproven_reset(db, sim, httpx.ReadTimeout("lost response"))
    reconcile_pending(db, lambda _panel: gateway(sim))
    with pytest.raises(AppError):
        resolve_attention(db, _reset_op(db).id, decision="close", actor=Actor("admin", 1), note="  ")
    view = resolve_attention(db, _reset_op(db).id, decision="close", actor=Actor("admin", 1),
                             note="پنل بررسی شد؛ ریست انجام نشده و هزینه نگه داشته شد")
    db.refresh(partner)
    assert view["status"] == "closed"
    assert sim.users[sub.v2_user_id]["u"] == 12 * GB
    assert _resets(sim) == 0
    assert int(partner.balance_toman) == before - 1000


def test_pending_reset_written_before_stage11_is_held_not_resent(db, sim):
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    sim.users[sub.v2_user_id]["u"] = 10 * GB
    before = int(partner.balance_toman)
    with pytest.raises(AppError):
        reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id),
                      gateway=_Cut(client, "update_user", 1, applied=False))
    db.rollback()
    op = _reset_op(db)
    data = json.loads(op.payload)
    data.pop("raw_before")
    data.pop("reset_sent")
    op.payload = json.dumps(data, ensure_ascii=False)
    db.commit()
    assert reconcile_pending(db, lambda _panel: gateway(sim)) == 0
    db.refresh(partner)
    assert _reset_op(db).status == "attention"
    assert sim.users[sub.v2_user_id]["u"] == 10 * GB
    assert int(partner.balance_toman) == before - 1000


# ---------------------------------------------------------------- R1: inviter is preserved

def test_reset_and_partner_ban_send_the_existing_inviter(db, sim):
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    user = sim.users[sub.v2_user_id]
    user.update(invite_user_id=99, invite_user={"id": 99, "email": "boss@gold.example.com"}, u=GB)
    reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id), gateway=client)
    db.commit()
    assert sim.updates[-1]["invite_user_email"] == "boss@gold.example.com"
    set_partner_ban(db, partner_id=partner.id, banned=True, actor=Actor("admin", 1),
                    gateway_factory=lambda _panel: gateway(sim))
    db.commit()
    assert sim.updates[-1]["banned"] == 1
    assert sim.updates[-1]["invite_user_email"] == "boss@gold.example.com"


def test_user_without_inviter_gets_no_invite_field(db, sim):
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    sim.users[sub.v2_user_id]["u"] = GB
    reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id), gateway=client)
    assert "invite_user_email" not in sim.updates[-1]


# ---------------------------------------------------------------- R1b: never adopt or delete a foreign user

def _foreign(sim, email="ali01@gold.example.com"):
    uid = sim.next_id
    sim.next_id += 1
    sim.users[uid] = {"id": uid, "email": email, "plan_id": 8, "expired_at": 1_900_000_000, "u": 5 * GB, "d": 0,
                      "transfer_enable": 100 * GB, "banned": 0, "token": "theirs", "subscribe_url": "x",
                      "device_limit": None, "speed_limit": None}
    return uid


def _create(db, partner, product, client):
    return create_subscription(
        db, partner_id=partner.id, product_id=product.id, username="ali01", traffic_cap_gb=5,
        end_date=jalali(date(2026, 6, 25)), note=None, actor=Actor("partner", partner.id),
        gateway=client, now=NOW,
    )


def test_existing_panel_user_is_refused_refunded_and_left_alone(db, sim):
    partner, product = seed(db)
    uid = _foreign(sim)
    snapshot = dict(sim.users[uid])
    before = int(partner.balance_toman)
    with pytest.raises(AppError) as caught:
        _create(db, partner, product, gateway(sim))
    assert caught.value.status_code == 409
    db.rollback()
    db.refresh(partner)
    assert sim.users[uid] == snapshot
    assert sim.deleted == [] and sim.updates == [] and sim.generates == 0
    assert int(partner.balance_toman) == before
    assert db.scalar(select(Subscription.id)) is None
    op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "create"))
    assert op.status == "failed"


class _RaceGenerate:
    """Another actor creates the same email between our lookup and generate."""

    def __init__(self, inner, sim):
        self.inner, self.sim = inner, sim

    def generate_user(self, **_kwargs):
        _foreign(self.sim)
        raise V2BoardError("کاربر قبلاً ثبت شده است")

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_email_taken_during_generate_is_refused_without_deleting(db, sim):
    partner, product = seed(db)
    before = int(partner.balance_toman)
    with pytest.raises(AppError) as caught:
        _create(db, partner, product, _RaceGenerate(gateway(sim), sim))
    assert caught.value.status_code == 409
    db.rollback()
    db.refresh(partner)
    assert sim.deleted == [] and len(sim.users) == 1
    assert int(partner.balance_toman) == before


def test_own_generate_with_lost_answer_is_still_adopted_on_reconcile(db, sim):
    partner, product = seed(db)
    client = gateway(sim)
    with pytest.raises(AppError):
        _create(db, partner, product, _Cut(client, "generate_user", 1, applied=True))
    db.rollback()
    op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "create"))
    assert json.loads(op.payload)["generate_sent"] is True
    assert reconcile_pending(db, lambda _panel: gateway(sim)) == 1
    op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "create"))
    assert op.status == "completed"
    assert sim.generates == 1 and sim.deleted == []
    assert db.scalar(select(Subscription.v2_user_id)) == 1


# ---------------------------------------------------------------- client: which errors prove a refusal

_INVALID = "The given data was invalid."


@pytest.mark.parametrize(("status", "body", "rejected"), [
    # user/update aborts before its UPDATE (wyx2685/v2board 4187826, APP_DEBUG=false)
    (500, {"message": "用户不存在"}, True),
    (500, {"message": "邮箱已被使用"}, True),
    (500, {"message": "订阅计划不存在"}, True),
    # UserUpdate FormRequest validation, before the controller runs
    (422, {"message": _INVALID, "errors": {"transfer_enable": ["流量格式不正确"]}}, True),
    (422, {"message": _INVALID, "errors": {"email": ["邮箱格式不正确"], "banned": ["是否封禁不能为空"]}}, True),
    # the UPDATE itself threw: it may have committed first
    (500, {"message": "保存失败"}, False),
    # a 422 without its validation shape, or naming fields UserUpdate does not have
    (422, {"message": _INVALID}, False),
    (422, {"message": _INVALID, "errors": {}}, False),
    (422, {"message": _INVALID, "errors": {"token": ["bad"]}}, False),
    (422, {"message": _INVALID, "errors": {"email": "bad"}}, False),
    (422, {"message": _INVALID, "errors": {"email": []}}, False),
    (422, {"message": "邮箱格式不正确"}, False),
    (422, {"message": _INVALID, "errors": {"email": ["x"]}, "extra": 1}, False),
    # debug-mode bodies carry the exception and can come from any stage
    (500, {"message": "用户不存在", "exception": "Symfony\\HttpException", "file": "x", "line": 1,
           "trace": []}, False),
    (500, {"message": "用户不存在", "trace": []}, False),
    (500, {"message": "SQLSTATE[40001]: deadlock"}, False),
    (500, {"message": "Uh-oh, we've had some problems, we're working on it."}, False),
    (500, {"message": "遇到了些问题，我们正在进行处理"}, False),
    (500, {"message": "Server Error"}, False),
    (500, {}, False),
    (500, ["x"], False),
    (502, {"message": "Bad Gateway"}, False),
    (503, {"message": "用户不存在"}, False),
    (504, {"message": "Gateway Timeout"}, False),
    (429, {"message": "Too Many Attempts."}, False),
    (408, {"message": "Request Timeout"}, False),
    (400, {"message": "用户不存在"}, False),
    (404, {"message": "Not Found"}, False),
    (405, {"message": "Method Not Allowed"}, False),
    (419, {"message": "CSRF token mismatch."}, False),
])
def test_panel_rejected_only_for_update_pre_write_contract(status, body, rejected):
    def handler(request):
        if request.url.path.endswith("/passport/auth/login"):
            return httpx.Response(200, json={"data": {"auth_data": "t", "is_admin": 1}})
        return httpx.Response(status, json=body)

    client = V2BoardClient("http://p.test", "s", "a@p.test", "pw", transport=httpx.MockTransport(handler))
    with pytest.raises(V2BoardError) as caught:
        client.update_user({"id": 1})
    assert caught.value.panel_rejected is rejected


@pytest.mark.parametrize("body", [
    {"message": "用户不存在"},
    {"message": _INVALID, "errors": {"email": ["邮箱格式不正确"]}},
])
@pytest.mark.parametrize("call", ["reset_secret", "delete_user"])
def test_other_endpoints_never_prove_a_refusal(body, call):
    status = 422 if "errors" in body else 500

    def handler(request):
        if request.url.path.endswith("/passport/auth/login"):
            return httpx.Response(200, json={"data": {"auth_data": "t", "is_admin": 1}})
        return httpx.Response(status, json=body)

    client = V2BoardClient("http://p.test", "s", "a@p.test", "pw", transport=httpx.MockTransport(handler))
    with pytest.raises(V2BoardError) as caught:
        getattr(client, call)(1)
    assert caught.value.panel_rejected is False


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"data": False}),
    httpx.Response(200, json={"data": None}),
    httpx.Response(500, text="<html>oops</html>"),
    httpx.Response(502, text="<html>Bad Gateway</html>"),
    httpx.Response(500, text="{not json"),
    httpx.Response(422, text=""),
])
def test_unconfirmed_or_unreadable_answers_are_not_rejections(response):
    def handler(request):
        if request.url.path.endswith("/passport/auth/login"):
            return httpx.Response(200, json={"data": {"auth_data": "t", "is_admin": 1}})
        return response

    client = V2BoardClient("http://p.test", "s", "a@p.test", "pw", transport=httpx.MockTransport(handler))
    with pytest.raises(V2BoardError) as caught:
        client.update_user({"id": 1})
    assert caught.value.panel_rejected is False
