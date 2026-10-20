"""Navigation adapter that sends goals to the robot over its gateway socket.

Implements the ``NavigationAdapter`` protocol used by ``DeliveryStateMachine``
(start / wait_result / cancel) without ROS or rosbridge. The robot gateway
reports ``command_ack`` / ``command_result``; those reports are protocol
messages only and never prove physical execution or a motor stop.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from sqlalchemy import select

from app.core.config import settings
from app.delivery.state_machine import InvalidTransitionError
from app.gateway.commands import build_command
from app.nav2.bridge import GoalResult, GoalStatus, Pose

logger = logging.getLogger(__name__)

SendFn = Callable[[dict], Awaitable[None]]
RobotResolver = Callable[[str], Awaitable[str]]

_RESULT_MAP = {
    "SUCCEEDED": GoalStatus.SUCCEEDED,
    "FAILED": GoalStatus.FAILED,
    "CANCELLED": GoalStatus.CANCELLED,
}


class RobotNotConnectedError(InvalidTransitionError):
    """The target robot has no authenticated gateway connection."""


class _Goal:
    def __init__(self, robot_id: str, future: asyncio.Future) -> None:
        self.robot_id = robot_id
        self.future = future
        self.acked = False


async def _resolve_robot_from_db(delivery_id: str) -> str:
    from app.db.models import Delivery
    from app.db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Delivery.robot_id).where(Delivery.id == delivery_id))
        robot_id = result.scalar_one_or_none()
    if robot_id is None:
        raise ValueError("Delivery not found")
    return str(robot_id)


class GatewayNavigationAdapter:
    def __init__(self, *, resolver: RobotResolver | None = None,
                 ttl_s: float | None = None, ack_timeout_s: float | None = None) -> None:
        self._resolver = resolver or _resolve_robot_from_db
        self._ttl_s = ttl_s
        self._ack_timeout_s = ack_timeout_s
        self._connections: dict[str, tuple[str, SendFn]] = {}
        self._goals: dict[str, _Goal] = {}

    # -- connection lifecycle (called by the /ws/robot handler) -------------
    def register_connection(self, robot_id: str, connection_id: str, send: SendFn) -> None:
        self._connections[robot_id] = (connection_id, send)

    def unregister_connection(self, robot_id: str, connection_id: str) -> None:
        current = self._connections.get(robot_id)
        if current is None or current[0] != connection_id:
            return
        del self._connections[robot_id]
        for goal_id, goal in list(self._goals.items()):
            if goal.robot_id == robot_id:
                self._finish(goal_id, GoalStatus.FAILED, "robot gateway disconnected")

    def is_connected(self, robot_id: str) -> bool:
        return robot_id in self._connections

    # -- NavigationAdapter protocol ----------------------------------------
    async def start(self, pose: Pose, delivery_id: str) -> str:
        robot_id = await self._resolver(delivery_id)
        if robot_id not in self._connections:
            raise RobotNotConnectedError("Robot gateway is not connected")
        ttl = self._ttl_s if self._ttl_s is not None else settings.ROBOT_COMMAND_TTL_S
        envelope = build_command(
            robot_id, "goto", delivery_id=delivery_id,
            goal={"x": pose.x, "y": pose.y, "theta": pose.theta}, ttl_s=ttl)
        goal_id = envelope["payload"]["command_id"]
        goal = _Goal(robot_id, asyncio.get_running_loop().create_future())
        self._goals[goal_id] = goal
        try:
            await self._send(robot_id, envelope)
        except Exception:
            self._goals.pop(goal_id, None)
            raise RobotNotConnectedError("Robot gateway send failed") from None
        asyncio.get_running_loop().create_task(self._ack_watchdog(goal_id))
        return goal_id

    async def wait_result(self, goal_id: str) -> GoalResult:
        goal = self._goals.get(goal_id)
        if goal is None:
            return GoalResult(goal_id, GoalStatus.FAILED, failure_reason="unknown goal")
        try:
            return await asyncio.shield(goal.future)
        finally:
            if goal.future.done():
                self._goals.pop(goal_id, None)

    async def cancel(self, goal_id: str) -> None:
        goal = self._goals.get(goal_id)
        if goal is None:
            return
        # Best effort: ask the robot to stop even if the goal already finished.
        try:
            await self._send(goal.robot_id, build_command(goal.robot_id, "stop", ttl_s=15.0))
        except Exception:
            logger.warning("cancel: stop command could not be sent to robot_id=%s", goal.robot_id)
        self._finish(goal_id, GoalStatus.CANCELLED, "cancellation requested")

    async def send_stop(self, robot_id: str) -> bool:
        """Send a stop request to a robot (used for operator stop). Returns delivery."""
        try:
            await self._send(robot_id, build_command(robot_id, "stop", ttl_s=15.0))
            return True
        except Exception:
            return False

    # -- reports from the robot --------------------------------------------
    async def handle_report(self, robot_id: str, kind: str, payload: dict) -> None:
        goal_id = payload.get("command_id")
        goal = self._goals.get(goal_id) if isinstance(goal_id, str) else None
        if goal is None or goal.robot_id != robot_id:
            return  # unknown command or a different robot's command: ignore
        if kind == "command_ack":
            goal.acked = True
            return
        status = _RESULT_MAP.get(str(payload.get("status", "")).upper())
        if status is None:
            return  # progress or unknown status: not terminal
        self._finish(goal_id, status, payload.get("detail"))

    # -- internals ----------------------------------------------------------
    async def _send(self, robot_id: str, envelope: dict) -> None:
        entry = self._connections.get(robot_id)
        if entry is None:
            raise RobotNotConnectedError("Robot gateway is not connected")
        await entry[1](envelope)

    def _finish(self, goal_id: str, status: GoalStatus, reason: str | None) -> None:
        goal = self._goals.get(goal_id)
        if goal is None or goal.future.done():
            return
        goal.future.set_result(GoalResult(
            goal_id, status, failure_reason=None if status == GoalStatus.SUCCEEDED else reason))

    async def _ack_watchdog(self, goal_id: str) -> None:
        timeout = self._ack_timeout_s if self._ack_timeout_s is not None \
            else settings.ROBOT_COMMAND_ACK_TIMEOUT_S
        await asyncio.sleep(timeout)
        goal = self._goals.get(goal_id)
        if goal is not None and not goal.acked and not goal.future.done():
            try:
                await self._send(goal.robot_id, build_command(goal.robot_id, "stop", ttl_s=15.0))
            except Exception:
                pass
            self._finish(goal_id, GoalStatus.FAILED, "robot did not acknowledge command")


gateway_navigation = GatewayNavigationAdapter()
