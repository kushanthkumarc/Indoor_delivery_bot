"""
Admin user management endpoints.

POST   /admin/users       — create user (Requirement 1.2)
GET    /admin/users       — list all users (Requirement 1.11)
PATCH  /admin/users/:id   — update role / deactivate (Requirement 1.11)
DELETE /admin/users/:id   — soft-delete (Requirement 1.12)

All routes require ADMIN JWT (Requirement 1.7).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_role
from app.auth.service import hash_password
from app.db.models import (
    Event,
    EventSeverity,
    Robot,
    User,
    UserRole,
    UserRobotAccess,
)
from app.db.session import get_db

router = APIRouter(prefix="/admin", tags=["admin"])

_require_admin = Depends(require_role(UserRole.ADMIN))


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str
    name: str
    role: UserRole = UserRole.USER


class UpdateUserRequest(BaseModel):
    role: UserRole | None = None
    is_active: bool | None = None


class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    role: UserRole
    is_active: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _log(
    db: AsyncSession,
    event_type: str,
    severity: EventSeverity,
    user_id: str | None = None,
    actor_id: str | None = None,
    payload: dict | None = None,
) -> None:
    event = Event(
        type=event_type,
        severity=severity,
        user_id=user_id,
        payload_json={"actor_id": actor_id, **(payload or {})},
    )
    db.add(event)
    await db.flush()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: User = _require_admin,
) -> UserResponse:
    """Create a new user account (Requirement 1.2)."""
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "EMAIL_ALREADY_EXISTS"},
        )

    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        name=body.name,
        role=body.role,
    )
    db.add(user)
    await db.flush()

    await _log(
        db,
        "admin.user_created",
        EventSeverity.INFO,
        user_id=str(user.id),
        actor_id=str(admin.id),
    )

    return UserResponse.model_validate(user)


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: User = _require_admin,
) -> list[UserResponse]:
    """List all user accounts."""
    result = await db.execute(select(User).order_by(User.created_at))
    users = result.scalars().all()
    return [UserResponse.model_validate(u) for u in users]


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    body: UpdateUserRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: User = _require_admin,
) -> UserResponse:
    """
    Promote a USER to ADMIN or deactivate an account (Requirement 1.11).
    Auth events are written on role change.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "USER_NOT_FOUND"})

    if body.role is not None:
        old_role = user.role
        user.role = body.role
        await _log(
            db,
            "admin.role_changed",
            EventSeverity.INFO,
            user_id=str(user.id),
            actor_id=str(admin.id),
            payload={"old_role": old_role.value, "new_role": body.role.value},
        )

    if body.is_active is not None:
        user.is_active = body.is_active
        await _log(
            db,
            "admin.user_deactivated" if not body.is_active else "admin.user_activated",
            EventSeverity.INFO,
            user_id=str(user.id),
            actor_id=str(admin.id),
        )

    await db.flush()
    return UserResponse.model_validate(user)


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_user(
    user_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: User = _require_admin,
) -> None:
    """
    Soft-delete a user: set is_active=False, preserve all delivery records
    (Requirement 1.12).
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "USER_NOT_FOUND"})

    if str(user.id) == str(admin.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "CANNOT_DELETE_SELF"},
        )

    user.is_active = False
    await db.flush()

    await _log(
        db,
        "admin.user_deleted",
        EventSeverity.WARN,
        user_id=str(user.id),
        actor_id=str(admin.id),
    )


# ---------------------------------------------------------------------------
# Robot access grants
# ---------------------------------------------------------------------------


class RobotAccessResponse(BaseModel):
    user_id: str
    robot_id: str
    granted_by: str
    granted_at: str

    model_config = {"from_attributes": True}


@router.post(
    "/users/{user_id}/robots/{robot_id}",
    response_model=RobotAccessResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "User or robot not found"},
        409: {"description": "Access already granted"},
    },
)
async def grant_robot_access(
    user_id: str,
    robot_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: User = _require_admin,
) -> RobotAccessResponse:
    """Grant a user access to a robot. ADMIN-only (Requirement: per-user robot scope)."""
    user_res = await db.execute(select(User).where(User.id == user_id))
    user = user_res.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"error": "USER_NOT_FOUND"}
        )

    robot_res = await db.execute(select(Robot).where(Robot.id == robot_id))
    robot = robot_res.scalar_one_or_none()
    if robot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"error": "ROBOT_NOT_FOUND"}
        )

    existing_res = await db.execute(
        select(UserRobotAccess).where(
            UserRobotAccess.user_id == user_id,
            UserRobotAccess.robot_id == robot_id,
        )
    )
    if existing_res.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "ACCESS_ALREADY_GRANTED"},
        )

    grant = UserRobotAccess(
        user_id=user_id, robot_id=robot_id, granted_by=str(admin.id)
    )
    db.add(grant)
    await db.flush()
    await db.refresh(grant)

    await _log(
        db,
        "admin.robot_access_granted",
        EventSeverity.INFO,
        user_id=str(user.id),
        actor_id=str(admin.id),
        payload={"robot_id": str(robot.id)},
    )

    return RobotAccessResponse(
        user_id=str(grant.user_id),
        robot_id=str(grant.robot_id),
        granted_by=str(grant.granted_by),
        granted_at=grant.granted_at.isoformat(),
    )


@router.delete(
    "/users/{user_id}/robots/{robot_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={404: {"description": "Access grant not found"}},
)
async def revoke_robot_access(
    user_id: str,
    robot_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: User = _require_admin,
) -> None:
    """Revoke a user's access to a robot. ADMIN-only."""
    res = await db.execute(
        select(UserRobotAccess).where(
            UserRobotAccess.user_id == user_id,
            UserRobotAccess.robot_id == robot_id,
        )
    )
    grant = res.scalar_one_or_none()
    if grant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"error": "ACCESS_NOT_FOUND"}
        )

    await db.delete(grant)
    await db.flush()

    await _log(
        db,
        "admin.robot_access_revoked",
        EventSeverity.INFO,
        user_id=user_id,
        payload={"robot_id": robot_id},
    )


@router.get(
    "/users/{user_id}/robots", response_model=list[RobotAccessResponse]
)
async def list_user_robot_access(
    user_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: User = _require_admin,
) -> list[RobotAccessResponse]:
    """List all robot-access grants for a given user. ADMIN-only."""
    user_res = await db.execute(select(User).where(User.id == user_id))
    if user_res.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"error": "USER_NOT_FOUND"}
        )

    res = await db.execute(
        select(UserRobotAccess)
        .where(UserRobotAccess.user_id == user_id)
        .order_by(UserRobotAccess.granted_at)
    )
    grants = res.scalars().all()
    return [
        RobotAccessResponse(
            user_id=str(g.user_id),
            robot_id=str(g.robot_id),
            granted_by=str(g.granted_by),
            granted_at=g.granted_at.isoformat(),
        )
        for g in grants
    ]
