"""
Destination Management Service — Module 3.

CRUD for named delivery destinations with coordinate validation against
finished mapping session bounds, Home-destination protection, and active
delivery protection.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Delivery,
    DeliveryStatus,
    Destination,
    EventSeverity,
    MappingSession,
    MappingSessionStatus,
)
from app.events.service import log_event

# Delivery statuses that mean the destination is still "in use"
_ACTIVE_DELIVERY_STATUSES = {
    DeliveryStatus.IDLE,
    DeliveryStatus.GO_TO_DESTINATION,
    DeliveryStatus.ARRIVED,
    DeliveryStatus.DELIVERED,
    DeliveryStatus.RETURN_HOME,
    DeliveryStatus.HOME,
}


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class DestinationNotFoundError(ValueError):
    pass


class HomeProtectedError(ValueError):
    """Raised when an attempt is made to delete the Home destination (Req 4.3)."""
    pass


class ActiveDeliveryError(ValueError):
    """Raised when destination is referenced by an active delivery (Req 4.7)."""
    pass


class InvalidCoordinatesError(ValueError):
    """Raised when coordinates fall outside the finished map bounds (Req 4.2, 4.6)."""
    pass


class MapNotFoundError(ValueError):
    """Raised when the referenced map_id is not a finished mapping session."""
    pass


# ---------------------------------------------------------------------------
# Coordinate validation
# ---------------------------------------------------------------------------


async def validate_coordinates(
    db: AsyncSession,
    x: float,
    y: float,
    map_id: str,
) -> None:
    """
    Validate (x, y) falls within the bounds of a finished mapping session.

    Bounds are derived from the pose history stored in last_pose_json.
    If no pose bounds metadata is available the check is skipped (permissive
    fallback so sessions with no poses don't block destination creation).

    Requirements: 4.2, 4.6
    """
    result = await db.execute(
        select(MappingSession).where(
            MappingSession.id == map_id,
            MappingSession.status == MappingSessionStatus.FINISHED,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise MapNotFoundError(
            f"No finished mapping session found with id '{map_id}'"
        )

    # If the session stored explicit bounds in its pose metadata, validate against them.
    # The map stores accumulated bounds as {"x_min": ..., "x_max": ..., "y_min": ..., "y_max": ...}
    # alongside the last pose. If those keys are absent we allow the coordinates through.
    bounds = session.last_pose_json or {}
    x_min = bounds.get("x_min")
    x_max = bounds.get("x_max")
    y_min = bounds.get("y_min")
    y_max = bounds.get("y_max")

    if all(v is not None for v in (x_min, x_max, y_min, y_max)):
        if not (x_min <= x <= x_max and y_min <= y <= y_max):
            raise InvalidCoordinatesError(
                f"Coordinates ({x}, {y}) are outside map bounds "
                f"x=[{x_min}, {x_max}], y=[{y_min}, {y_max}]"
            )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def create_destination(
    db: AsyncSession,
    *,
    name: str,
    map_id: str,
    x: float,
    y: float,
    theta: float,
    is_home: bool = False,
    user_id: str | None = None,
) -> Destination:
    """
    Create a new destination after validating coordinates (Requirements 4.1, 4.2).
    """
    await validate_coordinates(db, x, y, map_id)

    destination = Destination(
        name=name,
        map_id=map_id,
        x=x,
        y=y,
        theta=theta,
        is_home=is_home,
    )
    db.add(destination)
    await db.flush()

    await log_event(
        db,
        type="destination.created",
        severity=EventSeverity.INFO,
        user_id=user_id,
        payload={"destination_id": str(destination.id), "name": name, "map_id": map_id},
    )
    return destination


async def get_destination(db: AsyncSession, destination_id: str) -> Destination:
    """Return a single destination or raise DestinationNotFoundError (Requirement 4.8)."""
    return await _get_or_raise(db, destination_id)


async def list_destinations(
    db: AsyncSession,
    map_id: str | None = None,
) -> list[Destination]:
    """
    Return all destinations, optionally filtered by map_id (Requirement 4.5).
    """
    stmt = select(Destination).order_by(Destination.created_at.asc())
    if map_id is not None:
        stmt = stmt.where(Destination.map_id == map_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_destination(
    db: AsyncSession,
    destination_id: str,
    *,
    name: str | None = None,
    x: float | None = None,
    y: float | None = None,
    theta: float | None = None,
    user_id: str | None = None,
) -> Destination:
    """
    Update destination fields, re-validating coordinates if x or y change (Req 4.6).
    """
    destination = await _get_or_raise(db, destination_id)

    new_x = x if x is not None else destination.x
    new_y = y if y is not None else destination.y

    # Re-validate coordinates only if they are being changed
    if x is not None or y is not None:
        await validate_coordinates(db, new_x, new_y, str(destination.map_id))

    if name is not None:
        destination.name = name
    if x is not None:
        destination.x = x
    if y is not None:
        destination.y = y
    if theta is not None:
        destination.theta = theta

    await db.flush()

    await log_event(
        db,
        type="destination.updated",
        severity=EventSeverity.INFO,
        user_id=user_id,
        payload={"destination_id": destination_id},
    )
    return destination


async def delete_destination(
    db: AsyncSession,
    destination_id: str,
    *,
    user_id: str | None = None,
) -> None:
    """
    Delete a destination.

    - Raises HomeProtectedError if is_home=True (Requirement 4.3).
    - Raises ActiveDeliveryError if referenced by an active delivery (Requirement 4.7).
    """
    destination = await _get_or_raise(db, destination_id)

    if destination.is_home:
        raise HomeProtectedError(
            "The Home destination is reserved and cannot be deleted"
        )

    # Check for active deliveries referencing this destination
    active_result = await db.execute(
        select(Delivery).where(
            Delivery.destination_id == destination_id,
            Delivery.status.in_(_ACTIVE_DELIVERY_STATUSES),
        )
    )
    active_delivery = active_result.scalar_one_or_none()
    if active_delivery is not None:
        raise ActiveDeliveryError(
            f"Destination is referenced by active delivery '{active_delivery.id}'"
        )

    await db.delete(destination)
    await db.flush()

    await log_event(
        db,
        type="destination.deleted",
        severity=EventSeverity.INFO,
        user_id=user_id,
        payload={"destination_id": destination_id},
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_or_raise(db: AsyncSession, destination_id: str) -> Destination:
    result = await db.execute(
        select(Destination).where(Destination.id == destination_id)
    )
    destination = result.scalar_one_or_none()
    if destination is None:
        raise DestinationNotFoundError(
            f"Destination '{destination_id}' not found"
        )
    return destination
