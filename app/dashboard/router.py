"""
Dashboard REST and WebSocket endpoints — Module 2.

GET  /robot/status  — full robot state snapshot (ADMIN only)
WS   /ws/dashboard  — real-time robot events hub (ADMIN only)

The WebSocket endpoint also handles inbound messages from the robot
(heartbeat, pose updates, mode/safety state changes).

Requirements: 3.3, 3.4, 3.5, 3.6
"""

from __future__ import annotations

import logging
import secrets
import asyncio
import json
import time
from datetime import datetime, timezone
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedSession, require_role, require_session_role
from app.auth.service import is_session_revoked
from app.auth.robot_auth import authenticate_robot_credentials
from app.core.config import settings
from app.db.models import (
    Robot,
    User,
    UserRole,
)
from app.db.session import get_db
from app.db.redis import get_redis
from app.dashboard.service import RobotStatusService, status_service
from app.dashboard.ws_hub import DashboardWSHub, hub
from app.gateway.protocol import MessageOrderGuard, PROTOCOL_VERSION, TelemetryPayload, parse_envelope
from app.gateway.navigation import gateway_navigation

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard"])
_gateway_order_guard = MessageOrderGuard()

_admin_dep = Depends(require_role(UserRole.ADMIN))
_admin_session_dep = Depends(require_session_role(UserRole.ADMIN))
_DASHBOARD_TICKET_PREFIX = "dashboard-ws-ticket:"
_DASHBOARD_TICKET_TTL_S = 30
_ISSUE_DASHBOARD_TICKET_LUA = (
    "if redis.call('EXISTS',KEYS[1]) == 1 then return -1 end; "
    "return redis.call('SET',KEYS[2],ARGV[1],'EX',ARGV[2],'NX') and 1 or 0"
)
_REDEEM_DASHBOARD_TICKET_LUA = (
    "local value=redis.call('GET',KEYS[1]); "
    "if not value then return nil end; "
    "redis.call('DEL',KEYS[1]); "
    "local data=cjson.decode(value); "
    "if redis.call('EXISTS','auth:session-revoked:'..data.session_id) == 1 then return nil end; "
    "return value"
)


@router.post("/dashboard/ws-ticket")
async def create_dashboard_ws_ticket(
    session: AuthenticatedSession = _admin_session_dep,
    redis=Depends(get_redis),
) -> dict[str, str | int]:
    """Mint a short-lived one-use ticket for browser WebSocket authentication."""
    remaining = session.expires_at - int(time.time())
    if remaining <= 0:
        raise HTTPException(status_code=401, detail={"error": "TOKEN_EXPIRED"})
    ttl = min(_DASHBOARD_TICKET_TTL_S, remaining)
    ticket = secrets.token_urlsafe(32)
    value = json.dumps({
        "user_id": str(session.user.id),
        "session_id": session.session_id,
        "expires_at": session.expires_at,
    })
    try:
        created = await redis.eval(
            _ISSUE_DASHBOARD_TICKET_LUA,
            2,
            f"auth:session-revoked:{session.session_id}",
            f"{_DASHBOARD_TICKET_PREFIX}{ticket}",
            value,
            ttl,
        )
    except Exception:
        raise HTTPException(status_code=503, detail={"error": "AUTH_UNAVAILABLE"}) from None
    if created == -1:
        raise HTTPException(status_code=401, detail={"error": "SESSION_REVOKED"})
    if created != 1:
        raise HTTPException(status_code=503, detail={"error": "AUTH_UNAVAILABLE"})
    return {"ticket": ticket, "expires_in": ttl}


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class RobotStatusResponse(BaseModel):
    id: str
    name: str
    status: str
    control_mode: str
    safety_state: str
    slam_state: str
    battery_level: float | None
    last_seen: str | None
    firmware_version: str | None
    connection_state: str = "OFFLINE"


# ---------------------------------------------------------------------------
# REST — GET /robot/status
# ---------------------------------------------------------------------------


@router.get("/robot/status", response_model=RobotStatusResponse)
async def get_robot_status(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: User = _admin_dep,
) -> RobotStatusResponse:
    """
    Return the full current robot state snapshot.

    Requirement 3.3 — returns online status, control mode, safety state,
    SLAM state, battery level, last_seen.
    """
    result = await db.execute(select(Robot).limit(1))
    robot = result.scalar_one_or_none()
    if robot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "NO_ROBOT_REGISTERED"},
        )

    return RobotStatusResponse(
        id=str(robot.id),
        name=robot.name,
        status=robot.status.value,
        control_mode=robot.control_mode.value,
        safety_state=robot.safety_state.value,
        slam_state=robot.slam_state.value,
        battery_level=robot.battery_level,
        last_seen=robot.last_seen.isoformat() if robot.last_seen else None,
        firmware_version=robot.firmware_version,
        connection_state=status_service.connection_freshness(str(robot.id), robot.last_seen),
    )


# ---------------------------------------------------------------------------
# WebSocket — WS /ws/dashboard  (dashboard clients, ADMIN only)
# ---------------------------------------------------------------------------


async def _ws_auth(websocket: WebSocket, ticket: str | None = None) -> AuthenticatedSession | None:
    """
    Validate a short-lived, one-use Redis ticket (never a long-lived JWT).
    Returns None and closes the socket on failure.

    Starlette requires the socket to be accepted before websocket.close()
    can send a proper close frame; otherwise it returns HTTP 403 on the
    WebSocket handshake. So we accept() first, then validate, then close()
    if the token is invalid.
    """
    await websocket.accept()

    if not ticket or len(ticket) > 128:
        await websocket.close(code=4001)
        return None
    from app.db.session import AsyncSessionLocal

    try:
        redis = await get_redis()
        ticket_value = await redis.eval(
            _REDEEM_DASHBOARD_TICKET_LUA,
            1,
            f"{_DASHBOARD_TICKET_PREFIX}{ticket}",
        )
    except Exception:
        await websocket.close(code=1013)
        return None
    if not ticket_value:
        await websocket.close(code=4003)
        return None

    try:
        ticket_data = json.loads(ticket_value)
        user_id = ticket_data["user_id"]
        session_id = ticket_data["session_id"]
        expires_at = int(ticket_data["expires_at"])
    except (ValueError, TypeError, KeyError):
        await websocket.close(code=4003)
        return None
    if expires_at <= int(time.time()):
        await websocket.close(code=4001)
        return None

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
    except Exception as exc:
        logger.warning("Dashboard WebSocket user lookup failed (%s)", type(exc).__name__)
        await websocket.close(code=1013)
        return None
    if user is None or not user.is_active or user.role != UserRole.ADMIN:
        await websocket.close(code=4003)
        return None

    return AuthenticatedSession(user=user, session_id=session_id, expires_at=expires_at)


@router.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket, ticket: str | None = Query(default=None)) -> None:
    """
    WebSocket endpoint for ADMIN dashboard clients.

    On connect: sends current robot state snapshot (Req 3.7).
    Keeps connection open and broadcasts all robot events.
    """
    session = await _ws_auth(websocket, ticket)
    if session is None:
        return

    await hub.connect(websocket)
    try:
        while True:
            # Periodically recheck the Redis session marker. This bounds logout
            # termination delay without claiming instantaneous cross-instance closure.
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            try:
                if session.expires_at <= int(time.time()) or await is_session_revoked(
                    await get_redis(), session.session_id
                ):
                    await websocket.close(code=4001)
                    break
            except Exception:
                await websocket.close(code=1013)
                break
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(websocket)


# ---------------------------------------------------------------------------
# WebSocket — WS /ws/robot (authenticated versioned gateway protocol)
# ---------------------------------------------------------------------------


@router.websocket("/ws/robot")
async def robot_ws(
    websocket: WebSocket,
) -> None:
    """
    Credentials are supplied in X-Robot-ID and X-Robot-API-Key headers. The
    first authenticated protocol message must be a v1 hello. Credentials in
    query parameters are explicitly rejected.
    """
    if "api_key" in websocket.query_params or "token" in websocket.query_params:
        await websocket.close(code=4401)
        return
    robot_id = websocket.headers.get("x-robot-id")
    api_key = websocket.headers.get("x-robot-api-key")
    if not robot_id or not api_key:
        await websocket.close(code=4401)
        return

    from app.db.session import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            robot = await authenticate_robot_credentials(robot_id, api_key, db)
    except Exception as exc:
        logger.warning("Robot gateway authentication lookup failed (%s)", type(exc).__name__)
        await websocket.close(code=1013)
        return
    if robot is None:
        # Do not reveal whether the ID or credential was invalid.
        await websocket.close(code=4401)
        return
    robot_id = str(robot.id)
    await websocket.accept()
    connected = False
    connection_id = str(uuid.uuid4())

    try:
        try:
            raw = await websocket.receive_text()
            hello = parse_envelope(raw, max_bytes=settings.ROBOT_GATEWAY_MAX_MESSAGE_BYTES)
        except ValueError as exc:
            detail = "UNSUPPORTED_VERSION" if "version" in str(exc) else "INVALID_MESSAGE"
            await _gateway_error(websocket, robot_id, detail)
            await websocket.close(code=4400 if detail == "INVALID_MESSAGE" else 4406)
            return
        if hello.type != "hello" or hello.robot_id != robot_id or not _gateway_order_guard.accept(hello):
            await _gateway_error(websocket, robot_id, "HELLO_REQUIRED")
            await websocket.close(code=4400)
            return
        connected = True
        if not await status_service.gateway_connected(robot_id, connection_id):
            connected = False
            await websocket.send_json({
                "version": PROTOCOL_VERSION, "type": "error",
                "message_id": str(uuid.uuid4()), "robot_id": robot_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": {"code": "ROBOT_ALREADY_CONNECTED"},
            })
            await websocket.close(code=4409)
            return
        send_lock = asyncio.Lock()

        async def _send_to_robot(envelope: dict) -> None:
            async with send_lock:
                await websocket.send_json(envelope)

        if settings.ROBOT_COMMAND_CHANNEL_ENABLED:
            gateway_navigation.register_connection(robot_id, connection_id, _send_to_robot)
        await websocket.send_json({
            "version": PROTOCOL_VERSION,
            "type": "hello_ack",
            "message_id": str(uuid.uuid4()),
            "robot_id": robot_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {"accepted": True},
        })

        while True:
            raw = await websocket.receive_text()
            try:
                message = parse_envelope(raw, max_bytes=settings.ROBOT_GATEWAY_MAX_MESSAGE_BYTES)
            except ValueError as exc:
                detail = "UNSUPPORTED_VERSION" if "version" in str(exc) else "INVALID_MESSAGE"
                await _gateway_error(websocket, robot_id, detail)
                await websocket.close(code=4400 if detail == "INVALID_MESSAGE" else 4406)
                return
            if message.robot_id != robot_id:
                await _gateway_error(websocket, robot_id, "ROBOT_ID_MISMATCH")
                await websocket.close(code=4403)
                return
            if message.type == "hello":
                await _gateway_error(websocket, robot_id, "UNAUTHORIZED_MESSAGE_TYPE")
                continue
            if not _gateway_order_guard.accept(message):
                await _gateway_error(websocket, robot_id, "DUPLICATE_OR_OUT_OF_ORDER")
                continue
            if message.type == "heartbeat":
                await status_service.update_heartbeat(robot_id)
            elif message.type == "telemetry":
                telemetry = TelemetryPayload.model_validate(message.payload)
                await status_service.update_gateway_telemetry(robot_id, telemetry.model_dump(exclude_none=True))
            elif message.type in {"command_ack", "command_result"}:
                # Protocol reports only; they never confirm physical execution.
                if settings.ROBOT_COMMAND_CHANNEL_ENABLED:
                    await gateway_navigation.handle_report(robot_id, message.type, message.payload)
                await hub.broadcast("robot.command_protocol_message", {
                    "robot_id": robot_id, "type": message.type,
                    "message_id": message.message_id, "payload": message.payload,
                    "physical_execution_confirmed": False,
                })
            elif message.type == "error":
                await hub.broadcast("robot.gateway_error", {
                    "robot_id": robot_id, "payload": message.payload,
                })
    except WebSocketDisconnect:
        logger.info("Robot gateway disconnected: robot_id=%s", robot_id)
    except Exception as exc:
        # Avoid logging inbound data, headers, or credentials.
        logger.warning("Robot gateway protocol/session error for robot_id=%s (%s)", robot_id, type(exc).__name__)
        try:
            await _gateway_error(websocket, robot_id, "GATEWAY_ERROR")
        except Exception:
            pass
    finally:
        if connected:
            gateway_navigation.unregister_connection(robot_id, connection_id)
            await status_service.gateway_disconnected(robot_id, connection_id)


async def _gateway_error(websocket: WebSocket, robot_id: str, code: str) -> None:
    await websocket.send_json({
        "version": PROTOCOL_VERSION,
        "type": "error",
        "message_id": str(uuid.uuid4()),
        "robot_id": robot_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {"code": code},
    })
