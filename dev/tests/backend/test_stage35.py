"""Stage 3.5 regressions on independent connections to temporary SQLite files."""
import json
import sqlite3
import threading
from datetime import date

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, make_engine
from app.core.errors import AppError, V2BoardError
from app.models import PanelOperation, Partner, Subscription, WalletLedger
from app.services import money_sync as sync
from app.services.partner_ban import drive_partner_ban
from app.services.subscriptions import (
    Actor, create_subscription, increase_subscription, reset_traffic, set_partner_ban, set_subscription_ban,
)
from test_billing import NOW, GB, PanelSim, gateway, jalali, seed


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "stage35.db"
    engine = make_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    sim = PanelSim()
    with sessions() as db:
        partner, product = seed(db)
        client = gateway(sim)
        ids = []
        for username in ("ali01", "ali02"):
            sub, _, _ = create_subscription(
                db, partner_id=partner.id, product_id=product.id, username=username,
                traffic_cap_gb=10, end_date=jalali(date(2026, 6, 25)), note=None,
                actor=Actor("partner", partner.id), gateway=client, now=NOW,
            )
            ids.append(sub.id)
        client.close()
        pid = partner.id
        balance = partner.balance_toman
    yield sessions, sim, pid, ids, balance, path
    engine.dispose()


def probe_write(path):
    # A second connection must be able to acquire the write lock immediately.
    with sqlite3.connect(path, timeout=0.15) as other:
        other.execute("UPDATE partners SET balance_toman=balance_toman")


@pytest.mark.parametrize("expire", [False, True])
def test_reset_all_panel_calls_release_sqlite_lock(setup, expire):
    sessions, sim, pid, ids, balance, path = setup
    sim.users[1]["u"] = 2 * GB
    calls = []

    class Probe:
        def __init__(self):
            self.inner = gateway(sim)

        def __getattr__(self, name):
            method = getattr(self.inner, name)
            if name in ("get_plan", "get_user_info", "update_user"):
                def call(*args, **kwargs):
                    probe_write(path)
                    calls.append(name)
                    return method(*args, **kwargs)
                return call
            return method

    with sessions() as db:
        db.expire_on_commit = expire
        client = Probe()
        try:
            result = reset_traffic(db, sub_id=ids[0], partner_id=pid,
                                   actor=Actor("partner", pid), gateway=client)
        finally:
            client.close()
        assert result["cost_toman"] == 2000
        assert result["balance_toman"] == balance - 2000
        assert calls.count("get_user_info") == 2
        assert "update_user" in calls
        assert db.get(Subscription, ids[0]).traffic_cap_gb == 10


@pytest.mark.parametrize("failure", ["timeout_before", "timeout_after", "panel_error", "crash"])
def test_batch_failure_is_durable_and_reconcile_finishes(setup, failure):
    sessions, sim, pid, ids, balance, path = setup
    calls = []
    fail = [True]

    class Broken:
        def __init__(self):
            self.inner = gateway(sim)

        def get_plan(self, plan_id):
            probe_write(path)
            return self.inner.get_plan(plan_id)

        def get_user_info(self, user_id):
            probe_write(path)
            return self.inner.get_user_info(user_id)

        def update_user(self, payload):
            probe_write(path)
            calls.append(payload["id"])
            if payload["id"] == 2 and fail[0]:
                if failure in ("timeout_after", "crash"):
                    self.inner.update_user(payload)
                if failure == "crash":
                    raise SystemExit("worker died after remote update")
                if failure == "panel_error":
                    raise V2BoardError("panel refused update")
                raise httpx.ReadTimeout("lost response")
            self.inner.update_user(payload)

        def close(self):
            self.inner.close()

    def factory(panel):
        probe_write(path)
        return Broken()

    with sessions() as db:
        with pytest.raises(SystemExit if failure == "crash" else AppError):
            set_partner_ban(db, partner_id=pid, banned=True,
                            actor=Actor("admin", 1), gateway_factory=factory)
        db.rollback()
    with sessions() as db:
        op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "partner_ban"))
        assert op.status == "pending"
        assert db.get(Partner, pid).banned
        assert db.get(Subscription, ids[0]).blocked_by_partner_ban
        assert not db.get(Subscription, ids[1]).blocked_by_partner_ban
        assert json.loads(op.payload)["targets"][0]["done"]
        if failure == "crash":
            op.lease_until = 0
            db.commit()
        with pytest.raises(AppError, match="ناتمام"):
            set_partner_ban(db, partner_id=pid, banned=False,
                            actor=Actor("admin", 1), gateway_factory=factory)
        db.rollback()
        with pytest.raises(AppError, match="ناتمام"):
            set_subscription_ban(db, sub_id=ids[0], partner_id=None, banned=False,
                                 actor=Actor("admin", 1), gateway=gateway(sim))
        db.rollback()
        fail[0] = False
        assert sync.reconcile_pending(db, factory) == 1
        assert db.get(Partner, pid).balance_toman == balance
        assert db.scalar(select(PanelOperation).where(PanelOperation.kind == "partner_ban")).status == "completed"
        assert all(db.get(Subscription, sid).blocked_by_partner_ban for sid in ids)
        assert calls.count(1) == 1  # completed targets are not replayed


def test_unban_failure_keeps_login_disabled_and_manual_ban(setup):
    sessions, sim, pid, ids, balance, path = setup
    with sessions() as db:
        sub = db.get(Subscription, ids[1])
        sub.banned = True
        sim.users[2]["banned"] = 1
        db.commit()
        factory = lambda panel: gateway(sim)
        set_partner_ban(db, partner_id=pid, banned=True, actor=Actor("admin", 1), gateway_factory=factory)
        sim.fail_update = True
        with pytest.raises(AppError):
            set_partner_ban(db, partner_id=pid, banned=False, actor=Actor("admin", 1), gateway_factory=factory)
        db.rollback()
        assert db.get(Partner, pid).banned
        assert db.get(Subscription, ids[0]).blocked_by_partner_ban
        sim.fail_update = False
        assert sync.reconcile_pending(db, factory) == 1
        assert not db.get(Partner, pid).banned
        assert not db.get(Subscription, ids[0]).banned
        assert db.get(Subscription, ids[1]).banned
        assert not db.get(Subscription, ids[1]).blocked_by_partner_ban
        assert sim.users[2]["banned"] == 1


def test_concurrent_batch_rejects_duplicate_and_opposite_requests(setup):
    sessions, sim, pid, ids, balance, path = setup
    entered, release = threading.Event(), threading.Event()
    errors = []

    class Slow:
        def __init__(self):
            self.inner = gateway(sim)

        def get_plan(self, plan_id):
            entered.set()
            assert release.wait(5)
            return self.inner.get_plan(plan_id)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    def run():
        with sessions() as db:
            try:
                set_partner_ban(db, partner_id=pid, banned=True, actor=Actor("admin", 1),
                                gateway_factory=lambda panel: Slow())
            except BaseException as exc:
                errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5)
        probe_write(path)
        for banned in (True, False):
            with sessions() as db, pytest.raises(AppError):
                set_partner_ban(db, partner_id=pid, banned=banned, actor=Actor("admin", 1),
                                gateway_factory=lambda panel: gateway(sim))
        with sessions() as db:
            assert sync.reconcile_pending(db, lambda panel: gateway(sim)) == 0
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert errors == []
    assert len(sim.updates) == 4  # two creates, two batch targets


def test_stale_lease_cannot_commit_wallet_when_terminal_status_matches(setup):
    sessions, sim, pid, ids, balance, path = setup
    with sessions() as db:
        op = PanelOperation(partner_id=pid, product_id=1, kind="reset_traffic",
                            status="completed", payload="{}", lease_token=None)
        db.add(op)
        db.commit()
        op_id = op.id
        handle = sync._lease_token.set("stale")
        try:
            db.get(Partner, pid).balance_toman -= 100
            with pytest.raises(sync.LostLease):
                sync._save_owned(db, op_id, {"status": "completed"})
            assert db.get(Partner, pid).balance_toman == balance
        finally:
            sync._lease_token.reset(handle)


def test_overlapping_resets_recheck_usage_and_charge_once(setup):
    sessions, sim, pid, ids, balance, path = setup
    sim.users[1]["u"] = 2 * GB
    entered, release = threading.Event(), threading.Event()
    errors = []

    class SlowUsage:
        def __init__(self):
            self.inner = gateway(sim)
            self.once = True

        def get_user_info(self, uid):
            info = self.inner.get_user_info(uid)
            if self.once:
                self.once = False
                entered.set()
                assert release.wait(5)
            return info

        def __getattr__(self, name):
            return getattr(self.inner, name)

    def run():
        with sessions() as db:
            client = SlowUsage()
            try:
                reset_traffic(db, sub_id=ids[0], partner_id=pid,
                              actor=Actor("partner", pid), gateway=client)
            except AppError as exc:
                errors.append(exc.message)
            finally:
                client.close()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        with sessions() as db:
            client = gateway(sim)
            try:
                reset_traffic(db, sub_id=ids[0], partner_id=pid,
                              actor=Actor("partner", pid), gateway=client)
            finally:
                client.close()
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert errors == ["مصرفی برای ریست وجود ندارد"]
    with sessions() as db:
        assert db.get(Partner, pid).balance_toman == balance - 2000
        assert len(db.scalars(select(WalletLedger).where(WalletLedger.reason == "reset_traffic")).all()) == 1


def test_reset_retries_usage_after_concurrent_quota_change(setup):
    sessions, sim, pid, ids, balance, path = setup
    sim.users[1]["u"] = GB
    reads = []

    class Changed:
        def __init__(self):
            self.inner = gateway(sim)

        def get_user_info(self, uid):
            info = self.inner.get_user_info(uid)
            reads.append(uid)
            if len(reads) == 1:
                with sessions() as other:
                    client = gateway(sim)
                    try:
                        increase_subscription(other, sub_id=ids[0], partner_id=pid,
                                              traffic_cap_gb=12, end_date=None,
                                              actor=Actor("partner", pid), gateway=client, now=NOW)
                    finally:
                        client.close()
                sim.users[1]["u"] = 2 * GB
            return info

        def __getattr__(self, name):
            return getattr(self.inner, name)

    with sessions() as db:
        client = Changed()
        try:
            result = reset_traffic(db, sub_id=ids[0], partner_id=pid,
                                   actor=Actor("partner", pid), gateway=client)
        finally:
            client.close()
        assert result["cost_toman"] == 2000
        assert result["balance_toman"] == balance - 4000
        assert len(reads) == 3
        assert db.get(Subscription, ids[0]).traffic_cap_gb == 12
        assert sim.users[1]["transfer_enable"] == 12 * GB


@pytest.mark.parametrize("method", ["get_plan", "get_user_info"])
def test_reset_preflight_disconnect_moves_no_money(setup, method):
    sessions, sim, pid, ids, balance, path = setup

    class Disconnected:
        def __getattr__(self, name):
            def call(*args):
                if name == method:
                    raise httpx.ConnectError("offline")
                return {}
            return call

    with sessions() as db:
        with pytest.raises(AppError, match="مبلغی جابه‌جا نشده"):
            reset_traffic(db, sub_id=ids[0], partner_id=pid,
                          actor=Actor("partner", pid), gateway=Disconnected())
        db.rollback()
        assert db.get(Partner, pid).balance_toman == balance
        assert not db.scalars(select(PanelOperation).where(PanelOperation.kind == "reset_traffic")).all()


def test_batch_waits_for_pending_financial_operation(setup):
    sessions, sim, pid, ids, balance, path = setup
    with sessions() as db:
        op = PanelOperation(partner_id=pid, product_id=1, subscription_id=ids[0],
                            kind="reset_traffic", status="pending", payload="{}")
        db.add(op)
        db.commit()
        with pytest.raises(AppError, match="ناتمام"):
            set_partner_ban(db, partner_id=pid, banned=True, actor=Actor("admin", 1),
                            gateway_factory=lambda panel: pytest.fail("must not call panel"))
        db.rollback()
        assert not db.get(Partner, pid).banned
