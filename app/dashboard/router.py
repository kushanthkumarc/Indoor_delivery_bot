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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, require_role
from app.auth.robot_auth import validate_robot_api_key
from app.core.config import settings
from app.db.models import (
    ControlMode,
    Robot,
    SafetyState,
    User,
    UserRole,
)
from app.db.session import get_db
from app.dashboard.service import RobotStatusService, status_service
from app.dashboard.ws_hub import DashboardWSHub, hub

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard"])

_admin_dep = Depends(require_role(UserRole.ADMIN))


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
    )


# ---------------------------------------------------------------------------
# WebSocket — WS /ws/dashboard  (dashboard clients, ADMIN only)
# ---------------------------------------------------------------------------


async def _ws_auth(websocket: WebSocket, token: str | None = Query(default=None)) -> User | None:
    """
    Validate a JWT passed as ?token= query param for WebSocket connections.
    Returns None and closes the socket on failure.
    """
    if not token:
        await websocket.close(code=4001)
        return None

    import jwt as _jwt
    from app.auth.jwt import decode_token
    from app.db.session import AsyncSessionLocal

    try:
        payload = decode_token(token)
    except _jwt.InvalidTokenError:
        await websocket.close(code=4001)
        return None

    if payload.get("type") != "access":
        await websocket.close(code=4001)
        return None

    if payload.get("role") != UserRole.ADMIN.value:
        await websocket.close(code=4003)
        return None

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == payload.get("sub")))
        user = result.scalar_one_or_none()
        if user is None or not user.is_active or user.role != UserRole.ADMIN:
            await websocket.close(code=4003)
            return None

    return user


@router.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    """
    WebSocket endpoint for ADMIN dashboard clients.

    On connect: sends current robot state snapshot (Req 3.7).
    Keeps connection open and broadcasts all robot events.
    """
    user = await _ws_auth(websocket, token)
    if user is None:
        return

    await hub.connect(websocket)
    try:
        while True:
            # Keep the connection alive; dashboard clients are read-only listeners.
            # Any message from the client is discarded (UI pings etc.).
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(websocket)


# ---------------------------------------------------------------------------
# WebSocket — WS /ws/robot  (robot → backend telemetry channel)
# ---------------------------------------------------------------------------


@router.websocket("/ws/robot")
async def robot_ws(
    websocket: WebSocket,
    api_key: str | None = Query(default=None, alias="api_key"),
) -> None:
    """
    Inbound WebSocket for the robot to push telemetry.

    Authenticated via X-Robot-API-Key or ?api_key= query parameter.
    Handles: heartbeat, pose_update, mode_change, safety_update.

    Requirements: 3.1 (heartbeat), 3.4 (mode), 3.5 (pose), 3.6 (safety)
    """
    # Authenticate robot via query param api_key (WS can't set headers easily)
    if not api_key or api_key != settings.ROBOT_API_KEY:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    logger.info("Robot connected via WebSocket")

    # Fetch the robot record (we assume single robot)
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Robot).limit(1))
        robot = result.scalar_one_or_none()

    if robot is None:
        await websocket.close(code=4004)
        return

    robot_id = str(robot.id)

    try:
        while True:
            data: dict[str, Any] = await websocket.receive_json()
            await _handle_robot_message(robot_id, data)
    except WebSocketDisconnect:
        logger.info("Robot WebSocket disconnected")
    except Exception:
        logger.exception("Unexpected error on robot WebSocket")
    finally:
        pass  # Safety manager on_ws_disconnect wiring done in task 9


async def _handle_robot_message(robot_id: str, data: dict[str, Any]) -> None:
    """
    Dispatch incoming robot telemetry messages to the appropriate handler.

    Message format: { "type": "<msg_type>", "payload": { ... } }
    """
    msg_type: str = data.get("type", "")
    payload: dict[str, Any] = data.get("payload", {})

    if msg_type == "robot.heartbeat":
        # Requirement 3.1 — update last_seen and keep ONLINE status
        battery = payload.get("battery_level")
        await status_service.update_heartbeat(robot_id)
        if battery is not None:
            await _update_battery(robot_id, float(battery))

    elif msg_type == "robot.pose_update":
        # Requirement 3.5 — store and broadcast pose
        await status_service.update_pose(
            robot_id,
            x=float(payload.get("x", 0)),
            y=float(payload.get("y", 0)),
            theta=float(payload.get("theta", 0)),
        )

    elif msg_type == "robot.mode_change":
        # Requirement 3.4 — persist and broadcast control mode
        try:
            mode = ControlMode(payload.get("mode"))
            await status_service.update_control_mode(robot_id, mode)
        except ValueError:
            logger.warning("Unknown control mode received: %s", payload.get("mode"))

    elif msg_type == "robot.safety_update":
        # Requirement 3.6 — persist and broadcast safety state
        try:
            state = SafetyState(payload.get("safety_state"))
            await status_service.update_safety_state(robot_id, state)
        except ValueError:
            logger.warning("Unknown safety state received: %s", payload.get("safety_state"))

    else:
        logger.debug("Unhandled robot message type: %s", msg_type)


async def _update_battery(robot_id: str, level: float) -> None:
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Robot).where(Robot.id == robot_id))
        robot = result.scalar_one_or_none()
        if robot:
            robot.battery_level = level
            await db.commit()
