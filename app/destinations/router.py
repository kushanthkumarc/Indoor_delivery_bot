"""
Destination Management API — Module 3.

All endpoints are ADMIN-only (Requirements 4.1–4.8).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_role
from app.db.models import Destination, User, UserRole
from app.db.session import get_db
from app.destinations.service import (
    ActiveDeliveryError,
    DestinationNotFoundError,
    HomeProtectedError,
    InvalidCoordinatesError,
    MapNotFoundError,
    create_destination,
    delete_destination,
    get_destination,
    list_destinations,
    update_destination,
)

router = APIRouter(prefix="/destinations", tags=["destinations"])

_admin = Depends(require_role(UserRole.ADMIN))


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DestinationResponse(BaseModel):
    """Requirement 4.8 — all fields returned."""

    id: str
    name: str
    x: float
    y: float
    theta: float
    map_id: str
    is_home: bool
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm(cls, obj: Destination) -> "DestinationResponse":
        return cls(
            id=str(obj.id),
            name=obj.name,
            x=obj.x,
            y=obj.y,
            theta=obj.theta,
            map_id=str(obj.map_id),
            is_home=obj.is_home,
            created_at=obj.created_at.isoformat(),
            updated_at=obj.updated_at.isoformat(),
        )


class CreateDestinationRequest(BaseModel):
    name: str
    map_id: str
    x: float
    y: float
    theta: float
    is_home: bool = False


class UpdateDestinationRequest(BaseModel):
    name: str | None = None
    x: float | None = None
    y: float | None = None
    theta: float | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=DestinationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create(
    body: CreateDestinationRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: User = _admin,
) -> DestinationResponse:
    """
    Create a named destination linked to a finished map (Requirements 4.1, 4.2).
    """
    try:
        destination = await create_destination(
            db,
            name=body.name,
            map_id=body.map_id,
            x=body.x,
            y=body.y,
            theta=body.theta,
            is_home=body.is_home,
            user_id=str(current_user.id),
        )
    except MapNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "MAP_NOT_FOUND", "message": str(exc)},
        )
    except InvalidCoordinatesError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_COORDINATES", "message": str(exc)},
        )
    return DestinationResponse.from_orm(destination)


@router.get("", response_model=list[DestinationResponse])
async def list_all(
    db: Annotated[AsyncSession, Depends(get_db)],
    map_id: str | None = Query(default=None),
    current_user: User = _admin,
) -> list[DestinationResponse]:
    """
    List all destinations, optionally filtered by map_id (Requirement 4.5).
    """
    destinations = await list_destinations(db, map_id=map_id)
    return [DestinationResponse.from_orm(d) for d in destinations]


@router.get("/{destination_id}", response_model=DestinationResponse)
async def get_one(
    destination_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: User = _admin,
) -> DestinationResponse:
    """Get a single destination by ID (Requirement 4.8)."""
    try:
        destination = await get_destination(db, destination_id)
    except DestinationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "DESTINATION_NOT_FOUND"},
        )
    return DestinationResponse.from_orm(destination)


@router.patch("/{destination_id}", response_model=DestinationResponse)
async def update(
    destination_id: str,
    body: UpdateDestinationRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: User = _admin,
) -> DestinationResponse:
    """
    Update destination fields; re-validates coordinates if x or y change (Requirement 4.6).
    """
    try:
        destination = await update_destination(
            db,
            destination_id,
            name=body.name,
            x=body.x,
            y=body.y,
            theta=body.theta,
            user_id=str(current_user.id),
        )
    except DestinationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "DESTINATION_NOT_FOUND"},
        )
    except MapNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "MAP_NOT_FOUND", "message": str(exc)},
        )
    except InvalidCoordinatesError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_COORDINATES", "message": str(exc)},
        )
    return DestinationResponse.from_orm(destination)


@router.delete(
    "/{destination_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete(
    destination_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: User = _admin,
) -> None:
    """
    Delete a destination.

    - HTTP 400 if it is the Home destination (Requirement 4.3).
    - HTTP 409 if referenced by an active delivery (Requirement 4.7).
    """
    try:
        await delete_destination(db, destination_id, user_id=str(current_user.id))
    except DestinationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "DESTINATION_NOT_FOUND"},
        )
    except HomeProtectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "HOME_PROTECTED", "message": str(exc)},
        )
    except ActiveDeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "ACTIVE_DELIVERY", "message": str(exc)},
        )
