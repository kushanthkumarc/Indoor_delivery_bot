"""
Rosbridge event handlers for mapping sessions.

These handlers are invoked by the rosbridge client message router when the
robot publishes pose updates or SLAM status changes during an active session.

Requirements: 2.6, 2.7
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MappingSession, MappingSessionStatus, SlamStatusEnum
from app.events.service import log_event
from app.db.models import EventSeverity


async def handle_pose_update(
    db: AsyncSession,
    session_id: str,
    x: float,
    y: float,
    theta: float,
    timestamp: str | None = None,
) -> None:
    """
    Increment pose_update_count and store last_pose_json atomically (Requirement 2.6).

    Uses a single UPDATE statement rather than a read-modify-write cycle to
    avoid race conditions when multiple pose updates arrive rapidly.
    """
    pose = {"x": x, "y": y, "theta": theta, "timestamp": timestamp}

    # Atomically increment count and store latest pose in one UPDATE
    await db.execute(
        update(MappingSession)
        .where(
            MappingSession.id == session_id,
            MappingSession.status == MappingSessionStatus.MAPPING,
        )
        .values(
            pose_update_count=MappingSession.pose_update_count + 1,
            last_pose_json=pose,
        )
    )
    # flush so the change is visible within this transaction
    await db.flush()


async def handle_slam_status_update(
    db: AsyncSession,
    session_id: str,
    slam_status: str,
) -> None:
    """
    Mirror the robot's SLAM status change in the active session record (Requirement 2.7).

    Ignores the update if the session is not in MAPPING status (e.g. already FINISHED).
    Logs a WARN event when the SLAM status is LOST or RELOCATING.
    """
    try:
        status_enum = SlamStatusEnum(slam_status)
    except ValueError:
        # Unknown status string — log and ignore rather than crashing
        await log_event(
            db,
            type="mapping.unknown_slam_status",
            severity=EventSeverity.WARN,
            payload={"session_id": session_id, "raw_status": slam_status},
        )
        return

    await db.execute(
        update(MappingSession)
        .where(
            MappingSession.id == session_id,
            MappingSession.status == MappingSessionStatus.MAPPING,
        )
        .values(slam_status=status_enum)
    )
    await db.flush()

    severity = (
        EventSeverity.WARN
        if status_enum in (SlamStatusEnum.LOST, SlamStatusEnum.RELOCATING)
        else EventSeverity.INFO
    )
    await log_event(
        db,
        type="mapping.slam_status_updated",
        severity=severity,
        payload={"session_id": session_id, "slam_status": slam_status},
    )
