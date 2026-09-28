"""
Robot API key authentication.

The robot authenticates via a static X-Robot-API-Key header — not JWT.
Requirement 1.9
"""

import bcrypt
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.config import settings
from app.db.models import Robot
from app.db.session import AsyncSession, get_db
from sqlalchemy import select

_api_key_header = APIKeyHeader(name="X-Robot-API-Key", auto_error=False)


async def validate_robot_api_key(
    api_key: str | None = Security(_api_key_header),
    db: AsyncSession = Depends(get_db),
) -> Robot:
    """
    Validate the X-Robot-API-Key header and return the matching Robot record.

    Compares the provided key against each robot's stored bcrypt hash.
    Raises HTTP 401 if no valid key is found.
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_API_KEY"},
        )

    # Load all robots and check the key against each hash.
    # In practice there is only one robot, so this is acceptable.
    result = await db.execute(select(Robot))
    robots = result.scalars().all()

    for robot in robots:
        try:
            if bcrypt.checkpw(api_key.encode(), robot.api_key_hash.encode()):
                return robot
        except Exception:
            continue

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "INVALID_API_KEY"},
    )
