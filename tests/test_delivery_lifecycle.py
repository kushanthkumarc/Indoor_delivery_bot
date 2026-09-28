"""
Tests for Delivery Lifecycle - Module 4.
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone

from app.db.models import Delivery, DeliveryStatus, Robot, RobotStatus, ControlMode, Destination
from app.delivery.state_machine import DeliveryStateMachine, InvalidTransitionError
from app.nav2.bridge import GoalResult, GoalStatus
from app.simulation.mock_nav2 import MockNav2Bridge


# Helper models
def create_mock_delivery(status=DeliveryStatus.IDLE):
    return Delivery(
        id="test-delivery",
        robot_id="test-robot",
        destination_id="test-dest",
        requester_id="test-user",
        status=status,
        state_history_json=[]
    )

def create_mock_destination():
    return Destination(
        id="test-dest",
        x=2.0, y=3.0, theta=1.57
    )


@pytest.fixture
def mock_nav2_bridge():
    # Use zero delay for testing
    return MockNav2Bridge(delay_s=0.0, success_rate=1.0)


@pytest.fixture
def mock_db_session():
    with patch("app.delivery.state_machine.AsyncSessionLocal") as mock_session_cls:
        mock_session = AsyncMock()
        mock_session_cls.return_value.__aenter__.return_value = mock_session
        yield mock_session


@pytest.fixture
def mock_log_event():
    with patch("app.delivery.state_machine.log_event", new_callable=AsyncMock) as mock_log:
        yield mock_log


@pytest.fixture
def state_machine(mock_nav2_bridge):
    return DeliveryStateMachine(mock_nav2_bridge)


@pytest.mark.asyncio
async def test_full_delivery_lifecycle(state_machine, mock_db_session, mock_log_event, mock_nav2_bridge):
    # Setup mock returns
    delivery = create_mock_delivery()
    dest = create_mock_destination()
    
    # We must patch _get_delivery to return the delivery object
    with patch.object(state_machine, "_get_delivery", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = delivery
        delivery.destination = dest
        
        # 1. DISPATCH
        assert delivery.status == DeliveryStatus.IDLE
        await state_machine.dispatch("test-delivery")
        
        assert delivery.status == DeliveryStatus.GO_TO_DESTINATION
        assert mock_nav2_bridge.get_goal_status(delivery.nav_goal_id) == GoalStatus.PENDING
        
        # manually complete goal via simulation bridge internal method (since we bypass the tracker here)
        result = GoalResult(
            goal_id=delivery.nav_goal_id,
            status=GoalStatus.SUCCEEDED,
            result_code=0
        )
        
        # 2. ARRIVED (Simulate success from Nav2Bridge)
        await state_machine.on_nav_result("test-delivery", result)
        assert delivery.status == DeliveryStatus.ARRIVED
        
        # 3. CONFIRM PICKUP
        await state_machine.on_pickup_confirmed("test-delivery", "user-id")
        
        # Should now be in RETURN_HOME
        assert delivery.status == DeliveryStatus.RETURN_HOME
        assert delivery.delivered_at is not None
        
        # 4. HOME (Simulate return success)
        result.goal_id = delivery.nav_goal_id # the new goal
        await state_machine.on_nav_result("test-delivery", result)
        
        assert delivery.status == DeliveryStatus.IDLE
        assert delivery.completed_at is not None
        
        # Verify history was recorded for all transitions
        assert len(delivery.state_history_json) == 6 # IDLE->GO -> ARRIVED -> DELIVERED -> RETURN_HOME -> HOME -> IDLE


@pytest.mark.asyncio
async def test_emergency_stop_transition(state_machine, mock_db_session):
    delivery = create_mock_delivery(status=DeliveryStatus.GO_TO_DESTINATION)
    
    with patch.object(state_machine, "_get_delivery", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = delivery
        
        await state_machine.on_emergency_stop("test-delivery")
        
        assert delivery.status == DeliveryStatus.EMERGENCY_STOP
        assert delivery.state_history_json[-1]["to"] == DeliveryStatus.EMERGENCY_STOP.value


@pytest.mark.asyncio
async def test_nav_timeout(state_machine, mock_db_session):
    delivery = create_mock_delivery(status=DeliveryStatus.GO_TO_DESTINATION)
    
    with patch.object(state_machine, "_get_delivery", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = delivery
        
        # Simulate timeout resulting in FAILED goal from tracker
        result = GoalResult(
            goal_id="old-goal",
            status=GoalStatus.FAILED,
            failure_reason="Timeout"
        )
        
        await state_machine.on_nav_result("test-delivery", result)
        
        # Should transition to EMERGENCY_STOP
        assert delivery.status == DeliveryStatus.EMERGENCY_STOP
