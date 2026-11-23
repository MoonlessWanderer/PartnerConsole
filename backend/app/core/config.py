from pydantic_settings import BaseSettings, SettingsConfigDict
from urllib.parse import urlsplit

# Production HTTP is not implied by a loopback-looking name. These are the only
# origins the no-domain SSH tunnel may set explicitly in CORS_ORIGINS.
_PRODUCTION_HTTP_LOOPBACK = {
    "http://127.0.0.1:3741": ("127.0.0.1", 3741),
    "http://localhost:3741": ("localhost", 3741),
    "http://[::1]:3741": ("::1", 3741),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/partner.db"
    secret_key: str = "dev-only-change-me"
    admin_jwt_secret: str = ""
    partner_jwt_secret: str = ""
    panel_encryption_key: str = ""
    admin_username: str = "admin"
    admin_password: str = "admin12345"
    access_token_expire_minutes: int = 60 * 12
    cors_origins: str = "http://localhost:3741,http://127.0.0.1:3741"
    environment: str = "development"
    # Exact origins only. Required for private/loopback panel deployments.
    panel_private_origins: str = ""
    api_rate_limit_per_minute: int = 120

    def validate_security(self) -> None:
        """Fail before migrations/bootstrap when a public example key is used."""
        for name in ("secret_key", "admin_jwt_secret", "partner_jwt_secret", "panel_encryption_key"):
            value = getattr(self, name)
            if name != "secret_key" and not value:
                continue
            if (len(value) < 32 or len(set(value)) < 8
                    or "change-me" in value.lower() or "dev-only" in value.lower()):
                raise RuntimeError(f"Set a strong, non-example {name.upper()} before starting the API")
        if self.jwt_secret_for("admin") == self.jwt_secret_for("partner"):
            raise RuntimeError("Admin and partner signing keys must be different")
        if not 1 <= self.access_token_expire_minutes <= 1440:
            raise RuntimeError("ACCESS_TOKEN_EXPIRE_MINUTES must be between 1 and 1440")
        if not 1 <= self.api_rate_limit_per_minute <= 10000:
            raise RuntimeError("API_RATE_LIMIT_PER_MINUTE must be between 1 and 10000")
        explicit_cors = "cors_origins" in self.model_fields_set
        for origin in self.cors_origin_list:
            parts = urlsplit(origin)
            if (origin.lower() == "null" or parts.scheme not in {"https", "http"} or not parts.hostname
                    or parts.username or parts.password or parts.path or parts.query or parts.fragment
                    or "*" in origin):
                raise RuntimeError("CORS_ORIGINS must contain exact HTTP(S) origins")
            if self.environment == "production" and parts.scheme != "https":
                allowed = _PRODUCTION_HTTP_LOOPBACK.get(origin)
                host = (parts.hostname or "").lower()
                if (not explicit_cors or allowed is None
                        or host != allowed[0] or parts.port != allowed[1]):
                    raise RuntimeError("Production CORS origins must use HTTPS")

    def jwt_secret_for(self, role: str) -> str:
        if role == "admin" and self.admin_jwt_secret:
            return self.admin_jwt_secret
        if role == "partner" and self.partner_jwt_secret:
            return self.partner_jwt_secret
        return f"{self.secret_key}:{role}"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


settings = Settings()
