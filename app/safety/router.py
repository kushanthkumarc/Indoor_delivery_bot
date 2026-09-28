"""
Safety Router — Module 6 API endpoints.

Provides REST endpoints for safety state management:
- POST /robot/mode - Switch control mode (requires acknowledgment)
- POST /robot/emergency-stop - Trigger emergency stop
- POST /robot/emergency-stop/reset - Reset emergency stop (ADMIN only)
- GET /robot/safety-state - Get current safety state

Requirements: 7.3, 7.4, 7.7
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.dependencies import get_current_user, require_admin
from app.db.models import ControlMode, SafetyState, User
from app.safety.manager import (
    AcknowledgmentRequiredError,
    EmergencyStopActiveError,
    SafetyError,
    SafetyManager,
    safety_manager,
)


router = APIRouter(prefix="/robot", tags=["Safety"])


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class SetModeRequest(BaseModel):
    """Request to change robot control mode."""

    mode: ControlMode = Field(..., description="Target control mode")
    acknowledgment: bool = Field(
        False,
        description="Must be True to confirm the mode switch",
    )


class ModeResponse(BaseModel):
    """Response containing current mode."""

    mode: ControlMode
    safety_state: SafetyState


class EmergencyStopRequest(BaseModel):
    """Request to trigger emergency stop."""

    reason: str = Field("", description="Reason for emergency stop")


class SafetyStateResponse(BaseModel):
    """Response containing current safety state."""

    control_mode: ControlMode
    safety_state: SafetyState
    needs_mode_reselection: bool = Field(
        False,
        description="Whether explicit mode re-selection is required",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/mode",
    response_model=ModeResponse,
    status_code=status.HTTP_200_OK,
    responses={
        409: {"description": "Conflict - operation not allowed in current mode"},
        423: {"description": "Locked - emergency stop active"},
        422: {"description": "Unprocessable - acknowledgment required"},
    },
)
async def set_robot_mode(
    request: SetModeRequest,
    current_user: Annotated[User, Depends(require_admin())],
) -> ModeResponse:
    """
    Switch robot control mode.

    Requirement 7.2: Requires explicit acknowledgment.
    Requirement 7.3: Rejects manual commands when AUTONOMOUS is active.
    Requirement 7.8: Rejects all commands when EMERGENCY_STOP is active.
    """
    # Check if emergency stop is active (Requirement 7.8)
    if await safety_manager.is_emergency_stop_active():
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail={
                "error": "EMERGENCY_STOP_ACTIVE",
                "message": "Cannot change mode while emergency stop is active",
            },
        )

    # For manual mode switch, validate it's allowed (Requirement 7.3)
    if request.mode == ControlMode.MANUAL:
        # This would be a manual command - but we're explicitly setting mode
        # so we allow it. The rejection is for direct velocity commands.
        pass

    try:
        await safety_manager.set_mode(
            mode=request.mode,
            acknowledgment=request.acknowledgment,
            source="api",
        )
    except AcknowledgmentRequiredError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "ACKNOWLEDGMENT_REQUIRED",
                "message": "Mode switch requires explicit acknowledgment",
            },
        )
    except EmergencyStopActiveError:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail={
                "error": "EMERGENCY_STOP_ACTIVE",
                "message": "Cannot change mode while emergency stop is active",
            },
        )

    return ModeResponse(
        mode=await safety_manager.get_current_mode(),
        safety_state=await safety_manager.get_current_safety_state(),
    )


@router.post(
    "/emergency-stop",
    response_model=SafetyStateResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Emergency stop triggered successfully"},
    },
)
async def trigger_emergency_stop(
    request: EmergencyStopRequest,
    current_user: Annotated[User, Depends(require_admin())],
) -> SafetyStateResponse:
    """
    Trigger emergency stop.

    Requirement 7.4: Immediately overrides all modes and sends STOP to robot.
    """
    await safety_manager.trigger_emergency_stop(
        source="api",
        reason=request.reason,
    )

    return SafetyStateResponse(
        control_mode=await safety_manager.get_current_mode(),
        safety_state=await safety_manager.get_current_safety_state(),
        needs_mode_reselection=await safety_manager.needs_mode_reselection(),
    )


@router.post(
    "/emergency-stop/reset",
    response_model=SafetyStateResponse,
    status_code=status.HTTP_200_OK,
    responses={
        400: {"description": "Bad request - emergency stop not active"},
        423: {"description": "Locked - emergency stop must be reset first"},
    },
)
async def reset_emergency_stop(
    current_user: Annotated[User, Depends(require_admin())],
) -> SafetyStateResponse:
    """
    Reset emergency stop (ADMIN only).

    Requirement 7.4: Transitions to STOPPED mode.
    """
    try:
        await safety_manager.reset_emergency_stop(admin_user_id=str(current_user.id))
    except SafetyError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "RESET_FAILED", "message": str(e)},
        )

    return SafetyStateResponse(
        control_mode=await safety_manager.get_current_mode(),
        safety_state=await safety_manager.get_current_safety_state(),
        needs_mode_reselection=await safety_manager.needs_mode_reselection(),
    )


@router.get(
    "/safety-state",
    response_model=SafetyStateResponse,
    status_code=status.HTTP_200_OK,
)
async def get_safety_state(
    current_user: Annotated[User, Depends(require_admin())],
) -> SafetyStateResponse:
    """
    Get current safety state.

    Requirement 7.8: Returns current control mode and safety state.
    """
    return SafetyStateResponse(
        control_mode=await safety_manager.get_current_mode(),
        safety_state=await safety_manager.get_current_safety_state(),
        needs_mode_reselection=await safety_manager.needs_mode_reselection(),
    )