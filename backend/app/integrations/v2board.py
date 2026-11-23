from __future__ import annotations

import hashlib
import logging
import threading
import time
from urllib.parse import quote

import httpx

from app.core.errors import AuthExpired, PanelCredentialsRejected, V2BoardError
from app.core.outbound import PanelTransport, panel_origin

logger = logging.getLogger("partner.v2board")

# Upstream adds a never-expiring admin session per login, so operations share
# one auth_data per credential for a short window instead of logging in each time.
AUTH_REUSE_SECONDS = 600.0
# Upstream login rejections (zh-CN and en-US). Wrong passwords count toward the
# panel's lockout, so a rejected credential is not retried automatically.
_REJECTED_LOGIN = (
    "邮箱或密码错误",
    "Incorrect email or password",
    "密码错误次数过多",
    "There are too many password errors",
    "该账户已被停止使用",
    "Your account has been suspended",
)
_CREDENTIALS_REJECTED = "پنل رمز ادمین را رد کرده است؛ رمز پنل را اصلاح و اتصال را آزمایش کنید"
# user/update (wyx2685/v2board 4187826) aborts with these before its single UPDATE.
# 保存失败 is excluded: the UPDATE itself raised, which may happen after a commit.
_UPDATE_PRE_WRITE_ABORTS = frozenset({"用户不存在", "邮箱已被使用", "订阅计划不存在"})
# Fields of App\Http\Requests\Admin\UserUpdate, validated before the controller runs.
_UPDATE_FIELDS = frozenset({
    "email", "password", "transfer_enable", "device_limit", "expired_at", "banned", "plan_id",
    "commission_rate", "discount", "is_admin", "is_staff", "u", "d", "balance", "commission_type",
    "commission_balance", "remarks", "speed_limit",
})


def _update_not_applied(path: str | None, status: int, body) -> bool:
    """True only for user/update answers that the endpoint returns before writing."""
    if path != "user/update" or not isinstance(body, dict):
        return False
    if status == 500:
        return set(body) == {"message"} and body["message"] in _UPDATE_PRE_WRITE_ABORTS
    if status == 422:
        errors = body.get("errors")
        return (
            set(body) == {"message", "errors"}
            and body["message"] == "The given data was invalid."
            and isinstance(errors, dict) and bool(errors) and set(errors) <= _UPDATE_FIELDS
            and all(isinstance(items, list) and items and all(isinstance(item, str) for item in items)
                    for items in errors.values())
        )
    return False

_auth_guard = threading.Lock()
_auth_locks: dict[tuple, threading.Lock] = {}
_shared_auth: dict[tuple, tuple[str, float]] = {}
_rejected_logins: set[tuple] = set()


def reset_panel_auth() -> None:
    with _auth_guard:
        _auth_locks.clear()
        _shared_auth.clear()
        _rejected_logins.clear()


class PanelUserNotFound(V2BoardError):
    """A successful, validated empty lookup established user absence."""


class V2BoardClient:
    """Admin client for wyx2685/v2board /api/v1."""

    def __init__(
        self,
        base_url: str,
        secure_path: str,
        email: str,
        password: str,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = panel_origin(base_url)
        self.secure_path = secure_path.strip("/")
        self.email = email
        self.password = password
        self._auth_key = (
            self.base_url,
            email.strip().lower(),
            hashlib.sha256(password.encode("utf-8")).hexdigest(),
        )
        self._auth: str | None = None
        self._plans: dict[int, dict] | None = None
        # Connect straight to the panel URL. A system HTTP proxy returns 503 for localhost.
        self._client = httpx.Client(
            timeout=httpx.Timeout(15.0, connect=4.0),
            transport=transport if transport is not None else PanelTransport(self.base_url),
            trust_env=False,
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "V2BoardClient":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def _admin(self, path: str) -> str:
        return f"{self.base_url}/api/v1/{self.secure_path}/{path.lstrip('/')}"

    def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        with self._client.stream(method, url, **kwargs) as response:
            if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                raise V2BoardError("فشرده‌سازی پاسخ پنل مجاز نیست")
            content = bytearray()
            # Never decompress attacker-controlled content before enforcing the
            # bound. Panels/proxies must honor Accept-Encoding: identity.
            chunks = (response.content,) if response.is_stream_consumed else response.iter_raw(chunk_size=65536)
            for chunk in chunks:
                if len(content) + len(chunk) > 2 * 1024 * 1024:
                    raise V2BoardError("پاسخ پنل بیش از اندازه مجاز است")
                content.extend(chunk)
            return httpx.Response(response.status_code, content=bytes(content), request=response.request)

    def _read_json(self, response: httpx.Response, path: str | None = None):
        try:
            body = response.json()
        except (ValueError, RecursionError) as exc:
            raise V2BoardError("پاسخ JSON پنل نامعتبر است") from exc
        if 300 <= response.status_code < 400:
            raise V2BoardError("تغییر مسیر پنل مجاز نیست")
        if (response.status_code == 403 and isinstance(body, dict)
                and body.get("message") in ("expired", "未登录或登陆已过期")):
            raise AuthExpired("نشست ادمین پنل منقضی شده است")
        if not 200 <= response.status_code < 300:
            # Keep only fixed classifications needed for financial recovery.
            # Remote error text may contain credentials, tokens or SQL traces.
            text = str(body.get("message", "")) if isinstance(body, dict) else ""
            # HTTP errors, including routing failures, cannot prove user absence.
            # Only a successful validated lookup can establish that fact.
            if any(word in text.lower() for word in ("已存在", "already exists", "ثبت شده", "تکرار")):
                message = "کاربر قبلاً ثبت شده است"
            elif "邮箱格式不正确" in text:
                message = "邮箱格式不正确"
            else:
                message = f"خطای پنل ({response.status_code})"
            error = V2BoardError(message)
            error.panel_rejected = _update_not_applied(path, response.status_code, body)
            raise error
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    def _lock(self) -> threading.Lock:
        with _auth_guard:
            return _auth_locks.setdefault(self._auth_key, threading.Lock())

    def _login(self) -> str:
        response = self._send(
            "POST",
            f"{self.base_url}/api/v1/passport/auth/login",
            json={"email": self.email, "password": self.password},
        )
        if not 200 <= response.status_code < 300:
            try:
                body = response.json()
            except (ValueError, RecursionError):
                body = None
            message = str(body.get("message", "")) if isinstance(body, dict) else ""
            if response.status_code == 422 or any(text in message for text in _REJECTED_LOGIN):
                with _auth_guard:
                    _rejected_logins.add(self._auth_key)
                    _shared_auth.pop(self._auth_key, None)
                raise PanelCredentialsRejected(_CREDENTIALS_REJECTED)
        try:
            data = self._read_json(response)
        except AuthExpired as exc:
            raise V2BoardError("ورود به پنل رد شد") from exc
        if (not isinstance(data, dict) or not isinstance(data.get("auth_data"), str)
                or not data["auth_data"] or data.get("is_admin") not in (True, 1, "1")):
            raise V2BoardError("ورود ادمین پنل ناموفق بود یا این حساب ادمین نیست")
        self._auth = str(data["auth_data"])
        with _auth_guard:
            _rejected_logins.discard(self._auth_key)
            _shared_auth[self._auth_key] = (self._auth, time.monotonic() + AUTH_REUSE_SECONDS)
        return self._auth

    def login(self) -> str:
        """Explicit login (connection test). Always contacts the panel."""
        with self._lock():
            return self._login()

    def _ensure_auth(self) -> None:
        if self._auth:
            return
        with self._lock():
            with _auth_guard:
                if self._auth_key in _rejected_logins:
                    raise PanelCredentialsRejected(_CREDENTIALS_REJECTED)
                shared = _shared_auth.get(self._auth_key)
            if shared is not None and shared[1] > time.monotonic():
                self._auth = shared[0]
                return
            self._login()

    def _drop_auth(self) -> None:
        with _auth_guard:
            shared = _shared_auth.get(self._auth_key)
            if shared is not None and shared[0] == self._auth:
                del _shared_auth[self._auth_key]
        self._auth = None

    def _request(self, method: str, path: str, **kwargs):
        self._ensure_auth()
        response = self._send(
            method,
            self._admin(path),
            headers={"Authorization": self._auth},
            **kwargs,
        )
        try:
            result = self._read_json(response, path)
        except AuthExpired:
            self._drop_auth()
            self._ensure_auth()
            response = self._send(
                method,
                self._admin(path),
                headers={"Authorization": self._auth},
                **kwargs,
            )
            result = self._read_json(response, path)
        if method == "POST":
            # generate/update/resetSecret/delUser acknowledge with {data: true}.
            # Unknown envelopes remain uncertain, even when their value is truthy.
            body = response.json()
            if not isinstance(body, dict) or body.get("data") is not True:
                raise V2BoardError("پنل عملیات را تأیید نکرد")
        return result

    def fetch_plans(self) -> list[dict]:
        data = self._request("GET", "plan/fetch")
        if isinstance(data, dict):
            data = data.get("data")
        if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
            raise V2BoardError("پاسخ پلن‌های پنل نامعتبر است")
        return data

    def get_plan(self, plan_id: int) -> dict:
        if self._plans is None:
            plans = {}
            for plan in self.fetch_plans():
                if plan.get("id") is not None:
                    plans[self._response_id(plan["id"])] = plan
            self._plans = plans
        plan = self._plans.get(int(plan_id))
        if plan is None:
            raise V2BoardError("پلن روی پنل پیدا نشد")
        return plan

    def generate_user(
        self,
        *,
        email_prefix: str,
        email_suffix: str,
        plan_id: int,
        expired_at: int,
        password: str,
    ) -> None:
        self._request(
            "POST",
            "user/generate",
            json={
                "email_prefix": email_prefix,
                "email_suffix": email_suffix,
                "plan_id": plan_id,
                "expired_at": int(expired_at),
                "password": password,
            },
        )

    def _fetch(self, key: str, value) -> list[dict]:
        data = self._request(
            "GET",
            "user/fetch",
            params={
                "pageSize": 20,
                "current": 1,
                "filter[0][key]": key,
                "filter[0][condition]": "=",
                "filter[0][value]": str(value),
            },
        )
        if isinstance(data, dict):
            data = data.get("data")
        if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
            raise V2BoardError("پاسخ کاربران پنل نامعتبر است")
        return data

    def fetch_by_email(self, email: str) -> dict:
        rows = self._fetch("email", email)
        for row in rows:
            if str(row.get("email", "")).lower() == email.lower():
                self._response_id(row.get("id"))
                return self._with_subscribe(row)
        if rows:
            raise V2BoardError("پاسخ جستجوی کاربر با درخواست مطابقت ندارد")
        raise PanelUserNotFound("کاربر در پنل پیدا نشد")

    def fetch_by_id(self, user_id: int) -> dict:
        rows = self._fetch("id", user_id)
        for row in rows:
            if self._response_id(row.get("id")) == int(user_id):
                return self._with_subscribe(row)
        if rows:
            raise V2BoardError("پاسخ جستجوی کاربر با درخواست مطابقت ندارد")
        raise PanelUserNotFound("کاربر در پنل پیدا نشد")

    @staticmethod
    def _response_id(value) -> int:
        if (not isinstance(value, (int, str)) or isinstance(value, bool)
                or not str(value).isascii() or not str(value).isdigit()
                or len(str(value)) > 19):
            raise V2BoardError("شناسه در پاسخ پنل نامعتبر است")
        identifier = int(value)
        if not 0 < identifier <= 2**63 - 1:
            raise V2BoardError("شناسه در پاسخ پنل نامعتبر است")
        return identifier

    def get_user_info(self, user_id: int) -> dict:
        data = self._request("GET", "user/getUserInfoById", params={"id": int(user_id)})
        if not isinstance(data, dict):
            raise V2BoardError("پاسخ جزئیات کاربر نامعتبر است")
        try:
            if data.get("id") is not None and self._response_id(data["id"]) != int(user_id):
                raise ValueError()
            for field in ("u", "d"):
                value = data[field]
                if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit():
                    raise ValueError()
                if not 0 <= int(value) <= 2**63 - 1:
                    raise ValueError()
        except (KeyError, ValueError, TypeError) as exc:
            raise V2BoardError("مصرف یا هویت کاربر در پاسخ پنل نامعتبر است") from exc
        return data

    def update_user(self, payload: dict) -> None:
        self._request("POST", "user/update", json=payload)

    def reset_secret(self, user_id: int) -> None:
        self._request("POST", "user/resetSecret", json={"id": int(user_id)})

    def delete_user(self, user_id: int) -> None:
        try:
            self._request("POST", "user/delUser", json={"id": int(user_id)})
        except V2BoardError:
            # Idempotent recovery requires independent proof, not error wording.
            try:
                self.fetch_by_id(user_id)
            except PanelUserNotFound:
                return
            except Exception:
                pass
            raise

    def _with_subscribe(self, row: dict) -> dict:
        user = dict(row)
        if not user.get("subscribe_url") and user.get("token"):
            user["subscribe_url"] = f"{self.base_url}/api/v1/client/subscribe?token={quote(str(user['token']), safe='')}"
            user["subscribe_url_fallback"] = True
        return user
