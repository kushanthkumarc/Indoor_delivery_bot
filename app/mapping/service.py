"""
Mapping Session Service — Module 1.

Manages the full lifecycle of SLAM mapping sessions with state machine
transitions and accurate elapsed time accumulation across pause/resume cycles.

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.8, 2.11
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MappingSession, MappingSessionStatus, SlamStatusEnum
from app.events.service import log_event
from app.db.models import EventSeverity


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class InvalidTransitionError(Exception):
    """Raised when an invalid state machine transition is attempted (Req 2.11)."""

    def __init__(self, current: str, requested: str) -> None:
        self.current = current
        self.requested = requested
        super().__init__(
            f"Cannot transition from '{current}' to '{requested}'"
        )


# ---------------------------------------------------------------------------
# Valid transitions
# ---------------------------------------------------------------------------

_VALID_TRANSITIONS: dict[MappingSessionStatus, set[MappingSessionStatus]] = {
    MappingSessionStatus.IDLE: {MappingSessionStatus.MAPPING},
    MappingSessionStatus.MAPPING: {MappingSessionStatus.PAUSED, MappingSessionStatus.FINISHED},
    MappingSessionStatus.PAUSED: {MappingSessionStatus.MAPPING, MappingSessionStatus.FINISHED},
    MappingSessionStatus.FINISHED: set(),
}


def _assert_transition(
    current: MappingSessionStatus,
    target: MappingSessionStatus,
) -> None:
    if target not in _VALID_TRANSITIONS.get(current, set()):
        raise InvalidTransitionError(current.value, target.value)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Service functions
# ---------------------------------------------------------------------------


async def create_session(
    db: AsyncSession,
    robot_id: str,
) -> MappingSession:
    """
    Create a new mapping session in IDLE status (Requirement 2.1).
    """
    session = MappingSession(
        robot_id=robot_id,
        status=MappingSessionStatus.IDLE,
        elapsed_ms=0,
        pose_update_count=0,
    )
    db.add(session)
    await db.flush()

    await log_event(
        db,
        type="mapping.session_created",
        severity=EventSeverity.INFO,
        robot_id=robot_id,
        payload={"session_id": str(session.id)},
    )
    return session


async def start_session(
    db: AsyncSession,
    session_id: str,
) -> MappingSession:
    """
    Transition IDLE → MAPPING, record start timestamp (Requirement 2.2).
    Raises HTTP 409 caller-side if another session is already MAPPING (Req 2.10).
    """
    session = await _get_or_404(db, session_id)
    _assert_transition(session.status, MappingSessionStatus.MAPPING)

    session.status = MappingSessionStatus.MAPPING
    session.started_at = _utcnow()
    session.paused_at = None
    await db.flush()

    await log_event(
        db,
        type="mapping.session_started",
        severity=EventSeverity.INFO,
        robot_id=str(session.robot_id),
        payload={"session_id": session_id},
    )
    return session


async def pause_session(
    db: AsyncSession,
    session_id: str,
) -> MappingSession:
    """
    Transition MAPPING → PAUSED, accumulate elapsed time (Requirement 2.3).
    """
    session = await _get_or_404(db, session_id)
    _assert_transition(session.status, MappingSessionStatus.PAUSED)

    now = _utcnow()
    session.elapsed_ms += _elapsed_since_last_resume_ms(session, now)
    session.status = MappingSessionStatus.PAUSED
    session.paused_at = now
    await db.flush()

    await log_event(
        db,
        type="mapping.session_paused",
        severity=EventSeverity.INFO,
        robot_id=str(session.robot_id),
        payload={"session_id": session_id, "elapsed_ms": session.elapsed_ms},
    )
    return session


async def resume_session(
    db: AsyncSession,
    session_id: str,
) -> MappingSession:
    """
    Transition PAUSED → MAPPING, reset the running-clock reference (Requirement 2.4).
    """
    session = await _get_or_404(db, session_id)
    _assert_transition(session.status, MappingSessionStatus.MAPPING)

    now = _utcnow()
    session.status = MappingSessionStatus.MAPPING
    # Re-use started_at as the last-resume anchor so elapsed calculation stays
    # consistent when paused again.
    session.started_at = now
    session.paused_at = None
    await db.flush()

    await log_event(
        db,
        type="mapping.session_resumed",
        severity=EventSeverity.INFO,
        robot_id=str(session.robot_id),
        payload={"session_id": session_id},
    )
    return session


async def finish_session(
    db: AsyncSession,
    session_id: str,
    map_data_ref: str | None = None,
) -> MappingSession:
    """
    Transition MAPPING|PAUSED → FINISHED, record final elapsed time (Requirement 2.5).
    """
    session = await _get_or_404(db, session_id)
    _assert_transition(session.status, MappingSessionStatus.FINISHED)

    now = _utcnow()
    if session.status == MappingSessionStatus.MAPPING:
        session.elapsed_ms += _elapsed_since_last_resume_ms(session, now)

    session.status = MappingSessionStatus.FINISHED
    session.finished_at = now
    if map_data_ref is not None:
        session.map_data_ref = map_data_ref
    await db.flush()

    await log_event(
        db,
        type="mapping.session_finished",
        severity=EventSeverity.INFO,
        robot_id=str(session.robot_id),
        payload={"session_id": session_id, "elapsed_ms": session.elapsed_ms},
    )
    return session


async def get_session(
    db: AsyncSession,
    session_id: str,
) -> MappingSession:
    """Return a single session or raise ValueError if not found (Req 2.8, 2.9)."""
    return await _get_or_404(db, session_id)


async def list_sessions(
    db: AsyncSession,
    skip: int = 0,
    limit: int = 100,
) -> list[MappingSession]:
    """Return all historical sessions (Requirement 2.9)."""
    result = await db.execute(
        select(MappingSession).order_by(MappingSession.created_at.desc()).offset(skip).limit(limit)
    )
    return list(result.scalars().all())


async def get_active_session(db: AsyncSession) -> MappingSession | None:
    """Return the currently MAPPING session, if any (Requirement 2.10)."""
    result = await db.execute(
        select(MappingSession).where(
            MappingSession.status == MappingSessionStatus.MAPPING
        )
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_or_404(db: AsyncSession, session_id: str) -> MappingSession:
    result = await db.execute(
        select(MappingSession).where(MappingSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise ValueError(f"Mapping session '{session_id}' not found")
    return session


def _elapsed_since_last_resume_ms(session: MappingSession, now: datetime) -> int:
    """
    Compute milliseconds elapsed since the last resume (or initial start).

    `started_at` is updated on every resume to act as the running-clock anchor,
    so we can simply diff against it (Requirements 2.3, 2.4).
    """
    if session.started_at is None:
        return 0
    anchor = session.started_at
    # Ensure both are timezone-aware for subtraction
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    delta = now - anchor
    return max(0, int(delta.total_seconds() * 1000))
