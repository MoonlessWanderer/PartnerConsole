"""Resource cleanup and complete MySQL DDL inspection without any connection."""
from contextlib import contextmanager
from io import StringIO
import runpy
from types import SimpleNamespace

import alembic
from alembic import command
import pytest
from sqlalchemy import create_mock_engine

from local_http import BACKEND

from app.core import database
from app.core.config import settings
from app.core.database import Base
from test_mysql_compatibility import migration_config


@pytest.mark.parametrize('failure', ['connect', 'configure', 'migrate', None])
def test_migration_engine_is_disposed_even_on_failure(monkeypatch, failure):
    events = []
    class SyntheticFailure(RuntimeError):
        pass
    def checkpoint(name):
        events.append(name)
        if failure == name:
            raise SyntheticFailure(name)
    @contextmanager
    def transaction():
        yield
    @contextmanager
    def connection():
        checkpoint('connect')
        try:
            yield object()
        finally:
            events.append('connection_closed')
    engine = SimpleNamespace(connect=connection, dispose=lambda: events.append('disposed'))
    context = SimpleNamespace(
        config=SimpleNamespace(config_file_name=None, attributes={}),
        is_offline_mode=lambda: False,
        configure=lambda **kw: checkpoint('configure'),
        begin_transaction=transaction,
        run_migrations=lambda: checkpoint('migrate'),
    )
    monkeypatch.setattr(database, 'make_engine', lambda url: engine)
    monkeypatch.setattr(alembic, 'context', context)
    path = BACKEND / 'alembic' / 'env.py'
    if failure:
        with pytest.raises(SyntheticFailure, match=failure):
            runpy.run_path(str(path))
    else:
        runpy.run_path(str(path))
    assert events[-1] == 'disposed'
    assert events.count('disposed') == 1
    if failure != 'connect':
        assert events[-2] == 'connection_closed'


def mysql_script(monkeypatch, revision, downgrade=False):
    output = StringIO()
    monkeypatch.setattr(settings, 'database_url', 'mysql+pymysql://synthetic@127.0.0.1/partnerpanel_test_offline')
    if downgrade:
        command.downgrade(migration_config(output), revision, sql=True)
    else:
        command.upgrade(migration_config(output), revision, sql=True)
    return output.getvalue()


def test_mysql_migration_indexes_match_model_indexes(monkeypatch):
    migrated = mysql_script(monkeypatch, 'head')
    model_sql = []
    engine = create_mock_engine('mysql://', lambda stmt, *a, **kw: model_sql.append(
        str(stmt.compile(dialect=engine.dialect))))
    Base.metadata.create_all(engine)
    for statement in model_sql:
        if 'CREATE UNIQUE INDEX uq_panel_op_pending' in statement:
            assert statement.strip() + ';' in migrated
    assert 'ON DELETE SET NULL' in migrated
    assert 'UPDATE subscriptions' in migrated and 'SELECT products.email_domain' in migrated
    assert 'price_per_gb BIGINT' in migrated and 'price_per_day BIGINT' in migrated
    assert 'payload LONGTEXT' in migrated


def test_mysql_full_downgrade_compiles_with_matching_index_names(monkeypatch):
    sql = mysql_script(monkeypatch, 'head:base', downgrade=True)
    assert 'DROP INDEX uq_panel_op_pending_create_mysql ON panel_operations' in sql
    assert 'DROP INDEX uq_panel_op_pending_sub_mysql ON panel_operations' in sql
    assert 'DROP TABLE login_throttles' in sql and 'DROP TABLE revoked_tokens' in sql
    assert sql.index('DROP TABLE panel_operations') < sql.index('DROP TABLE subscriptions')


def test_mysql_revision_four_to_head_only_adds_security_state(monkeypatch):
    sql = mysql_script(monkeypatch, '0004_subscription_identity:head')
    assert 'CREATE TABLE login_throttles' in sql and 'CREATE TABLE revoked_tokens' in sql
    assert 'COLLATE utf8mb4_0900_bin' in sql or 'COLLATE=utf8mb4_0900_bin' in sql
    assert 'DROP TABLE' not in sql and 'DELETE FROM' not in sql and 'ALTER TABLE subscriptions' not in sql
