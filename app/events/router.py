"""
Events audit log — read endpoint.

GET /events  — ADMIN only, filterable, paginated.

Requirements: 9.10, 1.10
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_role
from app.db.models import Event, EventSeverity, User, UserRole
from app.db.session import get_db
from pydantic import BaseModel

router = APIRouter(prefix="/events", tags=["events"])

_require_admin = Depends(require_role(UserRole.ADMIN))


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------


class EventResponse(BaseModel):
    id: str
    type: str
    severity: EventSeverity
    payload_json: dict
    robot_id: str | None
    user_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class EventListResponse(BaseModel):
    items: list[EventResponse]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("", response_model=EventListResponse)
async def list_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: User = _require_admin,
    # Filters — Requirement 9.10
    severity: Annotated[EventSeverity | None, Query(description="Filter by severity")] = None,
    type: Annotated[str | None, Query(description="Filter by event type")] = None,
    robot_id: Annotated[str | None, Query(description="Filter by robot_id")] = None,
    user_id: Annotated[str | None, Query(description="Filter by user_id")] = None,
    from_date: Annotated[datetime | None, Query(description="Filter events on or after this UTC datetime")] = None,
    to_date: Annotated[datetime | None, Query(description="Filter events on or before this UTC datetime")] = None,
    # Pagination
    page: Annotated[int, Query(ge=1, description="Page number (1-based)")] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, description="Items per page")] = 50,
) -> EventListResponse:
    """
    Return a paginated, filterable list of audit log events.

    ADMIN only (Requirement 9.10).
    Supports filters: severity, type, robot_id, user_id, date range.
    """
    stmt = select(Event)

    if severity is not None:
        stmt = stmt.where(Event.severity == severity)
    if type is not None:
        stmt = stmt.where(Event.type == type)
    if robot_id is not None:
        stmt = stmt.where(Event.robot_id == robot_id)
    if user_id is not None:
        stmt = stmt.where(Event.user_id == user_id)
    if from_date is not None:
        stmt = stmt.where(Event.created_at >= from_date)
    if to_date is not None:
        stmt = stmt.where(Event.created_at <= to_date)

    # Total count (reuse the same filters via a subquery)
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total: int = (await db.execute(count_stmt)).scalar_one()

    # Apply ordering and pagination
    stmt = (
        stmt.order_by(Event.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(stmt)
    events = result.scalars().all()

    return EventListResponse(
        items=[EventResponse.model_validate(e) for e in events],
        total=total,
        page=page,
        page_size=page_size,
    )
