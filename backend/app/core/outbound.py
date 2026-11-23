"""Panel egress policy enforced on the exact address used by each TCP connect."""
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit

import httpcore
import httpx

from app.core.config import settings
from app.core.errors import AppError


def panel_origin(value: str) -> str:
    raw = value.strip()
    if len(raw) > 300 or any(ord(c) <= 32 or c in "\\" for c in raw):
        raise AppError("آدرس پنل نامعتبر است")
    try:
        parts = urlsplit(raw)
        port = parts.port
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.path not in ("", "/") or parts.query or parts.fragment
                or "%" in parts.hostname or port == 0):
            raise ValueError()
        url = httpx.URL(raw)
        host = url.raw_host.decode("ascii")
        if ":" in host:
            host = f"[{host}]"
        default_port = 443 if parts.scheme == "https" else 80
        suffix = f":{port}" if port is not None and port != default_port else ""
        return f"{parts.scheme}://{host}{suffix}"
    except (ValueError, httpx.InvalidURL, UnicodeError) as exc:
        raise AppError("آدرس پنل نامعتبر است") from exc


def private_origin_allowed(origin: str) -> bool:
    return origin in {panel_origin(item) for item in settings.panel_private_origins.split(",") if item.strip()}


def check_address(address: str, *, allow_private: bool) -> None:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if (ip.is_link_local or ip.is_multicast or ip.is_unspecified or (ip.is_reserved and not ip.is_loopback)
            or str(ip) in {"100.100.100.200", "fd00:ec2::254"}):
        raise AppError("مقصد شبکه برای پنل مجاز نیست", 502)
    # Python can classify deprecated IPv6 site-local addresses as global.
    site_local = isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local
    if (not ip.is_global or site_local) and not allow_private:
        raise AppError("پنل داخلی باید با مبدأ دقیق در PANEL_PRIVATE_ORIGINS مجاز شود", 502)


class PanelBackend(httpcore.SyncBackend):
    def __init__(self, origin: str):
        parts = urlsplit(origin)
        self.host = httpx.URL(origin).raw_host.decode("ascii")
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.allow_private = private_origin_allowed(origin)
        if settings.environment == "production" and parts.scheme != "https" and not self.allow_private:
            raise AppError("ارتباط پنل در محیط عملیاتی باید HTTPS باشد", 502)

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        started = time.monotonic()
        if isinstance(host, bytes):
            host = host.decode("ascii")
        if host.lower() != self.host.lower() or port != self.port:
            raise AppError("مقصد درخواست پنل مجاز نیست", 502)
        try:
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise httpcore.ConnectError("Panel DNS lookup failed") from exc
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        if not addresses:
            raise httpcore.ConnectError("Panel DNS lookup failed")
        # Validate the entire answer. Connect to the validated numeric address,
        # so a second DNS answer cannot redirect us into a private network.
        for address in addresses:
            check_address(address, allow_private=self.allow_private)
        last_error = None
        for address in addresses:
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                raise httpcore.ConnectTimeout("Panel connection timed out")
            try:
                return super().connect_tcp(address, port, remaining, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise last_error


class PanelTransport(httpx.HTTPTransport):
    def __init__(self, origin: str):
        # HTTPX 0.28's adapter exposes its httpcore pool as _pool. Use httpcore's
        # public network_backend API; keep HTTPX's exception/stream adaptation.
        # Dependency ranges constrain this adapter; real HTTP tests cover it.
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(), network_backend=PanelBackend(origin),
            max_connections=10, max_keepalive_connections=5, retries=0,
        )
