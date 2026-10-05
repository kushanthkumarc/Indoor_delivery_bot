"""
Delivery Router — Module 4.2 API endpoints.

REST API for managing deliveries.
Requirements: 5.1, 5.10, 5.11, 5.12
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.auth.dependencies import get_current_user, require_admin
from app.db.models import (
    ControlMode,
    Delivery,
    DeliveryStatus,
    Robot,
    RobotStatus,
    User,
    UserRole,
    UserRobotAccess,
)
from app.db.session import AsyncSessionLocal
from app.delivery.state_machine import InvalidTransitionError


router = APIRouter(prefix="/deliveries", tags=["Deliveries"])


class CreateDeliveryRequest(BaseModel):
    destination_id: str
    robot_id: str


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "Robot not available or NOT in AUTONOMOUS mode"}},
)
async def create_delivery(
    request: CreateDeliveryRequest,
    current_user: Annotated[User, Depends(get_current_user)],
) -> Any:
    """
    Book a delivery (USER or ADMIN).
    Requirement 5.1, 5.12
    """
    async with AsyncSessionLocal() as db:
        # Check robot status
        res = await db.execute(select(Robot).where(Robot.id == request.robot_id))
        robot = res.scalar_one_or_none()
        if not robot:
             raise HTTPException(status_code=404, detail="Robot not found")

        # Per-user robot access check (ADMIN bypasses)
        if current_user.role != UserRole.ADMIN:
            access_res = await db.execute(
                select(UserRobotAccess).where(
                    UserRobotAccess.user_id == str(current_user.id),
                    UserRobotAccess.robot_id == request.robot_id,
                )
            )
            if access_res.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"error": "ROBOT_ACCESS_DENIED"},
                )

        if robot.control_mode != ControlMode.AUTONOMOUS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "ROBOT_NOT_AUTONOMOUS"}
            )

        if robot.status != RobotStatus.ONLINE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "ROBOT_OFFLINE"}
            )
            
        # Check if robot is already doing a delivery
        active_res = await db.execute(
            select(Delivery).where(
                Delivery.robot_id == request.robot_id,
                Delivery.status != DeliveryStatus.IDLE
            )
        )
        if active_res.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "ROBOT_BUSY"}
            )
            
        delivery = Delivery(
            robot_id=request.robot_id,
            destination_id=request.destination_id,
            requester_id=str(current_user.id),
            status=DeliveryStatus.IDLE,
            state_history_json=[]
        )
        db.add(delivery)
        await db.commit()
        await db.refresh(delivery)
        
        return {"id": str(delivery.id), "status": delivery.status.value}


@router.get("")
async def get_all_deliveries(
    current_admin: Annotated[User, Depends(require_admin)],
) -> Any:
    """
    Get all deliveries. ADMIN only.
    Requirement 5.10
    """
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(Delivery))
        return [{"id": str(d.id), "status": d.status.value, "history": d.state_history_json} for d in res.scalars().all()]


@router.get("/mine")
async def get_my_deliveries(
    current_user: Annotated[User, Depends(get_current_user)],
) -> Any:
    """
    Get my deliveries. (USER or ADMIN).
    Requirement 5.11
    """
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(Delivery).where(Delivery.requester_id == str(current_user.id)))
        return [{"id": str(d.id), "status": d.status.value, "history": d.state_history_json} for d in res.scalars().all()]


@router.get("/{delivery_id}")
async def get_delivery(
    delivery_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
) -> Any:
    """Get single delivery."""
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(Delivery).where(Delivery.id == delivery_id))
        delivery = res.scalar_one_or_none()
        if not delivery:
            raise HTTPException(status_code=404, detail="Delivery not found")
            
        # Users can only see their own, admins can see all
        if current_user.role != UserRole.ADMIN and str(delivery.requester_id) != str(current_user.id):
             raise HTTPException(status_code=403, detail="Forbidden")
             
        return {"id": str(delivery.id), "status": delivery.status.value, "history": delivery.state_history_json}


@router.post("/{delivery_id}/dispatch")
async def dispatch_delivery(
    delivery_id: str,
    current_admin: Annotated[User, Depends(require_admin)],
) -> Any:
    """
    Dispatch delivery to robot (ADMIN only).
    """
    # Circular import resolution - load state machine from app context or singleton in real app
    # Placeholder for wiring
    from app.delivery.state_machine import DeliveryStateMachine
    from app.nav2.bridge import RosbridgeNav2Bridge
    # In a full DI setup, this would be injected.
    
    # We will simulate the failure for the sake of the endpoint implementation structure
    raise HTTPException(status_code=501, detail="Dependency injection not wired in this static file")

@router.post("/{delivery_id}/confirm-pickup")
async def confirm_pickup(
    delivery_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
) -> Any:
    """
    Confirm package pickup at destination.
    Requirement 5.4
    """
    raise HTTPException(status_code=501, detail="Dependency injection not wired in this static file")

@router.post("/{delivery_id}/cancel")
async def cancel_delivery(
    delivery_id: str,
    current_admin: Annotated[User, Depends(require_admin)],
) -> Any:
    """Cancel delivery (ADMIN only)."""
    raise HTTPException(status_code=501, detail="Dependency injection not wired in this static file")
