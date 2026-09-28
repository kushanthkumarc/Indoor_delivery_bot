"""
Auth service: password hashing, user authentication, token blacklist, rate limiting.

Requirements: 1.3, 1.4, 1.5, 1.8, 1.14
"""

import bcrypt
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User

# Redis key prefixes
_BLACKLIST_PREFIX = "auth:blacklist:"
_RATE_LIMIT_PREFIX = "auth:rate:"

_MAX_LOGIN_ATTEMPTS = 5
_RATE_LIMIT_WINDOW_S = 15 * 60  # 15 minutes


# ---------------------------------------------------------------------------
# Password utilities  (Requirement 1.14 — bcrypt cost >= 12)
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    """Hash a plaintext password with bcrypt at cost factor 12."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if plain matches the stored bcrypt hash."""
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# User lookup
# ---------------------------------------------------------------------------


async def authenticate_user(
    db: AsyncSession, email: str, password: str
) -> User | None:
    """
    Return the User if credentials are valid and account is active, else None.

    Does NOT handle rate limiting — the caller (router) must do that.
    """
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        return None
    if not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


# ---------------------------------------------------------------------------
# Refresh token blacklist  (Requirement 1.5)
# ---------------------------------------------------------------------------


async def blacklist_token(redis: Redis, jti: str, ttl_seconds: int) -> None:
    """Add a refresh token JTI to the Redis blacklist with expiry."""
    key = f"{_BLACKLIST_PREFIX}{jti}"
    await redis.setex(key, ttl_seconds, "1")


async def is_blacklisted(redis: Redis, jti: str) -> bool:
    """Return True if the given JTI has been blacklisted."""
    key = f"{_BLACKLIST_PREFIX}{jti}"
    return await redis.exists(key) == 1


# ---------------------------------------------------------------------------
# Login rate limiter  (Requirement 1.8 — 5 attempts per 15 min per IP)
# ---------------------------------------------------------------------------


async def check_rate_limit(redis: Redis, ip: str) -> bool:
    """
    Return True if the IP is allowed to attempt login (under the limit).
    Increment the attempt counter; set expiry on first attempt.
    """
    key = f"{_RATE_LIMIT_PREFIX}{ip}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, _RATE_LIMIT_WINDOW_S)
    return count <= _MAX_LOGIN_ATTEMPTS


async def get_rate_limit_ttl(redis: Redis, ip: str) -> int:
    """Return seconds until the rate limit window resets (for 429 response)."""
    key = f"{_RATE_LIMIT_PREFIX}{ip}"
    ttl = await redis.ttl(key)
    return max(ttl, 0)
