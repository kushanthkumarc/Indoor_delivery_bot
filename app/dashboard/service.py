"""
Robot Status Service — Module 2.

Handles heartbeat updates, offline detection, and provides the robot
state snapshot used by the dashboard hub.

Requirements: 3.1, 3.2
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import (
    ControlMode,
    EventSeverity,
    Robot,
    RobotStatus,
    SafetyState,
    SlamState,
)
from app.db.redis import get_redis
from app.db.session import AsyncSessionLocal
from app.events.service import log_event

logger = logging.getLogger(__name__)

_HEARTBEAT_KEY_PREFIX = "robot:heartbeat:"
_HEARTBEAT_TIMEOUT_S = settings.HEARTBEAT_TIMEOUT_S  # default 5s


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _robot_to_dict(robot: Robot) -> dict[str, Any]:
    """Serialise a Robot ORM instance to a plain dict for WS / REST responses."""
    return {
        "id": str(robot.id),
        "name": robot.name,
        "status": robot.status.value,
        "control_mode": robot.control_mode.value,
        "safety_state": robot.safety_state.value,
        "slam_state": robot.slam_state.value,
        "battery_level": robot.battery_level,
        "last_seen": robot.last_seen.isoformat() if robot.last_seen else None,
        "firmware_version": robot.firmware_version,
    }


class RobotStatusService:
    """
    Service layer for robot status management.

    Methods are called from the WebSocket route handler (heartbeat,
    pose updates, mode/safety changes) and from the background heartbeat
    monitor task.
    """

    def __init__(self, hub: Any) -> None:
        # hub is DashboardWSHub — typed as Any to avoid circular import at module level
        self._hub = hub
        self._monitor_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def update_heartbeat(self, robot_id: str) -> None:
        """
        Record a heartbeat: update last_seen in DB and refresh the Redis TTL.

        Requirement 3.1 — heartbeat updates last_seen timestamp.
        """
        now = _utcnow()
        redis: Redis = await get_redis()

        # Update Redis key with TTL slightly longer than the timeout window
        # so we can detect stale entries without relying on key expiry alone.
        await redis.setex(
            f"{_HEARTBEAT_KEY_PREFIX}{robot_id}",
            _HEARTBEAT_TIMEOUT_S * 2,
            now.isoformat(),
        )

        async with AsyncSessionLocal() as db:
            robot = await _get_robot(db, robot_id)
            if robot is None:
                return
            was_offline = robot.status == RobotStatus.OFFLINE
            robot.last_seen = now
            if was_offline:
                robot.status = RobotStatus.ONLINE
            await db.commit()
            await db.refresh(robot)

            if was_offline:
                await self._hub.broadcast("robot.online", _robot_to_dict(robot))

    # ------------------------------------------------------------------
    # Field updates
    # ------------------------------------------------------------------

    async def update_control_mode(self, robot_id: str, mode: ControlMode) -> None:
        """
        Persist a new control mode and broadcast it to dashboard clients.

        Requirement 3.4
        """
        async with AsyncSessionLocal() as db:
            robot = await _get_robot(db, robot_id)
            if robot is None:
                return
            robot.control_mode = mode
            await db.commit()
            await db.refresh(robot)

        await self._hub.broadcast("robot.mode_change", {"mode": mode.value})

    async def update_safety_state(self, robot_id: str, state: SafetyState) -> None:
        """
        Persist a new safety state and broadcast it to dashboard clients.

        Requirement 3.6
        """
        async with AsyncSessionLocal() as db:
            robot = await _get_robot(db, robot_id)
            if robot is None:
                return
            robot.safety_state = state
            await db.commit()
            await db.refresh(robot)

        await self._hub.broadcast(
            "robot.safety_update",
            {"safety_state": state.value, "timestamp": _utcnow().isoformat()},
        )

    async def update_pose(self, robot_id: str, x: float, y: float, theta: float) -> None:
        """
        Store the latest pose and forward to subscribed WebSocket clients.

        Requirement 3.5
        """
        pose_payload = {
            "x": x,
            "y": y,
            "theta": theta,
            "timestamp": _utcnow().isoformat(),
        }
        await self._hub.broadcast("robot.pose_update", pose_payload)

    # ------------------------------------------------------------------
    # Snapshot (used by hub.send_snapshot on connect — Req 3.7)
    # ------------------------------------------------------------------

    async def get_robot_snapshot(self) -> dict[str, Any]:
        """
        Return a full robot state dict for the initial WS snapshot.

        Requirement 3.3, 3.7
        """
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Robot).limit(1))
            robot = result.scalar_one_or_none()
            if robot is None:
                return {}
            return _robot_to_dict(robot)

    # ------------------------------------------------------------------
    # Background heartbeat timeout monitor
    # ------------------------------------------------------------------

    async def check_heartbeat_timeouts(self) -> None:
        """
        Background task running every 1 second.

        Marks robots OFFLINE and broadcasts robot.offline if no heartbeat
        was received within HEARTBEAT_TIMEOUT_S seconds (Requirement 3.1).
        """
        while True:
            try:
                await asyncio.sleep(1)
                await self._check_all_robots()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in heartbeat timeout monitor")

    async def _check_all_robots(self) -> None:
        redis: Redis = await get_redis()
        now = _utcnow()

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Robot).where(Robot.status == RobotStatus.ONLINE)
            )
            online_robots = result.scalars().all()

            for robot in online_robots:
                timed_out = False

                # Check Redis key first (fast path)
                key = f"{_HEARTBEAT_KEY_PREFIX}{robot.id}"
                last_seen_str: str | None = await redis.get(key)

                if last_seen_str is None:
                    # Key expired — rely on DB last_seen
                    timed_out = True
                else:
                    try:
                        last_seen_dt = datetime.fromisoformat(last_seen_str)
                        elapsed = (now - last_seen_dt).total_seconds()
                        if elapsed > _HEARTBEAT_TIMEOUT_S:
                            timed_out = True
                    except ValueError:
                        timed_out = True

                # Also check DB last_seen as a fallback
                if not timed_out and robot.last_seen:
                    elapsed_db = (now - robot.last_seen).total_seconds()
                    if elapsed_db > _HEARTBEAT_TIMEOUT_S:
                        timed_out = True

                if timed_out:
                    robot.status = RobotStatus.OFFLINE
                    await log_event(
                        db,
                        type="robot.offline",
                        severity=EventSeverity.WARN,
                        robot_id=str(robot.id),
                        payload={
                            "last_seen": robot.last_seen.isoformat()
                            if robot.last_seen
                            else None
                        },
                    )
                    await db.commit()
                    await self._hub.broadcast(
                        "robot.offline",
                        {"robot_id": str(robot.id), "last_seen": robot.last_seen.isoformat() if robot.last_seen else None},
                    )

    def start_monitor(self) -> None:
        """Schedule the heartbeat timeout monitor as an asyncio background task."""
        self._monitor_task = asyncio.create_task(
            self.check_heartbeat_timeouts(), name="heartbeat_monitor"
        )

    def stop_monitor(self) -> None:
        """Cancel the background monitor on shutdown."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


async def _get_robot(db: AsyncSession, robot_id: str) -> Robot | None:
    result = await db.execute(select(Robot).where(Robot.id == robot_id))
    return result.scalar_one_or_none()


# Module-level singleton — wired to the hub singleton in router.py
from app.dashboard.ws_hub import hub as _hub  # noqa: E402

status_service = RobotStatusService(hub=_hub)
_hub.set_status_service(status_service)
