"""No server required: SQL generation and unchanged SQLite semantics."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_mock_engine, select
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from local_http import BACKEND

from app.core.config import settings
from app.core.database import Base, make_engine
from app.core.db_types import MySQLUTCDateTime
from app.core.security import _reserve_bucket, create_token, revoke_token
from app.models import LoginThrottle, PanelOperation, Partner, PartnerProductCredit, Subscription


class SQLRecorder:
    def __init__(self, rowcount=1):
        self.sql = []
        self.rowcount = rowcount
        self.commits = 0

    def get_bind(self):
        return SimpleNamespace(dialect=mysql.dialect())

    def execute(self, stmt):
        self.sql.append(str(stmt.compile(dialect=mysql.dialect())))
        return SimpleNamespace(rowcount=self.rowcount)

    def commit(self):
        self.commits += 1


@pytest.mark.parametrize('rowcount,allowed', [(0, False), (1, True)])
def test_mysql_quota_is_conditional_and_has_no_returning(rowcount, allowed):
    db = SQLRecorder(rowcount)
    assert _reserve_bucket(db, 'synthetic', 8, 600, 1000) is allowed
    assert len(db.sql) == 2
    assert 'ON DUPLICATE KEY UPDATE' in db.sql[0]
    assert 'IGNORE' not in db.sql[0]
    assert 'WHERE' in db.sql[1] and 'attempts <' in db.sql[1]
    assert 'RETURNING' not in ' '.join(db.sql)
    # Ownership of the checkpoint transaction stays with the caller.
    assert db.commits == 0


def test_mysql_quota_does_not_mask_database_failure():
    class Broken(SQLRecorder):
        def execute(self, stmt):
            raise RuntimeError('synthetic database unavailable')
    with pytest.raises(RuntimeError):
        _reserve_bucket(Broken(), 'synthetic', 8, 600, 1000)


def test_mysql_requires_explicit_supported_driver_before_connecting():
    with pytest.raises(ValueError, match='mysql.pymysql'):
        make_engine('mysql://synthetic@127.0.0.1/partnerpanel_test_unused')


def test_mysql_revocation_uses_noop_upsert(monkeypatch):
    monkeypatch.setattr(settings, 'secret_key', 'synthetic-test-signing-key-with-enough-entropy')
    db = SQLRecorder()
    revoke_token(create_token('partner', 12, 'synthetic'), 'partner', db)
    assert 'ON DUPLICATE KEY UPDATE' in db.sql[0]
    assert 'IGNORE' not in db.sql[0] and 'RETURNING' not in db.sql[0]
    assert db.commits == 1


def test_metadata_creates_only_the_correct_pending_indexes_per_engine():
    for dialect in ('mysql', 'sqlite'):
        statements = []
        engine = create_mock_engine(f'{dialect}://', lambda stmt, *a, **kw: statements.append(
            str(stmt.compile(dialect=engine.dialect))))
        Base.metadata.create_all(engine)
        unique = [s for s in statements if 'CREATE UNIQUE INDEX uq_panel_op_pending' in s]
        assert len(unique) == 2
        if dialect == 'mysql':
            assert all('_mysql' in s and 'CASE WHEN' in s and 'ELSE NULL' in s for s in unique)
            tables = [s for s in statements if 'CREATE TABLE' in s]
            assert all('InnoDB' in s and 'utf8mb4_0900_bin' in s for s in tables)
        else:
            assert all('_mysql' not in s and 'WHERE status' in s for s in unique)


def test_mysql_storage_precision_and_financial_types():
    sub = str(CreateTable(Subscription.__table__).compile(dialect=mysql.dialect()))
    partner = str(CreateTable(Partner.__table__).compile(dialect=mysql.dialect()))
    op = str(CreateTable(PanelOperation.__table__).compile(dialect=mysql.dialect()))
    assert 'DATETIME(6)' in sub and 'NUMERIC(18, 6)' in sub
    assert 'balance_toman BIGINT' in partner and 'payload LONGTEXT' in op
    sqlite_partner = str(CreateTable(Partner.__table__).compile(dialect=sqlite.dialect()))
    assert 'balance_toman INTEGER' in sqlite_partner


def test_mysql_datetime_normalizes_utc_and_preserves_microseconds():
    column_type = MySQLUTCDateTime()
    original = datetime(2026, 11, 20, 14, 15, 16, 123456,
                        tzinfo=timezone(timedelta(hours=3, minutes=30)))
    bound = column_type.process_bind_param(original, mysql.dialect())
    assert bound.tzinfo is None and bound.microsecond == 123456
    assert column_type.process_result_value(bound, mysql.dialect()) == original
    assert column_type.process_bind_param(None, mysql.dialect()) is None
    assert column_type.process_result_value(None, mysql.dialect()) is None


def test_mysql_subscription_lock_follows_parent_and_does_not_join(monkeypatch):
    from app.services import subscriptions as subs, partner_ban
    events = []
    monkeypatch.setattr(subs, 'lock_partner_row', lambda db, pid, **kw: events.append(('parent', pid, kw)))
    monkeypatch.setattr(partner_ban, 'ensure_no_partner_ban', lambda *args: None)
    class DB(SQLRecorder):
        def scalar(self, stmt):
            return 19
        def execute(self, stmt):
            events.append(('subscription', str(stmt.compile(dialect=mysql.dialect()))))
            return SimpleNamespace(unique=lambda: SimpleNamespace(scalar_one_or_none=lambda: object()))
    subs.lock_subscription(DB(), 7, 19)
    assert events[0] == ('parent', 19, {'allow_banned': False})
    assert 'FOR UPDATE' in events[1][1] and 'JOIN' not in events[1][1]
    assert 'partner_id =' in events[1][1]


def test_mysql_parent_and_credit_queries_take_row_locks(monkeypatch):
    from app.services import subscriptions as subs
    recorded = []
    class DB(SQLRecorder):
        def scalar(self, stmt):
            recorded.append(str(stmt.compile(dialect=mysql.dialect())))
            return SimpleNamespace(banned=False)
    subs.lock_partner_row(DB(), 1, allow_banned=True)
    subs.lock_credit(DB(), 1, 2)
    assert all('FOR UPDATE' in s for s in recorded)


def migration_config(buffer=None):
    cfg = Config(str(BACKEND / 'alembic.ini'), output_buffer=buffer)
    cfg.set_main_option('script_location', str(BACKEND / 'alembic'))
    return cfg


def test_full_mysql_migration_chain_compiles_offline(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(settings, 'database_url', 'mysql+pymysql://synthetic@127.0.0.1/partnerpanel_test_offline')
    command.upgrade(migration_config(output), 'head', sql=True)
    sql = output.getvalue()
    assert '0005_security_state' in sql
    assert 'CREATE TABLE login_throttles' in sql and 'CREATE TABLE revoked_tokens' in sql
    assert 'uq_panel_op_pending_create_mysql' in sql and 'CASE WHEN' in sql
    assert 'CREATE UNIQUE INDEX uq_panel_op_pending_create ON' not in sql
    assert 'DATETIME(6)' in sql and 'balance_toman BIGINT' in sql
    assert sql.count('ENGINE=InnoDB') == len(Base.metadata.tables)


def test_full_sqlite_chain_matches_metadata_and_preserves_decimal_and_persian(tmp_path, monkeypatch):
    from sqlalchemy import inspect, text
    from test_billing import seed
    url = f'sqlite:///{(tmp_path / "fresh.db").as_posix()}'
    monkeypatch.setattr(settings, 'database_url', url)
    command.upgrade(migration_config(), 'head')
    command.upgrade(migration_config(), 'head')
    engine = make_engine(url)
    try:
        assert set(inspect(engine).get_table_names()) == set(Base.metadata.tables) | {'alembic_version'}
        with sessionmaker(bind=engine)() as db:
            partner, product = seed(db, balance=3_000_000_000, credit_gb=Decimal("1.234567"))
            product.name = 'آزمایش فارسی\u200c 🙂'
            db.commit()
            assert db.get(Partner, partner.id).balance_toman == 3_000_000_000
            db.expire_all()
            assert product.name == 'آزمایش فارسی\u200c 🙂'
            assert db.scalar(text('SELECT version_num FROM alembic_version')) == '0005_security_state'
            assert product.price_per_gb == 1000
            assert db.scalar(select(PartnerProductCredit.credit_gb)) == Decimal("1.234567")
    finally:
        engine.dispose()


def test_sqlite_quota_limit_expiration_and_rollback(tmp_path):
    engine = make_engine(f'sqlite:///{(tmp_path / "quota.db").as_posix()}')
    Base.metadata.create_all(engine)
    try:
        sessions = sessionmaker(bind=engine)
        with sessions() as db:
            assert _reserve_bucket(db, 'synthetic', 2, 60, 100)
            db.rollback()
            assert db.get(LoginThrottle, 'synthetic') is None
            assert _reserve_bucket(db, 'synthetic', 2, 60, 100)
            assert _reserve_bucket(db, 'synthetic', 2, 60, 100)
            assert not _reserve_bucket(db, 'synthetic', 2, 60, 100)
            assert _reserve_bucket(db, 'synthetic', 2, 60, 160)
            db.commit()
            assert db.get(LoginThrottle, 'synthetic').attempts == 1
    finally:
        engine.dispose()
