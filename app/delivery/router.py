"""
Delivery Router — Module 4.2 API endpoints.

REST API for managing deliveries.
Requirements: 5.1, 5.10, 5.11, 5.12
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.auth.dependencies import get_current_user, require_admin
from app.db.models import (
    ControlMode,
    Delivery,
    DeliveryStatus,
    Destination,
    MappingSession,
    MappingSessionStatus,
    Robot,
    RobotStatus,
    SafetyState,
    User,
    UserRole,
    UserRobotAccess,
)
from app.db.session import AsyncSessionLocal
from app.delivery.state_machine import DeliveryStateMachine, InvalidTransitionError


router = APIRouter(prefix="/deliveries", tags=["Deliveries"])
# No navigation transport is configured in this backend. Keep the state machine
# available for explicit adapter injection, but never simulate physical progress.
delivery_state_machine: DeliveryStateMachine | None = None


def _require_navigation() -> DeliveryStateMachine:
    if delivery_state_machine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "NAVIGATION_UNAVAILABLE", "message": "No navigation transport is configured"},
        )
    return delivery_state_machine


def _actor_id(user: User) -> str:
    return str(user.id)


def _delivery_response(delivery: Delivery, destination_name: str | None = None, source_name: str | None = None) -> dict:
    """Keep list/detail fields consistent with the Android delivery contract."""
    return {"id": str(delivery.id), "status": delivery.status.value,
            "history": delivery.state_history_json,
            "robot_id": str(delivery.robot_id),
            "destination_id": str(delivery.destination_id),
            "destination_name": destination_name,
            "source_id": str(delivery.source_id) if delivery.source_id else None,
            "source_name": source_name,
            "created_at": delivery.created_at.isoformat() if delivery.created_at else None,
            "completed_at": delivery.completed_at.isoformat() if delivery.completed_at else None,
            "physical_execution_confirmed": False}


async def _load_delivery(delivery_id: str) -> tuple[Delivery, Robot, Destination]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Delivery).where(Delivery.id == delivery_id))
        delivery = result.scalar_one_or_none()
        if delivery is None:
            raise HTTPException(status_code=404, detail={"error": "DELIVERY_NOT_FOUND"})
        robot_result = await db.execute(select(Robot).where(Robot.id == delivery.robot_id))
        robot = robot_result.scalar_one_or_none()
        destination_result = await db.execute(select(Destination).where(Destination.id == delivery.destination_id))
        destination = destination_result.scalar_one_or_none()
        if robot is None or destination is None:
            raise HTTPException(status_code=409, detail={"error": "DELIVERY_CONFIGURATION_INVALID"})
        return delivery, robot, destination


async def _validate_destination_map(map_id: str, robot_id: str) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(MappingSession).where(
            MappingSession.id == map_id, MappingSession.robot_id == robot_id
        ))
        map_session = result.scalar_one_or_none()
        if map_session is None or map_session.status != MappingSessionStatus.FINISHED:
            raise HTTPException(status_code=409, detail={"error": "DESTINATION_MAP_NOT_READY"})


def _validate_robot_for_dispatch(robot: Robot) -> None:
    if robot.status != RobotStatus.ONLINE:
        raise HTTPException(status_code=409, detail={"error": "ROBOT_OFFLINE"})
    if robot.control_mode != ControlMode.AUTONOMOUS:
        raise HTTPException(status_code=409, detail={"error": "ROBOT_NOT_AUTONOMOUS"})
    if robot.safety_state in {SafetyState.EMERGENCY_STOP, SafetyState.SLOW, SafetyState.REPLAN}:
        raise HTTPException(status_code=409, detail={"error": "ROBOT_SAFETY_STATE_BLOCKS_DISPATCH"})


class CreateDeliveryRequest(BaseModel):
    source_id: str
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

        # Booking is separate from dispatch: offline robots may receive queued
        # requests. Physical readiness is enforced again at dispatch time.
        if request.source_id == request.destination_id:
            raise HTTPException(status_code=422, detail={"error": "SOURCE_EQUALS_DESTINATION"})
        source = await db.get(Destination, request.source_id)
        destination = await db.get(Destination, request.destination_id)
        if source is None or destination is None:
            raise HTTPException(status_code=404, detail={"error": "LOCATION_NOT_FOUND"})
        if source.map_id != destination.map_id:
            raise HTTPException(status_code=409, detail={"error": "LOCATIONS_DIFFERENT_MAPS"})
        await _validate_destination_map(str(source.map_id), request.robot_id)
            
        delivery = Delivery(
            robot_id=request.robot_id,
            source_id=request.source_id,
            destination_id=request.destination_id,
            requester_id=str(current_user.id),
            status=DeliveryStatus.IDLE,
            state_history_json=[]
        )
        db.add(delivery)
        await db.commit()
        await db.refresh(delivery)
        
        return _delivery_response(delivery, destination.name, source.name)


@router.get("")
async def get_all_deliveries(
    current_admin: Annotated[User, Depends(require_admin())],
) -> Any:
    """
    Get all deliveries. ADMIN only.
    Requirement 5.10
    """
    async with AsyncSessionLocal() as db:
        source = aliased(Destination)
        res = await db.execute(select(Delivery, Destination.name, source.name)
            .join(Destination, Delivery.destination_id == Destination.id)
            .outerjoin(source, Delivery.source_id == source.id).order_by(Delivery.created_at.desc()))
        return [_delivery_response(d, name, pickup) for d, name, pickup in res.all()]


@router.get("/mine")
async def get_my_deliveries(
    current_user: Annotated[User, Depends(get_current_user)],
) -> Any:
    """
    Get my deliveries. (USER or ADMIN).
    Requirement 5.11
    """
    async with AsyncSessionLocal() as db:
        source = aliased(Destination)
        res = await db.execute(select(Delivery, Destination.name, source.name)
            .join(Destination, Delivery.destination_id == Destination.id)
            .outerjoin(source, Delivery.source_id == source.id).where(
            Delivery.requester_id == str(current_user.id)).order_by(Delivery.created_at.desc()))
        return [_delivery_response(d, name, pickup) for d, name, pickup in res.all()]


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
             
        destination = await db.get(Destination, delivery.destination_id)
        source = await db.get(Destination, delivery.source_id) if delivery.source_id else None
        return _delivery_response(delivery, destination.name if destination else None, source.name if source else None)


@router.post("/{delivery_id}/dispatch")
async def dispatch_delivery(
    delivery_id: str,
    current_admin: Annotated[User, Depends(require_admin())],
) -> Any:
    """
    Dispatch delivery to robot (ADMIN only).
    """
    delivery, robot, destination = await _load_delivery(delivery_id)
    state_machine = _require_navigation()
    _validate_robot_for_dispatch(robot)
    await _validate_destination_map(str(destination.map_id), str(robot.id))
    if delivery.status not in {DeliveryStatus.IDLE, DeliveryStatus.GO_TO_SOURCE, DeliveryStatus.GO_TO_DESTINATION}:
        raise HTTPException(status_code=409, detail={"error": "INVALID_DELIVERY_STATE"})
    try:
        updated = await state_machine.dispatch(delivery_id, _actor_id(current_admin))
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail={"error": "INVALID_DELIVERY_STATE", "message": str(exc)})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error": "DELIVERY_CONFIGURATION_INVALID", "message": str(exc)})
    return {"id": str(updated.id), "status": updated.status.value,
            "physical_execution_confirmed": False}


@router.post("/{delivery_id}/confirm-loading")
async def confirm_loading(delivery_id: str, current_user: Annotated[User, Depends(get_current_user)]) -> Any:
    """The requester/admin confirms the package is loaded at the mapped source."""
    delivery, robot, destination = await _load_delivery(delivery_id)
    if current_user.role != UserRole.ADMIN and str(delivery.requester_id) != _actor_id(current_user):
        raise HTTPException(status_code=403, detail={"error": "DELIVERY_ACCESS_DENIED"})
    machine = _require_navigation()
    _validate_robot_for_dispatch(robot)
    await _validate_destination_map(str(destination.map_id), str(robot.id))
    try:
        updated = await machine.dispatch(delivery_id, _actor_id(current_user), loading_confirmed=True)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail={"error": "INVALID_DELIVERY_STATE", "message": str(exc)})
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"error": "DELIVERY_CONFIGURATION_INVALID", "message": str(exc)})
    return {"id": str(updated.id), "status": updated.status.value, "physical_execution_confirmed": False}

@router.post("/{delivery_id}/confirm-pickup")
async def confirm_pickup(
    delivery_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
) -> Any:
    """
    Confirm package pickup at destination.
    Requirement 5.4
    """
    delivery, _robot, _destination = await _load_delivery(delivery_id)
    if current_user.role != UserRole.ADMIN and str(delivery.requester_id) != _actor_id(current_user):
        raise HTTPException(status_code=403, detail={"error": "DELIVERY_ACCESS_DENIED"})
    try:
        if delivery.source_id:
            # Receipt is an operator attestation, not a navigation command.
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Delivery).where(Delivery.id == delivery_id).with_for_update())
                updated = result.scalar_one_or_none()
                if updated is None:
                    raise ValueError("Delivery not found")
                await DeliveryStateMachine.record_source_receipt(db, updated, _actor_id(current_user))
                await db.commit()
        else:
            state_machine = _require_navigation()
            updated = await state_machine.on_pickup_confirmed(delivery_id, _actor_id(current_user))
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail={"error": "INVALID_DELIVERY_STATE", "message": str(exc)})
    except ValueError as exc:
        code = "HOME_DESTINATION_NOT_FOUND" if "Home destination" in str(exc) else "DELIVERY_CONFIGURATION_INVALID"
        raise HTTPException(status_code=409, detail={"error": code, "message": str(exc)})
    return {"id": str(updated.id), "status": updated.status.value,
            "physical_execution_confirmed": False}

@router.post("/{delivery_id}/cancel")
async def cancel_delivery(
    delivery_id: str,
    current_admin: Annotated[User, Depends(require_admin())],
) -> Any:
    """Request cancellation (ADMIN only); this does not verify physical stopping."""
    await _load_delivery(delivery_id)
    state_machine = _require_navigation()
    try:
        updated = await state_machine.cancel(delivery_id, _actor_id(current_admin))
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail={"error": "INVALID_DELIVERY_STATE", "message": str(exc)})
    except ValueError:
        raise HTTPException(status_code=404, detail={"error": "DELIVERY_NOT_FOUND"})
    return {"id": str(updated.id), "status": updated.status.value,
            "physical_execution_confirmed": False,
            "cancellation_requested": True}
