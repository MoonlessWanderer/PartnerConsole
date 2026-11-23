import logging
from contextlib import asynccontextmanager
from importlib.metadata import version

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api
from app.core.config import settings
from starlette.exceptions import HTTPException

from app.core.errors import AppError, SERVICE_ERROR, public_error
from app.services.consent import ConsentConflict
from app.core.request_limits import BodyLimitMiddleware
from app.services.bootstrap import recover_pending_operations, require_database_ready, seed_admin, upgrade_database

logging.basicConfig(level=logging.INFO)
# HTTPX request logs include private panel paths and query parameters.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.validate_security()
    if settings.environment == "production":
        require_database_ready()
    else:
        upgrade_database()
        seed_admin()
    recover_pending_operations()
    yield


app = FastAPI(
    title="Partner Console",
    version=version("partner-console"),
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


def error_response(status: int, code: str, detail: str, headers=None):
    return JSONResponse(status_code=status, content={"code": code, "detail": detail}, headers=headers)


@app.middleware("http")
async def unexpected_error(_request: Request, call_next):
    try:
        return await call_next(_request)
    except Exception as exc:
        logger.error("API request failed (%s)", type(exc).__name__)
        return error_response(500, "internal_error", SERVICE_ERROR)


app.add_middleware(BodyLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,  # authentication uses an explicit Bearer header
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api, prefix="/api")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(AppError)
async def app_error_handler(_request: Request, exc: AppError):
    detail = public_error(exc)
    if isinstance(exc, ConsentConflict):
        return error_response(409, "financial_consent", detail)
    code = "request_rejected" if exc.status_code < 500 else (
        "operation_status" if detail != SERVICE_ERROR else "service_error"
    )
    return error_response(exc.status_code, code, detail)


@app.exception_handler(httpx.TransportError)
async def panel_transport_error(_request: Request, exc: httpx.TransportError):
    if isinstance(exc, httpx.TimeoutException):
        return error_response(504, "panel_timeout", "مهلت دریافت پاسخ پنل تمام شد. وضعیت عملیات را بررسی کنید.")
    return error_response(502, "panel_unavailable", "ارتباط با پنل قطع شد. وضعیت عملیات را بررسی کنید.")


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    messages = {404: "مسیر درخواست پیدا نشد", 405: "روش درخواست مجاز نیست"}
    return error_response(exc.status_code, "http_error", messages.get(exc.status_code, SERVICE_ERROR), exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_handler(_request: Request, _exc: RequestValidationError):
    return error_response(422, "invalid_input", "ورودی نامعتبر است")
