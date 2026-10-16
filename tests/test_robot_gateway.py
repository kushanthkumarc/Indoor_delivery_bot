"""Isolated tests for the versioned robot gateway (no DB/Redis/server/ROS)."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bcrypt
import pytest
from fastapi import WebSocketDisconnect

from app.auth.robot_auth import authenticate_robot_credentials
from app.dashboard.service import RobotStatusService
from app.db.models import RobotStatus
import app.dashboard.service as dashboard_service
from app.dashboard import router as dashboard_router
from app.gateway.protocol import MessageOrderGuard, TelemetryPayload, parse_envelope
from app.main import health, readiness
from fastapi import HTTPException


def envelope(kind="heartbeat", *, msg_id="m1", ts=None, payload=None, robot_id="robot-1", version=1):
    return {
        "version": version,
        "type": kind,
        "message_id": msg_id,
        "robot_id": robot_id,
        "timestamp": ts or datetime.now(timezone.utc).isoformat(),
        "payload": payload or {},
    }


class FakeDBResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeDB:
    def __init__(self, robot):
        self.robot = robot

    async def execute(self, _statement):
        return FakeDBResult(self.robot)


@pytest.mark.asyncio
async def test_gateway_auth_uses_explicit_robot_and_hash():
    key = "isolated-test-key"
    robot = SimpleNamespace(id="robot-1", api_key_hash=bcrypt.hashpw(key.encode(), bcrypt.gensalt()).decode())
    assert await authenticate_robot_credentials("robot-1", key, FakeDB(robot)) is robot
    assert await authenticate_robot_credentials("robot-2", key, FakeDB(None)) is None
    assert await authenticate_robot_credentials("robot-1", "wrong", FakeDB(robot)) is None
    assert await authenticate_robot_credentials("robot-1", None, FakeDB(robot)) is None

    # Replacing the persisted hash revokes the old key for new connections.
    robot.api_key_hash = bcrypt.hashpw(b"rotated-key", bcrypt.gensalt()).decode()
    assert await authenticate_robot_credentials("robot-1", key, FakeDB(robot)) is None
    assert await authenticate_robot_credentials("robot-1", "rotated-key", FakeDB(robot)) is robot


def test_valid_envelope_and_optional_unknown_telemetry_fields():
    msg = parse_envelope(json.dumps(envelope("telemetry", payload={"pose": {"x": 1.0, "y": 2.0, "theta": 0.5}})), max_bytes=4096)
    payload = TelemetryPayload.model_validate(msg.payload)
    assert payload.pose.x == 1.0
    assert payload.mode is None
    assert payload.safety_state is None


@pytest.mark.parametrize("mutate", [
    lambda x: x.pop("robot_id"),
    lambda x: x.update(version=99),
    lambda x: x.update(timestamp="not-a-timestamp"),
    lambda x: x.update(type="unknown"),
    lambda x: x.update(extra="not-allowed"),
])
def test_invalid_envelopes_are_rejected(mutate):
    data = envelope()
    mutate(data)
    with pytest.raises(ValueError):
        parse_envelope(json.dumps(data), max_bytes=4096)


def test_oversize_and_invalid_telemetry_are_rejected():
    with pytest.raises(ValueError, match="size"):
        parse_envelope(json.dumps(envelope()), max_bytes=8)
    for payload in (
        {"pose": {"x": float("nan"), "y": 0.0, "theta": 0.0}},
        {"ultrasonic": [-0.1]},
        {"battery_level": 101.0},
        {"safety_state": "MAYBE"},
        {"unexpected": True},
    ):
        with pytest.raises(ValueError):
            parse_envelope(json.dumps(envelope("telemetry", payload=payload)), max_bytes=4096)


def test_duplicate_and_out_of_order_messages_are_rejected():
    guard = MessageOrderGuard()
    first_ts = datetime.now(timezone.utc)
    first = parse_envelope(json.dumps(envelope(ts=first_ts.isoformat())), max_bytes=4096)
    older = parse_envelope(json.dumps(envelope(msg_id="m2", ts=(first_ts - timedelta(seconds=1)).isoformat())), max_bytes=4096)
    later = parse_envelope(json.dumps(envelope(msg_id="m3", ts=(first_ts + timedelta(seconds=1)).isoformat())), max_bytes=4096)
    assert guard.accept(first)
    assert not guard.accept(first)
    assert not guard.accept(older)
    assert guard.accept(later)


def test_replay_guard_rejects_clock_manipulation_and_expires_ids():
    now = datetime.now(timezone.utc)
    guard = MessageOrderGuard(message_id_ttl_s=10, max_clock_skew_s=5)
    future = parse_envelope(json.dumps(envelope(ts=(now + timedelta(seconds=30)).isoformat())), max_bytes=4096)
    current = parse_envelope(json.dumps(envelope(ts=now.isoformat())), max_bytes=4096)
    assert not guard.accept(future, now=now)
    assert guard.accept(current, now=now)
    assert not guard.accept(current, now=now + timedelta(seconds=1))
    later_now = now + timedelta(seconds=11)
    later = parse_envelope(json.dumps(envelope(ts=later_now.isoformat())), max_bytes=4096)
    assert guard.accept(later, now=later_now)


def test_gateway_freshness_transitions():
    service = RobotStatusService(hub=AsyncMock())
    now = datetime.now(timezone.utc)
    assert service.connection_freshness("r", now) == "OFFLINE"
    service._gateway_connected["r"] = "session-1"
    assert service.connection_freshness("r", None, now) == "STALE"
    assert service.connection_freshness("r", now - timedelta(seconds=1), now) == "ONLINE"
    assert service.connection_freshness("r", now - timedelta(seconds=4), now) == "STALE"
    assert service.connection_freshness("r", now - timedelta(seconds=6), now) == "OFFLINE"


@pytest.mark.asyncio
async def test_gateway_disconnect_and_reconnect(monkeypatch):
    robot = SimpleNamespace(status=RobotStatus.ONLINE)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _query):
            return FakeDBResult(robot)

        async def commit(self):
            return None

    monkeypatch.setattr(dashboard_service, "AsyncSessionLocal", Session)
    hub_mock = AsyncMock()
    service = RobotStatusService(hub=hub_mock)
    assert await service.gateway_connected("robot-1", "session-1")
    assert not await service.gateway_connected("robot-1", "session-2")
    assert service.connection_freshness("robot-1", None) == "STALE"
    await service.gateway_disconnected("robot-1", "session-1")
    assert robot.status == RobotStatus.OFFLINE
    assert service.connection_freshness("robot-1", datetime.now(timezone.utc)) == "OFFLINE"
    assert await service.gateway_connected("robot-1", "session-2")
    # A late cleanup from the previous socket must not evict the reconnect.
    await service.gateway_disconnected("robot-1", "session-1")
    assert service.connection_freshness("robot-1", datetime.now(timezone.utc)) == "ONLINE"


@pytest.mark.asyncio
async def test_missing_redis_heartbeat_uses_db_timeout_boundary(monkeypatch):
    now = datetime.now(timezone.utc)
    robot = SimpleNamespace(
        id="robot-1", status=RobotStatus.ONLINE,
        last_seen=now - timedelta(seconds=4),
    )

    class Result:
        def scalars(self): return self
        def all(self): return [robot]

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def execute(self, _query): return Result()
        async def commit(self): return None

    redis = SimpleNamespace(get=AsyncMock(return_value=None))
    monkeypatch.setattr(dashboard_service, "get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(dashboard_service, "AsyncSessionLocal", Session)
    monkeypatch.setattr(dashboard_service, "_utcnow", lambda: now)
    monkeypatch.setattr(dashboard_service, "log_event", AsyncMock())
    service = RobotStatusService(hub=AsyncMock())
    service._gateway_connected["robot-1"] = "session-1"

    await service._check_all_robots()
    assert robot.status == RobotStatus.ONLINE
    service._hub.broadcast.assert_awaited_with("robot.freshness", {
        "robot_id": "robot-1", "connection_state": "STALE",
        "last_seen": robot.last_seen.isoformat(),
    })


@pytest.mark.asyncio
async def test_telemetry_broadcasts_without_command_execution():
    service = RobotStatusService(hub=AsyncMock())
    service.update_heartbeat = AsyncMock()
    service.update_control_mode = AsyncMock()
    service.update_safety_state = AsyncMock()
    service.update_pose = AsyncMock()
    await service.update_gateway_telemetry("robot-1", {
        "mode": "AUTONOMOUS", "pose": {"x": 1.0, "y": 2.0, "theta": 0.0},
        "ultrasonic": [0.5],
    })
    service.update_heartbeat.assert_awaited_once_with("robot-1")
    service.update_pose.assert_awaited_once_with("robot-1", 1.0, 2.0, 0.0)
    service._hub.broadcast.assert_awaited_with("robot.telemetry", {
        "robot_id": "robot-1", "mode": "AUTONOMOUS",
        "pose": {"x": 1.0, "y": 2.0, "theta": 0.0}, "ultrasonic": [0.5],
    })


@pytest.mark.asyncio
async def test_readiness_checks_dependencies_and_fails_closed(monkeypatch):
    import app.main as main_module

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _query):
            return 1

    monkeypatch.setattr(main_module, "AsyncSessionLocal", Session)
    monkeypatch.setattr(main_module, "get_redis", AsyncMock(return_value=SimpleNamespace(ping=AsyncMock(return_value=True))))
    assert await readiness() == {"status": "ready", "checks": {"database": True, "redis": True}}
    assert await health() == {"status": "ok"}

    class BrokenSession(Session):
        async def __aenter__(self):
            raise ConnectionError("isolated test failure")

    monkeypatch.setattr(main_module, "AsyncSessionLocal", BrokenSession)
    monkeypatch.setattr(main_module, "get_redis", AsyncMock(side_effect=ConnectionError("isolated test failure")))
    with pytest.raises(HTTPException) as error:
        await readiness()
    assert error.value.status_code == 503
    assert error.value.detail["checks"] == {"database": False, "redis": False}


@pytest.mark.asyncio
async def test_readiness_fails_if_either_dependency_is_down(monkeypatch):
    import app.main as main_module

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def execute(self, _query): return 1

    monkeypatch.setattr(main_module, "AsyncSessionLocal", Session)
    monkeypatch.setattr(main_module, "get_redis", AsyncMock(return_value=SimpleNamespace(ping=AsyncMock(return_value=False))))
    with pytest.raises(HTTPException) as error:
        await readiness()
    assert error.value.status_code == 503
    assert error.value.detail["checks"] == {"database": True, "redis": False}

    class BrokenSession(Session):
        async def __aenter__(self): raise ConnectionError("isolated test failure")
    monkeypatch.setattr(main_module, "AsyncSessionLocal", BrokenSession)
    monkeypatch.setattr(main_module, "get_redis", AsyncMock(return_value=SimpleNamespace(ping=AsyncMock(return_value=True))))
    with pytest.raises(HTTPException) as error:
        await readiness()
    assert error.value.status_code == 503
    assert error.value.detail["checks"] == {"database": False, "redis": True}


class FakeWebSocket:
    def __init__(self, messages, *, headers=None, query=None):
        self.headers = headers or {"x-robot-id": "robot-1", "x-robot-api-key": "never-log-this"}
        self.query_params = query or {}
        self.messages = list(messages)
        self.sent = []
        self.accepted = False
        self.close_code = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=None):
        self.close_code = code

    async def receive_text(self):
        if self.messages:
            return self.messages.pop(0)
        raise WebSocketDisconnect()

    async def send_json(self, data):
        self.sent.append(data)


@pytest.mark.asyncio
async def test_ws_auth_hello_telemetry_and_protocol_only_command(monkeypatch, caplog):
    monkeypatch.setattr(dashboard_router, "authenticate_robot_credentials", AsyncMock(return_value=SimpleNamespace(id="robot-1")))
    monkeypatch.setattr(dashboard_router.status_service, "gateway_connected", AsyncMock(return_value=True))
    monkeypatch.setattr(dashboard_router.status_service, "gateway_disconnected", AsyncMock())
    monkeypatch.setattr(dashboard_router.status_service, "update_gateway_telemetry", AsyncMock())
    monkeypatch.setattr(dashboard_router.status_service, "update_heartbeat", AsyncMock())
    monkeypatch.setattr(dashboard_router.hub, "broadcast", AsyncMock())
    hello = envelope("hello", msg_id="hello", payload={})
    telemetry = envelope("telemetry", msg_id="telemetry", payload={"ultrasonic": [0.4]})
    result = envelope("command_result", msg_id="result", payload={"command_id": "c1", "status": "complete"})
    ws = FakeWebSocket([json.dumps(hello), json.dumps(telemetry), json.dumps(result)])
    await dashboard_router.robot_ws(ws)
    assert ws.accepted
    assert ws.sent[0]["type"] == "hello_ack"
    dashboard_router.status_service.update_gateway_telemetry.assert_awaited_once_with("robot-1", {"ultrasonic": [0.4]})
    event = dashboard_router.hub.broadcast.await_args.args
    assert event[0] == "robot.command_protocol_message"
    assert event[1]["physical_execution_confirmed"] is False
    assert "never-log-this" not in caplog.text
    args = dashboard_router.status_service.gateway_disconnected.await_args.args
    assert args[0] == "robot-1"
    assert isinstance(args[1], str)


@pytest.mark.asyncio
async def test_ws_rejects_query_credentials_and_invalid_auth_before_accept(monkeypatch):
    auth = AsyncMock(return_value=None)
    monkeypatch.setattr(dashboard_router, "authenticate_robot_credentials", auth)
    query_ws = FakeWebSocket([], query={"api_key": "secret"})
    await dashboard_router.robot_ws(query_ws)
    assert not query_ws.accepted
    auth.assert_not_awaited()
    bad_ws = FakeWebSocket([])
    await dashboard_router.robot_ws(bad_ws)
    assert not bad_ws.accepted
