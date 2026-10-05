"""
SQLAlchemy ORM models for all 6 tables.

All ENUMs are defined as Python enum.Enum first, then wrapped in
SQLAlchemy's Enum type so they are shared between models and application code.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BIGINT,
    CHAR,
    FLOAT,
    JSON,
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.session import Base


# ---------------------------------------------------------------------------
# Python enums
# ---------------------------------------------------------------------------


class UserRole(str, enum.Enum):
    ADMIN = "ADMIN"
    USER = "USER"


class RobotStatus(str, enum.Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"


class ControlMode(str, enum.Enum):
    MANUAL = "MANUAL"
    AUTONOMOUS = "AUTONOMOUS"
    STOPPED = "STOPPED"


class SafetyState(str, enum.Enum):
    SAFE = "SAFE"
    SLOW = "SLOW"
    REPLAN = "REPLAN"
    EMERGENCY_STOP = "EMERGENCY_STOP"


class SlamState(str, enum.Enum):
    TRACKING = "TRACKING"
    LOST = "LOST"
    RELOCATING = "RELOCATING"
    IDLE = "IDLE"


class MappingSessionStatus(str, enum.Enum):
    IDLE = "IDLE"
    MAPPING = "MAPPING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"


class SlamStatusEnum(str, enum.Enum):
    TRACKING = "TRACKING"
    LOST = "LOST"
    RELOCATING = "RELOCATING"


class DeliveryStatus(str, enum.Enum):
    IDLE = "IDLE"
    GO_TO_DESTINATION = "GO_TO_DESTINATION"
    ARRIVED = "ARRIVED"
    DELIVERED = "DELIVERED"
    RETURN_HOME = "RETURN_HOME"
    HOME = "HOME"
    EMERGENCY_STOP = "EMERGENCY_STOP"


class EventSeverity(str, enum.Enum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------


class User(Base):
    """Requirement 9.2 — users table."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="userrole"),
        nullable=False,
        default=UserRole.USER,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    # Relationships
    deliveries: Mapped[list["Delivery"]] = relationship(
        "Delivery", back_populates="requester", foreign_keys="Delivery.requester_id"
    )
    events: Mapped[list["Event"]] = relationship("Event", back_populates="user")
    robot_access_grants: Mapped[list["UserRobotAccess"]] = relationship(
        "UserRobotAccess",
        back_populates="user",
        foreign_keys="UserRobotAccess.user_id",
        cascade="all, delete-orphan",
    )

    __table_args__ = (Index("ix_users_email", "email"),)


class Robot(Base):
    """Requirement 9.1 — robots table."""

    __tablename__ = "robots"

    id: Mapped[uuid.UUID] = mapped_column(
        CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[RobotStatus] = mapped_column(
        SAEnum(RobotStatus, name="robotstatus"),
        nullable=False,
        default=RobotStatus.OFFLINE,
    )
    control_mode: Mapped[ControlMode] = mapped_column(
        SAEnum(ControlMode, name="controlmode"),
        nullable=False,
        default=ControlMode.STOPPED,
    )
    safety_state: Mapped[SafetyState] = mapped_column(
        SAEnum(SafetyState, name="safetystate"),
        nullable=False,
        default=SafetyState.SAFE,
    )
    slam_state: Mapped[SlamState] = mapped_column(
        SAEnum(SlamState, name="slamstate"),
        nullable=False,
        default=SlamState.IDLE,
    )
    battery_level: Mapped[float | None] = mapped_column(FLOAT, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    firmware_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    # Relationships
    mapping_sessions: Mapped[list["MappingSession"]] = relationship(
        "MappingSession", back_populates="robot"
    )
    deliveries: Mapped[list["Delivery"]] = relationship(
        "Delivery", back_populates="robot"
    )
    events: Mapped[list["Event"]] = relationship("Event", back_populates="robot")
    user_access_grants: Mapped[list["UserRobotAccess"]] = relationship(
        "UserRobotAccess",
        back_populates="robot",
        foreign_keys="UserRobotAccess.robot_id",
        cascade="all, delete-orphan",
    )


class MappingSession(Base):
    """Requirement 9.3 — mapping_sessions table."""

    __tablename__ = "mapping_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    robot_id: Mapped[uuid.UUID] = mapped_column(
        CHAR(36),
        ForeignKey("robots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[MappingSessionStatus] = mapped_column(
        SAEnum(MappingSessionStatus, name="mappingsessionstatus"),
        nullable=False,
        default=MappingSessionStatus.IDLE,
    )
    elapsed_ms: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    pose_update_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_pose_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    slam_status: Mapped[SlamStatusEnum | None] = mapped_column(
        SAEnum(SlamStatusEnum, name="slamstatusenum"),
        nullable=True,
    )
    map_data_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    # Relationships
    robot: Mapped["Robot"] = relationship("Robot", back_populates="mapping_sessions")
    destinations: Mapped[list["Destination"]] = relationship(
        "Destination", back_populates="map_session"
    )

    __table_args__ = (Index("ix_mapping_sessions_robot_id", "robot_id"),)


class Destination(Base):
    """Requirement 9.4 — destinations table."""

    __tablename__ = "destinations"

    id: Mapped[uuid.UUID] = mapped_column(
        CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    map_id: Mapped[uuid.UUID] = mapped_column(
        CHAR(36),
        ForeignKey("mapping_sessions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    x: Mapped[float] = mapped_column(FLOAT, nullable=False)
    y: Mapped[float] = mapped_column(FLOAT, nullable=False)
    theta: Mapped[float] = mapped_column(FLOAT, nullable=False)
    is_home: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    # Relationships
    map_session: Mapped["MappingSession"] = relationship(
        "MappingSession", back_populates="destinations"
    )
    deliveries: Mapped[list["Delivery"]] = relationship(
        "Delivery", back_populates="destination"
    )

    __table_args__ = (Index("ix_destinations_map_id", "map_id"),)


class Delivery(Base):
    """Requirement 9.5 — deliveries table."""

    __tablename__ = "deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    robot_id: Mapped[uuid.UUID] = mapped_column(
        CHAR(36),
        ForeignKey("robots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    destination_id: Mapped[uuid.UUID] = mapped_column(
        CHAR(36),
        ForeignKey("destinations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requester_id: Mapped[uuid.UUID] = mapped_column(
        CHAR(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        SAEnum(DeliveryStatus, name="deliverystatus"),
        nullable=False,
        default=DeliveryStatus.IDLE,
    )
    state_history_json: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    nav_goal_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    robot: Mapped["Robot"] = relationship("Robot", back_populates="deliveries")
    destination: Mapped["Destination"] = relationship(
        "Destination", back_populates="deliveries"
    )
    requester: Mapped["User"] = relationship(
        "User",
        back_populates="deliveries",
        foreign_keys=[requester_id],
    )

    __table_args__ = (
        Index("ix_deliveries_robot_id", "robot_id"),
        Index("ix_deliveries_requester_id", "requester_id"),
        Index("ix_deliveries_status", "status"),
    )


class Event(Base):
    """Requirement 9.6 — events append-only audit log table."""

    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(
        CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    robot_id: Mapped[uuid.UUID | None] = mapped_column(
        CHAR(36),
        ForeignKey("robots.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        CHAR(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    severity: Mapped[EventSeverity] = mapped_column(
        SAEnum(EventSeverity, name="eventseverity"),
        nullable=False,
        default=EventSeverity.INFO,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    robot: Mapped["Robot | None"] = relationship("Robot", back_populates="events")
    user: Mapped["User | None"] = relationship("User", back_populates="events")

    __table_args__ = (
        Index("ix_events_robot_id", "robot_id"),
        Index("ix_events_user_id", "user_id"),
        Index("ix_events_severity", "severity"),
        Index("ix_events_type", "type"),
        Index("ix_events_created_at", "created_at"),
    )


class UserRobotAccess(Base):
    """
    Per-user robot-access grant.

    A USER can only dispatch deliveries on a robot if a row exists here for
    that (user_id, robot_id) pair. ADMIN bypasses this check (admin can act
    on any robot).

    A user may have access to multiple robots, and a robot may be granted to
    multiple users. `granted_by` records which admin provisioned the access
    (audit trail). Soft-deleting a user via DELETE /admin/users/:id cascades
    and revokes all their robot access automatically.
    """

    __tablename__ = "user_robot_access"

    user_id: Mapped[uuid.UUID] = mapped_column(
        CHAR(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    robot_id: Mapped[uuid.UUID] = mapped_column(
        CHAR(36),
        ForeignKey("robots.id", ondelete="CASCADE"),
        primary_key=True,
    )
    granted_by: Mapped[uuid.UUID] = mapped_column(
        CHAR(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    # Relationships
    user: Mapped["User"] = relationship(
        "User", back_populates="robot_access_grants", foreign_keys=[user_id]
    )
    robot: Mapped["Robot"] = relationship(
        "Robot", back_populates="user_access_grants", foreign_keys=[robot_id]
    )
    granted_by_user: Mapped["User"] = relationship(
        "User", foreign_keys=[granted_by]
    )

    __table_args__ = (
        Index("ix_ura_user_id", "user_id"),
        Index("ix_ura_robot_id", "robot_id"),
    )
