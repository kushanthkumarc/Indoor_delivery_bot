"""
Mapping Session API — Module 1.

All endpoints are ADMIN-only (Requirement 2.9, 2.10).

Requirements: 2.1, 2.9, 2.10
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_role
from app.db.models import (
    MappingSession,
    MappingSessionStatus,
    SlamStatusEnum,
    User,
    UserRole,
)
from app.db.session import get_db
from app.mapping.service import (
    InvalidTransitionError,
    create_session,
    finish_session,
    get_active_session,
    get_session,
    list_sessions,
    pause_session,
    resume_session,
    start_session,
)

router = APIRouter(prefix="/mapping/sessions", tags=["mapping"])

_admin = Depends(require_role(UserRole.ADMIN))


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------


class MappingSessionResponse(BaseModel):
    id: str
    robot_id: str
    status: MappingSessionStatus
    elapsed_ms: int
    pose_update_count: int
    last_pose_json: dict | None
    slam_status: SlamStatusEnum | None
    map_data_ref: str | None
    started_at: str | None
    paused_at: str | None
    finished_at: str | None
    created_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm(cls, obj: MappingSession) -> "MappingSessionResponse":
        return cls(
            id=str(obj.id),
            robot_id=str(obj.robot_id),
            status=obj.status,
            elapsed_ms=obj.elapsed_ms,
            pose_update_count=obj.pose_update_count,
            last_pose_json=obj.last_pose_json,
            slam_status=obj.slam_status,
            map_data_ref=obj.map_data_ref,
            started_at=obj.started_at.isoformat() if obj.started_at else None,
            paused_at=obj.paused_at.isoformat() if obj.paused_at else None,
            finished_at=obj.finished_at.isoformat() if obj.finished_at else None,
            created_at=obj.created_at.isoformat(),
        )


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    robot_id: str


class FinishSessionRequest(BaseModel):
    map_data_ref: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", response_model=MappingSessionResponse, status_code=status.HTTP_201_CREATED)
async def create(
    body: CreateSessionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: User = _admin,
) -> MappingSessionResponse:
    """Create a new mapping session in IDLE status (Requirement 2.1)."""
    session = await create_session(db, robot_id=body.robot_id)
    return MappingSessionResponse.from_orm(session)


@router.get("", response_model=list[MappingSessionResponse])
async def list_all(
    db: Annotated[AsyncSession, Depends(get_db)],
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    _admin: User = _admin,
) -> list[MappingSessionResponse]:
    """Return all historical sessions (Requirement 2.9)."""
    sessions = await list_sessions(db, skip=skip, limit=limit)
    return [MappingSessionResponse.from_orm(s) for s in sessions]


@router.get("/{session_id}", response_model=MappingSessionResponse)
async def get_one(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: User = _admin,
) -> MappingSessionResponse:
    """Get a single session by ID."""
    try:
        session = await get_session(db, session_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return MappingSessionResponse.from_orm(session)


@router.post("/{session_id}/start", response_model=MappingSessionResponse)
async def start(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: User = _admin,
) -> MappingSessionResponse:
    """
    Start a session (IDLE → MAPPING).

    Returns HTTP 409 if another session is already in MAPPING status (Req 2.10).
    Returns HTTP 422 for invalid state transitions (Req 2.11).
    """
    active = await get_active_session(db)
    if active is not None and str(active.id) != session_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "SESSION_ACTIVE", "active_session_id": str(active.id)},
        )

    try:
        session = await start_session(db, session_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "INVALID_TRANSITION",
                "current_state": exc.current,
                "requested": exc.requested,
            },
        )
    return MappingSessionResponse.from_orm(session)


@router.post("/{session_id}/pause", response_model=MappingSessionResponse)
async def pause(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: User = _admin,
) -> MappingSessionResponse:
    """Pause a MAPPING session (Requirement 2.3)."""
    try:
        session = await pause_session(db, session_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "INVALID_TRANSITION",
                "current_state": exc.current,
                "requested": exc.requested,
            },
        )
    return MappingSessionResponse.from_orm(session)


@router.post("/{session_id}/resume", response_model=MappingSessionResponse)
async def resume(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: User = _admin,
) -> MappingSessionResponse:
    """Resume a PAUSED session (Requirement 2.4)."""
    try:
        session = await resume_session(db, session_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "INVALID_TRANSITION",
                "current_state": exc.current,
                "requested": exc.requested,
            },
        )
    return MappingSessionResponse.from_orm(session)


@router.post("/{session_id}/finish", response_model=MappingSessionResponse)
async def finish(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: FinishSessionRequest = FinishSessionRequest(),
    _admin: User = _admin,
) -> MappingSessionResponse:
    """Finish a session (MAPPING|PAUSED → FINISHED) (Requirement 2.5)."""
    try:
        session = await finish_session(db, session_id, map_data_ref=body.map_data_ref)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "INVALID_TRANSITION",
                "current_state": exc.current,
                "requested": exc.requested,
            },
        )
    return MappingSessionResponse.from_orm(session)
