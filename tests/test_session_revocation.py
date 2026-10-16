"""Mocked access-session logout and dashboard ticket revocation tests."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.responses import Response

from app.auth.dependencies import AuthenticatedSession, get_current_session
from app.auth.jwt import create_access_token, create_refresh_token
from app.auth.router import logout, refresh_token
from app.dashboard import router as dashboard_router
from app.db.models import UserRole


def access_session(admin, session_id="session-a", *, expires_at=None):
    return AuthenticatedSession(
        user=admin,
        session_id=session_id,
        expires_at=expires_at or int(time.time()) + 300,
    )


class FakeResult:
    def __init__(self, value): self.value = value
    def scalar_one_or_none(self): return self.value


class FakeDB:
    def __init__(self, user): self.user = user; self.added = []
    async def execute(self, _statement): return FakeResult(self.user)
    def add(self, value): self.added.append(value)
    async def flush(self): return None


class FakeRedis:
    def __init__(self): self.values = {}; self.fail = False

    async def exists(self, key):
        if self.fail: raise ConnectionError("isolated Redis outage")
        return int(key in self.values)

    async def setex(self, key, ttl, value):
        if self.fail: raise ConnectionError("isolated Redis outage")
        self.values[key] = (value, ttl)

    async def eval(self, script, numkeys, *args):
        if self.fail: raise ConnectionError("isolated Redis outage")
        if "SET',KEYS[2]" in script:
            revoked_key, ticket_key, payload, ttl = args
            if revoked_key in self.values:
                return -1
            if ticket_key in self.values:
                return 0
            self.values[ticket_key] = (payload, int(ttl))
            return 1
        ticket_key = args[0]
        record = self.values.pop(ticket_key, None)
        if record is None:
            return None
        payload = json.loads(record[0])
        if f"auth:session-revoked:{payload['session_id']}" in self.values:
            return None
        return record[0]


@pytest.mark.asyncio
async def test_logout_revokes_access_and_refresh_and_blocks_outstanding_ticket(monkeypatch):
    admin = SimpleNamespace(id="admin-1", is_active=True, role=UserRole.ADMIN)
    sid = "session-logout"
    access = create_access_token(admin.id, "admin@example.test", "ADMIN", sid)
    refresh, refresh_jti = create_refresh_token(admin.id, sid)
    session = access_session(admin, sid)
    redis = FakeRedis()
    monkeypatch.setattr(dashboard_router, "get_redis", AsyncMock(return_value=redis))
    response = Response()

    # A ticket minted before logout must fail redemption afterward.
    ticket_response = await dashboard_router.create_dashboard_ws_ticket(session, redis)
    await logout(response, FakeDB(admin), redis, session, refresh)

    assert f"auth:session-revoked:{sid}" in redis.values
    assert f"auth:blacklist:{refresh_jti}" in redis.values
    assert response.headers.get("set-cookie")

    with pytest.raises(HTTPException) as issue_error:
        await dashboard_router.create_dashboard_ws_ticket(session, redis)
    assert issue_error.value.status_code == 401

    with pytest.raises(HTTPException) as refresh_error:
        await refresh_token(Response(), FakeDB(admin), redis, refresh)
    assert refresh_error.value.status_code == 401

    ws = SimpleNamespace(accepted=False, close_code=None)
    async def accept(): ws.accepted = True
    async def close(code=None): ws.close_code = code
    ws.accept = accept
    ws.close = close
    redeemed = await dashboard_router._ws_auth(ws, ticket_response["ticket"])
    assert redeemed is None
    assert ws.close_code == 4003

    # The same access JWT is rejected by the normal HTTP authentication dependency.
    from fastapi.security import HTTPAuthorizationCredentials
    with pytest.raises(HTTPException) as revoked:
        await get_current_session(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=access),
            FakeDB(admin), redis,
        )
    assert revoked.value.status_code == 401


@pytest.mark.asyncio
async def test_authentication_and_ticket_operations_fail_closed_when_redis_is_down():
    admin = SimpleNamespace(id="admin-1", is_active=True, role=UserRole.ADMIN)
    token = create_access_token(admin.id, "admin@example.test", "ADMIN", "session-down")
    from fastapi.security import HTTPAuthorizationCredentials
    redis = FakeRedis()
    redis.fail = True

    with pytest.raises(HTTPException) as auth_error:
        await get_current_session(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
            FakeDB(admin), redis,
        )
    assert auth_error.value.status_code == 503

    refresh, _ = create_refresh_token(admin.id, "session-down")
    with pytest.raises(HTTPException) as refresh_error:
        await refresh_token(Response(), FakeDB(admin), redis, refresh)
    assert refresh_error.value.status_code == 503

    ws = SimpleNamespace(accepted=False, close_code=None)
    async def accept(): ws.accepted = True
    async def close(code=None): ws.close_code = code
    ws.accept = accept
    ws.close = close
    assert await dashboard_router._ws_auth(ws, "ticket") is None
    assert ws.close_code == 1013


@pytest.mark.asyncio
async def test_logout_fails_closed_without_clearing_cookie_when_redis_is_down():
    admin = SimpleNamespace(id="admin-1", is_active=True, role=UserRole.ADMIN)
    response = Response()
    redis = FakeRedis()
    redis.fail = True
    with pytest.raises(HTTPException) as error:
        await logout(
            response, FakeDB(admin), redis,
            access_session(admin, "session-unavailable"), None,
        )
    assert error.value.status_code == 503
    assert response.headers.get("set-cookie") is None


@pytest.mark.asyncio
async def test_fresh_login_gets_a_distinct_unrevoked_session():
    admin = SimpleNamespace(id="admin-1", is_active=True, role=UserRole.ADMIN)
    old_session = "old-login"
    awaitable_tokens = (
        create_access_token(admin.id, "admin@example.test", "ADMIN", old_session),
        create_access_token(admin.id, "admin@example.test", "ADMIN", "fresh-login"),
    )
    assert awaitable_tokens[0] != awaitable_tokens[1]
    redis = FakeRedis()
    await redis.setex("auth:session-revoked:old-login", 3600, "1")
    from fastapi.security import HTTPAuthorizationCredentials

    with pytest.raises(HTTPException):
        await get_current_session(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=awaitable_tokens[0]),
            FakeDB(admin), redis,
        )
    fresh = await get_current_session(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=awaitable_tokens[1]),
        FakeDB(admin), redis,
    )
    assert fresh.session_id == "fresh-login"


@pytest.mark.asyncio
async def test_active_dashboard_socket_closes_after_session_revocation(monkeypatch):
    admin = SimpleNamespace(id="admin-1", is_active=True, role=UserRole.ADMIN)
    session = access_session(admin, "revoked-while-connected")
    monkeypatch.setattr(dashboard_router, "_ws_auth", AsyncMock(return_value=session))
    monkeypatch.setattr(dashboard_router.hub, "connect", AsyncMock())
    monkeypatch.setattr(dashboard_router.hub, "disconnect", lambda _ws: None)
    class RevokedDuringSocket(FakeRedis):
        def __init__(self): super().__init__(); self.checks = 0
        async def exists(self, _key):
            self.checks += 1
            return int(self.checks >= 2)

    redis = RevokedDuringSocket()
    monkeypatch.setattr(dashboard_router, "get_redis", AsyncMock(return_value=redis))

    class Socket:
        def __init__(self): self.close_code = None
        async def close(self, code=None): self.close_code = code
        async def receive_text(self): await asyncio.Event().wait()

    socket = Socket()
    await dashboard_router.dashboard_ws(socket, "already-authenticated-ticket")
    assert socket.close_code == 4001
    assert redis.checks >= 2
