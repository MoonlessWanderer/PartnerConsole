class AppError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class V2BoardError(AppError):
    # True only when the panel answered with its own handled error for this request.
    panel_rejected = False

    def __init__(self, message: str):
        super().__init__(message, status_code=502)


class AuthExpired(V2BoardError):
    pass


class PanelCredentialsRejected(V2BoardError):
    """The panel definitively refused the stored admin credentials."""


SERVICE_ERROR = "ارتباط با سرویس ناموفق بود؛ وضعیت عملیات را بررسی و دوباره تلاش کنید"


def public_error(exc: Exception) -> str:
    """Expose business rejections and fixed recovery instructions, not traces."""
    if isinstance(exc, AppError):
        if exc.status_code < 500:
            return exc.message
        if exc.message.startswith(("ارتباط با پنل قطع شد.", "این عملیات همین حالا در حال انجام است.")):
            return exc.message
    return SERVICE_ERROR
