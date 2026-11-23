"""Opt-in ONLY: release validation on an owner-approved local MySQL 8.4 test server.

Uses the same two guards as test_mysql_integration. PARTNERPANEL_MYSQL_TEST_URL
names an empty approved base schema; each test creates its own child schema
``<base>_<n>`` with CREATE DATABASE (the test account must be limited to the
partnerpanel_test_ prefix). Schemas are never dropped or cleared.

The financial, concurrency and recovery suites of test_billing, test_stage35 and
test_stage6_http are replayed here against MySQL; panels are simulated locally.
"""
import inspect as pyinspect
import itertools
import os
import subprocess
import sys
import threading
import time
from datetime import date
from decimal import Decimal

import httpx
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import event, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

import test_billing
import test_stage35
import test_stage6_http
from app.core.config import settings
from app.core.database import Base, make_engine
from app.models import AdminUser, PanelOperation, Partner, PartnerProductCredit, Product, WalletLedger
from app.services import bootstrap, money_sync
from app.services.money_sync import reconcile_pending
from app.services.subscriptions import (
    Actor,
    admin_adjust,
    create_subscription,
    decrease_subscription,
    delete_subscription,
    increase_subscription,
    lock_partner_row,
    reset_traffic,
)
from local_http import BACKEND, local_server
from test_billing import NOW, PanelSim, gateway, jalali, seed, sim  # noqa: F401  (sim is a fixture)
from test_mysql_compatibility import migration_config
from test_mysql_integration import isolated_mysql_url
from test_stage35 import setup  # noqa: F401  (fixture)
from test_stage6_http import ADMIN, PASSWORD, login

HEAD = "0005_security_state"
_serial = itertools.count(1)


@pytest.fixture(scope="module")
def mysql_base():
    raw = os.environ.get("PARTNERPANEL_MYSQL_TEST_URL")
    if not raw:
        pytest.skip("No owner-approved MySQL test environment provided")
    if os.environ.get("PARTNERPANEL_MYSQL_TEST_ALLOW_SCHEMA_WRITES") != "isolated-schema-approved":
        pytest.fail("Explicit approval of writes to an empty test schema is required", pytrace=False)
    try:
        url = isolated_mysql_url(raw)
    except ValueError as exc:
        pytest.fail(str(exc), pytrace=False)
    pytest.importorskip("pymysql", reason="PyMySQL has not been installed with owner approval")
    engine = make_engine(raw)
    try:
        with engine.connect() as conn:
            version = conn.scalar(text("SELECT VERSION()"))
            assert version.startswith("8.4.") and "mariadb" not in version.lower()
            assert inspect(conn).get_table_names() == [], "Base schema must be empty"
            children = conn.scalar(text("SELECT COUNT(*) FROM information_schema.schemata "
                                        "WHERE schema_name LIKE :prefix"),
                                   {"prefix": url.database.replace("_", r"\_") + r"\_%"})
            assert children == 0, "Child schemas of this base already exist; use a new base schema"
        yield url, engine
    finally:
        engine.dispose()


@pytest.fixture
def new_schema(mysql_base):
    """Return a factory creating a new child schema, migrated to head unless asked otherwise."""
    base, admin = mysql_base

    def create(*, revision=HEAD):
        name = f"{base.database}_{next(_serial):03d}"
        rendered = base.set(database=name).render_as_string(hide_password=False)
        isolated_mysql_url(rendered)
        with admin.connect() as conn:
            conn.exec_driver_sql(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin")
        if revision is not None:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(settings, "database_url", rendered)
                command.upgrade(migration_config(), revision)
        return rendered

    return create


@pytest.fixture
def engines():
    made = []

    def open_engine(url):
        engine = make_engine(url)
        made.append(engine)
        return engine

    yield open_engine
    for engine in made:
        engine.dispose()


@pytest.fixture(autouse=True)
def replay_on_mysql(new_schema, engines, monkeypatch):
    """Point the SQLite engines created by the replayed suites at fresh MySQL schemas."""
    mapped = {}

    def mysql_for(sqlite_url):
        if sqlite_url not in mapped:
            mapped[sqlite_url] = engines(new_schema())
        return mapped[sqlite_url]

    def probe_write(path):
        # MySQL counterpart of the SQLite probe: no row lock may be held while a
        # panel call is in progress, so a second connection updates at once.
        engine = mysql_for(f"sqlite:///{path.as_posix()}")
        with engine.connect() as conn:
            conn.exec_driver_sql("SET SESSION innodb_lock_wait_timeout = 1")
            for table in ("partners", "partner_product_credits", "subscriptions", "panel_operations"):
                conn.exec_driver_sql(f"UPDATE {table} SET id = id")
            conn.rollback()

    def api_env(tmp_path, lab_url):
        env = original_api_env(tmp_path, lab_url)
        env["DATABASE_URL"] = new_schema(revision=None)
        return env

    original_api_env = test_stage6_http.api_env
    monkeypatch.setattr(test_billing, "make_engine", mysql_for)
    monkeypatch.setattr(test_stage35, "make_engine", mysql_for)
    monkeypatch.setattr(test_stage35, "probe_write", probe_write)
    monkeypatch.setattr(test_stage6_http, "api_env", api_env)
    return mysql_for


@pytest.fixture
def db(replay_on_mysql, request):
    engine = replay_on_mysql(f"sqlite:///in-memory/{request.node.name}")
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def _replayed(module, excluded=()):
    for name, function in vars(module).items():
        if name.startswith("test_") and name not in excluded:
            params = pyinspect.signature(function).parameters
            if {"db", "tmp_path", "setup", "lab_url"} & set(params):
                yield name, function


# The child script of this test hard-codes SQLite; a MySQL multi-process
# equivalent is defined below.
globals().update(_replayed(test_billing, excluded={"test_two_processes_cannot_lose_a_balance_update"}))
globals().update(_replayed(test_stage35))
globals().update(_replayed(test_stage6_http))


# ---------------------------------------------------------------- bootstrap

def _release_env(url, admin_password=PASSWORD):
    return {
        "DATABASE_URL": url,
        "SECRET_KEY": "stage6-synthetic-test-signing-key-2874",
        "ADMIN_USERNAME": ADMIN,
        "ADMIN_PASSWORD": admin_password,
        "PANEL_PRIVATE_ORIGINS": "http://127.0.0.1:1",
        "ENVIRONMENT": "production",
        "CORS_ORIGINS": "https://portal.example.test",
    }


def _child_env(env):
    base = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP") if key in os.environ}
    return {**base, **env, "PYTHONPATH": str(BACKEND), "PYTHONDONTWRITEBYTECODE": "1"}


def _bootstrap_process(tmp_path, env):
    return subprocess.Popen([sys.executable, "-B", "-m", "app.bootstrap_database"], cwd=tmp_path,
                            env=_child_env(env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _run_bootstrap(tmp_path, env):
    process = _bootstrap_process(tmp_path, env)
    out, err = process.communicate(timeout=60)
    return process.returncode, out + err


@pytest.fixture
def production_settings(monkeypatch):
    def apply(url):
        for key, value in _release_env(url).items():
            monkeypatch.setattr(settings, key.lower(), value)
        engine = make_engine(url)
        monkeypatch.setattr(bootstrap, "engine", engine)
        monkeypatch.setattr(bootstrap, "SessionLocal", sessionmaker(bind=engine))
        return engine

    return apply


def _schema_report(engine, schema):
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        engines_used = set(conn.execute(text(
            "SELECT DISTINCT engine FROM information_schema.tables WHERE table_schema = :s"), {"s": schema}).scalars())
        collations = set(conn.execute(text(
            "SELECT DISTINCT table_collation FROM information_schema.tables WHERE table_schema = :s"),
            {"s": schema}).scalars())
        revision = conn.scalar(text("SELECT version_num FROM alembic_version")) if "alembic_version" in tables else None
        diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    return tables, engines_used, collations, revision, diffs


EXPECTED_INDEX_DRIFT = {
    # compare_metadata ignores ddl_if: these partial indexes are SQLite/PostgreSQL
    # only, and their *_mysql functional counterparts exist instead.
    ("add_index", "panel_operations", "uq_panel_op_pending_create"),
    ("add_index", "panel_operations", "uq_panel_op_pending_sub"),
    # 0001 created both UniqueConstraint("username") and a unique ix_*_username.
    ("remove_index", "admin_users", "username"),
    ("remove_index", "partners", "username"),
}


def _unexplained_drift(diffs):
    indexes = {(d[0], d[1].table.name, d[1].name) for d in diffs if d[0] in ("add_index", "remove_index")}
    others = [d for d in diffs if d[0] not in ("add_index", "remove_index")]
    return others + sorted(indexes ^ EXPECTED_INDEX_DRIFT)


def test_mysql_bootstrap_on_empty_schema_rerun_and_admin_password_kept(new_schema, engines, tmp_path):
    url = new_schema(revision=None)
    schema = url.rsplit("/", 1)[1].split("?", 1)[0]
    code, output = _run_bootstrap(tmp_path, _release_env(url))
    assert code == 0, output
    engine = engines(url)
    tables, engines_used, collations, revision, diffs = _schema_report(engine, schema)
    assert tables == set(Base.metadata.tables) | {"alembic_version"}
    assert engines_used == {"InnoDB"} and collations == {"utf8mb4_0900_bin"}
    assert revision == HEAD
    assert _unexplained_drift(diffs) == [], diffs
    with sessionmaker(bind=engine)() as db:
        admin = db.scalars(select(AdminUser)).one()
        original = (admin.id, admin.username, admin.password_hash)

    code, output = _run_bootstrap(tmp_path, _release_env(url, admin_password="synthetic-different-password-91"))
    assert code == 0, output
    with sessionmaker(bind=engine)() as db:
        admin = db.scalars(select(AdminUser)).one()
        assert (admin.id, admin.username, admin.password_hash) == original
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == HEAD


def test_mysql_concurrent_bootstrap_processes_create_one_admin(new_schema, engines, tmp_path):
    url = new_schema(revision=None)
    processes = [_bootstrap_process(tmp_path, _release_env(url)) for _ in range(4)]
    results = []
    for process in processes:
        out, err = process.communicate(timeout=90)
        results.append((process.returncode, out + err))
    assert any(code == 0 for code, _ in results), results
    for code, output in results:
        # A peer holding the maintenance lock makes the others stop without touching the schema.
        assert code == 0 or "Database bootstrap failed (RuntimeError)" in output, output
        assert "mysql+pymysql" not in output and "partnerpanel_tester:" not in output
    engine = engines(url)
    with sessionmaker(bind=engine)() as db:
        assert db.scalars(select(AdminUser.username)).all() == [ADMIN]
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
    code, output = _run_bootstrap(tmp_path, _release_env(url))
    assert code == 0, output


def test_mysql_workers_refuse_unprepared_schema_then_serve_concurrently(new_schema, engines, tmp_path):
    url = new_schema(revision=None)
    env = _release_env(url)
    with pytest.raises(RuntimeError, match="exited before becoming ready"):
        with local_server(tmp_path, "app.main:app", env=env):
            pytest.fail("unprepared worker must not listen")
    engine = engines(url)
    with engine.connect() as conn:
        assert inspect(conn).get_table_names() == []

    code, output = _run_bootstrap(tmp_path, env)
    assert code == 0, output
    listening = threading.Barrier(2)

    def serve():
        with local_server(tmp_path, "app.main:app", env=env) as (address, _):
            listening.wait(timeout=30)
            with httpx.Client(base_url=address, trust_env=False, timeout=10) as client:
                token = login(client, "admin", ADMIN)
                return client.get("/api/admin/me", headers=token).status_code

    results = []
    threads = [threading.Thread(target=lambda: results.append(serve())) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert results == [200, 200]
    with sessionmaker(bind=engine)() as db:
        assert db.scalars(select(AdminUser.username)).all() == [ADMIN]


def test_mysql_worker_rejects_partial_schema_and_active_maintenance(new_schema, engines, production_settings, tmp_path):
    url = new_schema(revision="0004_subscription_identity")
    production_settings(url)
    with pytest.raises(RuntimeError, match="revision"):
        bootstrap.require_database_ready()

    code, output = _run_bootstrap(tmp_path, _release_env(url))
    assert code == 0, output
    bootstrap.require_database_ready()

    holder = engines(url)
    with holder.connect() as conn:
        lock_name = bootstrap._mysql_bootstrap_lock(conn)
        assert conn.scalar(text("SELECT GET_LOCK(:n, 0)"), {"n": lock_name}) == 1
        try:
            with pytest.raises(RuntimeError, match="maintenance is active"):
                bootstrap.require_database_ready()
            code, output = _run_bootstrap(tmp_path, _release_env(url))
            assert code == 1 and "Database bootstrap failed (RuntimeError)" in output
            with pytest.raises(RuntimeError, match="exited before becoming ready"):
                with local_server(tmp_path, "app.main:app", env=_release_env(url)):
                    pytest.fail("worker must not start during maintenance")
        finally:
            assert conn.scalar(text("SELECT RELEASE_LOCK(:n)"), {"n": lock_name}) == 1
    bootstrap.require_database_ready()


def test_mysql_failed_ddl_midway_keeps_old_revision_and_blocks_workers(new_schema, engines, production_settings, tmp_path):
    url = new_schema(revision="0004_subscription_identity")
    schema = url.rsplit("/", 1)[1].split("?", 1)[0]
    engine = engines(url)
    with sessionmaker(bind=engine)() as db:
        partner, _ = seed(db, balance=700)
        partner.username = "synthetic_before_failure"
        db.commit()
    with engine.begin() as conn:
        # 0005 creates login_throttles first; this conflicting table makes its
        # second CREATE TABLE fail after MySQL has implicitly committed the first.
        conn.exec_driver_sql("CREATE TABLE revoked_tokens (conflict INT) ENGINE=InnoDB")

    code, output = _run_bootstrap(tmp_path, _release_env(url))
    assert code == 1 and "Database bootstrap failed" in output, output
    assert "mysql+pymysql" not in output
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0004_subscription_identity"
        tables = set(inspect(conn).get_table_names())
        # Non-transactional DDL: the first table of the failed revision remains.
        assert "login_throttles" in tables
        assert conn.scalar(text("SELECT COUNT(*) FROM admin_users")) == 0
        assert conn.scalar(text("SELECT balance_toman FROM partners")) == 700
        assert conn.scalar(text(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = :s AND engine <> 'InnoDB'"),
            {"s": schema}) == 0
    production_settings(url)
    with pytest.raises(RuntimeError, match="revision"):
        bootstrap.require_database_ready()
    # Repeating the command on the partial schema fails again rather than stamping a guess.
    code, output = _run_bootstrap(tmp_path, _release_env(url))
    assert code == 1
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0004_subscription_identity"


# ---------------------------------------------------------- locks and races

_CHILD = """
import os, sys
sys.path.insert(0, sys.argv[1])
from sqlalchemy.orm import sessionmaker
from app.core.database import make_engine
from app.core.errors import AppError
from app.services.subscriptions import Actor, admin_adjust
engine = make_engine(os.environ["PARTNERPANEL_CHILD_DB_URL"])
db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
try:
    admin_adjust(db, partner_id=int(sys.argv[2]), asset="toman", amount=int(sys.argv[3]),
                 product_id=None, actor=Actor("admin", 1))
    db.commit()
except AppError:
    db.rollback()
    sys.exit(3)
finally:
    db.close()
    engine.dispose()
"""


def _adjust_in_processes(url, partner_id, amount, count):
    env = {**os.environ, "PARTNERPANEL_CHILD_DB_URL": url}
    procs = [subprocess.Popen([sys.executable, "-B", "-c", _CHILD, str(BACKEND), str(partner_id), str(amount)],
                              env=env) for _ in range(count)]
    return [proc.wait(60) for proc in procs]


def _toman_ledgers(db, partner_id, reason):
    return db.scalars(select(WalletLedger).where(WalletLedger.partner_id == partner_id,
                                                 WalletLedger.asset == "toman",
                                                 WalletLedger.reason == reason)).all()


def test_mysql_processes_neither_lose_updates_nor_overdraw(new_schema, engines):
    url = new_schema()
    engine = engines(url)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        partner, _ = seed(db, balance=0)
        pid = partner.id
    assert _adjust_in_processes(url, pid, 1000, 8) == [0] * 8
    with sessions() as db:
        assert db.get(Partner, pid).balance_toman == 8000
        assert len(_toman_ledgers(db, pid, "admin_adjust")) == 8
        db.get(Partner, pid).balance_toman = 3000
        db.commit()
    codes = _adjust_in_processes(url, pid, -1000, 8)
    assert sorted(codes) == [0, 0, 0, 3, 3, 3, 3, 3]
    with sessions() as db:
        assert db.get(Partner, pid).balance_toman == 0
        rows = _toman_ledgers(db, pid, "admin_adjust")
        assert len(rows) == 11 and sum(Decimal(r.amount) for r in rows) == Decimal(8000 - 3000)


def test_mysql_lock_wait_timeout_moves_no_money(new_schema, engines):
    engine = engines(new_schema())
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        partner, _ = seed(db, balance=5000)
        pid = partner.id
    with sessions() as holder, sessions() as waiter:
        lock_partner_row(holder, pid, allow_banned=True)
        waiter.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
        started = time.monotonic()
        with pytest.raises(OperationalError) as caught:
            admin_adjust(waiter, partner_id=pid, asset="toman", amount=-1000, product_id=None,
                         actor=Actor("admin", 1))
        assert caught.value.orig.args[0] == 1205 and time.monotonic() - started < 5
        waiter.rollback()
        holder.rollback()
        admin_adjust(waiter, partner_id=pid, asset="toman", amount=-1000, product_id=None, actor=Actor("admin", 1))
        waiter.commit()
    with sessions() as db:
        assert db.get(Partner, pid).balance_toman == 4000
        assert len(_toman_ledgers(db, pid, "admin_adjust")) == 1


def test_mysql_deadlock_victim_is_rolled_back_whole(new_schema, engines):
    engine = engines(new_schema())
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        first, _ = seed(db, balance=1000)
        second = Partner(username="synthetic_second", password_hash="x", balance_toman=1000, banned=False)
        db.add(second)
        db.commit()
        ids = (first.id, second.id)
    barrier = threading.Barrier(2)
    outcome = {}

    def run(name, order):
        with sessions() as db:
            try:
                admin_adjust(db, partner_id=order[0], asset="toman", amount=100, product_id=None,
                             actor=Actor("admin", 1))
                db.flush()
                barrier.wait(10)
                admin_adjust(db, partner_id=order[1], asset="toman", amount=100, product_id=None,
                             actor=Actor("admin", 1))
                db.commit()
                outcome[name] = "committed"
            except OperationalError as exc:
                db.rollback()
                outcome[name] = exc.orig.args[0]

    threads = [threading.Thread(target=run, args=("a", ids)), threading.Thread(target=run, args=("b", ids[::-1]))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(map(str, outcome.values())) == ["1213", "committed"], outcome
    with sessions() as db:
        assert [db.get(Partner, pid).balance_toman for pid in ids] == [1100, 1100]
        assert len(db.scalars(select(WalletLedger).where(WalletLedger.reason == "admin_adjust")).all()) == 2


def test_mysql_killed_connection_rolls_back_and_releases_locks(new_schema, engines):
    url = new_schema()
    engine = engines(url)
    killer = engines(url)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        partner, _ = seed(db, balance=5000)
        pid = partner.id
    with sessions() as victim:
        admin_adjust(victim, partner_id=pid, asset="toman", amount=-2000, product_id=None, actor=Actor("admin", 1))
        victim.flush()
        connection_id = victim.scalar(text("SELECT CONNECTION_ID()"))
        with killer.connect() as conn:
            conn.exec_driver_sql(f"KILL {int(connection_id)}")
        with pytest.raises(OperationalError) as caught:
            victim.commit()
        assert caught.value.connection_invalidated
        victim.rollback()
    with sessions() as db:
        db.execute(text("SET SESSION innodb_lock_wait_timeout = 2"))
        assert db.get(Partner, pid).balance_toman == 5000
        assert _toman_ledgers(db, pid, "admin_adjust") == []
        admin_adjust(db, partner_id=pid, asset="toman", amount=-1000, product_id=None, actor=Actor("admin", 1))
        db.commit()
    with sessions() as db:
        assert db.get(Partner, pid).balance_toman == 4000


def test_mysql_connection_loss_after_panel_update_charges_once(new_schema, engines):
    url = new_schema()
    engine = engines(url)
    killer = engines(url)
    connection_ids = set()

    @event.listens_for(engine, "connect")
    def _remember(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("SELECT CONNECTION_ID()")
        connection_ids.add(cursor.fetchone()[0])
        cursor.close()

    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    sim = PanelSim()
    with sessions() as db, gateway(sim) as client:
        partner, product = seed(db)
        sub, _, _ = create_subscription(
            db, partner_id=partner.id, product_id=product.id, username="killed_conn", traffic_cap_gb=5,
            end_date=jalali(date(2026, 6, 25)), note=None, actor=Actor("partner", partner.id),
            gateway=client, now=NOW)
        pid, sub_id, v2_id, before = partner.id, sub.id, sub.v2_user_id, int(partner.balance_toman)
    updates_before = len(sim.updates)

    class DropDatabaseAfterUpdate:
        def __init__(self, inner):
            self.inner = inner

        def update_user(self, payload):
            result = self.inner.update_user(payload)
            with killer.connect() as conn:
                for connection_id in list(connection_ids):
                    try:
                        conn.exec_driver_sql(f"KILL {int(connection_id)}")
                    except OperationalError:
                        pass  # already closed
            return result

        def __getattr__(self, name):
            return getattr(self.inner, name)

    failure = None
    with sessions() as db, gateway(sim) as client:
        try:
            increase_subscription(db, sub_id=sub_id, partner_id=pid, traffic_cap_gb=Decimal(8), end_date=None,
                                  actor=Actor("partner", pid), gateway=DropDatabaseAfterUpdate(client), now=NOW)
        except Exception as exc:
            failure = exc
            db.rollback()
    with sessions() as db:
        op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "increase"))
        if failure is not None:
            assert op.status == "pending"
            op.lease_until = 1
            db.commit()
            assert reconcile_pending(db, lambda _panel: gateway(sim)) == 1
            db.expire_all()
            op = db.get(PanelOperation, op.id)
        assert op.status == "completed"
        assert db.get(Partner, pid).balance_toman == before - 3000
        assert len(_toman_ledgers(db, pid, "increase")) == 1
        assert _toman_ledgers(db, pid, "operation_rollback") == []
    assert len(sim.updates) == updates_before + 1
    assert sim.users[v2_id]["transfer_enable"] == test_billing.gb_to_bytes(8)


# ---------------------------------------------------------------- final status write vs. InnoDB deadlock (1213)

def _sub_on_mysql(sessions, sim, username, owner=None):
    with sessions() as db, gateway(sim) as client:
        if owner is None:
            partner, product = seed(db)
        else:
            partner, product = db.get(Partner, owner[0]), db.get(Product, owner[1])
        sub, _, _ = create_subscription(
            db, partner_id=partner.id, product_id=product.id, username=username, traffic_cap_gb=5,
            end_date=jalali(date(2026, 6, 25)), note=None, actor=Actor("partner", partner.id),
            gateway=client, now=NOW)
        return partner.id, sub.id, sub.v2_user_id, int(partner.balance_toman), product.id


def _count_deadlocks(monkeypatch) -> list:
    seen = []
    real = money_sync._is_deadlock

    def spy(exc):
        result = real(exc)
        if result:
            seen.append(exc.orig.args[0])
        return result

    monkeypatch.setattr(money_sync, "_is_deadlock", spy)
    return seen


def test_mysql_deadlocked_final_write_is_retried(new_schema, engines, monkeypatch):
    deadlocks = _count_deadlocks(monkeypatch)
    engine = engines(new_schema())
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    sim = PanelSim()
    pid, sub_id, v2_id, before, _product_id = _sub_on_mysql(sessions, sim, "deadlock_final")
    updates_before = len(sim.updates)
    panel_done, go = threading.Event(), threading.Event()

    class PauseAfterUpdate:
        def __init__(self, inner):
            self.inner = inner

        def update_user(self, payload):
            self.inner.update_user(payload)
            panel_done.set()
            assert go.wait(10)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    outcome = {}

    def worker():
        with sessions() as db, gateway(sim) as client:
            try:
                increase_subscription(db, sub_id=sub_id, partner_id=pid, traffic_cap_gb=Decimal(8), end_date=None,
                                      actor=Actor("partner", pid), gateway=PauseAfterUpdate(client), now=NOW)
                outcome["a"] = "completed"
            except Exception as exc:  # recorded for the assertion below
                outcome["a"] = repr(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    assert panel_done.wait(10)
    with sessions() as db:
        op = db.scalar(select(PanelOperation).where(PanelOperation.kind == "increase"))
        op_id, row = op.id, (op.partner_id, op.product_id, op.username, op.kind, op.payload,
                             op.created_at, op.updated_at)
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        insert = ("INSERT INTO panel_operations (partner_id, product_id, username, kind, payload, created_at, "
                  "updated_at, subscription_id, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)")
        # A heavier transaction here makes InnoDB pick the operation's write as the victim.
        for _ in range(30):
            cur.execute(insert, (*row, None, "failed"))
        with pytest.raises(Exception) as duplicate:
            cur.execute(insert, (*row, sub_id, "pending"))
        assert duplicate.value.args[0] == 1062  # leaves a shared lock on the pending index entry
        go.set()
        time.sleep(0.5)  # the final write now waits on that shared lock
        assert "a" not in outcome, outcome
        cur.execute("SELECT id FROM panel_operations WHERE id = %s FOR UPDATE", (op_id,))
        outcome["b"] = "locked"
        time.sleep(0.2)
    except Exception as exc:  # recorded for the assertion below
        outcome["b"] = repr(exc)
    finally:
        raw.rollback()
        raw.close()
        go.set()
    thread.join(30)
    assert not thread.is_alive()
    assert outcome == {"a": "completed", "b": "locked"}, outcome
    assert deadlocks == [1213]
    with sessions() as db:
        op = db.get(PanelOperation, op_id)
        assert op.status == "completed" and op.lease_token is None
        assert db.get(Partner, pid).balance_toman == before - 3000
        assert len(_toman_ledgers(db, pid, "increase")) == 1
        assert _toman_ledgers(db, pid, "operation_rollback") == []
        assert len(db.scalars(select(PanelOperation)).all()) == 2
    assert len(sim.updates) == updates_before + 1
    assert sim.users[v2_id]["transfer_enable"] == test_billing.gb_to_bytes(8)


def test_mysql_two_increases_on_one_subscription_repeatedly(new_schema, engines, monkeypatch):
    deadlocks = _count_deadlocks(monkeypatch)
    engine = engines(new_schema())
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    sim = PanelSim()
    real, panel_lock = sim.handler, threading.Lock()

    def safe(request):
        with panel_lock:
            return real(request)

    sim.handler = safe
    allowed = ("تغییری برای اعمال نیست", "در حال انجام")
    owner = None
    for round_no in range(20):
        pid, sub_id, v2_id, before, product_id = _sub_on_mysql(sessions, sim, f"race{round_no:02d}", owner)
        owner = (pid, product_id)
        barrier, errors = threading.Barrier(2), []

        def worker():
            with sessions() as db, gateway(sim) as client:
                try:
                    barrier.wait(10)
                    increase_subscription(db, sub_id=sub_id, partner_id=pid, traffic_cap_gb=Decimal(8),
                                          end_date=None, actor=Actor("partner", pid), gateway=client, now=NOW)
                except Exception as exc:  # recorded for the assertion below
                    errors.append(getattr(exc, "message", repr(exc)))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert not any(thread.is_alive() for thread in threads)
        with sessions() as db:
            statuses = list(db.scalars(select(PanelOperation.status).where(
                PanelOperation.kind == "increase", PanelOperation.subscription_id == sub_id)))
            assert statuses == ["completed"], (round_no, statuses, errors)
            assert db.get(Partner, pid).balance_toman == before - 3000, round_no
            charges = db.scalars(select(WalletLedger).where(WalletLedger.subscription_id == sub_id,
                                                            WalletLedger.reason == "increase")).all()
            assert len(charges) == 1, round_no
            assert _toman_ledgers(db, pid, "operation_rollback") == []
        assert sim.users[v2_id]["transfer_enable"] == test_billing.gb_to_bytes(8)
        assert all(any(bit in item for bit in allowed) for item in errors), (round_no, errors)
    print(f"[deadlocks retried in 20 rounds: {len(deadlocks)}]")


# ---------------------------------------------------------------- database failure after the panel applied a change

AFTER_PANEL_KINDS = ["create", "increase", "decrease", "reset", "delete"]


def _after_panel_setup(sessions, sim, kind):
    pid, sub_id, v2_id, _before, product_id = _sub_on_mysql(sessions, sim, f"after_{kind}")
    if kind == "reset":
        sim.users[v2_id]["u"] = test_billing.gb_to_bytes(2)
    return pid, sub_id, v2_id, product_id


def _after_panel_run(kind, db, client, pid, sub_id, product_id):
    actor = Actor("partner", pid)
    if kind == "create":
        return create_subscription(db, partner_id=pid, product_id=product_id, username=f"new_{kind}",
                                   traffic_cap_gb=4, end_date=jalali(date(2026, 6, 25)), note=None, actor=actor,
                                   gateway=client, now=NOW)
    if kind in {"increase", "decrease"}:
        perform = increase_subscription if kind == "increase" else decrease_subscription
        return perform(db, sub_id=sub_id, partner_id=pid, traffic_cap_gb=Decimal(8 if kind == "increase" else 3),
                       end_date=None, actor=actor, gateway=client, now=NOW)
    if kind == "reset":
        return reset_traffic(db, sub_id=sub_id, partner_id=pid, actor=actor, gateway=client)
    return delete_subscription(db, sub_id=sub_id, partner_id=pid, actor=actor, gateway=client, now=NOW)


def _after_panel_landed(sim, kind, v2_id) -> bool:
    gb = test_billing.gb_to_bytes
    if kind == "create":
        return [u["transfer_enable"] for u in sim.users.values() if u["email"].startswith("new_")] == [gb(4)]
    if kind == "reset":
        return sim.users[v2_id]["u"] == 0
    if kind == "delete":
        return v2_id not in sim.users
    return sim.users[v2_id]["transfer_enable"] == gb(8 if kind == "increase" else 3)


def _after_panel_money(sessions, pid, product_id):
    with sessions() as db:
        credit = db.scalar(select(PartnerProductCredit).where(PartnerProductCredit.partner_id == pid,
                                                              PartnerProductCredit.product_id == product_id))
        return (int(db.get(Partner, pid).balance_toman),
                (str(credit.credit_gb), int(credit.credit_days)) if credit else None,
                len(db.scalars(select(WalletLedger).where(WalletLedger.partner_id == pid,
                                                          WalletLedger.reason == "operation_rollback")).all()))


def _mutations(sim):
    return len(sim.updates), len(sim.deleted), sim.generates


def _latest_op(sessions, kind):
    name = {"reset": "reset_traffic"}.get(kind, kind)
    with sessions() as db:
        op = db.scalar(select(PanelOperation).where(PanelOperation.kind == name).order_by(PanelOperation.id.desc()))
        return op.id, op.status, op.lease_token, op.payload


class _AfterPanel:
    """Run `hook` right after the panel accepted the operation's mutating request."""

    def __init__(self, inner, kind, hook):
        self.inner, self.method, self.hook = inner, "delete_user" if kind == "delete" else "update_user", hook

    def __getattr__(self, name):
        target = getattr(self.inner, name)
        if name != self.method:
            return target

        def call(*args, **kwargs):
            result = target(*args, **kwargs)
            self.hook()
            return result

        return call


def _unrecorded(kind):
    """A reset whose success was never recorded cannot be told from an admin or plan reset."""
    return "attention" if kind == "reset" else "completed"


def _restart_and_finish(url, engines, sim, op_id, kind=None):
    """A new process: new engine and pool, expired lease, then the recovery job."""
    sessions = sessionmaker(bind=engines(url), autoflush=False, expire_on_commit=False)
    with sessions() as db:
        db.execute(text("UPDATE panel_operations SET lease_until = 1 WHERE id = :id AND lease_token IS NOT NULL"),
                   {"id": op_id})
        db.commit()
        assert reconcile_pending(db, lambda _panel: gateway(sim)) == (0 if kind == "reset" else 1)
    return sessions


def _expected_mutations(kind):
    return {"create": (1, 0, 1), "delete": (0, 1, 0)}.get(kind, (1, 0, 0))


def _assert_after_panel(sessions, sim, kind, v2_id, op_id, start, money, pid, product_id, status="completed"):
    with sessions() as db:
        assert db.get(PanelOperation, op_id).status == status
    assert _after_panel_money(sessions, pid, product_id) == money
    assert money[2] == 0
    assert tuple(a - b for a, b in zip(_mutations(sim), start)) == _expected_mutations(kind)
    assert _after_panel_landed(sim, kind, v2_id)


@pytest.mark.parametrize("unreachable", [True, False], ids=["killed_and_unreachable", "killed_only"])
@pytest.mark.parametrize("kind", AFTER_PANEL_KINDS)
def test_mysql_killed_connections_after_panel_success_refund_nothing(new_schema, engines, kind, unreachable):
    import pymysql

    url = new_schema()
    engine, killer = engines(url), engines(url)
    connection_ids, outage = set(), {"on": False}

    @event.listens_for(engine, "connect")
    def _track(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("SELECT CONNECTION_ID()")
        connection_ids.add(cursor.fetchone()[0])
        cursor.close()

    @event.listens_for(engine, "do_connect")
    def _refuse(*_args):
        if outage["on"]:
            raise pymysql.err.OperationalError(2003, "Can't connect to MySQL server (test outage)")

    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    sim = PanelSim()
    pid, sub_id, v2_id, product_id = _after_panel_setup(sessions, sim, kind)
    start = _mutations(sim)

    def kill_all():
        with killer.connect() as conn:
            for connection_id in list(connection_ids):
                try:
                    conn.exec_driver_sql(f"KILL {int(connection_id)}")
                except OperationalError:
                    pass  # already closed
        outage["on"] = unreachable

    with sessions() as db, gateway(sim) as client:
        try:
            _after_panel_run(kind, db, _AfterPanel(client, kind, kill_all), pid, sub_id, product_id)
            failure = None
        except money_sync.AppError as exc:
            failure = exc
        finally:
            outage["on"] = False
        db.rollback()
    assert _after_panel_landed(sim, kind, v2_id)
    op_id, status, lease, _payload = _latest_op(sessions, kind)
    money = _after_panel_money(sessions, pid, product_id)
    assert money[2] == 0
    if unreachable:
        assert failure is not None and failure.status_code == 503
        assert failure.message == money_sync.DB_UNAVAILABLE.format(id=op_id)
        assert status == "pending" and lease is not None
        sessions = _restart_and_finish(url, engines, sim, op_id, kind)
        settled = _unrecorded(kind)
    else:
        # pool_pre_ping replaces the killed connection before the checkpoint is written.
        assert failure is None and status == "completed"
        settled = "completed"
    _assert_after_panel(sessions, sim, kind, v2_id, op_id, start, money, pid, product_id, settled)


@pytest.mark.parametrize("kind", AFTER_PANEL_KINDS)
def test_mysql_lock_wait_on_checkpoint_after_panel_success_refunds_nothing(new_schema, engines, kind):
    url = new_schema()
    engine, blocker_engine = engines(url), engines(url)

    @event.listens_for(engine, "connect")
    def _short_wait(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("SET SESSION innodb_lock_wait_timeout = 1")
        cursor.close()

    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    sim = PanelSim()
    pid, sub_id, v2_id, product_id = _after_panel_setup(sessions, sim, kind)
    start = _mutations(sim)
    blocker = blocker_engine.raw_connection()
    errors = []
    real_remember = money_sync._remember

    def remember(db, op_id, exc):
        errors.append(getattr(getattr(exc, "orig", None), "args", (None,))[0])
        return real_remember(db, op_id, exc)

    def block_operation_row():
        # Every later write of the operation row now waits and times out (1205).
        name = {"reset": "reset_traffic"}.get(kind, kind)
        cursor = blocker.cursor()
        cursor.execute("SELECT id FROM panel_operations WHERE kind = %s AND status = 'pending' FOR UPDATE", (name,))
        assert cursor.fetchall()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(money_sync, "_remember", remember)
        try:
            with sessions() as db, gateway(sim) as client:
                with pytest.raises(money_sync.AppError) as caught:
                    _after_panel_run(kind, db, _AfterPanel(client, kind, block_operation_row), pid, sub_id,
                                     product_id)
                db.rollback()
        finally:
            blocker.rollback()
            blocker.close()
    assert caught.value.status_code == 503
    assert errors and errors[0] == 1205, errors
    assert _after_panel_landed(sim, kind, v2_id)
    op_id, status, lease, _payload = _latest_op(sessions, kind)
    assert status == "pending" and lease is not None  # not even the error could be saved
    money = _after_panel_money(sessions, pid, product_id)
    assert money[2] == 0
    fresh = _restart_and_finish(url, engines, sim, op_id, kind)
    _assert_after_panel(fresh, sim, kind, v2_id, op_id, start, money, pid, product_id, _unrecorded(kind))


@pytest.mark.parametrize("kind", AFTER_PANEL_KINDS)
def test_mysql_proxy_error_then_late_landing_refunds_nothing(new_schema, engines, kind):
    """A 504 from a proxy while the panel still runs the request: no refund, no second send."""
    url = new_schema()
    sessions = sessionmaker(bind=engines(url), autoflush=False, expire_on_commit=False)
    sim = PanelSim()
    pid, sub_id, v2_id, product_id = _after_panel_setup(sessions, sim, kind)
    start = _mutations(sim)
    path = {"create": "/user/generate", "delete": "/user/delUser"}.get(kind, "/user/update")
    held = []

    def handler(request):
        if not held and request.url.path.endswith(path):
            held.append(request)
            return httpx.Response(504, text="<html>504 Gateway Time-out</html>")
        return sim.handler(request)

    client = test_billing.V2BoardClient("http://panel.test", "secret", "admin@panel.test", "secret",
                                        transport=httpx.MockTransport(handler))
    with sessions() as db, client:
        with pytest.raises(money_sync.AppError) as caught:
            _after_panel_run(kind, db, client, pid, sub_id, product_id)
        db.rollback()
    op_id, status, _lease, _payload = _latest_op(sessions, kind)
    money = _after_panel_money(sessions, pid, product_id)
    assert money[2] == 0
    sim.handler(held[0])  # the panel finishes the request after recovery already read it
    if kind in {"reset", "increase", "decrease"}:
        # Possibly sent and not visible yet: held for the admin, nothing refunded or resent.
        assert caught.value.status_code == 409 and status == "attention"
        assert _after_panel_landed(sim, kind, v2_id)
        assert tuple(a - b for a, b in zip(_mutations(sim), start)) == _expected_mutations(kind)
        with sessions() as db:
            assert reconcile_pending(db, lambda _panel: gateway(sim)) == 0
        assert tuple(a - b for a, b in zip(_mutations(sim), start)) == _expected_mutations(kind)
        assert _after_panel_money(sessions, pid, product_id) == money
        return
    assert caught.value.status_code == 503 and status == "pending"
    fresh = _restart_and_finish(url, engines, sim, op_id)
    _assert_after_panel(fresh, sim, kind, v2_id, op_id, start, money, pid, product_id)


@pytest.mark.parametrize("kind", AFTER_PANEL_KINDS)
def test_mysql_crash_after_panel_success_recovers_once_after_restart(new_schema, engines, kind):
    url = new_schema()
    sessions = sessionmaker(bind=engines(url), autoflush=False, expire_on_commit=False)
    sim = PanelSim()
    pid, sub_id, v2_id, product_id = _after_panel_setup(sessions, sim, kind)
    start = _mutations(sim)

    def crash():
        raise SystemExit("worker killed after the panel applied the change")

    with sessions() as db, gateway(sim) as client:
        with pytest.raises(SystemExit):
            _after_panel_run(kind, db, _AfterPanel(client, kind, crash), pid, sub_id, product_id)
        db.rollback()
    assert _after_panel_landed(sim, kind, v2_id)
    op_id, status, lease, payload = _latest_op(sessions, kind)
    assert status == "pending" and lease is not None
    assert '"panel_applied": true' not in payload
    money = _after_panel_money(sessions, pid, product_id)
    with sessions() as db:
        # The lease is still live: a recovery run must not touch the row yet.
        assert reconcile_pending(db, lambda _panel: gateway(sim)) == 0
    assert tuple(a - b for a, b in zip(_mutations(sim), start)) == _expected_mutations(kind)
    fresh = _restart_and_finish(url, engines, sim, op_id, kind)
    _assert_after_panel(fresh, sim, kind, v2_id, op_id, start, money, pid, product_id, _unrecorded(kind))
