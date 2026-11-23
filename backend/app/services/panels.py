import re

from app.core.crypto import decrypt_secret
from app.core.errors import AppError
from app.core.outbound import panel_origin
from app.integrations.v2board import V2BoardClient
from app.models import Panel

_SECURE_PATH = re.compile(r"^[A-Za-z0-9_-]{1,120}$")


def normalize_base_url(value: str) -> str:
    return panel_origin(value)


def normalize_secure_path(value: str) -> str:
    path = value.strip().strip("/")
    if not _SECURE_PATH.fullmatch(path):
        raise AppError("مسیر امن پنل نامعتبر است")
    return path


def normalize_domain(value: str) -> str:
    domain = value.strip().lower().lstrip("@")
    if not domain or "@" in domain or " " in domain or "." not in domain:
        raise AppError("دامنه ایمیل نامعتبر است")
    return domain


def plan_warnings(plan: dict, email_domain: str) -> list[str]:
    warnings: list[str] = []
    method = plan.get("reset_traffic_method")
    ok = False
    if method is not None and method != "":
        try:
            ok = int(method) == 2
        except (TypeError, ValueError):
            ok = False
    if not ok:
        warnings.append(
            "ریست خودکار ترافیک این پلن خاموش نیست. reset_traffic_method باید ۲ باشد؛ "
            "اگر خالی باشد تنظیم سراسری پنل اعمال می‌شود."
        )
    if email_domain.endswith(".local"):
        warnings.append(
            "دامنه .local معمولاً اعتبارسنجی email:strict پنل را رد می‌کند. دامنه واقعی بگذارید."
        )
    return warnings


def client_for_panel(panel: Panel, transport=None) -> V2BoardClient:
    from app.services.consent import recovery_target

    if panel.status == "disabled":
        raise AppError("این پنل غیرفعال است")
    password = decrypt_secret(panel.admin_password_enc)
    client = V2BoardClient(
        panel.base_url,
        panel.secure_path,
        panel.admin_email,
        password,
        transport=transport,
    )
    client.financial_recovery_target = recovery_target(panel)
    return client
