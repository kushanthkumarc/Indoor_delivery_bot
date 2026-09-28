"""
Mock Nav2 Bridge — Module 8.3.

Provides a simulation transport that implements Nav2Bridge interface.
Used when SIMULATION_MODE=true to test without physical hardware.

Requirements: 8.2, 8.6, 8.7
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from typing import Any, Callable, Awaitable

from app.core.config import settings
from app.nav2.bridge import (
    Nav2Bridge,
    Pose,
    GoalResult,
    GoalStatus,
    ResultCallback,
    FeedbackCallback,
)

logger = logging.getLogger(__name__)


class MockNav2Bridge(Nav2Bridge):
    """
    Mock implementation of Nav2Bridge for simulation mode.

    Simulates navigation goals with configurable delay and success rate.
    Supports goal cancellation by cancelling the underlying asyncio task.

    Requirements: 8.2, 8.6, 8.7
    """

    def __init__(
        self,
        delay_s: float | None = None,
        success_rate: float | None = None,
    ) -> None:
        """
        Args:
            delay_s: Simulated navigation delay (uses SIM_NAV_DELAY_S from config if None)
            success_rate: Probability of goal success (uses SIM_NAV_SUCCESS_RATE from config if None)
        """
        self._delay_s = delay_s if delay_s is not None else settings.SIM_NAV_DELAY_S
        self._success_rate = success_rate if success_rate is not None else settings.SIM_NAV_SUCCESS_RATE

        self._goals: dict[str, asyncio.Task] = {}
        self._goal_statuses: dict[str, GoalStatus] = {}
        self._result_callback: ResultCallback | None = None
        self._feedback_callback: FeedbackCallback | None = None
        self._lock = asyncio.Lock()

        logger.info(
            "MockNav2Bridge initialized: delay=%.2fs success_rate=%.2f",
            self._delay_s,
            self._success_rate,
        )

    # ------------------------------------------------------------------
    # Nav2Bridge interface
    # ------------------------------------------------------------------

    async def send_goal(self, pose: Pose, delivery_id: str) -> str:
        """
        Simulate sending a NavigateToPose goal.

        Creates an asyncio task that will resolve after the configured delay
        with success or failure based on the configured success rate.
        """
        goal_id = str(uuid.uuid4())

        async with self._lock:
            self._goal_statuses[goal_id] = GoalStatus.PENDING

        # Create task that simulates navigation
        task = asyncio.create_task(
            self._simulate_navigation(goal_id, pose, delivery_id)
        )

        async with self._lock:
            self._goals[goal_id] = task

        logger.info(
            "MockNav2: goal sent: goal_id=%s delivery_id=%s x=%.3f y=%.3f",
            goal_id,
            delivery_id,
            pose.x,
            pose.y,
        )

        return goal_id

    async def cancel_goal(self, goal_id: str) -> None:
        """
        Cancel an in-flight Nav2 goal by cancelling the underlying asyncio task.

        Requirement 8.7
        """
        async with self._lock:
            task = self._goals.get(goal_id)

        if not task:
            logger.warning("MockNav2: cancel_goal: unknown goal_id %s", goal_id)
            return

        if task.done():
            logger.info("MockNav2: cancel_goal: goal_id %s already completed", goal_id)
            return

        # Cancel the task
        task.cancel()

        async with self._lock:
            self._goal_statuses[goal_id] = GoalStatus.CANCELLED
            if goal_id in self._goals:
                del self._goals[goal_id]

        logger.info("MockNav2: goal cancelled: goal_id=%s", goal_id)

    def get_goal_status(self, goal_id: str) -> GoalStatus | None:
        """Return current goal status."""
        return self._goal_statuses.get(goal_id)

    # ------------------------------------------------------------------
    # Simulation internals
    # ------------------------------------------------------------------

    async def _simulate_navigation(
        self,
        goal_id: str,
        pose: Pose,
        delivery_id: str,
    ) -> None:
        """
        Simulate the navigation process.

        1. Wait for the configured delay
        2. Determine success/failure based on success_rate
        3. Send feedback (ACTIVE state)
        4. Send result callback
        """
        try:
            # Simulate PENDING -> ACTIVE transition after a short delay
            await asyncio.sleep(0.1)

            async with self._lock:
                if self._goal_statuses.get(goal_id) == GoalStatus.CANCELLED:
                    return  # Goal was cancelled
                self._goal_statuses[goal_id] = GoalStatus.ACTIVE

            # Send feedback if callback registered
            if self._feedback_callback:
                await self._feedback_callback(
                    goal_id,
                    {
                        "status": "ACTIVE",
                        "pose": {"x": pose.x, "y": pose.y, "theta": pose.theta},
                    },
                )

            # Wait for the configured navigation delay
            await asyncio.sleep(self._delay_s)

            # Check if cancelled during sleep
            async with self._lock:
                if self._goal_statuses.get(goal_id) == GoalStatus.CANCELLED:
                    return

            # Determine success or failure
            success = random.random() < self._success_rate

            if success:
                async with self._lock:
                    self._goal_statuses[goal_id] = GoalStatus.SUCCEEDED

                result = GoalResult(
                    goal_id=goal_id,
                    status=GoalStatus.SUCCEEDED,
                    final_pose=Pose(x=pose.x, y=pose.y, theta=pose.theta),
                    result_code=0,
                )

                logger.info(
                    "MockNav2: goal succeeded: goal_id=%s delivery_id=%s",
                    goal_id,
                    delivery_id,
                )
            else:
                async with self._lock:
                    self._goal_statuses[goal_id] = GoalStatus.FAILED

                result = GoalResult(
                    goal_id=goal_id,
                    status=GoalStatus.FAILED,
                    result_code=1,
                    failure_reason="Simulated navigation failure",
                )

                logger.warning(
                    "MockNav2: goal failed: goal_id=%s delivery_id=%s",
                    goal_id,
                    delivery_id,
                )

            # Remove from active goals
            async with self._lock:
                if goal_id in self._goals:
                    del self._goals[goal_id]

            # Send result callback
            if self._result_callback:
                await self._result_callback(result)

        except asyncio.CancelledError:
            # Clean up on cancellation
            async with self._lock:
                self._goal_statuses[goal_id] = GoalStatus.CANCELLED
                if goal_id in self._goals:
                    del self._goals[goal_id]

            logger.info("MockNav2: goal cancelled during navigation: goal_id=%s", goal_id)
            raise