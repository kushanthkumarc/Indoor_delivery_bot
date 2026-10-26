"""Opt-in integration checks using a disposable, loopback-only Redis database.

Start a local Redis on 127.0.0.1:16379 and set
TEST_REDIS_URL=redis://127.0.0.1:16379/15. The fixture flushes only database 15.
It refuses remote hosts and any other port/database to prevent accidental use
of shared or production Redis.
"""
import asyncio
import json
import os
import time
from http.cookies import SimpleCookie
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
import pytest_asyncio
import redis.asyncio as redis_async
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.responses import Response

from app.auth.dependencies import AuthenticatedSession, get_current_session
from app.auth.jwt import create_access_token, decode_token
from app.auth.router import LoginRequest, login, logout, refresh_token
from app.auth.service import hash_password
from app.dashboard import router as dashboard_router
from app.db.models import UserRole


_URL = os.getenv("TEST_REDIS_URL")
if not _URL:
    pytest.skip("set TEST_REDIS_URL to a disposable local Redis DB 15", allow_module_level=True)
_parsed = urlparse(_URL)
if _parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or _parsed.port != 16379 or _parsed.path != "/15":
    raise RuntimeError("TEST_REDIS_URL must target loopback Redis at port 16379, database 15")


@pytest_asyncio.fixture
async def redis_client():
    client = redis_async.from_url(_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        try:
            await client.flushdb()
        except redis_async.RedisError:
            pass  # A failure-path test may already have closed this client.
        await client.aclose()


class _Result:
    def __init__(self, value): self.value = value
    def scalar_one_or_none(self): return self.value


class _DB:
    def __init__(self, user): self.user = user
    async def execute(self, _query): return _Result(self.user)
    def add(self, _event): pass
    async def flush(self): pass


class _Socket:
    def __init__(self): self.accepted = False; self.close_code = None
    async def accept(self): self.accepted = True
    async def close(self, code=None): self.close_code = code


def _admin():
    return SimpleNamespace(id="redis-integration-admin", email="admin@example.com",
                           name="Admin", is_active=True, role=UserRole.ADMIN)


@pytest.mark.asyncio
async def test_login_refresh_logout_revokes_access_and_outstanding_ticket(redis_client, monkeypatch):
    admin = _admin()
    admin.password_hash = hash_password("isolated-test-password")
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    login_response = Response()
    token_response = await login(
        LoginRequest(email=admin.email, password="isolated-test-password"),
        request, login_response, _DB(admin), redis_client,
    )
    access = token_response.access_token
    sid = decode_token(access)["sid"]
    cookie = SimpleCookie()
    cookie.load(login_response.headers["set-cookie"])
    refresh = cookie["refresh_token"].value
    session = await get_current_session(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=access), _DB(admin), redis_client,
    )

    assert session.session_id == sid

    monkeypatch.setattr(dashboard_router, "get_redis", lambda: _async_value(redis_client))
    ticket = await dashboard_router.create_dashboard_ws_ticket(session, redis_client)
    rotated_response = Response()
    rotated_tokens = await refresh_token(rotated_response, _DB(admin), redis_client, refresh)
    assert rotated_response.headers.get("set-cookie")
    rotated_cookie = SimpleCookie()
    rotated_cookie.load(rotated_response.headers["set-cookie"])
    rotated_refresh = rotated_cookie["refresh_token"].value

    with pytest.raises(HTTPException) as replay_error:
        await refresh_token(Response(), _DB(admin), redis_client, refresh)
    assert replay_error.value.status_code == 401

    await logout(Response(), _DB(admin), redis_client, session, rotated_refresh)
    with pytest.raises(HTTPException) as auth_error:
        await get_current_session(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=access), _DB(admin), redis_client,
        )
    assert auth_error.value.status_code == 401
    with pytest.raises(HTTPException) as rotated_auth_error:
        await get_current_session(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=rotated_tokens.access_token),
            _DB(admin), redis_client,
        )
    assert rotated_auth_error.value.status_code == 401

    ws = _Socket()
    assert await dashboard_router._ws_auth(ws, ticket["ticket"]) is None
    assert ws.close_code == 4003
    with pytest.raises(HTTPException) as refresh_error:
        await refresh_token(Response(), _DB(admin), redis_client, rotated_refresh)
    assert refresh_error.value.status_code == 401

    fresh_response = await login(
        LoginRequest(email=admin.email, password="isolated-test-password"),
        request, Response(), _DB(admin), redis_client,
    )
    fresh = await get_current_session(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=fresh_response.access_token),
        _DB(admin), redis_client,
    )
    assert fresh.session_id != sid


@pytest.mark.asyncio
async def test_ticket_redemption_is_atomic_single_use_under_concurrency(redis_client, monkeypatch):
    admin = _admin()

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def execute(self, _query): return _Result(admin)

    monkeypatch.setattr("app.db.session.AsyncSessionLocal", Session)
    monkeypatch.setattr(dashboard_router, "get_redis", lambda: _async_value(redis_client))
    auth = AuthenticatedSession(admin, "concurrent-session", int(time.time()) + 300)
    ticket = await dashboard_router.create_dashboard_ws_ticket(auth, redis_client)
    sockets = [_Socket(), _Socket()]
    results = await asyncio.gather(*[
        dashboard_router._ws_auth(ws, ticket["ticket"]) for ws in sockets
    ])
    assert sum(result is not None for result in results) == 1
    assert sum(ws.close_code == 4003 for ws in sockets) == 1


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_existing_dashboard_socket_closes_after_live_redis_revocation(redis_client, monkeypatch):
    admin = _admin()
    sid = "active-socket-session"

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def execute(self, _query): return _Result(admin)

    monkeypatch.setattr("app.db.session.AsyncSessionLocal", Session)
    monkeypatch.setattr(dashboard_router, "get_redis", lambda: _async_value(redis_client))
    monkeypatch.setattr(dashboard_router.hub, "connect", _async_value_none)
    monkeypatch.setattr(dashboard_router.hub, "disconnect", lambda _ws: None)
    auth = AuthenticatedSession(admin, sid, int(time.time()) + 300)
    ticket = await dashboard_router.create_dashboard_ws_ticket(auth, redis_client)

    class RevokingSocket(_Socket):
        async def receive_text(self):
            await redis_client.setex(f"auth:session-revoked:{sid}", 300, "1")
            await asyncio.Event().wait()

    socket = RevokingSocket()
    await dashboard_router.dashboard_ws(socket, ticket["ticket"])
    assert socket.close_code == 4001


@pytest.mark.asyncio
async def test_unavailable_loopback_redis_fails_auth_and_ticket_redemption_closed(redis_client, monkeypatch):
    admin = _admin()
    sid = "redis-outage-session"
    access = create_access_token(admin.id, admin.email, admin.role.value, sid)
    # Use the same real Redis client implementation against a local, unused
    # loopback port to model the service becoming unavailable.
    outage_client = redis_async.from_url(
        "redis://127.0.0.1:16380/15", decode_responses=True, socket_connect_timeout=0.2,
    )

    with pytest.raises(HTTPException) as auth_error:
        await get_current_session(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=access), _DB(admin), outage_client,
        )
    assert auth_error.value.status_code == 503

    monkeypatch.setattr(dashboard_router, "get_redis", lambda: _async_value(outage_client))
    ws = _Socket()
    assert await dashboard_router._ws_auth(ws, "outage-ticket") is None
    assert ws.close_code == 1013
    await outage_client.aclose()


async def _async_value_none(*_args, **_kwargs):
    return None
