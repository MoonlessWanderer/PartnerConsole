"""Stage 12 policy decisions P1 and P3, with the attention decisions that follow from them.

P1: after a reset that may have been sent, lower usage proves nothing: an admin reset, a
plan-scheduled reset or new usage after our own reset all look alike. The row waits for the
admin: no completion, no refund, no second reset.

P3: after an increase, decrease, renewal or cap/expiry change that may have been sent, a panel
still showing the old value is no licence to resend. The row waits for the admin. Only stable
evidence that nothing was sent (marker cleared on a connection refusal) allows the first send.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.errors import AppError, V2BoardError
from app.integrations.v2board import V2BoardClient
from app.models import PanelOperation
from app.services import money_sync
from app.services.subscriptions import Actor
from test_billing import GB, PanelSim, _refund_ledgers, gateway
from test_stage11_db_failure import _after_refund, _money, _mutations, _op, _prepare, _restart_and_reconcile, _rollbacks
from test_stage11_reproductions import _ApplyThenFail, _make_sub, db  # noqa: F401
from test_stage12_recovery_audit import GATEWAY_ERRORS, LateLanding, _Lose, _reset_setup

ADMIN = Actor("admin", 1)


@pytest.fixture()
def sim():
    return PanelSim()


def _factory(sim):
    return lambda _panel: gateway(sim)


def _decide(db, sim, kind, decision, note="وضعیت پنل بررسی شد", **extra):
    op = _op(db, kind)
    return money_sync.resolve_attention(db, op.id, decision=decision, actor=ADMIN, note=note,
                                        seen_updated_at=money_sync.operation_view(op)["updated_at"],
                                        gateway_factory=_factory(sim), **extra)


def _blocks(db, sim, kind):
    op = _op(db, kind)
    return money_sync.operation_decision_blocks(op, money_sync.operation_panel_state(db, op.id, _factory(sim)))


def _client(handler):
    return V2BoardClient("http://panel.test", "secret", "admin@panel.test", "secret",
                         transport=httpx.MockTransport(handler))


# --- P1: reset ----------------------------------------------------------------------------

def _held_reset(db, sim, partner, product, uid, updates, charged):
    assert _op(db, "reset").status == "attention"
    assert len(sim.updates) == updates
    assert _money(db, partner.id, product.id) == charged and _rollbacks(db) == 0


def test_reset_lost_while_the_panel_admin_resets_is_held(db, sim):
    partner, product, sub, run = _reset_setup(db, sim)
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "update_user", httpx.ReadTimeout("lost"), applied=False))
    charged = _money(db, partner.id, product.id)
    updates = len(sim.updates)
    sim.users[sub.v2_user_id].update(u=0, d=0)  # the panel admin reset it by hand
    assert _restart_and_reconcile(db, sim) == 0
    _held_reset(db, sim, partner, product, sub.v2_user_id, updates, charged)


def test_reset_lost_while_the_plan_resets_on_schedule_is_held(db, sim):
    partner, product, sub, run = _reset_setup(db, sim)
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "update_user", httpx.ReadTimeout("lost"), applied=False))
    charged = _money(db, partner.id, product.id)
    updates = len(sim.updates)
    sim.users[sub.v2_user_id].update(u=2 * GB, d=0)  # monthly plan reset, then fresh usage
    assert _restart_and_reconcile(db, sim) == 0
    _held_reset(db, sim, partner, product, sub.v2_user_id, updates, charged)
    assert sim.users[sub.v2_user_id]["u"] == 2 * GB


def test_reset_landed_then_new_usage_is_held_and_not_reset_again(db, sim):
    partner, product, sub, run = _reset_setup(db, sim)
    with pytest.raises(AppError):
        run(_ApplyThenFail(gateway(sim), sim, sub.v2_user_id, new_usage=3 * GB, error=httpx.ReadTimeout("lost")))
    charged = _money(db, partner.id, product.id)
    updates = len(sim.updates)
    assert _restart_and_reconcile(db, sim) == 0
    _held_reset(db, sim, partner, product, sub.v2_user_id, updates, charged)
    assert sim.users[sub.v2_user_id]["u"] == 3 * GB


def test_reset_landed_with_the_response_lost_is_held_not_completed(db, sim):
    partner, product, sub, run = _reset_setup(db, sim)
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "update_user", httpx.ReadTimeout("lost"), applied=True))
    assert sim.users[sub.v2_user_id]["u"] == 0
    charged = _money(db, partner.id, product.id)
    updates = len(sim.updates)
    assert _restart_and_reconcile(db, sim) == 0
    assert _restart_and_reconcile(db, sim) == 0
    _held_reset(db, sim, partner, product, sub.v2_user_id, updates, charged)
    closed = _decide(db, sim, "reset", "close", note="ریست روی پنل دیده شد")
    assert closed["status"] == "closed"
    assert len(sim.updates) == updates and _money(db, partner.id, product.id) == charged


def test_reset_uncertain_error_after_landing_is_held_in_the_same_request(db, sim):
    partner, product, sub, run = _reset_setup(db, sim)
    with pytest.raises(AppError) as caught:
        run(_ApplyThenFail(gateway(sim), sim, sub.v2_user_id, new_usage=0, error=V2BoardError("پاسخ نامعتبر")))
    assert caught.value.status_code == 409
    assert _op(db, "reset").status == "attention" and _rollbacks(db) == 0


def test_held_reset_retried_by_the_admin_is_rechecked_and_sent_once(db, sim):
    partner, product, sub, run = _reset_setup(db, sim)
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "update_user", httpx.ReadTimeout("lost"), applied=False))
    charged = _money(db, partner.id, product.id)
    assert _restart_and_reconcile(db, sim) == 0
    updates = len(sim.updates)
    view = _decide(db, sim, "reset", "retry", note="ریستی روی پنل دیده نشد")
    assert view["status"] == "pending"
    assert _restart_and_reconcile(db, sim) == 1
    assert _op(db, "reset").status == "completed" and len(sim.updates) == updates + 1
    assert sim.users[sub.v2_user_id]["u"] == 0
    assert _money(db, partner.id, product.id) == charged and _rollbacks(db) == 0


def test_held_reset_for_a_user_deleted_on_the_panel_offers_only_close(db, sim):
    partner, product, sub, run = _reset_setup(db, sim)
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "update_user", httpx.ReadTimeout("lost"), applied=True))
    assert _restart_and_reconcile(db, sim) == 0
    charged = _money(db, partner.id, product.id)
    del sim.users[sub.v2_user_id]
    updates = len(sim.updates)
    blocks = _blocks(db, sim, "reset")
    assert "وجود ندارد" in blocks["retry"] and "close" not in blocks
    with pytest.raises(AppError, match="وجود ندارد") as refused:
        _decide(db, sim, "reset", "retry")
    assert refused.value.status_code == 409 and _op(db, "reset").status == "attention"
    assert _decide(db, sim, "reset", "close")["status"] == "closed"
    assert len(sim.updates) == updates and _money(db, partner.id, product.id) == charged


# --- P3: cap and expiry changes -----------------------------------------------------------

CAP_KINDS = ["increase", "renewal", "decrease"]


@pytest.mark.parametrize("kind", CAP_KINDS)
def test_change_landed_then_reverted_by_an_admin_is_held_not_resent(db, sim, kind):
    partner_id, product_id, run, _expected, landed = _prepare(db, sim, kind)
    (uid,) = sim.users
    old = {key: sim.users[uid][key] for key in ("transfer_enable", "expired_at")}
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "update_user", httpx.ReadTimeout("lost"), applied=True))
    assert landed()
    sim.users[uid].update(old)  # someone on the panel put the old values back
    charged = _money(db, partner_id, product_id)
    start = _mutations(sim)
    assert _restart_and_reconcile(db, sim) == 0
    op = _op(db, kind)
    assert op.status == "attention" and op.lease_token is None
    assert money_sync.operation_view(op)["retry_action"] == "resend_change"
    assert _mutations(sim) == start and not landed()
    assert _money(db, partner_id, product_id) == charged and _rollbacks(db) == 0
    assert _restart_and_reconcile(db, sim) == 0 and _mutations(sim) == start


@pytest.mark.parametrize("kind", CAP_KINDS)
def test_connection_refused_is_stable_proof_of_no_send_and_the_first_send_proceeds(db, sim, kind):
    partner_id, product_id, run, expected, landed = _prepare(db, sim, kind)
    before = _money(db, partner_id, product_id)
    start = _mutations(sim)
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "update_user", httpx.ConnectError("refused"), applied=False))
    op = _op(db, kind)
    assert op.status == "pending" and money_sync._loads(op)["panel_sent"] is False
    charged = _money(db, partner_id, product_id)
    if kind == "decrease":
        assert charged == before and _refund_ledgers(db, kind) == []
    assert _restart_and_reconcile(db, sim) == 1
    assert _op(db, kind).status == "completed" and landed()
    assert tuple(a - b for a, b in zip(_mutations(sim), start)) == expected
    assert _money(db, partner_id, product_id) == _after_refund(db, _op(db, kind), kind, charged)
    assert _rollbacks(db) == 0


@pytest.mark.parametrize("kind", CAP_KINDS)
def test_unproven_change_retried_by_the_admin_is_sent_exactly_once(db, sim, kind):
    partner_id, product_id, run, expected, landed = _prepare(db, sim, kind)
    before = _money(db, partner_id, product_id)
    start = _mutations(sim)
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "update_user", httpx.ReadTimeout("lost"), applied=False))
    charged = _money(db, partner_id, product_id)
    assert _restart_and_reconcile(db, sim) == 0
    assert _op(db, kind).status == "attention" and _mutations(sim) == start
    with pytest.raises(AppError, match="توضیح"):
        money_sync.resolve_attention(db, _op(db, kind).id, decision="retry", actor=ADMIN, note=" ",
                                     gateway_factory=_factory(sim))
    blocks = _blocks(db, sim, kind)
    if kind == "decrease":
        assert set(blocks) == {"close"} and "قطعی نیست" in blocks["close"]
        assert charged == before and _refund_ledgers(db, kind) == []
        with pytest.raises(AppError, match="قطعی نیست"):
            _decide(db, sim, kind, "close")
    else:
        assert blocks == {}
    _decide(db, sim, kind, "retry")
    assert _restart_and_reconcile(db, sim) == 1
    assert _restart_and_reconcile(db, sim) == 0
    assert _op(db, kind).status == "completed" and landed()
    assert tuple(a - b for a, b in zip(_mutations(sim), start)) == expected
    assert _money(db, partner_id, product_id) == _after_refund(db, _op(db, kind), kind, charged)
    assert _rollbacks(db) == 0


def test_unproven_change_for_a_user_deleted_on_the_panel_offers_only_close(db, sim):
    partner_id, product_id, run, _expected, _landed = _prepare(db, sim, "increase")
    (uid,) = sim.users
    with pytest.raises(AppError):
        run(LateLanding(sim, "/user/update", GATEWAY_ERRORS["json_502"]).client())
    charged = _money(db, partner_id, product_id)
    del sim.users[uid]
    blocks = _blocks(db, sim, "increase")
    assert "وجود ندارد" in blocks["retry"] and "close" not in blocks
    with pytest.raises(AppError, match="وجود ندارد"):
        _decide(db, sim, "increase", "retry")
    assert _op(db, "increase").status == "attention"
    assert _decide(db, sim, "increase", "close")["status"] == "closed"
    assert _money(db, partner_id, product_id) == charged and _rollbacks(db) == 0


def test_unreadable_panel_blocks_a_manual_retry(db, sim):
    partner_id, product_id, run, _expected, _landed = _prepare(db, sim, "increase")
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "update_user", httpx.ReadTimeout("lost"), applied=False))
    assert _restart_and_reconcile(db, sim) == 0
    op = _op(db, "increase")

    def down(_panel):
        return _client(lambda request: httpx.Response(502, text="<html>bad gateway</html>"))

    with pytest.raises(AppError, match="قابل خواندن نیست"):
        money_sync.resolve_attention(db, op.id, decision="retry", actor=ADMIN, note="بررسی", gateway_factory=down)
    assert _op(db, "increase").status == "attention"


# --- no endless pending loop --------------------------------------------------------------

def _stuck_delete(db, sim):
    partner_id, product_id, run, _expected, _landed = _prepare(db, sim, "delete")
    before = _money(db, partner_id, product_id)
    (uid,) = sim.users
    calls = []

    def flaky(request):
        if request.url.path.endswith("/user/delUser"):
            calls.append(1)
            return httpx.Response(500, json={"message": "unknown"})
        return sim.handler(request)

    with pytest.raises(AppError):
        run(_client(flaky))
    assert _money(db, partner_id, product_id) == before and _refund_ledgers(db, "delete") == []
    for _ in range(5):
        money_sync.reconcile_pending(db, lambda _panel: _client(flaky))
    op = _op(db, "delete")
    assert op.status == "attention", op.last_error
    assert "چند بار" in op.last_error
    assert len(calls) == 1 + money_sync.MAX_AUTO_RESENDS
    assert uid in sim.users
    assert _money(db, partner_id, product_id) == before and _rollbacks(db) == 0
    assert _refund_ledgers(db, "delete") == []
    return partner_id, product_id, uid, before


def test_repeated_uncertain_delete_failures_stop_at_attention(db, sim):
    _stuck_delete(db, sim)


def test_repeated_delete_failure_with_the_user_still_there_allows_only_retry(db, sim):
    partner_id, product_id, uid, before = _stuck_delete(db, sim)
    blocks = _blocks(db, sim, "delete")
    assert set(blocks) == {"close"} and "قطعی نیست" in blocks["close"]
    with pytest.raises(AppError, match="قطعی نیست"):
        _decide(db, sim, "delete", "close")
    _decide(db, sim, "delete", "retry")
    assert _restart_and_reconcile(db, sim) == 1
    op = _op(db, "delete")
    assert op.status == "completed" and uid not in sim.users
    assert _money(db, partner_id, product_id) == _after_refund(db, op, "delete", before) and _rollbacks(db) == 0


def test_repeated_delete_failure_with_the_user_gone_releases_credit_without_resend(db, sim):
    partner_id, product_id, uid, before = _stuck_delete(db, sim)
    del sim.users[uid]
    deleted = list(sim.deleted)
    blocks = _blocks(db, sim, "delete")
    assert set(blocks) == {"close"} and "اعتبار رزروشده" in blocks["close"]
    with pytest.raises(AppError, match="اعتبار رزروشده"):
        _decide(db, sim, "delete", "close")
    _decide(db, sim, "delete", "retry")
    assert _restart_and_reconcile(db, sim) == 1
    op = _op(db, "delete")
    assert op.status == "completed" and sim.deleted == deleted
    assert _money(db, partner_id, product_id) == _after_refund(db, op, "delete", before) and _rollbacks(db) == 0


def test_repeated_uncertain_generate_failures_stop_at_attention(db, sim):
    partner_id, product_id, run, _expected, _landed = _prepare(db, sim, "create")
    calls = []

    def flaky(request):
        if request.url.path.endswith("/user/generate"):
            calls.append(1)
            return httpx.Response(500, json={"message": "unknown"})
        return sim.handler(request)

    with pytest.raises(AppError):
        run(_client(flaky))
    charged = _money(db, partner_id, product_id)
    for _ in range(5):
        money_sync.reconcile_pending(db, lambda _panel: _client(flaky))
    op = _op(db, "create")
    assert op.status == "attention", op.last_error
    assert len(calls) == 1 + money_sync.MAX_AUTO_RESENDS and not sim.users
    assert _money(db, partner_id, product_id) == charged and _rollbacks(db) == 0
    assert money_sync.operation_view(op)["retry_action"] == "resend_change"
    _decide(db, sim, "create", "retry", note="پلن و پنل بررسی شد")
    assert _restart_and_reconcile(db, sim) == 1
    assert _op(db, "create").status == "completed" and len(sim.users) == 1
    assert _money(db, partner_id, product_id) == charged and _rollbacks(db) == 0


# --- what the admin sees ------------------------------------------------------------------

def test_raw_technical_error_text_is_not_shown_to_the_admin(db, sim, caplog):
    partner_id, product_id, run, _expected, _landed = _prepare(db, sim, "increase")
    run(gateway(sim))
    op = _op(db, "increase")
    op.last_error = "changed by another worker"
    db.commit()
    with caplog.at_level("WARNING", logger="partner.billing"):
        shown = money_sync.operation_view(op)["last_error"]
    assert shown == money_sync.TECHNICAL_ERROR and "worker" not in shown
    assert "changed by another worker" in caplog.text
    op.last_error = money_sync.CHANGE_UNPROVEN
    db.commit()
    assert money_sync.operation_view(op)["last_error"] == money_sync.CHANGE_UNPROVEN


def test_panel_state_for_a_foreign_user_reveals_nothing_about_it(db, sim):
    partner_id, product_id, run, _expected, _landed = _prepare(db, sim, "increase")
    (uid,) = sim.users
    with pytest.raises(AppError):
        run(LateLanding(sim, "/user/update", GATEWAY_ERRORS["html_504"]).client())
    sim.users[uid]["email"] = "someone@other.example"
    op = _op(db, "increase")
    panel = money_sync.operation_panel_state(db, op.id, _factory(sim))
    assert panel == {"available": True, "exists": True, "owned": False}
    assert db.get(PanelOperation, op.id).status == "attention"
