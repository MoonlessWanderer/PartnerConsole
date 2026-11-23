"""Opt-in ONLY, on an owner-approved, empty local MySQL test schema.

Never creates a database or drops/clears existing data. Artifacts remain for
inspection; another run requires a separately approved empty test schema.
No external panel is used (only httpx.MockTransport).
"""
import json
import multiprocessing
import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import make_engine
from app.core.errors import AppError
from app.core.security import _reserve_bucket, create_token, decode_token, revoke_token
from app.models import LoginThrottle, PanelOperation, Partner, PartnerProductCredit, Subscription, WalletLedger, RevokedToken
from app.services import money_sync
from app.services.subscriptions import Actor, create_subscription, increase_subscription, set_partner_ban, update_note
from test_billing import seed, gateway, PanelSim, NOW, jalali
from test_mysql_compatibility import migration_config


def isolated_mysql_url(raw):
    try:
        url = make_url(raw)
    except (ArgumentError, ValueError):
        raise ValueError("Invalid MySQL test URL") from None
    if (url.drivername != 'mysql+pymysql' or url.host not in {'localhost', '127.0.0.1', '::1'}
            or not url.database or not url.database.startswith('partnerpanel_test_')):
        raise ValueError("Only an explicitly named local partnerpanel_test_ schema is permitted")
    # The dialect can merge query parameters into the driver's connection
    # arguments, bypassing checks made on the URL's structured fields.
    if url.query and dict(url.query) != {"charset": "utf8mb4"}:
        raise ValueError("MySQL test URL may only include charset=utf8mb4")
    return url


@pytest.fixture(scope='module')
def mysql_engine():
    raw = os.environ.get('PARTNERPANEL_MYSQL_TEST_URL')
    if not raw:
        pytest.skip('No owner-approved MySQL test environment provided')
    # Two explicit guards; never fall back to DATABASE_URL or app startup.
    if os.environ.get('PARTNERPANEL_MYSQL_TEST_ALLOW_SCHEMA_WRITES') != 'isolated-schema-approved':
        pytest.fail('Explicit approval of writes to an empty test schema is required', pytrace=False)
    try:
        isolated_mysql_url(raw)
    except ValueError as exc:
        pytest.fail(str(exc), pytrace=False)
    pytest.importorskip('pymysql', reason='PyMySQL has not been installed with owner approval')
    engine = make_engine(raw)
    try:
        with engine.connect() as conn:
            version = conn.scalar(text('SELECT VERSION()'))
            assert version.startswith('8.4.') and 'mariadb' not in version.lower()
            assert inspect(conn).get_table_names() == [], 'Test schema must be empty; no data will be removed'
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(settings, 'database_url', raw)
            command.upgrade(migration_config(), '0004_subscription_identity')
            with sessionmaker(bind=engine)() as db:
                partner, product = seed(db, balance=3_000_000_000, credit_gb=Decimal('1.234567'))
                partner.username = 'migration_synthetic'
                pid, product_id = partner.id, product.id
                db.commit()
            command.upgrade(migration_config(), 'head')
            command.upgrade(migration_config(), 'head')
        with sessionmaker(bind=engine)() as db:
            assert db.get(Partner, pid).balance_toman == 3_000_000_000
            assert db.scalar(select(PartnerProductCredit.credit_gb).where(
                PartnerProductCredit.product_id == product_id)) == Decimal('1.234567')
            assert db.scalar(text('SELECT version_num FROM alembic_version')) == '0005_security_state'
        yield engine
    finally:
        engine.dispose()


def seeded(engine):
    with sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)() as db:
        partner, product = seed(db)
        partner.username = 'synthetic_' + secrets.token_hex(8)
        db.commit()
        return partner.id, product.id


def _reserve_in_process(raw, bucket, queue):
    engine = make_engine(raw)
    try:
        with sessionmaker(bind=engine)() as db:
            accepted = _reserve_bucket(db, bucket, 5, 600, 1000)
            db.commit()
            queue.put(('ok', accepted))
    except Exception as exc:
        queue.put(('error', type(exc).__name__))
    finally:
        engine.dispose()


def test_mysql_quota_multiple_processes(mysql_engine):
    ctx = multiprocessing.get_context('spawn')
    queue = ctx.Queue()
    bucket = secrets.token_hex(32)
    raw = os.environ['PARTNERPANEL_MYSQL_TEST_URL']
    processes = [ctx.Process(target=_reserve_in_process, args=(raw, bucket, queue)) for _ in range(12)]
    try:
        for process in processes:
            process.start()
        results = [queue.get(timeout=45) for _ in processes]
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        assert all(status == 'ok' for status, _ in results)
        assert sum(value for _, value in results) == 5
        with sessionmaker(bind=mysql_engine)() as db:
            assert db.get(LoginThrottle, bucket).attempts == 5
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        queue.close()


def test_mysql_quota_expiry_and_transaction_rollback(mysql_engine):
    bucket = secrets.token_hex(32)
    with sessionmaker(bind=mysql_engine)() as db:
        assert _reserve_bucket(db, bucket, 1, 60, 100)
        db.rollback()
        assert db.get(LoginThrottle, bucket) is None
        assert _reserve_bucket(db, bucket, 1, 60, 100)
        db.commit()
        assert not _reserve_bucket(db, bucket, 1, 60, 159)
        db.commit()
        assert _reserve_bucket(db, bucket, 1, 60, 160)
        db.commit()
        assert db.get(LoginThrottle, bucket).reset_at == 220


def test_mysql_logout_is_idempotent_and_visible_on_another_connection(mysql_engine, monkeypatch):
    monkeypatch.setattr(settings, 'secret_key', 'synthetic-mysql-test-only-key-for-signing-tokens')
    token = create_token('partner', 999, 'synthetic')
    with sessionmaker(bind=mysql_engine)() as first:
        revoke_token(token, 'partner', first)
        revoke_token(token, 'partner', first)
    with sessionmaker(bind=mysql_engine)() as other:
        assert other.get(RevokedToken, decode_token(token, 'partner')['jti']) is not None


def test_mysql_pending_uniqueness_and_history(mysql_engine):
    pid, product_id = seeded(mysql_engine)
    with sessionmaker(bind=mysql_engine)() as db:
        sub = Subscription(partner_id=pid, product_id=product_id, username='unique_sub', v2_user_id=1,
                           traffic_cap_gb=10, expired_at=2000000000, banned=False, blocked_by_partner_ban=False)
        db.add(sub)
        db.flush()
        def operation(status, kind='increase', subscription_id=sub.id):
            return PanelOperation(partner_id=pid, product_id=product_id, username='unique_sub',
                                  subscription_id=subscription_id, kind=kind, status=status, payload='{}')
        db.add_all([operation('completed'), operation('completed'), operation('failed'), operation('pending')])
        db.flush()
        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(operation('pending'))
            db.flush()
        pending = db.scalar(select(PanelOperation).where(PanelOperation.partner_id == pid,
                                                       PanelOperation.status == 'pending'))
        pending.status = 'completed'
        db.flush()
        db.add(operation('pending'))
        db.flush()
        db.add(operation('pending', kind='create', subscription_id=None))
        db.flush()
        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(operation('pending', kind='create', subscription_id=None))
            db.flush()
        db.rollback()  # Only this test's synthetic operations.


def test_mysql_storage_connection_policy_and_fk_checks(mysql_engine):
    pid, product_id = seeded(mysql_engine)
    with mysql_engine.connect() as conn:
        row = conn.execute(text('SELECT @@transaction_isolation, @@session.time_zone, '
                                '@@character_set_connection, @@collation_connection, @@session.sql_mode')).one()
        assert row[0] == 'READ-COMMITTED' and row[1] == '+00:00'
        assert row[2] == 'utf8mb4' and row[3] == 'utf8mb4_0900_bin'
        assert 'STRICT_TRANS_TABLES' in row[4]
    with sessionmaker(bind=mysql_engine, expire_on_commit=False)() as db:
        partner = db.get(Partner, pid)
        partner.balance_toman = 3_000_000_000
        stamp = datetime(2026, 11, 20, 13, 14, 15, 123456, tzinfo=timezone.utc)
        sub = Subscription(partner_id=pid, product_id=product_id, username='unicode_sub', v2_user_id=8,
                           note='یادداشت فارسی\u200c🙂', traffic_cap_gb=Decimal('1.234567'),
                           expired_at=2000000000, updated_at=stamp)
        db.add(sub)
        db.commit()
        db.expire_all()
        assert sub.updated_at == stamp and sub.note == 'یادداشت فارسی\u200c🙂'
        assert sub.traffic_cap_gb == Decimal('1.234567') and partner.balance_toman == 3_000_000_000
        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(PartnerProductCredit(partner_id=-1, product_id=product_id, credit_gb=0, credit_days=0))
            db.flush()
        # PyMySQL maps ER_CHECK_CONSTRAINT_VIOLATED (3819) to OperationalError, not IntegrityError.
        with pytest.raises(OperationalError) as rejected, db.begin_nested():
            partner.balance_toman = -1
            db.flush()
        assert rejected.value.orig.args[0] == 3819 and 'ck_partner_toman_nonneg' in rejected.value.orig.args[1]
        db.expire_all()
        assert db.get(Partner, pid).balance_toman == 3_000_000_000
        db.rollback()


def test_mysql_mock_panel_finance_replay_and_group_ban(mysql_engine):
    pid, product_id = seeded(mysql_engine)
    sessions = sessionmaker(bind=mysql_engine, autoflush=False, expire_on_commit=False)
    sim = PanelSim()
    with sessions() as db, gateway(sim) as client:
        sub, _, _ = create_subscription(
            db, partner_id=pid, product_id=product_id, username='financial_sub', traffic_cap_gb=10,
            end_date=jalali(date(2026, 6, 25)), note=None, actor=Actor('partner', pid), gateway=client, now=NOW)
        sub_id = sub.id
        balance = db.get(Partner, pid).balance_toman
        increase_subscription(db, sub_id=sub_id, partner_id=pid, traffic_cap_gb=12,
                              end_date=None, actor=Actor('partner', pid), gateway=client, now=NOW)
        assert db.get(Partner, pid).balance_toman == balance - 2000
        money_sync.reconcile_pending(db, lambda panel: gateway(sim))
        assert db.get(Partner, pid).balance_toman == balance - 2000
        set_partner_ban(db, partner_id=pid, banned=True, actor=Actor('admin', 1),
                        gateway_factory=lambda panel: gateway(sim))
        db.expire_all()
        assert db.get(Partner, pid).banned and db.get(Subscription, sub_id).blocked_by_partner_ban
        with pytest.raises(AppError) as denied:
            update_note(db, sub_id=sub_id, partner_id=pid, note='must be denied')
        assert denied.value.status_code == 403
        db.rollback()
        update_note(db, sub_id=sub_id, partner_id=None, note='admin permitted')
        db.commit()
        assert db.get(Subscription, sub_id).note == 'admin permitted'


def test_mysql_account_ban_race_refreshes_cached_account(mysql_engine):
    from app.services.subscriptions import lock_partner_row
    pid, product_id = seeded(mysql_engine)
    sessions = sessionmaker(bind=mysql_engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        sub = Subscription(partner_id=pid, product_id=product_id, username='race_sub', v2_user_id=20,
                           traffic_cap_gb=10, expired_at=2000000000)
        db.add(sub)
        db.commit()
        sub_id = sub.id
    cached, proceed = threading.Event(), threading.Event()
    def request():
        with sessions() as db:
            account = db.get(Partner, pid)
            assert not account.banned
            cached.set()
            assert proceed.wait(10)
            try:
                update_note(db, sub_id=sub_id, partner_id=pid, note='must not persist')
            except AppError as exc:
                db.rollback()
                return exc.status_code
            db.commit()
            return 200
    with ThreadPoolExecutor(max_workers=1) as executor:
        with sessions() as admin:
            partner = lock_partner_row(admin, pid, allow_banned=True)
            future = executor.submit(request)
            assert cached.wait(10)
            partner.banned = True
            admin.commit()
            proceed.set()
        assert future.result(timeout=15) == 403
    with sessions() as db:
        assert db.get(Subscription, sub_id).note is None


def test_mysql_uncertain_panel_result_stays_pending_and_recovers_once(mysql_engine):
    import httpx
    pid, product_id = seeded(mysql_engine)
    sessions = sessionmaker(bind=mysql_engine, autoflush=False, expire_on_commit=False)
    sim = PanelSim()
    with sessions() as db, gateway(sim) as client:
        sub, _, _ = create_subscription(
            db, partner_id=pid, product_id=product_id, username='recovery_sub', traffic_cap_gb=10,
            end_date=jalali(date(2026, 6, 25)), note=None, actor=Actor('partner', pid), gateway=client, now=NOW)
        sub_id, balance = sub.id, db.get(Partner, pid).balance_toman
    original = sim.handler
    def ambiguous(request):
        if request.url.path.endswith('/user/update'):
            original(request)  # Simulator applied it, confirmation was lost.
            return httpx.Response(500, json={'message': 'synthetic response lost'})
        if request.url.path.endswith('/user/fetch'):
            return httpx.Response(200, json={'data': {}})
        return original(request)
    sim.handler = ambiguous
    with sessions() as db, gateway(sim) as client:
        with pytest.raises(AppError):
            increase_subscription(db, sub_id=sub_id, partner_id=pid, traffic_cap_gb=12,
                                  end_date=None, actor=Actor('partner', pid), gateway=client, now=NOW)
        db.rollback()
        assert db.scalar(select(PanelOperation).where(PanelOperation.partner_id == pid,
                                                     PanelOperation.status == 'pending')) is not None
        assert db.get(Partner, pid).balance_toman == balance - 2000
    sim.handler = original
    with sessions() as db:
        money_sync.reconcile_pending(db, lambda panel: gateway(sim))
        money_sync.reconcile_pending(db, lambda panel: gateway(sim))
        assert db.get(Partner, pid).balance_toman == balance - 2000
        assert db.get(Subscription, sub_id).traffic_cap_gb == 12
        assert db.scalar(select(PanelOperation).where(PanelOperation.partner_id == pid,
                                                     PanelOperation.status == 'pending')) is None
        assert not db.scalars(select(WalletLedger).where(WalletLedger.partner_id == pid,
                                                         WalletLedger.reason == 'operation_rollback')).all()
