"""
Auth API endpoints.

POST /auth/login    — Requirement 1.3
POST /auth/refresh  — Requirement 1.4
POST /auth/logout   — Requirement 1.5
GET  /auth/me       — current user profile

Refresh token is delivered as an httpOnly, Secure, SameSite=Strict cookie.
Auth events are written to the events audit table (Requirement 1.10).
"""

from datetime import timezone
from typing import Annotated

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.jwt import create_access_token, create_refresh_token, decode_token
from app.auth.service import (
    authenticate_user,
    blacklist_token,
    check_rate_limit,
    get_rate_limit_ttl,
    is_blacklisted,
)
from app.core.config import settings
from app.db.models import Event, EventSeverity, User, UserRole
from app.db.redis import get_redis
from app.db.session import get_db

router = APIRouter(prefix="/auth", tags=["auth"])

_REFRESH_COOKIE = "refresh_token"
_REFRESH_TTL_S = settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    role: UserRole
    is_active: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _log_event(
    db: AsyncSession,
    event_type: str,
    severity: EventSeverity,
    user_id: str | None = None,
    payload: dict | None = None,
) -> None:
    """Write a single row to the events audit table."""
    event = Event(
        type=event_type,
        severity=severity,
        user_id=user_id,
        payload_json=payload or {},
    )
    db.add(event)
    # Flush without committing — the session commit happens in get_db dependency.
    await db.flush()


def _set_refresh_cookie(response: Response, token: str) -> None:
    """Set the refresh token as httpOnly, Secure, SameSite=Strict cookie (Req 1.15)."""
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=token,
        max_age=_REFRESH_TTL_S,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/auth",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=_REFRESH_COOKIE, path="/auth")


def _client_ip(request: Request) -> str:
    """Best-effort client IP extraction."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis=Depends(get_redis),
) -> TokenResponse:
    """
    Authenticate with email + password.
    Returns an access token and sets a refresh token cookie.
    Rate-limited to 5 attempts per 15 min per IP (Requirement 1.8).
    """
    ip = _client_ip(request)

    allowed = await check_rate_limit(redis, ip)
    if not allowed:
        ttl = await get_rate_limit_ttl(redis, ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "RATE_LIMITED", "retry_after": ttl},
        )

    user = await authenticate_user(db, body.email, body.password)

    if user is None:
        await _log_event(
            db,
            "auth.login_failed",
            EventSeverity.WARN,
            payload={"email": body.email, "ip": ip},
        )
        # Check if the account exists but is inactive (return 403 not 401)
        from sqlalchemy import select as _select

        result = await db.execute(
            _select(User).where(User.email == body.email)
        )
        existing = result.scalar_one_or_none()
        if existing and not existing.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "ACCOUNT_DISABLED"},
            )

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "INVALID_CREDENTIALS"},
        )

    access_token = create_access_token(str(user.id), user.email, user.role.value)
    refresh_token, _jti = create_refresh_token(str(user.id))

    _set_refresh_cookie(response, refresh_token)

    await _log_event(
        db,
        "auth.login",
        EventSeverity.INFO,
        user_id=str(user.id),
        payload={"ip": ip},
    )

    return TokenResponse(access_token=access_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis=Depends(get_redis),
    refresh_token: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
) -> TokenResponse:
    """
    Issue a new access token using the refresh token cookie (Requirement 1.4).
    """
    _401 = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "INVALID_REFRESH_TOKEN"},
    )

    if not refresh_token:
        raise _401

    try:
        payload = decode_token(refresh_token)
    except jwt.InvalidTokenError:
        raise _401

    if payload.get("type") != "refresh":
        raise _401

    jti = payload.get("jti")
    if not jti:
        raise _401

    if await is_blacklisted(redis, jti):
        raise _401

    user_id: str = payload["sub"]

    from sqlalchemy import select as _select

    result = await db.execute(_select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise _401

    # Rotate: blacklist the old token, issue a fresh pair
    exp = payload.get("exp", 0)
    import time

    remaining_ttl = max(int(exp - time.time()), 1)
    await blacklist_token(redis, jti, remaining_ttl)

    new_access = create_access_token(str(user.id), user.email, user.role.value)
    new_refresh, _new_jti = create_refresh_token(str(user.id))
    _set_refresh_cookie(response, new_refresh)

    return TokenResponse(access_token=new_access)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis=Depends(get_redis),
    current_user: User = Depends(get_current_user),
    refresh_token: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
) -> None:
    """
    Invalidate the refresh token server-side via Redis blacklist (Requirement 1.5).
    """
    if refresh_token:
        try:
            payload = decode_token(refresh_token)
            jti = payload.get("jti")
            exp = payload.get("exp", 0)
            if jti:
                import time

                remaining_ttl = max(int(exp - time.time()), 1)
                await blacklist_token(redis, jti, remaining_ttl)
        except jwt.InvalidTokenError:
            pass  # Already invalid — still clear the cookie

    _clear_refresh_cookie(response)

    await _log_event(
        db,
        "auth.logout",
        EventSeverity.INFO,
        user_id=str(current_user.id),
        payload={},
    )


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)) -> UserResponse:
    """Return the authenticated user's profile."""
    return UserResponse.model_validate(current_user)
