"""Stage 12 recovery audit: reproductions and regressions.

A proxy error (502/504, HTML or JSON) after a mutating request does not prove the
request was not executed: it may still land after recovery reads the panel. Only a
strict pre-write abort (`panel_rejected`) or a request that was never sent may be
refunded. Separately, the user at the stored panel id must still be ours before any
update, delete, reset or link change.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.core.errors import AppError
from app.integrations.v2board import V2BoardClient
from app.models import PanelOperation, Subscription
from app.services import money_sync
from app.services.present import present_live
from app.services.subscriptions import (
    Actor,
    increase_subscription,
    reset_link,
    set_partner_ban,
    set_subscription_ban,
)
from test_billing import GB, NOW, PanelSim
from test_stage11_db_failure import (
    _money,
    _mutations,
    _op,
    _prepare,
    _restart_and_reconcile,
    _rollbacks,
)
from test_stage11_reproductions import _make_sub, db  # noqa: F401
from test_billing import gateway


@pytest.fixture()
def sim():
    return PanelSim()


GATEWAY_ERRORS = {
    "html_504": lambda: httpx.Response(504, text="<html><body>504 Gateway Time-out</body></html>"),
    "json_502": lambda: httpx.Response(502, json={"message": "Bad Gateway"}),
}

STALL_PATH = {
    "create": "/user/generate",
    "increase": "/user/update",
    "renewal": "/user/update",
    "decrease": "/user/update",
    "delete": "/user/delUser",
}


class LateLanding:
    """The first request to `path` gets a proxy error while the panel still holds it;
    `land()` lets the panel execute it later, as a slow PHP worker behind nginx would."""

    def __init__(self, sim: PanelSim, path: str, error):
        self.sim, self.path, self.error = sim, path, error
        self.held: httpx.Request | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.held is None and request.url.path.endswith(self.path) and not getattr(self, "used", False):
            self.used = True
            self.held = request
            return self.error()
        return self.sim.handler(request)

    def land(self) -> None:
        assert self.held is not None
        self.sim.handler(self.held)
        self.held = None

    def client(self) -> V2BoardClient:
        return V2BoardClient("http://panel.test", "secret", "admin@panel.test", "secret",
                             transport=httpx.MockTransport(self.handler))


CAP_KINDS = {"increase", "renewal", "decrease"}


def _admin_retry(db, sim, kind, note="وضعیت پنل بررسی شد"):
    op = _op(db, kind)
    return money_sync.resolve_attention(db, op.id, decision="retry", actor=Actor("admin", 1), note=note,
                                        seen_updated_at=money_sync.operation_view(op)["updated_at"],
                                        gateway_factory=lambda _panel: gateway(sim))


@pytest.mark.parametrize("error", list(GATEWAY_ERRORS))
@pytest.mark.parametrize("kind", list(STALL_PATH))
def test_proxy_error_then_late_landing_is_not_refunded(db, sim, kind, error):
    partner_id, product_id, run, expected, landed = _prepare(db, sim, kind)
    before = _money(db, partner_id, product_id)
    start = _mutations(sim)
    late = LateLanding(sim, STALL_PATH[kind], GATEWAY_ERRORS[error])
    with pytest.raises(AppError) as caught:
        run(late.client())
    # A cap change that may have been sent and is not visible yet waits for the admin.
    assert caught.value.status_code == (409 if kind in CAP_KINDS else 503), caught.value.message
    op = _op(db, kind)
    assert op.status == ("attention" if kind in CAP_KINDS else "pending"), op.last_error
    assert _rollbacks(db) == 0
    charged = _money(db, partner_id, product_id)
    assert charged != before
    late.land()
    assert _restart_and_reconcile(db, sim) == (0 if kind in CAP_KINDS else 1)
    assert tuple(a - b for a, b in zip(_mutations(sim), start)) == expected
    if kind in CAP_KINDS:
        assert _op(db, kind).status == "attention"
        _admin_retry(db, sim, kind)
        assert _restart_and_reconcile(db, sim) == 1
    op = _op(db, kind)
    assert op.status == "completed", op.last_error
    assert _rollbacks(db) == 0
    assert _money(db, partner_id, product_id) == charged
    assert landed()


@pytest.mark.parametrize("kind", list(STALL_PATH))
def test_proxy_error_without_landing_is_never_resent_automatically_for_a_cap_change(db, sim, kind):
    partner_id, product_id, run, expected, landed = _prepare(db, sim, kind)
    start = _mutations(sim)
    late = LateLanding(sim, STALL_PATH[kind], GATEWAY_ERRORS["html_504"])
    with pytest.raises(AppError):
        run(late.client())
    charged = _money(db, partner_id, product_id)
    late.held = None
    if kind in CAP_KINDS:
        # The panel shows the old value: no proof the request never ran, so nothing is resent.
        assert _restart_and_reconcile(db, sim) == 0
        assert _op(db, kind).status == "attention" and _mutations(sim) == start
        assert _money(db, partner_id, product_id) == charged and _rollbacks(db) == 0
        _admin_retry(db, sim, kind)
    assert _restart_and_reconcile(db, sim) == 1
    op = _op(db, kind)
    assert op.status == "completed", op.last_error
    assert _rollbacks(db) == 0
    assert _money(db, partner_id, product_id) == charged
    assert tuple(a - b for a, b in zip(_mutations(sim), start)) == expected
    assert landed()


def test_proxy_error_on_reset_goes_to_attention_without_refund(db, sim):
    partner_id, product_id, run, _expected, landed = _prepare(db, sim, "reset")
    late = LateLanding(sim, "/user/update", GATEWAY_ERRORS["html_504"])
    with pytest.raises(AppError) as caught:
        run(late.client())
    assert caught.value.status_code == 409
    assert _op(db, "reset").status == "attention"
    assert _rollbacks(db) == 0
    late.land()
    assert landed()


def test_strict_pre_write_abort_is_still_refunded_at_once(db, sim):
    partner_id, product_id, run, _expected, _landed = _prepare(db, sim, "increase")
    before = _money(db, partner_id, product_id)

    def reject(request):
        if request.url.path.endswith("/user/update"):
            return httpx.Response(500, json={"message": "订阅计划不存在"})
        return sim.handler(request)

    client = V2BoardClient("http://panel.test", "secret", "admin@panel.test", "secret",
                           transport=httpx.MockTransport(reject))
    with pytest.raises(AppError):
        run(client)
    assert _op(db, "increase").status == "failed"
    assert _money(db, partner_id, product_id) == before
    assert _rollbacks(db) == 1


# --- ownership: the user at the stored id must still be ours -------------------------

FOREIGN = "someone@other.example"


def _foreign(sim, uid):
    sim.users[uid]["email"] = FOREIGN
    return dict(sim.users[uid])


@pytest.mark.parametrize("kind", ["increase", "renewal", "decrease", "reset", "delete"])
def test_foreign_user_at_our_id_is_never_touched_and_money_returns(db, sim, kind):
    partner_id, product_id, run, _expected, _landed = _prepare(db, sim, kind)
    (uid,) = sim.users
    snapshot = _foreign(sim, uid)
    before = _money(db, partner_id, product_id)
    start = _mutations(sim)
    with pytest.raises(AppError) as caught:
        run(gateway(sim))
    assert caught.value.status_code == 409, caught.value.message
    assert _mutations(sim) == start
    assert sim.users[uid] == snapshot
    op = _op(db, kind)
    assert op.status == "failed", op.last_error
    assert "متعلق" in (op.last_error or "")
    assert _money(db, partner_id, product_id) == before
    db.rollback()
    assert db.scalar(select(Subscription.id)) is not None


def test_foreign_user_after_a_possible_send_is_held_for_review(db, sim):
    partner_id, product_id, run, _expected, _landed = _prepare(db, sim, "increase")
    (uid,) = sim.users
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "update_user", httpx.ReadTimeout("lost"), applied=False))
    assert _op(db, "increase").status == "pending"
    charged = _money(db, partner_id, product_id)
    snapshot = _foreign(sim, uid)
    start = _mutations(sim)
    _restart_and_reconcile(db, sim)
    op = _op(db, "increase")
    assert op.status == "attention", op.last_error
    assert _mutations(sim) == start and sim.users[uid] == snapshot
    assert _money(db, partner_id, product_id) == charged
    assert _rollbacks(db) == 0


def test_foreign_attention_offers_only_close(db, sim):
    test_foreign_user_after_a_possible_send_is_held_for_review(db, sim)
    op = _op(db, "increase")
    assert money_sync.operation_view(op)["retry_action"] is None
    with pytest.raises(AppError, match="فقط بستن"):
        money_sync.resolve_attention(db, op.id, decision="retry", actor=Actor("admin", 1), note="ارسال دوباره")
    assert _op(db, "increase").status == "attention"
    closed = money_sync.resolve_attention(db, op.id, decision="close", actor=Actor("admin", 1), note="کاربر خارجی")
    assert closed["status"] == "closed"


def test_manual_retry_rereads_the_panel_and_refuses_a_user_that_changed_owner(db, sim):
    partner_id, product_id, run, _expected, _landed = _prepare(db, sim, "increase")
    (uid,) = sim.users
    with pytest.raises(AppError):
        run(LateLanding(sim, "/user/update", GATEWAY_ERRORS["html_504"]).client())
    op = _op(db, "increase")
    assert op.status == "attention" and money_sync.operation_view(op)["retry_action"] == "resend_change"
    charged = _money(db, partner_id, product_id)
    snapshot = _foreign(sim, uid)
    start = _mutations(sim)
    panel = money_sync.operation_panel_state(db, op.id, lambda _panel: gateway(sim))
    assert panel == {"available": True, "exists": True, "owned": False}
    assert "متعلق" in money_sync.operation_decision_blocks(_op(db, "increase"), panel)["retry"]
    with pytest.raises(AppError, match="متعلق") as refused:
        _admin_retry(db, sim, "increase")
    assert refused.value.status_code == 409
    assert _op(db, "increase").status == "attention"
    assert _mutations(sim) == start and sim.users[uid] == snapshot
    assert _money(db, partner_id, product_id) == charged and _rollbacks(db) == 0


def test_decision_on_a_stale_view_is_refused(db, sim):
    test_foreign_user_after_a_possible_send_is_held_for_review(db, sim)
    op = _op(db, "increase")
    seen = money_sync.operation_view(op)["updated_at"]
    with pytest.raises(AppError) as stale:
        money_sync.resolve_attention(db, op.id, decision="close", actor=Actor("admin", 1), note="دیدم و بستم",
                                     seen_updated_at="2000-01-01T00:00:00+00:00")
    assert stale.value.status_code == 409 and _op(db, "increase").status == "attention"
    closed = money_sync.resolve_attention(db, op.id, decision="close", actor=Actor("admin", 1), note="دیدم و بستم",
                                          seen_updated_at=seen)
    assert closed["status"] == "closed"


def test_foreign_user_is_not_banned_reset_or_shown(db, sim):
    client = gateway(sim)
    partner, product, sub = _make_sub(db, client)
    uid = sub.v2_user_id
    snapshot = _foreign(sim, uid)
    actor = Actor("admin", 1)
    for action in (
        lambda: set_subscription_ban(db, sub_id=sub.id, partner_id=partner.id, banned=True, actor=actor,
                                     gateway=client),
        lambda: reset_link(db, sub_id=sub.id, partner_id=partner.id, gateway=client),
    ):
        with pytest.raises(AppError) as caught:
            action()
        assert caught.value.status_code == 409
        db.rollback()
    assert sim.users[uid] == snapshot and not sim.updates[1:]
    db.refresh(sub)
    shown = present_live(sub, client, NOW)
    assert shown["subscribe_url"] is None and shown["used_gb"] is None and shown["live_error"]


def test_partner_ban_skips_a_foreign_user(db, sim):
    client = gateway(sim)
    partner, product, sub = _make_sub(db, client)
    uid = sub.v2_user_id
    snapshot = _foreign(sim, uid)
    updates = len(sim.updates)
    set_partner_ban(db, partner_id=partner.id, banned=True, actor=Actor("admin", 1),
                    gateway_factory=lambda _panel: gateway(sim))
    assert len(sim.updates) == updates and sim.users[uid] == snapshot
    op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "partner_ban"))
    assert op.status == "completed"
    target = json.loads(op.payload)["targets"][0]
    assert target["done"] and target["foreign"]


# --- reset ------------------------------------------------------------------------------

from test_stage11_reproductions import _ApplyThenFail, _reset_op  # noqa: E402
from test_stage11_db_failure import _expire_lease  # noqa: E402
from test_billing import _toman_ledgers  # noqa: E402
from app.services.subscriptions import delete_subscription, reset_traffic  # noqa: E402


class _Lose:
    """`method` raises `error`; with applied=True the panel executes the request first."""

    def __init__(self, inner, method, error, *, applied):
        self.inner, self.method, self.error, self.applied = inner, method, error, applied
        self.fired = False

    def __getattr__(self, name):
        if name == self.method and not self.fired:
            def wrapped(*args, **kwargs):
                self.fired = True
                if self.applied:
                    getattr(self.inner, name)(*args, **kwargs)
                raise self.error
            return wrapped
        return getattr(self.inner, name)


def _reset_setup(db, sim, usage=10):
    client = gateway(sim)
    partner, product, sub = _make_sub(db, client)
    sim.users[sub.v2_user_id]["u"] = usage * GB
    run = lambda gw: reset_traffic(db, sub_id=sub.id, partner_id=partner.id,  # noqa: E731
                                   actor=Actor("partner", partner.id), gateway=gw)
    return partner, product, sub, run


def test_reset_lost_response_then_usage_back_at_the_old_value_is_held(db, sim):
    partner, product, sub, run = _reset_setup(db, sim)
    cut = _ApplyThenFail(gateway(sim), sim, sub.v2_user_id, new_usage=10 * GB,
                         error=httpx.ReadTimeout("lost"))
    with pytest.raises(AppError):
        run(cut)
    charged = _money(db, partner.id, product.id)
    updates = len(sim.updates)
    _restart_and_reconcile(db, sim)
    assert _op(db, "reset").status == "attention"
    assert len(sim.updates) == updates and sim.users[sub.v2_user_id]["u"] == 10 * GB
    assert _money(db, partner.id, product.id) == charged and _rollbacks(db) == 0


def test_external_reset_before_our_send_is_refunded_not_charged(db, sim):
    partner, product, sub, run = _reset_setup(db, sim)
    before = _money(db, partner.id, product.id)

    class ZeroBeforeApply:
        """perform_reset reads 10GB and charges; before our request leaves, someone else resets."""

        def __init__(self, inner):
            self.inner, self.reads = inner, 0

        def __getattr__(self, name):
            if name != "get_user_info":
                return getattr(self.inner, name)

            def wrapped(uid):
                self.reads += 1
                if self.reads == 2:
                    sim.users[uid]["u"] = 0
                return self.inner.get_user_info(uid)
            return wrapped

    updates = len(sim.updates)
    with pytest.raises(AppError) as caught:
        run(ZeroBeforeApply(gateway(sim)))
    assert caught.value.status_code == 409
    assert len(sim.updates) == updates
    assert _op(db, "reset").status == "failed"
    assert _money(db, partner.id, product.id) == before and _rollbacks(db) == 1


def test_reset_without_usage_is_refused_before_any_charge(db, sim):
    partner, product, sub, run = _reset_setup(db, sim, usage=0)
    before = _money(db, partner.id, product.id)
    with pytest.raises(AppError, match="مصرفی"):
        run(gateway(sim))
    assert _money(db, partner.id, product.id) == before
    assert _reset_op(db) is None


def test_reset_row_with_raw_before_zero_is_never_counted_as_landed(db, sim):
    partner, product, sub, run = _reset_setup(db, sim)
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "update_user", httpx.ReadTimeout("lost"), applied=False))
    op = _op(db, "reset")
    data = json.loads(op.payload)
    data["raw_before"] = 0
    op.payload = json.dumps(data)
    db.commit()
    charged = _money(db, partner.id, product.id)
    updates = len(sim.updates)
    _restart_and_reconcile(db, sim)
    assert _op(db, "reset").status == "attention"
    assert len(sim.updates) == updates
    assert _money(db, partner.id, product.id) == charged and _rollbacks(db) == 0


def test_reset_state_read_timeout_moves_nothing(db, sim):
    partner, product, sub, run = _reset_setup(db, sim)
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "update_user", httpx.ReadTimeout("lost"), applied=False))
    charged = _money(db, partner.id, product.id)
    updates = len(sim.updates)
    fresh = _Lose(gateway(sim), "get_user_info", httpx.ReadTimeout("read"), applied=False)
    money_sync.reconcile_pending(db, lambda _panel: fresh)
    assert _op(db, "reset").status == "pending"
    assert len(sim.updates) == updates
    assert _money(db, partner.id, product.id) == charged and _rollbacks(db) == 0


class _Crash(BaseException):
    pass


@pytest.mark.parametrize("kind,method", [("reset", "update_user"), ("increase", "update_user"),
                                         ("delete", "delete_user"), ("create", "update_user")])
def test_crash_between_marker_commit_and_send(db, sim, kind, method):
    """The marker is committed, then the worker dies before the request leaves."""
    partner_id, product_id, run, expected, landed = _prepare(db, sim, kind)
    start = _mutations(sim)
    with pytest.raises(_Crash):
        run(_Lose(gateway(sim), method, _Crash(), applied=False))
    op = _op(db, kind)
    data = json.loads(op.payload)
    assert data.get("panel_sent") or data.get("reset_sent")
    after_crash = _mutations(sim)
    charged = _money(db, partner_id, product_id)
    _expire_lease(db, op.id)
    _restart_and_reconcile(db, sim)
    op = _op(db, kind)
    assert _rollbacks(db) == 0 and _money(db, partner_id, product_id) == charged
    if kind != "delete":
        # The marker says the request may have left; the old state cannot prove it did not.
        assert op.status == "attention" and _mutations(sim) == after_crash
    else:
        # A user still on the panel proves the delete did not run; deleting again is harmless.
        assert op.status == "completed", op.last_error
        assert tuple(a - b for a, b in zip(_mutations(sim), start)) == expected and landed()


# --- delete -----------------------------------------------------------------------------

def _delete_setup(db, sim):
    partner_id, product_id, run, _expected, _landed = _prepare(db, sim, "delete")
    (uid,) = sim.users
    return partner_id, product_id, run, uid


def test_delete_executed_but_response_lost_completes_without_resend(db, sim):
    partner_id, product_id, run, uid = _delete_setup(db, sim)
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "delete_user", httpx.ReadTimeout("lost"), applied=True))
    refunded = _money(db, partner_id, product_id)
    _restart_and_reconcile(db, sim)
    assert _op(db, "delete").status == "completed"
    assert sim.deleted == [uid] and _rollbacks(db) == 0
    assert _money(db, partner_id, product_id) == refunded
    db.rollback()
    assert db.scalar(select(Subscription.id)) is None


def test_delete_not_executed_and_user_still_there_is_sent_once_more(db, sim):
    partner_id, product_id, run, uid = _delete_setup(db, sim)
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "delete_user", httpx.ReadTimeout("lost"), applied=False))
    assert uid in sim.users
    refunded = _money(db, partner_id, product_id)
    _restart_and_reconcile(db, sim)
    assert _op(db, "delete").status == "completed"
    assert sim.deleted == [uid] and _rollbacks(db) == 0
    assert _money(db, partner_id, product_id) == refunded


def test_delete_with_an_invalid_lookup_moves_nothing(db, sim):
    partner_id, product_id, run, uid = _delete_setup(db, sim)
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "delete_user", httpx.ReadTimeout("lost"), applied=False))
    refunded = _money(db, partner_id, product_id)

    def broken(request):
        if request.url.path.endswith("/user/fetch"):
            return httpx.Response(200, json={"data": "not-a-list"})
        return sim.handler(request)

    client = V2BoardClient("http://panel.test", "secret", "admin@panel.test", "secret",
                           transport=httpx.MockTransport(broken))
    money_sync.reconcile_pending(db, lambda _panel: client)
    assert _op(db, "delete").status == "pending"
    assert uid in sim.users and sim.deleted == []
    assert _money(db, partner_id, product_id) == refunded and _rollbacks(db) == 0


def test_concurrent_delete_waits_for_the_lease_and_deletes_once(db, sim):
    partner_id, product_id, run, uid = _delete_setup(db, sim)
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "delete_user", httpx.ReadTimeout("lost"), applied=False))
    refunded = _money(db, partner_id, product_id)
    op = _op(db, "delete")
    assert money_sync._claim(db, op.id) is not None  # another worker holds it
    with pytest.raises(AppError) as busy:
        run(gateway(sim))
    assert busy.value.status_code == 503 and sim.deleted == []
    _expire_lease(db, op.id)
    run(gateway(sim))
    with pytest.raises(AppError):
        run(gateway(sim))
    assert sim.deleted == [uid] and _op(db, "delete").status == "completed"
    assert _money(db, partner_id, product_id) == refunded and _rollbacks(db) == 0


# --- 503 and duplicate requests ----------------------------------------------------------

LOST_METHOD = {"create": "update_user", "increase": "update_user", "renewal": "update_user",
               "decrease": "update_user", "reset": "update_user", "delete": "delete_user"}


@pytest.mark.parametrize("kind", list(LOST_METHOD))
def test_same_request_sent_again_after_503_charges_and_applies_once(db, sim, kind):
    partner_id, product_id, run, expected, landed = _prepare(db, sim, kind)
    start = _mutations(sim)
    with pytest.raises(AppError) as first:
        run(_Lose(gateway(sim), LOST_METHOD[kind], httpx.ReadTimeout("lost"), applied=True))
    assert first.value.status_code == 503
    charged = _money(db, partner_id, product_id)
    if kind == "reset":
        # Lower usage after a lost reset response is no proof it was ours: held, never re-sent.
        with pytest.raises(AppError) as second:
            run(gateway(sim))
        assert second.value.status_code == 409 and _op(db, kind).status == "attention"
    else:
        run(gateway(sim))
        assert _op(db, kind).status == "completed"
    assert _money(db, partner_id, product_id) == charged and _rollbacks(db) == 0
    assert tuple(a - b for a, b in zip(_mutations(sim), start)) == expected and landed()
    ops = db.scalars(select(PanelOperation).where(PanelOperation.kind != "create")).all() if kind != "create" else []
    assert len(ops) == (1 if kind != "create" else 0)


def test_a_different_request_while_one_is_pending_is_refused_without_a_charge(db, sim):
    partner_id, product_id, run, expected, landed = _prepare(db, sim, "increase")
    (uid,) = sim.users
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "update_user", httpx.ReadTimeout("lost"), applied=False))
    charged = _money(db, partner_id, product_id)
    updates = len(sim.updates)
    sub_id = db.scalar(select(Subscription.id))
    with pytest.raises(AppError) as other:
        increase_subscription(db, sub_id=sub_id, partner_id=partner_id, traffic_cap_gb=Decimal(30), end_date=None,
                              actor=Actor("partner", partner_id), gateway=gateway(sim), now=NOW)
    # The pending request is driven first; its unproven send stops it for review, and the new
    # request is neither charged nor sent.
    assert other.value.status_code == 409 and "بررسی" in other.value.message, other.value.message
    assert _op(db, "increase").status == "attention" and not landed()
    assert _money(db, partner_id, product_id) == charged and len(sim.updates) == updates
    assert len(db.scalars(select(PanelOperation).where(PanelOperation.kind == "increase")).all()) == 1


def test_an_identical_independent_request_after_completion_is_not_charged_again(db, sim):
    partner_id, product_id, run, _expected, _landed = _prepare(db, sim, "increase")
    run(gateway(sim))
    charged = _money(db, partner_id, product_id)
    updates = len(sim.updates)
    with pytest.raises(AppError):
        run(gateway(sim))
    assert _money(db, partner_id, product_id) == charged and len(sim.updates) == updates


# --- legacy rows written before panel_sent existed ---------------------------------------

@pytest.mark.parametrize("applied", [True, False])
def test_legacy_increase_without_marker_is_never_resent_blindly(db, sim, applied):
    partner_id, product_id, run, _expected, landed = _prepare(db, sim, "increase")
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "update_user", httpx.ReadTimeout("lost"), applied=applied))
    op = _op(db, "increase")
    data = json.loads(op.payload)
    data.pop("panel_sent", None)
    op.payload = json.dumps(data)
    db.commit()
    charged = _money(db, partner_id, product_id)
    updates = len(sim.updates)
    _restart_and_reconcile(db, sim)
    # A row from before the marker may already have been sent: only the target state completes it.
    assert _op(db, "increase").status == ("completed" if applied else "attention")
    assert bool(landed()) == applied and len(sim.updates) == updates
    assert _money(db, partner_id, product_id) == charged and _rollbacks(db) == 0


def test_legacy_delete_without_marker_that_already_landed_is_not_sent_again(db, sim):
    partner_id, product_id, run, uid = _delete_setup(db, sim)
    with pytest.raises(AppError):
        run(_Lose(gateway(sim), "delete_user", httpx.ReadTimeout("lost"), applied=True))
    op = _op(db, "delete")
    data = json.loads(op.payload)
    data.pop("panel_sent", None)
    op.payload = json.dumps(data)
    db.commit()
    refunded = _money(db, partner_id, product_id)
    _restart_and_reconcile(db, sim)
    assert _op(db, "delete").status == "completed"
    assert sim.deleted == [uid] and _rollbacks(db) == 0
    assert _money(db, partner_id, product_id) == refunded
