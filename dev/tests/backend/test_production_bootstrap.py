"""Exercise release startup against disposable databases and loopback workers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import asyncio
import os
import sqlite3
import subprocess
import sys
from threading import Barrier
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import make_engine
from app.models import AdminUser, Partner
from app.services import bootstrap
from local_http import BACKEND, local_server
from test_stage6_http import ADMIN, PASSWORD, api_env, login


@pytest.fixture
def release_db(tmp_path, monkeypatch):
    env = api_env(tmp_path, "http://127.0.0.1:1")
    env.update(ENVIRONMENT="production", CORS_ORIGINS="https://portal.example.test")
    for key, value in env.items():
        monkeypatch.setattr(settings, key.lower(), value)
    engine = make_engine(env["DATABASE_URL"])
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(bootstrap, "engine", engine)
    monkeypatch.setattr(bootstrap, "SessionLocal", sessions)
    try:
        yield env, sessions
    finally:
        engine.dispose()


def test_two_bootstrap_invocations_create_only_one_admin(release_db, monkeypatch):
    _, sessions = release_db
    start = Barrier(2)

    def prepare():
        start.wait(timeout=10)
        bootstrap.initialize_database()

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(prepare) for _ in range(2)]
        for job in jobs:
            job.result(timeout=30)

    with sessions() as db:
        original = db.scalars(select(AdminUser)).one()
        original_id, original_hash = original.id, original.password_hash
    monkeypatch.setattr(settings, "admin_password", "synthetic-different-bootstrap-password")
    bootstrap.initialize_database()
    with sessions() as db:
        saved = db.scalars(select(AdminUser)).one()
        assert (saved.id, saved.username, saved.password_hash) == (original_id, ADMIN, original_hash)
    bootstrap.require_database_ready()


def test_failed_migration_leaves_old_sqlite_revision_and_never_seeds(release_db, monkeypatch):
    _, sessions = release_db
    config = bootstrap._alembic_config()
    bootstrap.command.upgrade(config, "0004_subscription_identity")
    with sessions.begin() as db:
        db.add(Partner(username="synthetic_before_failure", password_hash="dummy", balance_toman=700))

    original_upgrade = bootstrap.command.upgrade
    def broken(config, revision):
        original_upgrade(config, revision)
        raise RuntimeError("synthetic failure before completing the maintenance transaction")
    monkeypatch.setattr(bootstrap.command, "upgrade", broken)
    monkeypatch.setattr(bootstrap, "seed_admin", lambda *_: pytest.fail("failed migration must not seed"))
    with pytest.raises(RuntimeError, match="synthetic failure"):
        bootstrap.initialize_database()

    with bootstrap.engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0004_subscription_identity"
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM admin_users").scalar_one() == 0
        assert not inspect(conn).has_table("revoked_tokens")
    with sessions() as db:
        assert db.scalar(select(Partner.balance_toman)) == 700
    with pytest.raises(RuntimeError, match="revision"):
        bootstrap.require_database_ready()


def test_production_workers_refuse_unprepared_database_then_serve_concurrently(release_db, tmp_path):
    env, sessions = release_db
    with pytest.raises(RuntimeError, match="exited before becoming ready"):
        with local_server(tmp_path, "app.main:app", env=env):
            pytest.fail("unprepared worker must not listen")
    with closing(sqlite3.connect(tmp_path / "isolated.db")) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == []

    # Use the real module entry point to prepare the same disposable database.
    child_env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")
                 if key in os.environ}
    result = subprocess.run([sys.executable, "-B", "-m", "app.bootstrap_database"],
                            cwd=tmp_path, env={**child_env, **env, "PYTHONPATH": str(BACKEND)},
                            capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stderr

    listening = Barrier(2)
    def serve():
        with local_server(tmp_path, "app.main:app", env=env) as (url, _):
            listening.wait(timeout=20)
            with httpx.Client(base_url=url, trust_env=False, timeout=10) as client:
                token = login(client, "admin", ADMIN)
                return client.get("/api/admin/me", headers=token).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(serve) for _ in range(2)]
        assert [job.result(timeout=45) for job in jobs] == [200, 200]
    with sessions() as db:
        assert db.scalars(select(AdminUser.username)).all() == [ADMIN]


def test_worker_rejects_current_schema_without_an_admin_before_recovery(release_db, monkeypatch):
    from app import main
    bootstrap.upgrade_database()
    with pytest.raises(RuntimeError, match="No administrator"):
        bootstrap.require_database_ready()

    def unexpected():
        pytest.fail("production started a write before readiness")
    for name in ("recover_pending_operations", "seed_admin", "upgrade_database"):
        monkeypatch.setattr(main, name, unexpected)

    async def start():
        async with main.lifespan(main.app):
            pytest.fail("unprepared worker must not yield")

    with pytest.raises(RuntimeError, match="No administrator"):
        asyncio.run(start())


@pytest.mark.parametrize("value", [0, None, 1])
def test_mysql_named_lock_guards_migration_and_is_released_on_failure(release_db, monkeypatch, value):
    connection = MagicMock()
    connection.dialect.name = "mysql"
    connection.engine.url.database = "synthetic_schema"
    connection.invalidated = False
    connection.scalar.side_effect = [value, 1]
    fake_engine = MagicMock()
    fake_engine.connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(bootstrap, "engine", fake_engine)
    migrate = Mock(side_effect=RuntimeError("synthetic migration error"))
    seed = Mock()
    monkeypatch.setattr(bootstrap, "upgrade_database", migrate)
    monkeypatch.setattr(bootstrap, "seed_admin", seed)
    with pytest.raises(RuntimeError, match="synthetic migration error" if value == 1 else "lock is unavailable"):
        bootstrap.initialize_database()
    if value == 1:
        migrate.assert_called_once_with(connection)
        acquire, release = connection.scalar.call_args_list
        assert "GET_LOCK" in str(acquire.args[0])
        assert "RELEASE_LOCK" in str(release.args[0])
        assert acquire.args[1] == release.args[1]
    else:
        migrate.assert_not_called()
        assert connection.scalar.call_count == 1
    seed.assert_not_called()


def test_mysql_worker_rejects_active_maintenance_before_checking_revision(release_db, monkeypatch):
    connection = MagicMock()
    connection.dialect.name = "mysql"
    connection.engine.url.database = "synthetic_schema"
    connection.scalar.return_value = 0
    session_factory = MagicMock()
    session = session_factory.return_value.__enter__.return_value
    session.connection.return_value = connection
    monkeypatch.setattr(bootstrap, "SessionLocal", session_factory)
    read_revision = Mock()
    monkeypatch.setattr(bootstrap.MigrationContext, "configure", read_revision)
    with pytest.raises(RuntimeError, match="maintenance is active"):
        bootstrap.require_database_ready()
    assert "IS_FREE_LOCK" in str(connection.scalar.call_args.args[0])
    read_revision.assert_not_called()
