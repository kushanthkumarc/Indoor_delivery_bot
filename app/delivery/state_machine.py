"""Transactional delivery lifecycle using an injected navigation adapter."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import (
    ControlMode, Delivery, DeliveryStatus, Destination, EventSeverity,
    MappingSession, MappingSessionStatus, Robot, RobotStatus, SafetyState,
)
from app.db.session import AsyncSessionLocal
from app.events.service import log_event
from app.nav2.bridge import GoalResult, GoalStatus, Pose

logger = logging.getLogger(__name__)


class InvalidTransitionError(Exception):
    """Raised when a delivery state transition is not allowed."""


class NavigationAdapter(Protocol):
    async def start(self, pose: Pose, delivery_id: str) -> str: ...
    async def wait_result(self, goal_id: str) -> GoalResult: ...
    async def cancel(self, goal_id: str) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DeliveryStateMachine:
    """State transitions are committed before awaiting navigation results."""

    def __init__(self, navigation: NavigationAdapter,
                 freshness_check: Callable[[str, datetime | None], str] | None = None) -> None:
        self._navigation = navigation
        self._freshness_check = freshness_check
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, delivery_id: str) -> asyncio.Lock:
        return self._locks.setdefault(delivery_id, asyncio.Lock())

    async def dispatch(self, delivery_id: str, actor_id: str | None = None) -> Delivery:
        async with self._lock_for(delivery_id):
            async with AsyncSessionLocal() as db:
                delivery = await self._get_delivery(db, delivery_id, lock=True)
                if delivery is None:
                    raise ValueError("Delivery not found")
                if delivery.status == DeliveryStatus.GO_TO_DESTINATION and delivery.nav_goal_id:
                    return delivery
                if delivery.status != DeliveryStatus.IDLE:
                    raise InvalidTransitionError(f"Cannot dispatch delivery in state {delivery.status.value}")
                destination = await self._get_destination(db, delivery.destination_id)
                if destination is None:
                    raise ValueError("Destination not found")
                robot_result = await db.execute(
                    select(Robot).where(Robot.id == delivery.robot_id).with_for_update()
                )
                robot = robot_result.scalar_one_or_none()
                if robot is None:
                    raise ValueError("Delivery robot not found")
                if robot.status != RobotStatus.ONLINE:
                    raise InvalidTransitionError("Cannot dispatch while robot is offline")
                if robot.control_mode != ControlMode.AUTONOMOUS:
                    raise InvalidTransitionError("Cannot dispatch unless robot is AUTONOMOUS")
                if robot.safety_state != SafetyState.SAFE:
                    raise InvalidTransitionError("Cannot dispatch unless robot safety state is SAFE")
                if self._freshness_check and self._freshness_check(str(robot.id), robot.last_seen) != "ONLINE":
                    raise InvalidTransitionError("Cannot dispatch while robot gateway telemetry is stale or offline")
                map_result = await db.execute(select(MappingSession).where(
                    MappingSession.id == destination.map_id,
                    MappingSession.robot_id == delivery.robot_id,
                    MappingSession.status == MappingSessionStatus.FINISHED,
                ))
                if map_result.scalar_one_or_none() is None:
                    raise InvalidTransitionError("Destination map is not a finished map for this robot")
                goal_id = await self._navigation.start(
                    Pose(destination.x, destination.y, destination.theta), delivery_id
                )
                try:
                    await self._transition(
                        db, delivery, DeliveryStatus.GO_TO_DESTINATION, actor_id,
                        "dispatch requested", goal_id=goal_id,
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    await self._navigation.cancel(goal_id)
                    raise
        return await self._await_navigation(delivery_id, goal_id, DeliveryStatus.GO_TO_DESTINATION, actor_id)

    async def on_pickup_confirmed(self, delivery_id: str, user_id: str) -> Delivery:
        async with self._lock_for(delivery_id):
            async with AsyncSessionLocal() as db:
                delivery = await self._get_delivery(db, delivery_id, lock=True)
                if delivery is None:
                    raise ValueError("Delivery not found")
                if delivery.status != DeliveryStatus.ARRIVED:
                    raise InvalidTransitionError(f"Cannot confirm pickup in state {delivery.status.value}")
                destination = await self._get_destination(db, delivery.destination_id)
                if destination is None:
                    raise ValueError("Destination not found")
                homes_result = await db.execute(select(Destination).where(
                    Destination.map_id == destination.map_id, Destination.is_home.is_(True)
                ))
                home = homes_result.scalars().first()
                if home is None:
                    raise ValueError("Home destination not configured for delivery map")
                goal_id = await self._navigation.start(Pose(home.x, home.y, home.theta), delivery_id)
                delivery.delivered_at = _utcnow()
                try:
                    await self._transition(
                        db, delivery, DeliveryStatus.DELIVERED, user_id,
                        "pickup confirmed",
                    )
                    await log_event(db, type="delivery.pickup_confirmed", severity=EventSeverity.INFO,
                                    robot_id=str(delivery.robot_id), user_id=user_id,
                                    payload={"delivery_id": delivery_id})
                    await self._transition(
                        db, delivery, DeliveryStatus.RETURN_HOME, user_id,
                        "return to configured Home destination", goal_id=goal_id,
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    await self._navigation.cancel(goal_id)
                    raise
        return await self._await_navigation(delivery_id, goal_id, DeliveryStatus.RETURN_HOME, user_id)

    async def cancel(self, delivery_id: str, actor_id: str) -> Delivery:
        async with self._lock_for(delivery_id):
            async with AsyncSessionLocal() as db:
                delivery = await self._get_delivery(db, delivery_id, lock=True)
                if delivery is None:
                    raise ValueError("Delivery not found")
                if delivery.status == DeliveryStatus.EMERGENCY_STOP:
                    return delivery
                if delivery.status not in {DeliveryStatus.GO_TO_DESTINATION, DeliveryStatus.RETURN_HOME}:
                    raise InvalidTransitionError(f"Cannot cancel delivery in state {delivery.status.value}")
                goal_id = delivery.nav_goal_id
                await self._transition(
                    db, delivery, DeliveryStatus.EMERGENCY_STOP, actor_id,
                    "cancellation requested; physical stop unconfirmed",
                )
                await db.commit()
        if goal_id:
            await self._navigation.cancel(goal_id)
        return delivery

    async def _await_navigation(self, delivery_id: str, goal_id: str,
                                expected: DeliveryStatus, actor_id: str | None) -> Delivery:
        try:
            result = await asyncio.wait_for(
                self._navigation.wait_result(goal_id), timeout=settings.DELIVERY_STATE_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            await self._navigation.cancel(goal_id)
            result = GoalResult(goal_id=goal_id, status=GoalStatus.FAILED,
                                failure_reason="Navigation result timed out")
        async with self._lock_for(delivery_id):
            async with AsyncSessionLocal() as db:
                delivery = await self._get_delivery(db, delivery_id, lock=True)
                if delivery is None:
                    raise ValueError("Delivery not found")
                # Ignore late navigation completions after cancellation or a newer goal.
                if delivery.status != expected or delivery.nav_goal_id != goal_id:
                    return delivery
                if result.status == GoalStatus.SUCCEEDED:
                    if expected == DeliveryStatus.GO_TO_DESTINATION:
                        await self._transition(db, delivery, DeliveryStatus.ARRIVED, actor_id,
                                               "navigation succeeded")
                    else:
                        await self._transition(db, delivery, DeliveryStatus.HOME, actor_id,
                                               "return navigation succeeded")
                        await self._transition(db, delivery, DeliveryStatus.IDLE, actor_id,
                                               "delivery lifecycle completed")
                        delivery.completed_at = _utcnow()
                else:
                    # Existing EMERGENCY_STOP is the state machine's fail-closed,
                    # operator-review state; it does not claim physical stop confirmation.
                    await self._transition(
                        db, delivery, DeliveryStatus.EMERGENCY_STOP, actor_id,
                        "navigation failed; physical execution unconfirmed: "
                        f"{result.failure_reason or result.status.value}",
                    )
                await db.commit()
                return delivery

    async def _get_delivery(self, db: AsyncSession, delivery_id: str,
                            *, lock: bool = False) -> Delivery | None:
        stmt = select(Delivery).where(Delivery.id == delivery_id)
        if lock:
            stmt = stmt.with_for_update()
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_destination(self, db: AsyncSession, destination_id: str) -> Destination | None:
        result = await db.execute(select(Destination).where(Destination.id == destination_id))
        return result.scalar_one_or_none()

    async def _transition(self, db: AsyncSession, delivery: Delivery,
                          new_state: DeliveryStatus, actor_id: str | None,
                          reason: str, *, goal_id: str | None = None) -> None:
        previous = delivery.status
        at = _utcnow()
        history = list(delivery.state_history_json or [])
        entry = {"from": previous.value, "to": new_state.value,
                 "timestamp": at.isoformat(), "actor_id": actor_id, "reason": reason}
        history.append(entry)
        delivery.state_history_json = history
        delivery.status = new_state
        if goal_id is not None:
            delivery.nav_goal_id = goal_id
        await log_event(
            db, type="delivery.state_transition",
            severity=EventSeverity.WARN if new_state == DeliveryStatus.EMERGENCY_STOP else EventSeverity.INFO,
            robot_id=str(delivery.robot_id), user_id=actor_id,
            payload={"delivery_id": str(delivery.id), **entry},
        )
