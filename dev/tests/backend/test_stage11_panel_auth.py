"""R3/R4: shared short-lived admin auth and fail-fast on rejected credentials."""

from __future__ import annotations

import threading
import time

import httpx
import pytest

from app.core.errors import AuthExpired, PanelCredentialsRejected, V2BoardError
from app.integrations import v2board
from app.integrations.v2board import V2BoardClient

PLANS = {"data": [{"id": 7, "transfer_enable": 50, "device_limit": 2, "speed_limit": None}]}


class Panel:
    """Login counts, token rotation and failure modes of an upstream panel."""

    def __init__(self):
        self.lock = threading.Lock()
        self.logins = 0
        self.valid: set[str] = set()
        self.password = "secret"
        self.login_failure: httpx.Response | Exception | None = None
        self.login_delay = 0.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/passport/auth/login"):
            with self.lock:
                self.logins += 1
                number = self.logins
            time.sleep(self.login_delay)
            if isinstance(self.login_failure, Exception):
                raise self.login_failure
            if self.login_failure is not None:
                return self.login_failure
            body = httpx.Request("POST", request.url, content=request.content).read()
            if b'"password":"%s"' % self.password.encode() not in body.replace(b" ", b""):
                return httpx.Response(500, json={"message": "邮箱或密码错误"})
            token = f"auth-{number}"
            with self.lock:
                self.valid.add(token)
            return httpx.Response(200, json={"data": {"auth_data": token, "is_admin": 1, "token": "t"}})
        if request.headers.get("authorization") not in self.valid:
            return httpx.Response(403, json={"message": "未登录或登陆已过期"})
        return httpx.Response(200, json=PLANS)

    def expire_all(self):
        with self.lock:
            self.valid.clear()


@pytest.fixture()
def panel():
    return Panel()


def client(panel: Panel, password: str = "secret", base: str = "http://panel.test") -> V2BoardClient:
    return V2BoardClient(base, "secret", "admin@panel.test", password, transport=httpx.MockTransport(panel.handler))


def test_operations_share_one_login_within_the_window(panel):
    for _ in range(5):
        with client(panel) as c:
            c.fetch_plans()
    assert panel.logins == 1


def test_concurrent_clients_single_flight_one_login(panel):
    panel.login_delay = 0.2
    errors: list[BaseException] = []

    def work():
        try:
            with client(panel) as c:
                c.fetch_plans()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []
    assert panel.logins == 1


def test_reuse_window_expires(panel, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(v2board.time, "monotonic", lambda: now[0])
    with client(panel) as c:
        c.fetch_plans()
    now[0] += v2board.AUTH_REUSE_SECONDS - 1
    with client(panel) as c:
        c.fetch_plans()
    assert panel.logins == 1
    now[0] += 2
    with client(panel) as c:
        c.fetch_plans()
    assert panel.logins == 2


def test_expired_403_relogs_once_and_other_clients_use_the_new_session(panel):
    with client(panel) as c:
        c.fetch_plans()
    panel.expire_all()
    with client(panel) as c:
        c.fetch_plans()
    assert panel.logins == 2
    with client(panel) as c:
        c.fetch_plans()
    assert panel.logins == 2


def test_persistent_403_after_relogin_still_fails(panel):
    class Always403(Panel):
        def handler(self, request):
            response = super().handler(request)
            if not request.url.path.endswith("/passport/auth/login"):
                return httpx.Response(403, json={"message": "未登录或登陆已过期"})
            return response

    stubborn = Always403()
    with client(stubborn) as c, pytest.raises(AuthExpired):
        c.fetch_plans()
    assert stubborn.logins == 2


def test_sessions_are_not_shared_across_panels_or_credentials(panel):
    with client(panel) as c:
        c.fetch_plans()
    with client(panel, base="http://other-panel.test") as c:
        c.fetch_plans()
    assert panel.logins == 2
    assert all(len(key) == 3 and "secret" not in key for key in v2board._shared_auth)


def test_rejected_password_is_not_retried_until_credentials_change(panel):
    with client(panel, password="wrong") as c, pytest.raises(PanelCredentialsRejected):
        c.fetch_plans()
    for _ in range(3):
        with client(panel, password="wrong") as c, pytest.raises(PanelCredentialsRejected):
            c.fetch_plans()
    assert panel.logins == 1
    with client(panel, password="secret") as c:
        c.fetch_plans()
    assert panel.logins == 2


def test_explicit_login_retries_and_success_clears_the_block(panel):
    with client(panel, password="wrong") as c, pytest.raises(PanelCredentialsRejected):
        c.fetch_plans()
    panel.password = "wrong"  # the panel admin restored the stored password
    with client(panel, password="wrong") as c:
        assert c.login()
    with client(panel, password="wrong") as c:
        c.fetch_plans()
    assert panel.logins == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"message": "密码错误次数过多，请 60 分钟后再试"}),
        httpx.Response(500, json={"message": "Incorrect email or password"}),
        httpx.Response(500, json={"message": "该账户已被停止使用"}),
        httpx.Response(422, json={"message": "The given data was invalid.", "errors": {"password": ["x"]}}),
    ],
)
def test_lockout_banned_and_invalid_input_also_fail_fast(panel, response):
    panel.login_failure = response
    for _ in range(3):
        with client(panel) as c, pytest.raises(PanelCredentialsRejected):
            c.fetch_plans()
    assert panel.logins == 1


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("slow panel"),
        httpx.ConnectError("down"),
        httpx.Response(500, json={"message": "Uh-oh, we've had some problems, we're working on it."}),
        httpx.Response(502, text="bad gateway"),
    ],
)
def test_transient_login_failures_never_block(panel, failure):
    panel.login_failure = failure
    for _ in range(2):
        with client(panel) as c, pytest.raises((httpx.HTTPError, V2BoardError)) as exc:
            c.fetch_plans()
        assert not isinstance(exc.value, PanelCredentialsRejected)
    assert panel.logins == 2
    panel.login_failure = None
    with client(panel) as c:
        c.fetch_plans()
    assert panel.logins == 3
