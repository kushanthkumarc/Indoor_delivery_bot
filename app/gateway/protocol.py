"""Schemas and per-process replay/order checks for robot gateway messages.

This module validates telemetry only. It deliberately contains no ROS or
physical-command adapter.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PROTOCOL_VERSION = 1


class PosePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    x: float
    y: float
    theta: float

    @field_validator("x", "y", "theta")
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("pose values must be finite")
        return value


class TelemetryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["MANUAL", "AUTONOMOUS", "STOPPED"] | None = None
    controller_connected: bool | None = None
    safety_state: Literal["SAFE", "SLOW", "REPLAN", "EMERGENCY_STOP"] | None = None
    localization_status: str | None = Field(default=None, max_length=64)
    pose: PosePayload | None = None
    ultrasonic: list[float] | None = Field(default=None, max_length=64)
    diagnostics: dict[str, Any] | None = None
    battery_level: float | None = None

    @field_validator("ultrasonic")
    @classmethod
    def valid_ranges(cls, values: list[float] | None) -> list[float] | None:
        if values is not None and any(not math.isfinite(v) or v < 0 for v in values):
            raise ValueError("ultrasonic readings must be finite and non-negative")
        return values

    @field_validator("battery_level")
    @classmethod
    def valid_battery(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or not 0 <= value <= 100):
            raise ValueError("battery_level must be between 0 and 100")
        return value


class GatewayEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int
    type: Literal["hello", "heartbeat", "telemetry", "command_ack", "command_result", "error"]
    message_id: str = Field(min_length=1, max_length=128)
    robot_id: str = Field(min_length=1, max_length=64)
    timestamp: datetime
    payload: dict[str, Any]

    @field_validator("timestamp")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(timezone.utc)


def parse_envelope(raw: str, *, max_bytes: int) -> GatewayEnvelope:
    if len(raw.encode("utf-8")) > max_bytes:
        raise ValueError("message exceeds configured size limit")
    try:
        data = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if not isinstance(data, dict):
            raise TypeError("envelope must be an object")
        timestamp = data.get("timestamp")
        if isinstance(timestamp, str):
            data["timestamp"] = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        envelope = GatewayEnvelope.model_validate(data)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError, RecursionError) as exc:
        raise ValueError("malformed message envelope") from exc
    if envelope.version != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    if envelope.type == "telemetry":
        try:
            TelemetryPayload.model_validate(envelope.payload)
        except ValidationError as exc:
            raise ValueError("invalid telemetry payload") from exc
    elif envelope.type == "hello" and envelope.payload:
        raise ValueError("hello payload must be empty")
    elif envelope.type == "heartbeat" and envelope.payload:
        raise ValueError("heartbeat payload must be empty")
    elif envelope.type == "error":
        if (set(envelope.payload) - {"code", "message"}
                or not isinstance(envelope.payload.get("code"), str)
                or len(envelope.payload["code"]) > 64
                or ("message" in envelope.payload and
                    (not isinstance(envelope.payload["message"], str)
                     or len(envelope.payload["message"]) > 256))):
            raise ValueError("invalid error payload")
    elif envelope.type in {"command_ack", "command_result"}:
        # These are telemetry/protocol reports only; no command is executed here.
        if (set(envelope.payload) - {"command_id", "status", "detail"}
                or not isinstance(envelope.payload.get("command_id"), str)
                or not envelope.payload["command_id"]
                or len(envelope.payload["command_id"]) > 128
                or ("status" in envelope.payload and
                    (not isinstance(envelope.payload["status"], str)
                     or len(envelope.payload["status"]) > 32))
                or ("detail" in envelope.payload and
                    (not isinstance(envelope.payload["detail"], str)
                     or len(envelope.payload["detail"]) > 256))):
            raise ValueError("command protocol message requires command_id")
    return envelope


class MessageOrderGuard:
    """TTL-bounded deduplication and timestamp ordering per robot, per process."""

    def __init__(self, max_ids_per_robot: int = 2048, message_id_ttl_s: float = 900,
                 max_clock_skew_s: float = 30) -> None:
        self._max_ids = max_ids_per_robot
        self._message_id_ttl_s = message_id_ttl_s
        self._max_clock_skew_s = max_clock_skew_s
        self._message_ids: dict[str, OrderedDict[str, datetime]] = {}
        self._last_timestamp: dict[str, datetime] = {}

    def accept(self, envelope: GatewayEnvelope, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must include a timezone")
        now = now.astimezone(timezone.utc)
        timestamp = envelope.timestamp.astimezone(timezone.utc)
        # Device time is only useful for ordering. Bound both sides so a future
        # clock jump cannot pin the high-water mark and old packets cannot refresh status.
        if abs((timestamp - now).total_seconds()) > self._max_clock_skew_s:
            return False
        ids = self._message_ids.setdefault(envelope.robot_id, OrderedDict())
        expiry_before = now.timestamp() - self._message_id_ttl_s
        while ids and next(iter(ids.values())).timestamp() < expiry_before:
            ids.popitem(last=False)
        if envelope.message_id in ids:
            return False
        if envelope.type in {"heartbeat", "telemetry"}:
            previous = self._last_timestamp.get(envelope.robot_id)
            if previous is not None and timestamp < previous:
                return False
            self._last_timestamp[envelope.robot_id] = timestamp
        ids[envelope.message_id] = now
        while len(ids) > self._max_ids:
            ids.popitem(last=False)
        return True

    def clear(self, robot_id: str) -> None:
        self._message_ids.pop(robot_id, None)
        self._last_timestamp.pop(robot_id, None)
