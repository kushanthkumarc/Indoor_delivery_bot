"""
FastAPI dependencies for JWT authentication and role-based access control.

Requirements: 1.6, 1.7
"""

from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.jwt import decode_token
from app.auth.service import is_session_revoked
from app.db.redis import get_redis
from app.db.models import User, UserRole
from app.db.session import AsyncSession, get_db
from sqlalchemy import select

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthenticatedSession:
    user: User
    session_id: str
    expires_at: int


async def get_current_session(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis=Depends(get_redis),
) -> AuthenticatedSession:
    """
    Extract and validate the Bearer JWT; return the authenticated User.

    Raises HTTP 401 if token is missing, expired, or invalid (Requirement 1.6).
    Raises HTTP 403 if the account is deactivated (Requirement 1.13).
    """
    _401 = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "UNAUTHORIZED"},
        headers={"WWW-Authenticate": "Bearer"},
    )

    if credentials is None:
        raise _401

    try:
        payload = decode_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "TOKEN_EXPIRED"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise _401

    if payload.get("type") != "access":
        raise _401

    session_id = payload.get("sid")
    token_id = payload.get("jti")
    expires_at = payload.get("exp")
    if not session_id or not token_id or not isinstance(expires_at, int):
        raise _401

    try:
        if await is_session_revoked(redis, session_id):
            raise _401
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "AUTH_UNAVAILABLE"},
        ) from None

    user_id: str | None = payload.get("sub")
    if not user_id:
        raise _401

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise _401

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "ACCOUNT_DISABLED"},
        )

    return AuthenticatedSession(user=user, session_id=session_id, expires_at=expires_at)


async def get_current_user(
    session: Annotated[AuthenticatedSession, Depends(get_current_session)],
) -> User:
    """Compatibility dependency for handlers that need the user model only."""
    return session.user


def require_role(*roles: UserRole):
    """
    Dependency factory: ensure the current user has one of the given roles.

    Usage: Depends(require_role(UserRole.ADMIN))
    Raises HTTP 403 for insufficient role (Requirement 1.7).
    """

    async def _check(
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> User:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "FORBIDDEN"},
            )
        return current_user

    return _check


def require_session_role(*roles: UserRole):
    """Role check that preserves the authenticated session for session-bound flows."""

    async def _check(
        session: Annotated[AuthenticatedSession, Depends(get_current_session)],
    ) -> AuthenticatedSession:
        if session.user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "FORBIDDEN"},
            )
        return session

    return _check


def require_admin():
    """Shorthand dependency for ADMIN-only routes."""
    return require_role(UserRole.ADMIN)
