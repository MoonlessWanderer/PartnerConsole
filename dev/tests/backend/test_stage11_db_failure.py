"""A failure after the panel accepted a change must not refund or resend it.

The database error, crash or unexpected exception is injected exactly where the
`panel_applied` checkpoint is written, i.e. after V2Board already applied the
change. test_mysql_release_validation.py repeats the key cases on MySQL.
"""

from __future__ import annotations

import json
import time
from datetime import date
from decimal import Decimal

import httpx
import pymysql
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import PanelOperation, Partner, PartnerProductCredit, Subscription, WalletLedger
from app.services import money_sync
from app.services.money_sync import reconcile_pending, resolve_attention
from app.services.subscriptions import (
    Actor,
    create_subscription,
    decrease_subscription,
    delete_subscription,
    increase_subscription,
    reset_traffic,
    set_partner_ban,
)
from test_billing import GB, NOW, PanelSim, gateway, jalali, seed
from test_stage11_reproductions import _make_sub, db  # noqa: F401


@pytest.fixture()
def sim():
    return PanelSim()


def _db_error(errno: int) -> OperationalError:
    return OperationalError("UPDATE panel_operations", {}, pymysql.err.OperationalError(errno, "injected"))


class _FailCheckpoint:
    """Raise once on the first write that records panel_applied, before it is written.
    `outage` more lease-fenced writes fail afterwards, so not even the error can be saved."""

    def __init__(self, monkeypatch, error: BaseException, outage: int = 0):
        self.error, self.outage, self.fired = error, outage, False
        self.real_write, self.real_save = money_sync._write, money_sync._save_owned
        monkeypatch.setattr(money_sync, "_write", self.write)
        monkeypatch.setattr(money_sync, "_save_owned", self.save)

    def write(self, db, op, data, **kwargs):
        if data.get("panel_applied") and not self.fired:
            self.fired = True
            raise self.error
        return self.real_write(db, op, data, **kwargs)

    def save(self, db, op_id, values, **kwargs):
        if self.fired and self.outage > 0:
            self.outage -= 1
            raise _db_error(2013)
        return self.real_save(db, op_id, values, **kwargs)


FAILURES = {
    "disconnect": lambda: _db_error(2013),
    "lock_wait_1205": lambda: _db_error(1205),
    "deadlock_1213": lambda: _db_error(1213),
    "unexpected": lambda: RuntimeError("bug after the panel call"),
}

KINDS = ["create", "increase", "renewal", "decrease", "reset", "delete"]


def _money(db, partner_id, product_id):
    db.rollback()
    balance = db.scalar(select(Partner.balance_toman).where(Partner.id == partner_id))
    credit = db.scalar(select(PartnerProductCredit).where(PartnerProductCredit.partner_id == partner_id,
                                                          PartnerProductCredit.product_id == product_id))
    db.rollback()
    return int(balance), (Decimal(credit.credit_gb), int(credit.credit_days)) if credit else (Decimal(0), 0)


def _rollbacks(db) -> int:
    return len(db.scalars(select(WalletLedger).where(WalletLedger.reason == "operation_rollback")).all())


def _mutations(sim) -> tuple[int, int, int]:
    return len(sim.updates), len(sim.deleted), sim.generates


def _prepare(db, sim, kind):
    """Return (partner_id, product_id, run, expected panel mutations, check panel state)."""
    client = gateway(sim)
    if kind == "create":
        partner, product = seed(db, balance=50_000, price_gb=100)

        def run(gw):
            return create_subscription(
                db, partner_id=partner.id, product_id=product.id, username="ali01", traffic_cap_gb=20,
                end_date=jalali(date(2026, 6, 25)), note=None, actor=Actor("partner", partner.id),
                gateway=gw, now=NOW,
            )

        def landed():
            (user,) = sim.users.values()
            return user["transfer_enable"] == 20 * GB

        return partner.id, product.id, run, (1, 0, 1), landed
    partner, product, sub = _make_sub(db, client)
    uid = sub.v2_user_id
    if kind == "reset":
        sim.users[uid]["u"] = 10 * GB

        def run(gw):
            return reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id),
                                 gateway=gw)

        return partner.id, product.id, run, (1, 0, 0), lambda: sim.users[uid]["u"] == 0
    if kind == "delete":
        def run(gw):
            return delete_subscription(db, sub_id=sub.id, partner_id=partner.id,
                                       actor=Actor("partner", partner.id), gateway=gw, now=NOW)

        return partner.id, product.id, run, (0, 1, 0), lambda: uid not in sim.users
    if kind == "renewal":
        new_end = jalali(date(2026, 7, 25))

        def run(gw):
            return increase_subscription(db, sub_id=sub.id, partner_id=partner.id, traffic_cap_gb=None,
                                         end_date=new_end, actor=Actor("partner", partner.id), gateway=gw,
                                         now=NOW)

        before_exp = sim.users[uid]["expired_at"]
        return partner.id, product.id, run, (1, 0, 0), lambda: sim.users[uid]["expired_at"] > before_exp
    cap = 23 if kind == "increase" else 15
    perform = increase_subscription if kind == "increase" else decrease_subscription

    def run(gw):
        return perform(db, sub_id=sub.id, partner_id=partner.id, traffic_cap_gb=Decimal(cap), end_date=None,
                       actor=Actor("partner", partner.id), gateway=gw, now=NOW)

    return partner.id, product.id, run, (1, 0, 0), lambda: sim.users[uid]["transfer_enable"] == cap * GB


def _op(db, kind) -> PanelOperation:
    db.rollback()
    db.expire_all()
    name = {"renewal": "increase", "reset": "reset_traffic"}.get(kind, kind)
    return db.scalar(select(PanelOperation).where(PanelOperation.kind == name))


def _expire_lease(db, op_id):
    db.execute(update(PanelOperation).where(PanelOperation.id == op_id).values(lease_until=int(time.time()) - 1))
    db.commit()


def _restart_and_reconcile(db, sim) -> int:
    """A new process: fresh session, fresh panel client, nothing cached."""
    fresh = Session(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    try:
        return reconcile_pending(fresh, lambda _panel: gateway(sim))
    finally:
        fresh.close()


def _settled(kind):
    """A reset whose success was never recorded cannot be told from an admin or plan reset."""
    return "attention" if kind == "reset" else "completed"


def _after_refund(db, op, kind, money):
    if kind not in {"decrease", "delete"}:
        return money
    data = json.loads(op.payload)
    gb, days = Decimal(data["gb_back"]), int(data["days_back"])
    reason = "decrease" if kind == "decrease" else "delete_refund"
    ledger = db.execute(select(WalletLedger.asset, WalletLedger.amount).where(
        WalletLedger.partner_id == op.partner_id, WalletLedger.reason == reason,
    )).all()
    assert sorted(ledger) == sorted((asset, amount) for asset, amount in (("gb", gb), ("days", days)) if amount)
    toman, (have_gb, have_days) = money
    return toman, (have_gb + gb, have_days + days)


def _assert_no_replay(db, sim, kind, partner_id, product_id, start, expected, landed, money):
    op = _op(db, kind)
    assert op.status == _settled(kind), op.last_error
    assert _rollbacks(db) == 0
    assert _money(db, partner_id, product_id) == _after_refund(db, op, kind, money)
    done = _mutations(sim)
    assert tuple(after - before for after, before in zip(done, start)) == expected
    assert landed()


@pytest.mark.parametrize("failure", list(FAILURES))
@pytest.mark.parametrize("kind", KINDS)
def test_checkpoint_failure_after_panel_success_keeps_money_and_recovers_once(db, sim, monkeypatch, kind, failure):
    partner_id, product_id, run, expected, landed = _prepare(db, sim, kind)
    before = _money(db, partner_id, product_id)
    start = _mutations(sim)
    fail = _FailCheckpoint(monkeypatch, FAILURES[failure]())
    if failure == "unexpected":
        # Not a database error: the in-request recovery reads the panel and finishes.
        if kind == "reset":
            with pytest.raises(AppError) as caught:
                run(gateway(sim))
            assert caught.value.status_code == 409
        else:
            run(gateway(sim))
        assert fail.fired
        charged = _money(db, partner_id, product_id)
    else:
        with pytest.raises(AppError) as caught:
            run(gateway(sim))
        assert caught.value.status_code == 503
        assert fail.fired and landed()
        op = _op(db, kind)
        data = json.loads(op.payload)
        assert op.status == "pending" and op.lease_token is None
        assert not data.get("panel_applied")
        assert _rollbacks(db) == 0
        charged = _money(db, partner_id, product_id)
        if kind in {"decrease", "delete"}:
            assert charged == before
        monkeypatch.undo()
        assert _restart_and_reconcile(db, sim) == (kind != "reset")
    _assert_no_replay(db, sim, kind, partner_id, product_id, start, expected, landed,
                      before if kind in {"decrease", "delete"} else charged)


@pytest.mark.parametrize("kind", KINDS)
def test_database_down_after_panel_success_moves_no_money(db, sim, monkeypatch, kind):
    partner_id, product_id, run, expected, landed = _prepare(db, sim, kind)
    before = _money(db, partner_id, product_id)
    start = _mutations(sim)
    _FailCheckpoint(monkeypatch, _db_error(2013), outage=10)
    with pytest.raises(AppError) as caught:
        run(gateway(sim))
    assert caught.value.status_code == 503
    assert caught.value.message == money_sync.DB_UNAVAILABLE.format(id=_op(db, kind).id)
    monkeypatch.undo()
    op = _op(db, kind)
    # Nothing could be written: the row keeps its lease until it expires.
    assert op.status == "pending" and op.lease_token is not None
    assert _rollbacks(db) == 0
    charged = _money(db, partner_id, product_id)
    if kind in {"decrease", "delete"}:
        assert charged == before
    assert _restart_and_reconcile(db, sim) == 0
    _expire_lease(db, op.id)
    assert _restart_and_reconcile(db, sim) == (kind != "reset")
    _assert_no_replay(db, sim, kind, partner_id, product_id, start, expected, landed, charged)


@pytest.mark.parametrize("kind", KINDS)
def test_crash_between_panel_success_and_checkpoint_recovers_after_restart(db, sim, monkeypatch, kind):
    partner_id, product_id, run, expected, landed = _prepare(db, sim, kind)
    before = _money(db, partner_id, product_id)
    start = _mutations(sim)
    _FailCheckpoint(monkeypatch, SystemExit("worker killed"))
    with pytest.raises(SystemExit):
        run(gateway(sim))
    monkeypatch.undo()
    db.rollback()
    assert landed()
    charged = _money(db, partner_id, product_id)
    if kind in {"decrease", "delete"}:
        assert charged == before
    op = _op(db, kind)
    assert op.status == "pending"
    _expire_lease(db, op.id)
    assert _restart_and_reconcile(db, sim) == (kind != "reset")
    _assert_no_replay(db, sim, kind, partner_id, product_id, start, expected, landed, charged)


@pytest.mark.parametrize("kind", ["increase", "decrease", "create"])
def test_sent_change_with_unexplained_panel_state_waits_for_admin(db, sim, monkeypatch, kind):
    partner_id, product_id, run, expected, landed = _prepare(db, sim, kind)
    start = _mutations(sim)
    _FailCheckpoint(monkeypatch, SystemExit("worker killed"))
    with pytest.raises(SystemExit):
        run(gateway(sim))
    monkeypatch.undo()
    db.rollback()
    charged = _money(db, partner_id, product_id)
    op = _op(db, kind)
    uid = json.loads(op.payload).get("v2_user_id") or next(iter(sim.users))
    sim.users[int(uid)]["transfer_enable"] = 31 * GB  # someone else touched the panel meanwhile
    _expire_lease(db, op.id)
    assert _restart_and_reconcile(db, sim) == 0
    op = _op(db, kind)
    assert op.status == "attention"
    assert op.last_error == money_sync.CHANGE_UNPROVEN
    assert _mutations(sim) == tuple(a + b for a, b in zip(start, expected))
    assert _money(db, partner_id, product_id) == charged and _rollbacks(db) == 0
    assert _restart_and_reconcile(db, sim) == 0
    assert _mutations(sim) == tuple(a + b for a, b in zip(start, expected))
    # The admin checked the panel and asked for one more send.
    resolve_attention(db, op.id, decision="retry", actor=Actor("admin", 1), note="پنل بررسی شد",
                      gateway_factory=lambda _panel: gateway(sim))
    assert _restart_and_reconcile(db, sim) == 1
    assert _op(db, kind).status == "completed"
    assert landed()
    assert _money(db, partner_id, product_id) == _after_refund(db, _op(db, kind), kind, charged)
    assert _rollbacks(db) == 0
    updates = _mutations(sim)[0] - start[0]
    assert updates == expected[0] + 1


def test_sent_change_that_may_not_have_landed_waits_for_the_admin_then_is_sent_once(db, sim, monkeypatch):
    partner_id, product_id, run, _expected, landed = _prepare(db, sim, "increase")
    start = len(sim.updates)
    real = money_sync._send_marked

    def crash_before_request(db_, op, data, call):
        real(db_, op, data, lambda: (_ for _ in ()).throw(SystemExit("killed before the request")))

    monkeypatch.setattr(money_sync, "_send_marked", crash_before_request)
    with pytest.raises(SystemExit):
        run(gateway(sim))
    monkeypatch.undo()
    db.rollback()
    charged = _money(db, partner_id, product_id)
    op = _op(db, "increase")
    assert json.loads(op.payload)["panel_sent"] is True and not landed()
    _expire_lease(db, op.id)
    assert _restart_and_reconcile(db, sim) == 0
    assert _op(db, "increase").status == "attention" and len(sim.updates) == start
    assert _money(db, partner_id, product_id) == charged and _rollbacks(db) == 0
    op = _op(db, "increase")
    money_sync.resolve_attention(db, op.id, decision="retry", actor=Actor("admin", 1), note="پنل بررسی شد",
                                 gateway_factory=lambda _panel: gateway(sim))
    assert _restart_and_reconcile(db, sim) == 1
    assert _op(db, "increase").status == "completed" and landed()
    assert len(sim.updates) - start == 1
    assert _money(db, partner_id, product_id) == charged and _rollbacks(db) == 0


def test_panel_read_failure_during_recovery_keeps_the_row_pending(db, sim, monkeypatch):
    partner_id, product_id, run, _expected, landed = _prepare(db, sim, "increase")
    _FailCheckpoint(monkeypatch, _db_error(2013))
    with pytest.raises(AppError):
        run(gateway(sim))
    monkeypatch.undo()
    charged = _money(db, partner_id, product_id)
    updates = len(sim.updates)
    real = sim.handler

    def broken(request):
        if request.url.path.endswith(("/user/getUserInfoById", "/user/fetch")):
            return httpx.Response(502, text="bad gateway")
        return real(request)

    sim.handler = broken
    assert _restart_and_reconcile(db, sim) == 0
    assert _op(db, "increase").status == "pending"
    assert len(sim.updates) == updates
    assert _money(db, partner_id, product_id) == charged and _rollbacks(db) == 0
    sim.handler = real
    assert _restart_and_reconcile(db, sim) == 1
    assert len(sim.updates) == updates and landed()


def test_partner_ban_crash_after_update_does_not_resend(db, sim):
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    uid = sub.v2_user_id
    start = len(sim.updates)

    class Crash:
        def __init__(self, inner):
            self.inner = inner

        def update_user(self, payload):
            self.inner.update_user(payload)
            raise SystemExit("worker killed after the ban landed")

        def __getattr__(self, name):
            return getattr(self.inner, name)

    with pytest.raises(SystemExit):
        set_partner_ban(db, partner_id=partner.id, banned=True, actor=Actor("admin", 1),
                        gateway_factory=lambda _panel: Crash(gateway(sim)))
    db.rollback()
    assert sim.users[uid]["banned"] in (1, True)
    op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "partner_ban"))
    assert op.status == "pending"
    _expire_lease(db, op.id)
    assert _restart_and_reconcile(db, sim) == 1
    db.rollback()
    assert db.scalar(select(PanelOperation.status).where(PanelOperation.id == op.id)) == "completed"
    assert db.scalar(select(Subscription.banned).where(Subscription.id == sub.id)) is True
    assert len(sim.updates) - start == 1


def test_partner_ban_checkpoint_db_failure_does_not_resend(db, sim, monkeypatch):
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    start = len(sim.updates)
    real = money_sync._save_owned
    state = {"fired": False}

    def fail_checkpoint(db_, op_id, values, **kwargs):
        if "payload" in values and not state["fired"] and '"done": true' in values["payload"]:
            state["fired"] = True
            raise _db_error(2013)
        return real(db_, op_id, values, **kwargs)

    monkeypatch.setattr(money_sync, "_save_owned", fail_checkpoint)
    with pytest.raises(AppError) as caught:
        set_partner_ban(db, partner_id=partner.id, banned=True, actor=Actor("admin", 1),
                        gateway_factory=lambda _panel: gateway(sim))
    assert caught.value.status_code == 503 and state["fired"]
    monkeypatch.undo()
    assert _restart_and_reconcile(db, sim) == 1
    assert len(sim.updates) - start == 1
