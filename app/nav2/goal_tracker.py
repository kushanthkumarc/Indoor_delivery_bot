"""
Goal Tracker — Module 5.2.

Tracks active Nav2 goals with timeout management. Runs a background task
that checks for expired goals and triggers error recovery.

Requirements: 6.3, 6.4, 6.5, 6.6, 6.7
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Callable, Awaitable

from app.nav2.bridge import GoalResult, GoalStatus, Nav2Bridge

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Goal tracker entry
# ---------------------------------------------------------------------------


@dataclass
class TrackedGoal:
    """Represents an active Nav2 goal being tracked."""

    goal_id: str
    delivery_id: str
    status: GoalStatus = GoalStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    deadline: datetime | None = None
    timeout_s: int = 120  # Default timeout in seconds


# ---------------------------------------------------------------------------
# Callback types
# ---------------------------------------------------------------------------

# Callback invoked when a goal reaches a terminal state (succeeded, failed, cancelled)
GoalResultCallback = Callable[[str, GoalResult], Awaitable[None]]
# Callback invoked when a goal times out
GoalTimeoutCallback = Callable[[str, str], Awaitable[None]]  # (delivery_id, goal_id)


# ---------------------------------------------------------------------------
# Goal Tracker
# ---------------------------------------------------------------------------


class GoalTracker:
    """
    Tracks active Nav2 goals and manages timeouts.

    The tracker maintains a mapping of goal_id -> TrackedGoal and provides
    a background task that periodically checks for expired goals.

    Requirements: 6.3, 6.4, 6.5, 6.6, 6.7
    """

    def __init__(
        self,
        nav2_bridge: Nav2Bridge,
        timeout_callback: GoalTimeoutCallback | None = None,
        default_timeout_s: int = 120,
    ) -> None:
        """
        Args:
            nav2_bridge: The Nav2Bridge implementation to use for cancelling goals.
            timeout_callback: Async callback invoked when a goal times out.
            default_timeout_s: Default timeout for goals in seconds.
        """
        self._bridge = nav2_bridge
        self._timeout_callback = timeout_callback
        self._default_timeout_s = default_timeout_s
        self._goals: dict[str, TrackedGoal] = {}
        self._result_callback: GoalResultCallback | None = None

        # Register with the bridge to receive results
        self._bridge.register_result_callback(self._on_goal_result)

        # Lock for thread-safe access
        self._lock = asyncio.Lock()

    def register_result_callback(self, callback: GoalResultCallback) -> None:
        """Register a callback for goal results."""
        self._result_callback = callback

    async def track(
        self,
        goal_id: str,
        delivery_id: str,
        timeout_s: int | None = None,
    ) -> None:
        """
        Start tracking a new goal with an optional timeout.

        Args:
            goal_id: The unique goal identifier.
            delivery_id: The delivery this goal is for.
            timeout_s: Timeout in seconds (uses default if None).
        """
        timeout = timeout_s or self._default_timeout_s
        deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout)

        async with self._lock:
            self._goals[goal_id] = TrackedGoal(
                goal_id=goal_id,
                delivery_id=delivery_id,
                deadline=deadline,
                timeout_s=timeout,
            )

        logger.info(
            "Goal tracked: goal_id=%s delivery_id=%s timeout=%ds",
            goal_id,
            delivery_id,
            timeout,
        )

    async def cancel(self, goal_id: str) -> bool:
        """
        Cancel a tracked goal via the Nav2 bridge.

        Returns True if the goal was found and cancellation was attempted.
        """
        async with self._lock:
            goal = self._goals.get(goal_id)
            if not goal:
                logger.warning("cancel: goal_id %s not found", goal_id)
                return False

            goal.status = GoalStatus.CANCELLED

        # Send cancel to bridge
        await self._bridge.cancel_goal(goal_id)
        logger.info("Goal cancelled: goal_id=%s", goal_id)
        return True

    def get_delivery_id(self, goal_id: str) -> str | None:
        """Get the delivery_id associated with a goal."""
        goal = self._goals.get(goal_id)
        return goal.delivery_id if goal else None

    async def check_timeouts(self) -> list[str]:
        """
        Check for goals that have exceeded their timeout.

        This method should be called periodically (e.g., every 1 second).
        It cancels timed-out goals and invokes the timeout callback.

        Returns list of delivery_ids for which timeouts were triggered.
        """
        now = datetime.now(timezone.utc)
        timed_out_delivery_ids: list[str] = []

        async with self._lock:
            for goal_id, goal in list(self._goals.items()):
                if (
                    goal.status not in (GoalStatus.SUCCEEDED, GoalStatus.FAILED, GoalStatus.CANCELLED)
                    and goal.deadline
                    and now >= goal.deadline
                ):
                    # Mark as failed and trigger timeout handling
                    goal.status = GoalStatus.FAILED
                    timed_out_delivery_ids.append(goal.delivery_id)
                    logger.warning(
                        "Goal timeout: goal_id=%s delivery_id=%s",
                        goal_id,
                        goal.delivery_id,
                    )

        # Cancel the timed-out goals via the bridge
        for goal_id in timed_out_delivery_ids:
            async with self._lock:
                goal = self._goals.get(goal_id)
                if goal:
                    await self._bridge.cancel_goal(goal_id)

            # Invoke timeout callback if registered
            if self._timeout_callback:
                await self._timeout_callback(goal_id, self._goals[goal_id].delivery_id)

        return timed_out_delivery_ids

    async def _on_goal_result(self, result: GoalResult) -> None:
        """
        Internal callback registered with the Nav2Bridge.
        Updates tracked goal status and forwards to result callback.
        """
        goal_id = result.goal_id

        async with self._lock:
            goal = self._goals.get(goal_id)
            if goal:
                goal.status = result.status
                logger.info(
                    "Goal result received: goal_id=%s status=%s delivery_id=%s",
                    goal_id,
                    result.status.value,
                    goal.delivery_id,
                )

        # Forward to registered callback
        if self._result_callback:
            delivery_id = goal.delivery_id if goal else ""
            await self._result_callback(delivery_id, result)

    async def get_status(self, goal_id: str) -> GoalStatus | None:
        """Get current status of a tracked goal."""
        async with self._lock:
            goal = self._goals.get(goal_id)
            return goal.status if goal else None

    def get_active_goals(self) -> list[TrackedGoal]:
        """Get all active (non-terminal) goals."""
        async with self._lock:
            return [
                g for g in self._goals.values()
                if g.status not in (GoalStatus.SUCCEEDED, GoalStatus.FAILED, GoalStatus.CANCELLED)
            ]