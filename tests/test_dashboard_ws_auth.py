"""Isolated tests for dashboard WebSocket ticket authentication."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime, timedelta, timezone
import json
import time

import jwt
import pytest
import httpx
from fastapi import HTTPException

from app.dashboard import router
from app.auth.dependencies import AuthenticatedSession
from app.db.models import UserRole


class FakeWebSocket:
    def __init__(self):
        self.accepted = False
        self.close_code = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=None):
        self.close_code = code


@pytest.mark.asyncio
async def test_dashboard_rejects_missing_and_expired_token_query(monkeypatch):
    redis = SimpleNamespace(eval=AsyncMock(return_value=None))
    monkeypatch.setattr(router, "get_redis", AsyncMock(return_value=redis))

    missing = FakeWebSocket()
    assert await router._ws_auth(missing) is None
    assert missing.close_code == 4001
    redis.eval.assert_not_awaited()

    # A JWT (including an expired JWT) is not a ticket and has no Redis record.
    expired = FakeWebSocket()
    assert await router._ws_auth(expired, "expired.access.jwt") is None
    assert expired.close_code == 4003
    redis.eval.assert_awaited_once()


@pytest.mark.asyncio
async def test_valid_dashboard_ticket_is_single_use_and_rechecks_admin(monkeypatch):
    admin = SimpleNamespace(id="admin-1", is_active=True, role=UserRole.ADMIN)

    class Result:
        def scalar_one_or_none(self): return admin

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def execute(self, _query): return Result()

    class Redis:
        def __init__(self): self.available = True
        async def eval(self, *_args):
            if not self.available: return None
            self.available = False
            return json.dumps({
                "user_id": "admin-1", "session_id": "session-1",
                "expires_at": int(datetime.now(timezone.utc).timestamp()) + 60,
            })

    redis = Redis()
    monkeypatch.setattr(router, "get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", Session)

    first = FakeWebSocket()
    auth_session = await router._ws_auth(first, "one-use-ticket")
    assert auth_session.user is admin
    assert auth_session.session_id == "session-1"
    second = FakeWebSocket()
    assert await router._ws_auth(second, "one-use-ticket") is None
    assert second.close_code == 4003


@pytest.mark.asyncio
async def test_ticket_with_expired_authenticated_session_is_rejected(monkeypatch):
    redis = SimpleNamespace(eval=AsyncMock(return_value=json.dumps({
        "user_id": "admin-1", "session_id": "expired-session",
        "expires_at": int(time.time()) - 1,
    })))
    monkeypatch.setattr(router, "get_redis", AsyncMock(return_value=redis))
    ws = FakeWebSocket()
    assert await router._ws_auth(ws, "expired-session-ticket") is None
    assert ws.close_code == 4001


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [
    None,
    SimpleNamespace(id="admin-1", is_active=False, role=UserRole.ADMIN),
    SimpleNamespace(id="user-1", is_active=True, role=UserRole.USER),
])
async def test_ticket_redemption_rejects_missing_inactive_or_non_admin_user(monkeypatch, user):
    class Result:
        def scalar_one_or_none(self): return user

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def execute(self, _query): return Result()

    redis = SimpleNamespace(eval=AsyncMock(return_value=json.dumps({
        "user_id": "admin-1", "session_id": "session-1",
        "expires_at": int(time.time()) + 60,
    })))
    monkeypatch.setattr(router, "get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", Session)
    ws = FakeWebSocket()

    assert await router._ws_auth(ws, "ticket") is None
    assert ws.close_code == 4003


@pytest.mark.asyncio
async def test_ticket_minting_stores_only_user_binding_and_never_logs_ticket(monkeypatch, caplog):
    redis = SimpleNamespace(eval=AsyncMock(return_value=1))
    monkeypatch.setattr(router.secrets, "token_urlsafe", lambda _size: "isolated-ticket")
    auth_session = AuthenticatedSession(
        user=SimpleNamespace(id="admin-1", role=UserRole.ADMIN),
        session_id="session-1",
        expires_at=int(datetime.now(timezone.utc).timestamp()) + 120,
    )
    ticket = await router.create_dashboard_ws_ticket(
        auth_session, redis
    )
    assert ticket == {"ticket": "isolated-ticket", "expires_in": 30}
    redis.eval.assert_awaited_once()
    assert "isolated-ticket" not in caplog.text
    args = redis.eval.await_args.args
    assert args[1] == 2
    assert args[2] == "auth:session-revoked:session-1"
    assert args[3] == f"{router._DASHBOARD_TICKET_PREFIX}isolated-ticket"
    assert json.loads(args[4]) == {
        "user_id": "admin-1", "session_id": "session-1",
        "expires_at": auth_session.expires_at,
    }


@pytest.mark.asyncio
async def test_ticket_expiry_is_bounded_by_access_session_expiry(monkeypatch):
    redis = SimpleNamespace(eval=AsyncMock(return_value=1))
    session = AuthenticatedSession(
        user=SimpleNamespace(id="admin-1", role=UserRole.ADMIN),
        session_id="near-expiry-session",
        expires_at=int(time.time()) + 4,
    )
    result = await router.create_dashboard_ws_ticket(session, redis)
    assert 1 <= result["expires_in"] <= 4
    assert redis.eval.await_args.args[5] == result["expires_in"]


@pytest.mark.asyncio
async def test_ticket_issuance_fails_closed_on_redis_error():
    redis = SimpleNamespace(eval=AsyncMock(side_effect=ConnectionError("offline")))
    session = AuthenticatedSession(
        user=SimpleNamespace(id="admin-1", role=UserRole.ADMIN),
        session_id="redis-down-session",
        expires_at=int(time.time()) + 60,
    )
    with pytest.raises(HTTPException) as error:
        await router.create_dashboard_ws_ticket(session, redis)
    assert getattr(error.value, "status_code", None) == 503


@pytest.mark.asyncio
async def test_non_admin_cannot_mint_dashboard_ticket(monkeypatch):
    from app.auth.dependencies import get_current_session
    from app.db.redis import get_redis
    from app.main import app

    async def current_user():
        return AuthenticatedSession(
            user=SimpleNamespace(id="user-1", role=UserRole.USER),
            session_id="user-session", expires_at=int(time.time()) + 60,
        )

    async def redis_client():
        return SimpleNamespace(eval=AsyncMock(return_value=1))

    app.dependency_overrides[get_current_session] = current_user
    app.dependency_overrides[get_redis] = redis_client
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://isolated") as client:
            response = await client.post("/dashboard/ws-ticket")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_expired_access_token_cannot_mint_dashboard_ticket():
    from app.core.config import settings
    from app.main import app

    expired = jwt.encode({
        "sub": "admin-1", "type": "access", "role": "ADMIN",
        "email": "admin@example.test",
        "exp": datetime.now(timezone.utc) - timedelta(seconds=5),
    }, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://isolated") as client:
        response = await client.post(
            "/dashboard/ws-ticket", headers={"Authorization": f"Bearer {expired}"}
        )
    assert response.status_code == 401
