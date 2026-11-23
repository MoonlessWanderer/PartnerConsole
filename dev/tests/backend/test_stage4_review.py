import logging
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy.dialects import mysql, sqlite

from app import main  # Sets the application's logging policy.
from app.core.database import make_engine
from app.core.errors import AppError
from app.core.outbound import check_address
from app.integrations.v2board import V2BoardClient
from app.services import bootstrap, money_sync, subscriptions


@pytest.mark.parametrize("dialect", [sqlite.dialect(), mysql.dialect()])
def test_foreign_owner_is_filtered_before_account_lock(monkeypatch, dialect):
    def lookup(query):
        sql = str(query.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
        assert "subscriptions.id = 42" in sql
        assert "subscriptions.partner_id = 7" in sql
        return None

    db = Mock()
    db.scalar.side_effect = lookup
    account_lock = Mock()
    monkeypatch.setattr(subscriptions, "lock_partner_row", account_lock)
    with pytest.raises(AppError) as denied:
        subscriptions.update_note(db, sub_id=42, partner_id=7, note="synthetic note")
    assert denied.value.status_code == 404
    account_lock.assert_not_called()


def test_panel_private_path_and_query_are_not_logged(caplog):
    caplog.set_level(logging.INFO)
    private_path = "synthetic-private-path"
    private_query = "synthetic-query-marker"

    def handler(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"data": {"auth_data": "dummy", "is_admin": 1}})
        assert private_path in request.url.path
        assert request.url.params["filter"] == private_query
        return httpx.Response(200, json={"data": []})

    with V2BoardClient("https://panel.test", private_path, "dummy", "dummy",
                       transport=httpx.MockTransport(handler)) as gateway:
        assert gateway._request("GET", "user/fetch", params={"filter": private_query}) == []
    assert private_path not in caplog.text
    assert private_query not in caplog.text


def test_recovery_logs_exception_type_without_raw_exception(monkeypatch, caplog):
    db = Mock()
    marker = "synthetic-sensitive-recovery-detail"
    monkeypatch.setattr(bootstrap, "SessionLocal", lambda: db)
    monkeypatch.setattr(money_sync, "reconcile_pending", Mock(side_effect=ValueError(marker)))
    bootstrap.recover_pending_operations()
    assert marker not in caplog.text
    assert "ValueError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    db.rollback.assert_called_once()
    db.close.assert_called_once()


def test_engine_redacts_bound_sql_parameters():
    engine = make_engine("sqlite://")
    try:
        assert engine.hide_parameters
    finally:
        engine.dispose()


def test_ipv6_site_local_requires_explicit_private_origin_permission():
    with pytest.raises(AppError):
        check_address("fec0::1", allow_private=False)
    check_address("fec0::1", allow_private=True)
