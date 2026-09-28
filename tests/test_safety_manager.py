"""
Tests for Safety Manager - Module 6 (Task 9).
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.db.models import ControlMode, SafetyState, Robot
from app.safety.manager import (
    SafetyManager,
    AcknowledgmentRequiredError,
    EmergencyStopActiveError,
    SafetyError,
)

# Helper to create a mock robot
def create_mock_robot(
    control_mode=ControlMode.STOPPED, safety_state=SafetyState.SAFE
):
    robot = Robot(
        id="test-robot-id",
        name="Test Robot",
        api_key_hash="hash",
        control_mode=control_mode,
        safety_state=safety_state,
    )
    return robot

@pytest.fixture
def mock_db_session():
    with patch("app.safety.manager.AsyncSessionLocal") as mock_session_cls:
        mock_session = AsyncMock()
        mock_session_cls.return_value.__aenter__.return_value = mock_session
        yield mock_session

@pytest.fixture
def mock_get_robot(mock_db_session):
    with patch("app.safety.manager.SafetyManager._get_robot", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = create_mock_robot()
        yield mock_get

@pytest.fixture
def mock_log_event():
    with patch("app.safety.manager.log_event", new_callable=AsyncMock) as mock_log:
        yield mock_log

@pytest.fixture
def safety_manager():
    sm = SafetyManager()
    sm._hub = AsyncMock()
    sm._stop_command_sender = AsyncMock()
    return sm


@pytest.mark.asyncio
async def test_mode_exclusivity_only_one_active(safety_manager, mock_get_robot, mock_db_session, mock_log_event):
    """Requirement 7.1: Only one mode active at a time (implicit in how we set it)"""
    mock_get_robot.return_value = create_mock_robot(control_mode=ControlMode.MANUAL)
    
    await safety_manager.set_mode(ControlMode.AUTONOMOUS, acknowledgment=True)
    
    # Verify cached mode updated
    assert await safety_manager.get_current_mode() == ControlMode.AUTONOMOUS
    
    # Verify DB was updated
    robot = mock_get_robot.return_value
    assert robot.control_mode == ControlMode.AUTONOMOUS
    mock_db_session.commit.assert_called()

@pytest.mark.asyncio
async def test_set_mode_requires_acknowledgment(safety_manager, mock_get_robot):
    """Requirement 7.2: Mode switch requires acknowledgment"""
    with pytest.raises(AcknowledgmentRequiredError):
        await safety_manager.set_mode(ControlMode.AUTONOMOUS, acknowledgment=False)

@pytest.mark.asyncio
async def test_set_mode_rejected_when_emergency_stop_active(safety_manager, mock_get_robot):
    """Requirement 7.8: Reject commands during EMERGENCY_STOP"""
    # Simulate active emergency stop
    safety_manager._cache.current_safety_state = SafetyState.EMERGENCY_STOP
    
    with pytest.raises(EmergencyStopActiveError):
        await safety_manager.set_mode(ControlMode.MANUAL, acknowledgment=True)

@pytest.mark.asyncio
async def test_trigger_emergency_stop_overrides_mode(safety_manager, mock_get_robot, mock_db_session, mock_log_event):
    """Requirement 7.4: Emergency stop overrides all modes and sends STOP to robot"""
    robot = create_mock_robot(control_mode=ControlMode.AUTONOMOUS)
    mock_get_robot.return_value = robot
    
    await safety_manager.trigger_emergency_stop(source="test", reason="test_reason")
    
    # Verify DB updated
    assert robot.control_mode == ControlMode.STOPPED
    assert robot.safety_state == SafetyState.EMERGENCY_STOP
    mock_db_session.commit.assert_called()
    
    # Verify STOP command sent
    safety_manager._stop_command_sender.assert_called_once()
    
    # Verify cache updated
    assert await safety_manager.get_current_mode() == ControlMode.STOPPED
    assert await safety_manager.get_current_safety_state() == SafetyState.EMERGENCY_STOP

@pytest.mark.asyncio
async def test_trigger_emergency_stop_broadcasts(safety_manager, mock_get_robot, mock_db_session, mock_log_event):
    """Requirement 7.8: Broadcast safety state on emergency stop"""
    dashboard_hub = AsyncMock()
    safety_manager.set_hub(dashboard_hub)
    
    await safety_manager.trigger_emergency_stop(source="test")
    
    dashboard_hub.broadcast.assert_called_with(
        "robot.emergency_stop",
        {
            "source": "test",
            "reason": "",
            "old_safety_state": SafetyState.SAFE.value,
        }
    )

@pytest.mark.asyncio
async def test_reset_emergency_stop_transitions_to_stopped(safety_manager, mock_get_robot, mock_db_session, mock_log_event):
    """Requirement 7.4: Reset transitions to STOPPED mode"""
    # Setup active emergency stop
    safety_manager._cache.current_safety_state = SafetyState.EMERGENCY_STOP
    
    robot = create_mock_robot(control_mode=ControlMode.STOPPED, safety_state=SafetyState.EMERGENCY_STOP)
    mock_get_robot.return_value = robot
    
    await safety_manager.reset_emergency_stop(admin_user_id="admin-id")
    
    assert robot.control_mode == ControlMode.STOPPED
    assert robot.safety_state == SafetyState.SAFE
    
    assert await safety_manager.get_current_mode() == ControlMode.STOPPED
    assert await safety_manager.get_current_safety_state() == SafetyState.SAFE

@pytest.mark.asyncio
async def test_reset_requires_active_emergency_stop(safety_manager):
    """Requirement 7.4: Can't reset if not in emergency stop"""
    safety_manager._cache.current_safety_state = SafetyState.SAFE
    
    with pytest.raises(SafetyError, match="Emergency stop is not active"):
        await safety_manager.reset_emergency_stop(admin_user_id="admin-id")

@pytest.mark.asyncio
async def test_validate_command_blocks_during_emergency_stop(safety_manager):
    """Requirement 7.8: Reject all commands when EMERGENCY_STOP is active."""
    safety_manager._cache.current_safety_state = SafetyState.EMERGENCY_STOP
    
    assert await safety_manager.validate_command("some_command") is False

@pytest.mark.asyncio
async def test_validate_manual_command_blocks_during_autonomous(safety_manager, mock_get_robot):
    """Requirement 7.3: Reject manual commands when AUTONOMOUS is active."""
    # First set to autonomous mode
    mock_get_robot.return_value = create_mock_robot()
    safety_manager._cache.current_mode = ControlMode.AUTONOMOUS
    
    assert await safety_manager.validate_manual_command("move_forward") is False

@pytest.mark.asyncio
async def test_on_ws_disconnect_sets_stopped_mode(safety_manager, mock_get_robot, mock_db_session, mock_log_event):
    """Requirement 7.5: Disconnect sets STOPPED mode and sends final STOP"""
    robot = create_mock_robot(control_mode=ControlMode.AUTONOMOUS)
    mock_get_robot.return_value = robot
    
    await safety_manager.on_ws_disconnect(robot_id="test-robot")
    
    assert robot.control_mode == ControlMode.STOPPED
    assert await safety_manager.get_current_mode() == ControlMode.STOPPED
    safety_manager._stop_command_sender.assert_called_once()
    
    assert await safety_manager.needs_mode_reselection() is True

@pytest.mark.asyncio
async def test_on_ws_reconnect_requires_mode_reselection(safety_manager, mock_get_robot, mock_db_session, mock_log_event):
    """Requirement 7.6: Reconnect requires explicit mode re-selection"""
    await safety_manager.on_ws_reconnect(robot_id="test-robot")
    
    assert await safety_manager.needs_mode_reselection() is True
    
    # Try to validate command, should be blocked because re-selection required
    assert await safety_manager.validate_command("some_command") is False
    
    # Now set mode explicitly to simulate re-selection
    await safety_manager.set_mode(ControlMode.MANUAL, acknowledgment=True)
    
    # Should be cleared and commands allowed
    assert await safety_manager.needs_mode_reselection() is False
    assert await safety_manager.validate_command("some_command") is True
