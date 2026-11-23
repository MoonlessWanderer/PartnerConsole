"""Release gates verified with synthetic settings and temporary databases."""
from contextlib import closing
import sqlite3

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import make_engine
from app.core.errors import AppError
from app.models import PanelOperation, Partner, RevokedToken, Subscription, WalletLedger
from app.services import bootstrap, subscriptions
from test_billing import NOW, gateway
from test_stage35 import setup


def test_bootstrap_accepts_percent_encoded_database_credentials(monkeypatch):
    url = "mysql+pymysql://synthetic:dummy%25value@127.0.0.1/partnerpanel_test_offline"
    monkeypatch.setattr(settings, "database_url", url)
    calls = []

    def upgrade(cfg, revision):
        # The Alembic environment reads the URL from settings. This stub
        # isolates bootstrap's config parsing and never opens the connection.
        calls.append((settings.database_url, revision))

    monkeypatch.setattr(bootstrap.command, "upgrade", upgrade)
    bootstrap.upgrade_database()
    assert calls == [(url, "head")]


def test_sqlite_backup_restores_wallet_identity_and_pending_state(setup, tmp_path):
    sessions, sim, partner_id, ids, balance, source = setup
    original = sim.handler

    def interrupted(request):
        if request.url.path.endswith("/user/update"):
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return original(request)

    sim.handler = interrupted
    with sessions() as db, gateway(sim) as client:
        with pytest.raises(AppError) as failure:
            subscriptions.increase_subscription(
                db, sub_id=ids[0], partner_id=partner_id, traffic_cap_gb=12,
                end_date=None, actor=subscriptions.Actor("partner", partner_id), gateway=client, now=NOW,
            )
        assert failure.value.status_code == 503
        db.rollback()
        db.add(RevokedToken(jti="4" * 32, expires_at=2000000000))
        db.commit()

    snapshot = tmp_path / "snapshot.sqlite3"
    restore = tmp_path / "restore.sqlite3"
    assert source.resolve().is_relative_to(tmp_path.resolve())
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live:
        with closing(sqlite3.connect(snapshot)) as backup:
            live.backup(backup)
            with closing(sqlite3.connect(restore)) as restored:
                backup.backup(restored)
                assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                assert restored.execute("PRAGMA foreign_key_check").fetchall() == []

    engine = make_engine(f"sqlite:///{restore.as_posix()}")
    try:
        with Session(engine) as db:
            assert db.get(Partner, partner_id).balance_toman == balance - 2000
            sub = db.get(Subscription, ids[0])
            assert (sub.email_domain, sub.plan_id) == ("gold.example.com", 7)
            op = db.scalar(select(PanelOperation).where(PanelOperation.status == "pending"))
            assert op.kind == "increase" and op.subscription_id == sub.id
            ledger = db.scalars(select(WalletLedger).where(
                WalletLedger.subscription_id == sub.id, WalletLedger.reason == "increase",
                WalletLedger.asset == "toman",
            )).all()
            assert [row.amount for row in ledger] == [-2000]
            assert db.get(RevokedToken, "4" * 32) is not None
    finally:
        engine.dispose()
