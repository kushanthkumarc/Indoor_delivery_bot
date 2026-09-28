"""
Events audit log service.

Provides `log_event()` — the single write path used by all other modules
to append to the append-only events table.

Requirements: 9.6, 1.10, 7.7
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, EventSeverity


async def log_event(
    db: AsyncSession,
    *,
    type: str,
    severity: EventSeverity = EventSeverity.INFO,
    payload: dict[str, Any] | None = None,
    robot_id: str | None = None,
    user_id: str | None = None,
) -> Event:
    """
    Append a new entry to the events audit log.

    This is the only write path for the events table — no updates or deletes
    are performed anywhere in the system (Requirement 9.6).

    Args:
        db:        The current async DB session.
        type:      A dot-namespaced event type, e.g. "auth.login" or "safety.emergency_stop".
        severity:  INFO | WARN | ERROR | CRITICAL (Requirement 9.6).
        payload:   Arbitrary JSON-serialisable metadata for the event.
        robot_id:  Optional FK to robots.id (Requirement 9.6).
        user_id:   Optional FK to users.id (Requirement 9.6).

    Returns:
        The newly created (flushed, not yet committed) Event ORM instance.
    """
    event = Event(
        type=type,
        severity=severity,
        payload_json=payload or {},
        robot_id=robot_id,
        user_id=user_id,
    )
    db.add(event)
    # Flush so the id is populated and the caller can reference it,
    # but leave the commit to the request lifecycle / caller.
    await db.flush()
    return event
