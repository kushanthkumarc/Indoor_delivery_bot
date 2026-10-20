"""Outbound (server -> robot) command envelopes for the v1 robot gateway.

Only two command kinds exist: ``goto`` (a destination goal) and ``stop``.
There is deliberately no velocity or raw-motor command. The robot-side
executor must still re-validate every command (expiry, known frame, limits,
supervisor health) before acting; this module only builds well-formed messages.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import uuid

from app.gateway.protocol import PROTOCOL_VERSION

COMMAND_KINDS = frozenset({"goto", "stop"})
MAX_ABS_COORDINATE_M = 1000.0


def _check_finite(name: str, value: float, limit: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or abs(value) > limit:
        raise ValueError(f"{name} must be finite and within +/-{limit}")
    return float(value)


def build_command(robot_id: str, kind: str, *, command_id: str | None = None,
                  delivery_id: str | None = None, goal: dict | None = None,
                  ttl_s: float = 15.0, now: datetime | None = None) -> dict:
    """Return a JSON-serialisable command envelope addressed to one robot."""
    if kind not in COMMAND_KINDS:
        raise ValueError(f"unsupported command kind: {kind}")
    if ttl_s <= 0 or ttl_s > 300:
        raise ValueError("ttl_s must be in (0, 300]")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload: dict = {
        "command_id": command_id or str(uuid.uuid4()),
        "kind": kind,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_s)).isoformat(),
    }
    if delivery_id is not None:
        payload["delivery_id"] = str(delivery_id)[:128]
    if kind == "goto":
        if goal is None:
            raise ValueError("goto requires a goal")
        payload["goal"] = {
            "frame": "map",
            "x": _check_finite("x", goal["x"], MAX_ABS_COORDINATE_M),
            "y": _check_finite("y", goal["y"], MAX_ABS_COORDINATE_M),
            "theta": _check_finite("theta", goal["theta"], 2 * math.pi + 1e-6),
        }
    elif goal is not None:
        raise ValueError("stop must not carry a goal")
    return {
        "version": PROTOCOL_VERSION,
        "type": "command",
        "message_id": str(uuid.uuid4()),
        "robot_id": robot_id,
        "timestamp": now.isoformat(),
        "payload": payload,
    }
