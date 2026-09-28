"""
JWT utilities: token creation and decoding.

Requirements: 1.3, 1.4, 1.5, 1.14, 1.15
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import jwt

from app.core.config import settings

# Token type claim used to distinguish access vs refresh tokens
_ACCESS_TYPE = "access"
_REFRESH_TYPE = "refresh"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(user_id: str, email: str, role: str) -> str:
    """Create a short-lived JWT access token (Requirement 1.3)."""
    expire = _now_utc() + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    payload: dict[str, Any] = {
        "sub": user_id,
        "email": email,
        "role": role,
        "type": _ACCESS_TYPE,
        "iat": _now_utc(),
        "exp": expire,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> tuple[str, str]:
    """
    Create a long-lived refresh token.

    Returns (encoded_token, jti) so the caller can store the jti for blacklisting.
    Requirement 1.4, 1.5, 1.15
    """
    jti = str(uuid4())
    expire = _now_utc() + timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
    payload: dict[str, Any] = {
        "sub": user_id,
        "type": _REFRESH_TYPE,
        "jti": jti,
        "iat": _now_utc(),
        "exp": expire,
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, jti


def decode_token(token: str) -> dict[str, Any]:
    """
    Decode and verify a JWT token.

    Raises jwt.ExpiredSignatureError or jwt.InvalidTokenError on failure.
    """
    return jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALGORITHM],
    )
