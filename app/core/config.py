"""Application configuration loaded from environment / .env.

Includes automatic `redis://` → `rediss://` upgrade for known
TLS-required Redis providers (Upstash, Aiven, Redis Cloud, Render KV).
Local `redis://` (plaintext) connections are left untouched so that
local Docker development still works.
"""
from urllib.parse import urlparse

from pydantic import AnyUrl, EmailStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Cloud Redis providers that REQUIRE TLS. The right-hand side is the
# host suffix that uniquely identifies the provider. If a REDIS_URL host
# matches any of these AND the scheme is `redis://`, we auto-upgrade to
# `rediss://` (TLS) so the user doesn't have to remember to add the `s`.
_TLS_REQUIRED_HOST_SUFFIXES: tuple[str, ...] = (
    ".upstash.io",
    "upstash.io",
    ".aivencloud.com",
    ".redis-cloud.com",
    ".render.com",          # Render Key Value Store
    ".redislabs.com",
    ".elastic-cloud.com",
)


def _auto_upgrade_redis_url(url: str) -> str:
    """
    If the URL points at a known TLS-required provider and uses plaintext
    `redis://`, return a copy with `rediss://` so the connection is
    encrypted. Otherwise return the URL unchanged.

    This protects against the common "Connection closed by server" error
    on Upstash / Aiven / etc. when users forget to add the second `s`.
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    if parsed.scheme != "redis":
        return url
    host = (parsed.hostname or "").lower()
    if not host:
        return url
    if any(host == s.lstrip(".") or host.endswith(s) for s in _TLS_REQUIRED_HOST_SUFFIXES):
        return url.replace("redis://", "rediss://", 1)
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Database
    DATABASE_URL: str = "mysql+aiomysql://root:root@localhost:3306/robot_db"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Robot authentication
    ROBOT_API_KEY: str = "change-me-robot-key"

    # First admin seeding
    ADMIN_EMAIL: EmailStr = "admin@example.com"
    ADMIN_PASSWORD: str = "change-me-admin-password"

    # Simulation mode
    SIMULATION_MODE: bool = False
    SIM_NAV_DELAY_S: float = 5.0
    SIM_NAV_SUCCESS_RATE: float = 0.9
    SIM_SLAM_HZ: float = 10.0
    SIM_PATH: str = "random_walk"  # "scripted" | "random_walk"

    # Application
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    DEBUG: bool = False

    # Safety
    DELIVERY_STATE_TIMEOUT_S: int = 120
    HEARTBEAT_TIMEOUT_S: int = 5

    @field_validator("REDIS_URL")
    @classmethod
    def _upgrade_redis_to_tls(cls, v: str) -> str:
        return _auto_upgrade_redis_url(v)


settings = Settings()