"""
Delivery State Machine — Module 4.1

Manages the lifecycle of a delivery.
Requirements: 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Delivery, DeliveryStatus, EventSeverity
from app.db.session import AsyncSessionLocal
from app.events.service import log_event
from app.nav2.bridge import GoalResult, GoalStatus, Nav2Bridge, Pose

logger = logging.getLogger(__name__)


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DeliveryStateMachine:
    """
    State machine for an autonomous delivery.
    
    States: IDLE -> GO_TO_DESTINATION -> ARRIVED -> DELIVERED -> RETURN_HOME -> HOME -> IDLE
    """
    
    def __init__(self, nav2_bridge: Nav2Bridge) -> None:
        self._bridge = nav2_bridge
    
    async def dispatch(self, delivery_id: str) -> None:
        """
        Transition IDLE -> GO_TO_DESTINATION and trigger Nav2 goal.
        Requirement 5.2
        """
        async with AsyncSessionLocal() as db:
            delivery = await self._get_delivery(db, delivery_id)
            if not delivery:
                raise ValueError(f"Delivery {delivery_id} not found")
                
            if delivery.status != DeliveryStatus.IDLE:
                raise InvalidTransitionError(f"Cannot dispatch delivery in state {delivery.status}")
                
            # Get destination pose (requires joining destination or fetching it)
            await db.refresh(delivery, ["destination"])
            dest = delivery.destination
            
            nav_pose = Pose(x=dest.x, y=dest.y, theta=dest.theta)
            
            # Record transition and update status BEFORE sending goal to avoid race conditions
            self._record_transition(delivery, DeliveryStatus.IDLE, DeliveryStatus.GO_TO_DESTINATION)
            delivery.status = DeliveryStatus.GO_TO_DESTINATION
            
            # Send nav goal
            goal_id = await self._bridge.send_goal(nav_pose, delivery_id)
            delivery.nav_goal_id = goal_id
            
            await db.commit()
            
            logger.info("Delivery %s dispatched. Nav goal: %s", delivery_id, goal_id)
            
    async def on_pickup_confirmed(self, delivery_id: str, user_id: str) -> None:
        """
        Transition ARRIVED -> DELIVERED -> RETURN_HOME and trigger return Nav2 goal.
        Requirement 5.4, 5.5
        """
        async with AsyncSessionLocal() as db:
            delivery = await self._get_delivery(db, delivery_id)
            if not delivery:
                raise ValueError(f"Delivery {delivery_id} not found")
                
            if delivery.status != DeliveryStatus.ARRIVED:
                raise InvalidTransitionError(f"Cannot confirm pickup in state {delivery.status}")
                
            self._record_transition(delivery, DeliveryStatus.ARRIVED, DeliveryStatus.DELIVERED)
            delivery.delivered_at = _utcnow()
            
            # Trigger log event for pickup
            await log_event(
                db, 
                type="delivery.pickup_confirmed", 
                severity=EventSeverity.INFO, 
                robot_id=str(delivery.robot_id),
                user_id=user_id,
                payload={"delivery_id": delivery_id}
            )
            
            # Now transition to RETURN_HOME
            self._record_transition(delivery, DeliveryStatus.DELIVERED, DeliveryStatus.RETURN_HOME)
            delivery.status = DeliveryStatus.RETURN_HOME
            
            # Note: A real app would get the "Home" destination pose for this map.
            # Assuming origin for now as a fallback.
            nav_pose = Pose(x=0.0, y=0.0, theta=0.0)
            
            goal_id = await self._bridge.send_goal(nav_pose, delivery_id)
            delivery.nav_goal_id = goal_id
            
            await db.commit()
            logger.info("Delivery %s pickup confirmed. Returning home. Nav goal: %s", delivery_id, goal_id)

    async def on_emergency_stop(self, delivery_id: str, reason: str = "") -> None:
        """
        Transition any state -> EMERGENCY_STOP.
        Requirement 5.7
        """
        async with AsyncSessionLocal() as db:
            delivery = await self._get_delivery(db, delivery_id)
            if not delivery:
                return
                
            old_status = delivery.status
            if old_status == DeliveryStatus.EMERGENCY_STOP:
                return
                
            self._record_transition(delivery, old_status, DeliveryStatus.EMERGENCY_STOP)
            delivery.status = DeliveryStatus.EMERGENCY_STOP
            
            await db.commit()
            logger.warning("Delivery %s transitioned to EMERGENCY_STOP", delivery_id)
            
    async def on_nav_result(self, delivery_id: str, result: GoalResult) -> None:
        """
        Handle a navigation result callback from the goal tracker.
        Advances state based on current state and goal outcome.
        Requirement 5.3, 5.6
        """
        if not delivery_id:
            return
            
        async with AsyncSessionLocal() as db:
            delivery = await self._get_delivery(db, delivery_id)
            if not delivery:
                return
                
            # If goal failed or cancelled, trigger emergency/error state
            if result.status in (GoalStatus.FAILED, GoalStatus.CANCELLED):
                logger.error("Delivery %s nav goal failed/cancelled", delivery_id)
                old_status = delivery.status
                self._record_transition(delivery, old_status, DeliveryStatus.EMERGENCY_STOP)
                delivery.status = DeliveryStatus.EMERGENCY_STOP
                await db.commit()
                return

            # Note: Goal is SUCCEEDED
            if delivery.status == DeliveryStatus.GO_TO_DESTINATION:
                # Arrived at destination
                self._record_transition(delivery, DeliveryStatus.GO_TO_DESTINATION, DeliveryStatus.ARRIVED)
                delivery.status = DeliveryStatus.ARRIVED
                await db.commit()
                logger.info("Delivery %s arrived at destination", delivery_id)
                
            elif delivery.status == DeliveryStatus.RETURN_HOME:
                # Arrived back home
                self._record_transition(delivery, DeliveryStatus.RETURN_HOME, DeliveryStatus.HOME)
                self._record_transition(delivery, DeliveryStatus.HOME, DeliveryStatus.IDLE)
                delivery.status = DeliveryStatus.IDLE
                delivery.completed_at = _utcnow()
                await db.commit()
                logger.info("Delivery %s completed and robot is IDLE", delivery_id)

    async def _get_delivery(self, db: AsyncSession, delivery_id: str) -> Delivery | None:
        res = await db.execute(select(Delivery).where(Delivery.id == delivery_id))
        return res.scalar_one_or_none()
        
    def _record_transition(self, delivery: Delivery, from_state: DeliveryStatus, to_state: DeliveryStatus) -> None:
        """
        Records the transition in the history json.
        Requirement 5.8
        """
        # Ensure we are working with a new list object so SQLAlchemy detects the mutation
        history = list(delivery.state_history_json) if delivery.state_history_json else []
        history.append({
            "from": from_state.value,
            "to": to_state.value,
            "timestamp": _utcnow().isoformat()
        })
        delivery.state_history_json = history
