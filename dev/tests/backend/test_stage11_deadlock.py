"""Bounded retry of the final status write after an InnoDB deadlock (errno 1213).

The deadlock is injected here; test_mysql_release_validation.py produces a
real one on MySQL.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal

import pymysql
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.core.errors import AppError
from app.models import PanelOperation, Partner
from app.services import money_sync
from app.services.money_sync import reconcile_pending
from app.services.subscriptions import Actor, increase_subscription, reset_traffic
from test_billing import GB, PanelSim, _toman_ledgers, gateway
from test_stage11_reproductions import _make_sub, _reset_op, db  # noqa: F401


@pytest.fixture()
def sim():
    return PanelSim()


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(money_sync, "DEADLOCK_BACKOFF", 0.001)


def _db_error(errno: int) -> OperationalError:
    return OperationalError("UPDATE panel_operations", {}, pymysql.err.OperationalError(errno, "injected"))


class _FailFinal:
    """Raise `errno` for the first `times` final status writes, before anything is written."""

    def __init__(self, monkeypatch, errno: int, times: int, before=None):
        self.errno, self.times, self.before, self.calls = errno, times, before, 0
        self.real = money_sync._save_owned
        monkeypatch.setattr(money_sync, "_save_owned", self)

    def __call__(self, db, op_id, values, **kwargs):
        if values.get("status") == "completed":
            self.calls += 1
            if self.calls <= self.times:
                if self.before:
                    self.before(db, op_id)
                raise _db_error(self.errno)
        return self.real(db, op_id, values, **kwargs)


COST = 300  # 3 GB at 100 toman


def _increase(db, partner, sub, client):
    return increase_subscription(db, sub_id=sub.id, partner_id=partner.id, traffic_cap_gb=Decimal(23),
                                 end_date=None, actor=Actor("partner", partner.id), gateway=client)


def _increase_op(db) -> PanelOperation:
    return db.scalar(select(PanelOperation).where(PanelOperation.kind == "increase"))


def _cap_updates(sim) -> int:
    return sum(1 for body in sim.updates if body.get("transfer_enable") == 23 * GB)


def test_one_deadlock_is_retried_in_a_fresh_transaction(db, sim, monkeypatch, caplog):
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    before = int(partner.balance_toman)
    fail = _FailFinal(monkeypatch, 1213, 1)
    with caplog.at_level(logging.WARNING, logger="partner.billing"):
        _increase(db, partner, sub, client)
    db.refresh(partner)
    assert fail.calls == 2
    assert _increase_op(db).status == "completed"
    assert _cap_updates(sim) == 1
    assert int(partner.balance_toman) == before - COST
    assert len(_toman_ledgers(db, "increase")) == 1
    assert any("deadlock" in record.getMessage() for record in caplog.records)


def test_exhausted_retries_leave_a_recoverable_pending_operation(db, sim, monkeypatch):
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    before = int(partner.balance_toman)
    fail = _FailFinal(monkeypatch, 1213, 3)
    with pytest.raises(AppError) as caught:
        _increase(db, partner, sub, client)
    assert caught.value.status_code == 503
    db.rollback()
    db.refresh(partner)
    op = _increase_op(db)
    assert fail.calls == 3
    assert op.status == "pending" and op.lease_token is None
    assert json.loads(op.payload)["panel_applied"] is True
    assert int(partner.balance_toman) == before - COST
    assert not _toman_ledgers(db, "operation_rollback")
    monkeypatch.setattr(money_sync, "_save_owned", fail.real)
    assert reconcile_pending(db, lambda _panel: gateway(sim)) == 1
    db.refresh(partner)
    assert _increase_op(db).status == "completed"
    assert _cap_updates(sim) == 1
    assert int(partner.balance_toman) == before - COST
    assert len(_toman_ledgers(db, "increase")) == 1


def test_lock_wait_timeout_is_not_retried(db, sim, monkeypatch):
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    before = int(partner.balance_toman)
    fail = _FailFinal(monkeypatch, 1205, 1)
    with pytest.raises(AppError):
        _increase(db, partner, sub, client)
    db.rollback()
    db.refresh(partner)
    assert fail.calls == 1
    assert _increase_op(db).status == "pending"
    assert int(partner.balance_toman) == before - COST
    assert reconcile_pending(db, lambda _panel: gateway(sim)) == 1
    assert _cap_updates(sim) == 1


def _steal_lease(db, op_id):
    db.execute(update(PanelOperation).where(PanelOperation.id == op_id).values(lease_token="other-worker"))
    db.commit()


def _complete_elsewhere(db, op_id):
    db.execute(update(PanelOperation).where(PanelOperation.id == op_id)
               .values(status="completed", lease_token=None, lease_until=None))
    db.commit()


@pytest.mark.parametrize("change", [_steal_lease, _complete_elsewhere])
def test_retry_rechecks_status_and_lease_before_writing(db, sim, monkeypatch, change):
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    before = int(partner.balance_toman)
    fail = _FailFinal(monkeypatch, 1213, 1, before=change)
    with pytest.raises(AppError) as caught:
        _increase(db, partner, sub, client)
    assert caught.value.message == money_sync.BUSY
    db.rollback()
    db.refresh(partner)
    assert fail.calls == 1
    op = _increase_op(db)
    if change is _steal_lease:
        assert (op.status, op.lease_token) == ("pending", "other-worker")
    else:
        assert op.status == "completed"
    assert _cap_updates(sim) == 1
    assert int(partner.balance_toman) == before - COST


def test_reset_final_write_is_retried_without_a_second_reset(db, sim, monkeypatch):
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    sim.users[sub.v2_user_id]["u"] = 10 * GB
    before = int(partner.balance_toman)
    fail = _FailFinal(monkeypatch, 1213, 2)
    reset_traffic(db, sub_id=sub.id, partner_id=partner.id, actor=Actor("partner", partner.id), gateway=client)
    db.refresh(partner)
    assert fail.calls == 3
    assert _reset_op(db).status == "completed"
    assert sum(1 for body in sim.updates if body.get("u") == 0 and body.get("d") == 0) == 1
    assert int(partner.balance_toman) == before - 1000


def test_final_write_refuses_to_share_a_transaction(db, sim):
    client = gateway(sim)
    partner, _product, sub = _make_sub(db, client)
    db.get(Partner, partner.id).balance_toman = 1
    op = db.scalar(select(PanelOperation))
    with pytest.raises(RuntimeError):
        money_sync._complete_owned(db, op, json.loads(op.payload))
    db.rollback()
