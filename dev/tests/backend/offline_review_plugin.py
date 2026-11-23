"""Explicitly opt-in no-database/no-server review; never loaded by normal pytest.

Selection is reported as deselected, never as passed or silently skipped.
All original tests remain intact and runnable in the normal test suite.
"""
import socket
import sqlite3
import subprocess

import pytest
from sqlalchemy.engine import Engine


def pytest_collection_modifyitems(config, items):
    forbidden = {'db', 'setup', 'security_api', 'tmp_path', 'lab_url', 'mysql_engine'}
    selected, deselected = [], []
    for item in items:
        # On Windows asyncio.run uses socketpair, which can create a loopback
        # listener. Do not start it during an explicitly network-free review.
        if (forbidden.intersection(item.fixturenames)
                or item.originalname == 'test_unsafe_startup_fails_before_any_database_action'):
            deselected.append(item)
        else:
            selected.append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)


@pytest.fixture(autouse=True)
def enforce_no_database_or_real_network(monkeypatch):
    def prohibited(*args, **kwargs):
        raise AssertionError('Offline review prohibits database connections, real sockets and subprocesses')
    monkeypatch.setattr(Engine, 'connect', prohibited)
    monkeypatch.setattr(sqlite3, 'connect', prohibited)
    monkeypatch.setattr(socket.socket, 'connect', prohibited)
    monkeypatch.setattr(socket.socket, 'connect_ex', prohibited)
    monkeypatch.setattr(socket.socket, 'bind', prohibited)
    monkeypatch.setattr(socket, 'create_connection', prohibited)
    monkeypatch.setattr(subprocess, 'Popen', prohibited)
