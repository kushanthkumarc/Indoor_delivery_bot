"""
Safety Manager — Module 6.

Enforces strict safety invariants on robot control modes:
- Only one control mode active at a time (MANUAL xor AUTONOMOUS xor STOPPED)
- Acknowledgment required for mode switches
- Emergency stop override and reset
- WebSocket disconnect/reconnect handling

Requirements: 7.1, 7.2, 7.4, 7.5, 7.6, 7.8
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.db.models import ControlMode, EventSeverity, Robot, SafetyState
from app.db.session import AsyncSessionLocal
from app.events.service import log_event

if TYPE_CHECKING:
    from app.dashboard.ws_hub import DashboardWSHub

logger = logging.getLogger(__name__)


# Type alias for the STOP command sender
StopCommandSender = Callable[[], Awaitable[None]]


class SafetyError(Exception):
    """Base exception for safety manager errors."""

    pass


class AcknowledgmentRequiredError(SafetyError):
    """Raised when mode switch is attempted without acknowledgment."""

    pass


class EmergencyStopActiveError(SafetyError):
    """Raised when operation is attempted while emergency stop is active."""

    pass


class ModeSwitchRejectedError(SafetyError):
    """Raised when mode switch is rejected for other safety reasons."""

    pass


@dataclass
class SafetyStateCache:
    """
    In-memory cache of safety state for fast access.
    
    Cleared on WebSocket reconnect to require explicit mode re-selection.
    """
    current_mode: ControlMode = ControlMode.STOPPED
    current_safety_state: SafetyState = SafetyState.SAFE
    # Set when robot WS disconnects - requires explicit mode re-selection
    needs_mode_reselection: bool = False


class SafetyManager:
    """
    Central safety control for the robot.
    
    Enforces:
    - Mode exclusivity (Requirement 7.1)
    - Acknowledgment requirement for mode switches (Requirement 7.2)
    - Emergency stop override (Requirement 7.4)
    - WebSocket disconnect handling (Requirement 7.5)
    - WebSocket reconnect handling (Requirement 7.6)
    - Command validation during emergency stop (Requirement 7.8)
    """

    def __init__(self) -> None:
        self._cache = SafetyStateCache()
        self._stop_command_sender: StopCommandSender | None = None
        self._hub: "DashboardWSHub | None" = None

    def set_stop_command_sender(self, sender: StopCommandSender) -> None:
        """Register callback to send STOP command to robot."""
        self._stop_command_sender = sender

    def set_hub(self, hub: "DashboardWSHub") -> None:
        """Set the dashboard WebSocket hub for broadcasting safety events."""
        self._hub = hub

    async def get_current_mode(self) -> ControlMode:
        """Return the current control mode from cache."""
        return self._cache.current_mode

    async def get_current_safety_state(self) -> SafetyState:
        """Return the current safety state from cache."""
        return self._cache.current_safety_state

    async def is_emergency_stop_active(self) -> bool:
        """Check if emergency stop is currently active."""
        return self._cache.current_safety_state == SafetyState.EMERGENCY_STOP

    async def needs_mode_reselection(self) -> bool:
        """
        Check if explicit mode re-selection is required.
        
        Returns True after WebSocket reconnect until mode is explicitly set.
        """
        return self._cache.needs_mode_reselection

    # ------------------------------------------------------------------
    # Mode management
    # ------------------------------------------------------------------

    async def set_mode(
        self,
        mode: ControlMode,
        acknowledgment: bool = False,
        source: str = "api",
    ) -> None:
        """
        Switch to a new control mode.
        
        Requirement 7.1: Only one control mode active at a time.
        Requirement 7.2: Acknowledgment required for mode switch.
        
        Args:
            mode: Target control mode (MANUAL, AUTONOMOUS, or STOPPED)
            acknowledgment: Must be True to confirm the switch
            source: Source of the request (api, rosbridge, etc.)
            
        Raises:
            AcknowledgmentRequiredError: If acknowledgment is False
            EmergencyStopActiveError: If emergency stop is active
        """
        # Check emergency stop first
        if await self.is_emergency_stop_active():
            raise EmergencyStopActiveError(
                "Cannot change mode while EMERGENCY_STOP is active"
            )

        # Require explicit acknowledgment for mode switch (Requirement 7.2)
        if not acknowledgment:
            raise AcknowledgmentRequiredError(
                "Mode switch requires explicit acknowledgment"
            )

        # Mode exclusivity is enforced by always setting to the requested mode
        # (we don't support concurrent modes)
        
        async with AsyncSessionLocal() as db:
            robot = await self._get_robot(db)
            if robot is None:
                logger.warning("set_mode: no robot configured")
                return

            old_mode = robot.control_mode
            robot.control_mode = mode
            await db.commit()

            # Update cache
            self._cache.current_mode = mode
            # Clear needs_mode_reselection if we're setting a mode explicitly
            self._cache.needs_mode_reselection = False

            # Broadcast mode change to dashboard clients
            if self._hub:
                await self._hub.broadcast(
                    "robot.mode_change",
                    {
                        "mode": mode.value,
                        "old_mode": old_mode.value,
                        "source": source,
                    },
                )

            # Log the mode change
            await log_event(
                db,
                type="safety.mode_change",
                severity=EventSeverity.INFO,
                robot_id=str(robot.id),
                payload={
                    "old_mode": old_mode.value,
                    "new_mode": mode.value,
                    "source": source,
                },
            )
            await db.commit()

            logger.info("Control mode changed: %s -> %s (source: %s)", 
                        old_mode.value, mode.value, source)

    # ------------------------------------------------------------------
    # Emergency stop
    # ------------------------------------------------------------------

    async def trigger_emergency_stop(
        self,
        source: str = "unknown",
        reason: str = "",
    ) -> None:
        """
        Trigger emergency stop.
        
        Requirement 7.4:
        - Override all other modes
        - Send STOP to robot
        - Set DB state to EMERGENCY_STOP
        - Broadcast to all dashboard clients
        """
        async with AsyncSessionLocal() as db:
            robot = await self._get_robot(db)
            if robot is None:
                logger.warning("trigger_emergency_stop: no robot configured")
                return

            old_safety_state = robot.safety_state
            old_control_mode = robot.control_mode

            # Override mode to STOPPED
            robot.control_mode = ControlMode.STOPPED
            robot.safety_state = SafetyState.EMERGENCY_STOP
            await db.commit()

            # Update cache
            self._cache.current_mode = ControlMode.STOPPED
            self._cache.current_safety_state = SafetyState.EMERGENCY_STOP

            # Send STOP command to robot
            if self._stop_command_sender:
                try:
                    await self._stop_command_sender()
                    logger.info("STOP command sent to robot")
                except Exception:
                    logger.exception("Failed to send STOP command")

            # Broadcast emergency stop to dashboard clients (Requirement 7.8)
            if self._hub:
                await self._hub.broadcast(
                    "robot.emergency_stop",
                    {
                        "source": source,
                        "reason": reason,
                        "old_safety_state": old_safety_state.value,
                    },
                )

            # Log CRITICAL event (Requirement 7.7)
            await log_event(
                db,
                type="safety.emergency_stop",
                severity=EventSeverity.CRITICAL,
                robot_id=str(robot.id),
                payload={
                    "source": source,
                    "reason": reason,
                    "old_safety_state": old_safety_state.value,
                    "old_control_mode": old_control_mode.value,
                },
            )
            await db.commit()

            logger.critical(
                "EMERGENCY STOP triggered: source=%s reason=%s", 
                source, reason
            )

    async def reset_emergency_stop(self, admin_user_id: str) -> None:
        """
        Reset emergency stop.
        
        Requirement 7.4: Only allowed by ADMIN, transitions to STOPPED mode.
        
        Args:
            admin_user_id: ID of the admin performing the reset
            
        Raises:
            EmergencyStopActiveError: If emergency stop is not active
        """
        if not await self.is_emergency_stop_active():
            raise SafetyError("Emergency stop is not active")

        async with AsyncSessionLocal() as db:
            robot = await self._get_robot(db)
            if robot is None:
                logger.warning("reset_emergency_stop: no robot configured")
                return

            # Reset to STOPPED mode (not AUTONOMOUS or MANUAL)
            robot.control_mode = ControlMode.STOPPED
            robot.safety_state = SafetyState.SAFE
            await db.commit()

            # Update cache
            self._cache.current_mode = ControlMode.STOPPED
            self._cache.current_safety_state = SafetyState.SAFE

            # Log the reset
            await log_event(
                db,
                type="safety.emergency_stop_reset",
                severity=EventSeverity.INFO,
                robot_id=str(robot.id),
                user_id=admin_user_id,
                payload={"reset_by": admin_user_id},
            )
            await db.commit()

            logger.info("Emergency stop reset by admin: %s", admin_user_id)

    # ------------------------------------------------------------------
    # Command validation
    # ------------------------------------------------------------------

    async def validate_command(self, command_type: str) -> bool:
        """
        Validate if a command can be executed.
        
        Requirement 7.8: Returns false for any command during EMERGENCY_STOP.
        
        Args:
            command_type: Type of command being attempted
            
        Returns:
            True if command is allowed, False if blocked
        """
        if await self.is_emergency_stop_active():
            logger.warning(
                "Command blocked: %s rejected during EMERGENCY_STOP", 
                command_type
            )
            return False

        # Also reject if mode re-selection is needed after reconnect
        if await self.needs_mode_reselection():
            logger.warning(
                "Command blocked: %s rejected - mode re-selection required", 
                command_type
            )
            return False

        return True

    async def validate_manual_command(self, command_type: str) -> bool:
        """
        Validate manual commands.
        
        Requirement 7.3: Reject manual commands when AUTONOMOUS is active.
        
        Args:
            command_type: Type of manual command being attempted
            
        Returns:
            True if command is allowed, False if blocked
        """
        # First check emergency stop
        if not await self.validate_command(command_type):
            return False

        # Check if in autonomous mode (Requirement 7.3)
        current_mode = await self.get_current_mode()
        if current_mode == ControlMode.AUTONOMOUS:
            logger.warning(
                "Manual command blocked: %s rejected - robot in AUTONOMOUS mode",
                command_type
            )
            return False

        return True

    # ------------------------------------------------------------------
    # WebSocket disconnect/reconnect handling
    # ------------------------------------------------------------------

    async def on_ws_disconnect(self, robot_id: str) -> None:
        """
        Handle WebSocket disconnection.
        
        Requirement 7.5:
        - Set control mode to STOPPED
        - Attempt to send final STOP command
        - Log CRITICAL event
        """
        async with AsyncSessionLocal() as db:
            robot = await self._get_robot(db)
            if robot is None:
                logger.warning("on_ws_disconnect: no robot configured")
                return

            # Set mode to STOPPED
            old_mode = robot.control_mode
            robot.control_mode = ControlMode.STOPPED
            await db.commit()

            # Update cache
            self._cache.current_mode = ControlMode.STOPPED
            # Require mode re-selection on reconnect
            self._cache.needs_mode_reselection = True

            # Attempt final STOP command
            if self._stop_command_sender:
                try:
                    await self._stop_command_sender()
                    logger.info("Final STOP command sent on disconnect")
                except Exception:
                    logger.exception("Failed to send final STOP command")

            # Log CRITICAL event (Requirement 7.7)
            await log_event(
                db,
                type="safety.ws_disconnect",
                severity=EventSeverity.CRITICAL,
                robot_id=str(robot.id),
                payload={
                    "old_mode": old_mode.value,
                    "action": "mode_set_to_stopped",
                },
            )
            await db.commit()

            logger.warning("Robot WebSocket disconnected: mode set to STOPPED")

    async def on_ws_reconnect(self, robot_id: str) -> None:
        """
        Handle WebSocket reconnection.
        
        Requirement 7.6:
        - Clear cached mode
        - Require explicit mode re-selection via API before any operation
        """
        # Clear cached mode and require explicit re-selection
        self._cache.needs_mode_reselection = True
        # Keep current safety state but mode needs to be explicitly set
        
        async with AsyncSessionLocal() as db:
            robot = await self._get_robot(db)
            if robot is None:
                logger.warning("on_ws_reconnect: no robot configured")
                return

            # Log the reconnect
            await log_event(
                db,
                type="safety.ws_reconnect",
                severity=EventSeverity.INFO,
                robot_id=str(robot.id),
                payload={"needs_mode_reselection": True},
            )
            await db.commit()

        logger.info("Robot WebSocket reconnected: mode re-selection required")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_robot(self, db: AsyncSession) -> Robot | None:
        """Get the robot instance from DB."""
        result = await db.execute(select(Robot).limit(1))
        return result.scalar_one_or_none()

    async def sync_from_db(self) -> None:
        """Sync cached state from database on startup."""
        async with AsyncSessionLocal() as db:
            robot = await self._get_robot(db)
            if robot:
                self._cache.current_mode = robot.control_mode
                self._cache.current_safety_state = robot.safety_state
                logger.info(
                    "Safety state synced from DB: mode=%s safety=%s",
                    self._cache.current_mode.value,
                    self._cache.current_safety_state.value,
                )


# Module-level singleton
safety_manager = SafetyManager()