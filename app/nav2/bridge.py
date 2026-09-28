"""
Nav2 Bridge — Module 5.

Defines the abstract Nav2Bridge interface and the RosbridgeNav2Bridge
implementation that sends NavigateToPose goals to the robot via the
rosbridge WebSocket protocol.

Requirements: 6.1, 6.2, 6.8
"""

from __future__ import annotations

import json
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Goal status enum
# ---------------------------------------------------------------------------


class GoalStatus(str, Enum):
    """Lifecycle states for a Nav2 NavigateToPose goal. Requirement 6.2"""

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# ---------------------------------------------------------------------------
# Pose dataclass
# ---------------------------------------------------------------------------


@dataclass
class Pose:
    """2-D pose in the map frame (x, y metres, theta radians)."""

    x: float
    y: float
    theta: float

    def to_rosbridge_pose(self) -> dict[str, Any]:
        """
        Serialise to the ROS geometry_msgs/Pose format used by rosbridge.
        theta is converted to a quaternion (rotation about Z axis).
        Requirement 6.1
        """
        import math

        half = self.theta / 2.0
        return {
            "position": {"x": self.x, "y": self.y, "z": 0.0},
            "orientation": {
                "x": 0.0,
                "y": 0.0,
                "z": math.sin(half),
                "w": math.cos(half),
            },
        }


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class GoalResult:
    """Encapsulates the outcome of a Nav2 goal. Requirement 6.8"""

    goal_id: str
    status: GoalStatus
    final_pose: Pose | None = None
    result_code: int | None = None
    failure_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Callback type aliases
# ---------------------------------------------------------------------------

ResultCallback = Callable[[GoalResult], Awaitable[None]]
FeedbackCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class Nav2Bridge(ABC):
    """
    Abstract interface for the Nav2 navigation bridge.

    Both the real rosbridge transport and the simulation mock implement
    this interface so that all business logic is transport-agnostic.

    Requirements: 6.1, 6.2
    """

    @abstractmethod
    async def send_goal(self, pose: Pose, delivery_id: str) -> str:
        """
        Send a NavigateToPose goal and return the generated goal_id.

        Requirement 6.1
        """

    @abstractmethod
    async def cancel_goal(self, goal_id: str) -> None:
        """
        Cancel an in-flight Nav2 goal. Requirement 6.5
        """

    @abstractmethod
    def get_goal_status(self, goal_id: str) -> GoalStatus | None:
        """
        Return the current status of a tracked goal, or None if unknown.

        Requirement 6.2
        """

    def register_result_callback(self, callback: ResultCallback) -> None:
        """Register a coroutine called when any goal reaches a terminal state."""
        self._result_callback: ResultCallback | None = callback

    def register_feedback_callback(self, callback: FeedbackCallback) -> None:
        """Register a coroutine called on each Nav2 feedback message."""
        self._feedback_callback: FeedbackCallback | None = callback


# ---------------------------------------------------------------------------
# Rosbridge transport implementation
# ---------------------------------------------------------------------------


class RosbridgeNav2Bridge(Nav2Bridge):
    """
    Sends Nav2 NavigateToPose action goals to the robot via the
    rosbridge WebSocket protocol.

    The actual WebSocket connection is managed by the rosbridge client
    (app/rosbridge/client.py).  This bridge holds a reference to that
    client's send coroutine so it stays decoupled from transport details.

    Requirements: 6.1, 6.2, 6.8
    """

    def __init__(self, ws_send: Callable[[str], Awaitable[None]]) -> None:
        """
        Args:
            ws_send: An async callable that sends a raw JSON string over the
                     rosbridge WebSocket to the robot.
        """
        self._ws_send = ws_send
        self._goals: dict[str, GoalStatus] = {}
        self._result_callback: ResultCallback | None = None
        self._feedback_callback: FeedbackCallback | None = None

    # ------------------------------------------------------------------
    # Nav2Bridge interface
    # ------------------------------------------------------------------

    async def send_goal(self, pose: Pose, delivery_id: str) -> str:
        """
        Publish a NavigateToPose action goal via rosbridge call_service.

        The goal_id is a UUID generated server-side and embedded in the
        request so we can correlate responses.

        Rosbridge message schema per design doc — Requirement 6.1.
        """
        goal_id = str(uuid.uuid4())
        self._goals[goal_id] = GoalStatus.PENDING

        message = {
            "op": "call_service",
            "id": goal_id,
            "service": "/navigate_to_pose",
            "args": {
                "goal_id": goal_id,
                "delivery_id": delivery_id,
                "pose": {
                    "header": {"frame_id": "map"},
                    "pose": pose.to_rosbridge_pose(),
                },
            },
        }

        await self._ws_send(json.dumps(message))
        logger.info(
            "Nav2 goal sent: goal_id=%s delivery_id=%s x=%.3f y=%.3f",
            goal_id,
            delivery_id,
            pose.x,
            pose.y,
        )
        return goal_id

    async def cancel_goal(self, goal_id: str) -> None:
        """
        Send a cancel command to Nav2 via rosbridge. Requirement 6.5
        """
        if goal_id not in self._goals:
            logger.warning("cancel_goal: unknown goal_id %s", goal_id)
            return

        message = {
            "op": "call_service",
            "id": f"cancel_{goal_id}",
            "service": "/navigate_to_pose/cancel_goal",
            "args": {"goal_id": goal_id},
        }
        await self._ws_send(json.dumps(message))
        self._goals[goal_id] = GoalStatus.CANCELLED
        logger.info("Nav2 goal cancelled: goal_id=%s", goal_id)

    def get_goal_status(self, goal_id: str) -> GoalStatus | None:
        """Return current goal status. Requirement 6.2"""
        return self._goals.get(goal_id)

    # ------------------------------------------------------------------
    # Incoming rosbridge message handler (called by rosbridge client)
    # ------------------------------------------------------------------

    async def handle_message(self, message: dict[str, Any]) -> None:
        """
        Process an incoming rosbridge message from the robot.

        Dispatches to result or feedback handlers as appropriate.
        Requirements: 6.2, 6.3, 6.4, 6.8
        """
        op = message.get("op")

        if op == "service_response":
            await self._handle_service_response(message)
        elif op == "publish" and message.get("topic", "").startswith("/navigate_to_pose"):
            await self._handle_feedback(message)

    async def _handle_service_response(self, message: dict[str, Any]) -> None:
        """Process NavigateToPose action result. Requirement 6.8"""
        goal_id = message.get("id", "")
        if not goal_id or goal_id not in self._goals:
            return

        values = message.get("values", {})
        result_code: int = values.get("result_code", -1)
        success: bool = message.get("result", False) and result_code == 0

        status = GoalStatus.SUCCEEDED if success else GoalStatus.FAILED
        self._goals[goal_id] = status

        failure_reason: str | None = None
        if not success:
            failure_reason = values.get("error_msg") or f"result_code={result_code}"

        # Extract final pose if provided
        final_pose: Pose | None = None
        raw_pose = values.get("final_pose")
        if raw_pose:
            import math
            pos = raw_pose.get("position", {})
            ori = raw_pose.get("orientation", {})
            # Convert quaternion back to theta
            theta = 2.0 * math.atan2(ori.get("z", 0.0), ori.get("w", 1.0))
            final_pose = Pose(
                x=pos.get("x", 0.0),
                y=pos.get("y", 0.0),
                theta=theta,
            )

        result = GoalResult(
            goal_id=goal_id,
            status=status,
            final_pose=final_pose,
            result_code=result_code,
            failure_reason=failure_reason,
        )

        logger.info(
            "Nav2 goal result: goal_id=%s status=%s failure=%s",
            goal_id,
            status.value,
            failure_reason,
        )

        if self._result_callback:
            await self._result_callback(result)

    async def _handle_feedback(self, message: dict[str, Any]) -> None:
        """Process NavigateToPose action feedback."""
        msg_data = message.get("msg", {})
        goal_id = msg_data.get("goal_id", "")
        if goal_id in self._goals and self._goals[goal_id] == GoalStatus.PENDING:
            self._goals[goal_id] = GoalStatus.ACTIVE

        if self._feedback_callback and goal_id:
            await self._feedback_callback(goal_id, msg_data)
