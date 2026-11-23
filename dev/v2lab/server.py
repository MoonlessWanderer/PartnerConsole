"""V2Board-compatible lab.

Implements the admin routes our panel calls, with the same traps as
wyx2685/v2board: generate returns only true, speed_limit is not set on
generate, every update must send plan_id or group_id is cleared (and
invite_user_id unless invite_user_email is sent), and subscribe_url exists
only on user/fetch. generate does not validate the email; update validates
like Laravel and answers 422. getUserInfoById returns the raw model
(password hash, token, uuid) and fails with a generic 500 for an unknown id.
Every login adds a session that is never pruned, and five wrong passwords
lock the email out (password_limit_enable saved in the panel config).
"""

from __future__ import annotations

import secrets
import threading
from copy import deepcopy

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ADMIN_EMAIL = "admin@panel.test"
ADMIN_PASSWORD = "secret"
SECURE_PATH = "labsecure"
HOST = "127.0.0.1"
PORT = 8788
GB = 1073741824
PASSWORD_LIMIT_COUNT = 5
GENERIC_ERROR = "Uh-oh, we've had some problems, we're working on it."

PLANS = {
    7: {
        "id": 7,
        "name": "طلایی",
        "transfer_enable": 50,
        "device_limit": 2,
        "speed_limit": 100,
        "reset_traffic_method": 2,
        "group_id": 3,
    },
    8: {
        "id": 8,
        "name": "نقره‌ای",
        "transfer_enable": 20,
        "device_limit": 1,
        "speed_limit": None,
        "reset_traffic_method": None,
        "group_id": 4,
    },
}

_lock = threading.Lock()
_state: dict = {}


def _fresh() -> dict:
    return {"users": {}, "next_id": 1, "sessions": {}, "password_errors": {}}


def reset_state() -> None:
    global _state
    with _lock:
        _state = _fresh()


reset_state()
app = FastAPI(title="V2Board lab", docs_url=None, redoc_url=None)


@app.middleware("http")
async def lab_only_local(request: Request, call_next):
    if request.url.path.startswith("/_lab"):
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1"}:
            return JSONResponse({"message": "forbidden"}, status_code=403)
    return await call_next(request)


def _fail(message: str, status: int = 500) -> JSONResponse:
    return JSONResponse({"message": message}, status_code=status)


def _invalid(errors: dict[str, str]) -> JSONResponse:
    return JSONResponse(
        {"message": "The given data was invalid.", "errors": {key: [text] for key, text in errors.items()}},
        status_code=422,
    )


def _ok(data):
    return {"data": data}


def _subscribe_url(request: Request, token: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/v1/client/subscribe?token={token}"


def _fetch_row(request: Request, user: dict) -> dict:
    # Upstream selects * (no $hidden), so the password hash is part of the row.
    row = deepcopy(user)
    row["total_used"] = int(row["u"]) + int(row["d"])
    row["alive_ip"] = 0
    row["ips"] = ""
    row["subscribe_url"] = _subscribe_url(request, row["token"])
    return row


async def _body(request: Request) -> dict:
    if not request.headers.get("content-type", "").startswith("application/json"):
        return {}
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _admin_user(request: Request) -> dict | None:
    token = request.headers.get("authorization", "")
    with _lock:
        session = _state["sessions"].get(token)
    if not session or not session.get("is_admin"):
        return None
    return session


def _email_ok(email: str) -> bool:
    # Approximates Laravel email:strict (egulias NoRFCWarningsValidation):
    # single-label and .local domains pass, malformed dot placement does not.
    if email.count("@") != 1 or any(ch.isspace() for ch in email):
        return False
    for part in email.split("@"):
        if not part or part.startswith(".") or part.endswith(".") or ".." in part:
            return False
    return True


@app.get("/_lab/health")
def health():
    return {"ok": True, "secure_path": SECURE_PATH}


@app.post("/_lab/reset")
def lab_reset():
    reset_state()
    return {"ok": True}


@app.post("/_lab/expire")
def lab_expire():
    with _lock:
        _state["sessions"].clear()
    return {"ok": True}


@app.get("/_lab/sessions")
def lab_sessions():
    with _lock:
        return {"total": len(_state["sessions"])}


@app.post("/_lab/users/{user_id}/inviter")
async def lab_inviter(user_id: int, request: Request):
    body = await _body(request)
    with _lock:
        user = _state["users"].get(user_id)
        if user is None:
            return _fail("用户不存在")
        user["invite_user_id"] = body.get("invite_user_id")
    return {"ok": True}


@app.post("/_lab/users/{user_id}/traffic")
async def lab_traffic(user_id: int, request: Request):
    body = await _body(request)
    with _lock:
        user = _state["users"].get(user_id)
        if user is None:
            return _fail("用户不存在")
        user["u"] = int(body.get("u") or 0)
        user["d"] = int(body.get("d") or 0)
        snapshot = deepcopy(user)
    return _ok(snapshot)


@app.get("/_lab/users/{user_id}")
def lab_user(user_id: int):
    with _lock:
        user = _state["users"].get(user_id)
        if user is None:
            return _fail("用户不存在")
        snapshot = deepcopy(user)
    snapshot.pop("password", None)
    return _ok(snapshot)


@app.get("/_lab/users")
def lab_users():
    with _lock:
        rows = [deepcopy(user) for user in _state["users"].values()]
    for row in rows:
        row.pop("password", None)
    return {"data": rows, "total": len(rows)}


@app.post("/api/v1/passport/auth/login")
async def login(request: Request):
    body = await _body(request)
    email = str(body.get("email") or "")
    password = str(body.get("password") or "")
    with _lock:
        if _state["password_errors"].get(email, 0) >= PASSWORD_LIMIT_COUNT:
            return _fail("There are too many password errors, please try again after 60 minutes.")
        if email in {ADMIN_EMAIL, "staff@panel.test"} and password != ADMIN_PASSWORD:
            _state["password_errors"][email] = _state["password_errors"].get(email, 0) + 1
    if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
        auth_data = secrets.token_urlsafe(24)
        with _lock:
            _state["sessions"][auth_data] = {"id": 1, "email": email, "is_admin": 1}
        return _ok({"token": "admin-token", "is_admin": 1, "auth_data": auth_data})
    if email == "staff@panel.test" and password == ADMIN_PASSWORD:
        auth_data = secrets.token_urlsafe(24)
        with _lock:
            _state["sessions"][auth_data] = {"id": 2, "email": email, "is_admin": 0}
        return _ok({"token": "staff-token", "is_admin": 0, "auth_data": auth_data})
    return _fail("邮箱或密码错误")


@app.get("/api/v1/client/subscribe")
def subscribe(token: str = ""):
    with _lock:
        found = next((user for user in _state["users"].values() if user["token"] == token), None)
    if found is None:
        return _fail("token is error")
    return {"data": f"lab-subscribe {found['email']}"}


def _guard(request: Request):
    if _admin_user(request) is None:
        return _fail("未登录或登陆已过期", 403)
    return None


@app.get(f"/api/v1/{SECURE_PATH}/plan/fetch")
def plan_fetch(request: Request):
    denied = _guard(request)
    if denied:
        return denied
    return _ok(list(PLANS.values()))


@app.post(f"/api/v1/{SECURE_PATH}/user/generate")
async def generate(request: Request):
    denied = _guard(request)
    if denied:
        return denied
    body = await _body(request)
    prefix = str(body.get("email_prefix") or "")
    suffix = str(body.get("email_suffix") or "")
    if not suffix:
        return _invalid({"email_suffix": "The email suffix field is required."})
    if not prefix:
        # Upstream switches to CSV batch generation; our client never does that.
        return _fail("参数错误")
    email = f"{prefix}@{suffix}"
    plan = PLANS.get(int(body["plan_id"])) if body.get("plan_id") else None
    if body.get("plan_id") and plan is None:
        return _fail("订阅计划不存在")
    with _lock:
        if any(user["email"] == email for user in _state["users"].values()):
            return _fail("邮箱已存在于系统中")
        user_id = _state["next_id"]
        _state["next_id"] += 1
        _state["users"][user_id] = {
            "id": user_id,
            "email": email,
            "password": "$2y$10$" + secrets.token_hex(26),
            "invite_user_id": None,
            "plan_id": plan["id"] if plan else None,
            "group_id": plan["group_id"] if plan else None,
            "transfer_enable": (plan["transfer_enable"] * GB) if plan else 0,
            "device_limit": plan["device_limit"] if plan else None,
            "speed_limit": None,
            "expired_at": body.get("expired_at"),
            "u": 0,
            "d": 0,
            "banned": 0,
            "is_admin": 0,
            "is_staff": 0,
            "uuid": secrets.token_hex(16),
            "token": secrets.token_hex(16),
        }
    return _ok(True)


@app.get(f"/api/v1/{SECURE_PATH}/user/fetch")
def fetch(request: Request):
    denied = _guard(request)
    if denied:
        return denied
    key = request.query_params.get("filter[0][key]")
    value = request.query_params.get("filter[0][value]")
    with _lock:
        rows = list(_state["users"].values())
    if key == "email" and value is not None:
        rows = [user for user in rows if user["email"] == value]
    elif key == "id" and value is not None:
        rows = [user for user in rows if str(user["id"]) == str(value)]
    data = [_fetch_row(request, user) for user in sorted(rows, key=lambda user: user["id"], reverse=True)]
    return {"data": data, "total": len(data)}


@app.get(f"/api/v1/{SECURE_PATH}/user/getUserInfoById")
def user_info(request: Request, id: str = ""):
    denied = _guard(request)
    if denied:
        return denied
    if not id or id == "0":
        return _fail("参数错误")
    with _lock:
        user = _state["users"].get(int(id)) if id.isdigit() else None
        if user is None:
            # Upstream dereferences a null model here; production hides the cause.
            return _fail(GENERIC_ERROR)
        snapshot = deepcopy(user)
        inviter = _state["users"].get(snapshot.get("invite_user_id") or 0)
        if inviter is not None:
            snapshot["invite_user"] = deepcopy(inviter)
    snapshot.pop("subscribe_url", None)
    return _ok(snapshot)


@app.post(f"/api/v1/{SECURE_PATH}/user/update")
async def update(request: Request):
    denied = _guard(request)
    if denied:
        return denied
    body = await _body(request)
    errors = {}
    if not body.get("email"):
        errors["email"] = "邮箱不能为空"
    elif not _email_ok(str(body["email"])):
        errors["email"] = "邮箱格式不正确"
    for key, label in (("banned", "是否封禁"), ("is_admin", "是否管理员"), ("is_staff", "是否员工")):
        if body.get(key) in (None, ""):
            errors[key] = f"{label}不能为空"
        elif str(body[key]) not in ("0", "1"):
            errors[key] = f"{label}格式不正确"
    if errors:
        return _invalid(errors)
    email = str(body["email"])
    with _lock:
        user = _state["users"].get(int(body.get("id") or 0))
        if user is None:
            return _fail("用户不存在")
        if any(other["email"] == email and other["id"] != user["id"] for other in _state["users"].values()):
            return _fail("邮箱已被使用")
        if body.get("plan_id") not in (None, ""):
            plan = PLANS.get(int(body["plan_id"]))
            if plan is None:
                return _fail("订阅计划不存在")
            user["plan_id"] = plan["id"]
            user["group_id"] = plan["group_id"]
        else:
            user["plan_id"] = None
            user["group_id"] = None
        inviter = next(
            (other for other in _state["users"].values() if other["email"] == body.get("invite_user_email")),
            None,
        ) if body.get("invite_user_email") else None
        if inviter is not None:
            user["invite_user_id"] = inviter["id"]
        elif not body.get("invite_user_email"):
            user["invite_user_id"] = None
        user["email"] = email
        user["banned"] = int(body["banned"])
        user["is_admin"] = int(body["is_admin"])
        user["is_staff"] = int(body["is_staff"])
        for key in ("transfer_enable", "expired_at", "device_limit", "speed_limit", "u", "d"):
            if key in body:
                user[key] = body[key]
    return _ok(True)


@app.post(f"/api/v1/{SECURE_PATH}/user/resetSecret")
async def reset_secret(request: Request):
    denied = _guard(request)
    if denied:
        return denied
    body = await _body(request)
    with _lock:
        user = _state["users"].get(int(body.get("id") or 0))
        if user is None:
            return _fail("用户不存在")
        user["token"] = secrets.token_hex(16)
        user["uuid"] = secrets.token_hex(16)
    return _ok(True)


@app.post(f"/api/v1/{SECURE_PATH}/user/delUser")
async def delete_user(request: Request):
    denied = _guard(request)
    if denied:
        return denied
    body = await _body(request)
    with _lock:
        user_id = int(body.get("id") or 0)
        if user_id not in _state["users"]:
            return _fail("用户不存在")
        del _state["users"][user_id]
    return _ok(True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
