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
        self._gateway_connected: dict[str, str] = {}
        self._gateway_lock = asyncio.Lock()
        self._broadcasted_freshness: dict[str, str] = {}

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

            freshness = self.connection_freshness(robot_id, robot.last_seen)
            await self._broadcast_freshness(robot_id, freshness, robot.last_seen)

            if was_offline:
                await self._hub.broadcast("robot.online", _robot_to_dict(robot))

    async def gateway_connected(self, robot_id: str, connection_id: str) -> bool:
        """Record an authenticated gateway connection; heartbeat is still required."""
        async with self._gateway_lock:
            if robot_id in self._gateway_connected:
                return False
            self._gateway_connected[robot_id] = connection_id
        await self._broadcast_freshness(robot_id, "STALE", None)
        return True

    async def gateway_disconnected(self, robot_id: str, connection_id: str) -> None:
        """Mark the transport offline after its authenticated socket closes."""
        async with self._gateway_lock:
            if self._gateway_connected.get(robot_id) != connection_id:
                return
            self._gateway_connected.pop(robot_id, None)
        self._broadcasted_freshness.pop(robot_id, None)
        async with AsyncSessionLocal() as db:
            robot = await _get_robot(db, robot_id)
            if robot is not None and robot.status != RobotStatus.OFFLINE:
                robot.status = RobotStatus.OFFLINE
                await db.commit()
        await self._hub.broadcast(
            "robot.offline", {"robot_id": robot_id, "connection_state": "OFFLINE"}
        )

    async def _broadcast_freshness(self, robot_id: str, state: str,
                                   last_seen: datetime | None) -> None:
        if self._broadcasted_freshness.get(robot_id) == state:
            return
        self._broadcasted_freshness[robot_id] = state
        await self._hub.broadcast("robot.freshness", {
            "robot_id": robot_id,
            "connection_state": state,
            "last_seen": last_seen.isoformat() if last_seen else None,
        })

    def connection_freshness(self, robot_id: str, last_seen: datetime | None,
                             now: datetime | None = None) -> str:
        """Derive ONLINE/STALE/OFFLINE without changing the legacy DB enum."""
        if robot_id not in self._gateway_connected:
            return "OFFLINE"
        if last_seen is None:
            return "STALE"
        now = now or _utcnow()
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        age = max(0.0, (now - last_seen).total_seconds())
        if age > settings.HEARTBEAT_TIMEOUT_S:
            return "OFFLINE"
        if age > settings.ROBOT_STALE_AFTER_S:
            return "STALE"
        return "ONLINE"

    async def update_gateway_telemetry(self, robot_id: str, payload: dict[str, Any]) -> None:
        """Refresh freshness and mirror only fields represented by existing models."""
        await self.update_heartbeat(robot_id)
        mode = payload.get("mode")
        if mode is not None:
            await self.update_control_mode(robot_id, ControlMode(mode))
        safety = payload.get("safety_state")
        if safety is not None:
            await self.update_safety_state(robot_id, SafetyState(safety))
        pose = payload.get("pose")
        if pose is not None:
            await self.update_pose(robot_id, pose["x"], pose["y"], pose["theta"])
        battery = payload.get("battery_level")
        if battery is not None:
            async with AsyncSessionLocal() as db:
                robot = await _get_robot(db, robot_id)
                if robot is not None:
                    robot.battery_level = battery
                    await db.commit()
        # Preserve all supplied values (and absent/unknown fields) for dashboards.
        await self._hub.broadcast(
            "robot.telemetry", {"robot_id": robot_id, **payload}
        )

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
            snapshot = _robot_to_dict(robot)
            snapshot["connection_state"] = self.connection_freshness(str(robot.id), robot.last_seen)
            return snapshot

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
                    # Redis can be unavailable/expired before the DB timeout.
                    # DB last_seen is authoritative for the timeout boundary.
                    timed_out = bool(
                        robot.last_seen is None
                        or (now - robot.last_seen.replace(tzinfo=timezone.utc)
                            if robot.last_seen.tzinfo is None else now - robot.last_seen).total_seconds()
                        > _HEARTBEAT_TIMEOUT_S
                    )
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
                    await self._broadcast_freshness(str(robot.id), "OFFLINE", robot.last_seen)
                elif robot.last_seen:
                    freshness = self.connection_freshness(str(robot.id), robot.last_seen, now)
                    await self._broadcast_freshness(str(robot.id), freshness, robot.last_seen)

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
