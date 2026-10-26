"""Isolated tests for the robot command channel (no network, ROS, MySQL, Redis)."""
import asyncio
import math

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base, ControlMode, Delivery, DeliveryStatus, Destination, MappingSession,
    MappingSessionStatus, Robot, RobotStatus, SafetyState, User, UserRole,
)
import app.delivery.state_machine as state_module
from app.delivery.state_machine import DeliveryStateMachine
from app.gateway.commands import build_command
from app.gateway.navigation import GatewayNavigationAdapter, RobotNotConnectedError
from app.nav2.bridge import GoalStatus, Pose


def test_goto_command_shape_and_expiry():
    cmd = build_command("r1", "goto", delivery_id="d1", goal={"x": 1.5, "y": -2.0, "theta": 0.5}, ttl_s=10)
    p = cmd["payload"]
    assert cmd["type"] == "command" and cmd["version"] == 1 and cmd["robot_id"] == "r1"
    assert p["kind"] == "goto" and p["goal"] == {"frame": "map", "x": 1.5, "y": -2.0, "theta": 0.5}
    assert p["command_id"] and p["issued_at"] < p["expires_at"]
    assert set(p) == {"command_id", "kind", "issued_at", "expires_at", "delivery_id", "goal"}


@pytest.mark.parametrize("bad", [
    dict(kind="velocity"), dict(kind="goto"), dict(kind="stop", goal={"x": 0, "y": 0, "theta": 0}),
    dict(kind="goto", goal={"x": math.nan, "y": 0, "theta": 0}),
    dict(kind="goto", goal={"x": 1e9, "y": 0, "theta": 0}),
    dict(kind="goto", goal={"x": True, "y": 0, "theta": 0}),
    dict(kind="goto", goal={"x": 0, "y": 0, "theta": 0}, ttl_s=0),
    dict(kind="goto", goal={"x": 0, "y": 0, "theta": 0}, ttl_s=9999),
])
def test_invalid_commands_are_rejected(bad):
    with pytest.raises(ValueError):
        build_command("r1", **bad)


class FakeRobot:
    def __init__(self):
        self.sent = []

    async def send(self, envelope):
        self.sent.append(envelope)


async def resolver(_delivery_id):
    return "robot-1"


@pytest.mark.asyncio
async def test_start_requires_connected_robot():
    adapter = GatewayNavigationAdapter(resolver=resolver)
    with pytest.raises(RobotNotConnectedError):
        await adapter.start(Pose(1, 1, 0), "d1")


@pytest.mark.asyncio
async def test_goal_succeeds_only_on_robot_result_report():
    robot, adapter = FakeRobot(), GatewayNavigationAdapter(resolver=resolver, ttl_s=10, ack_timeout_s=5)
    adapter.register_connection("robot-1", "c1", robot.send)
    goal_id = await adapter.start(Pose(2.0, 3.0, 0.1), "d1")
    assert robot.sent[0]["payload"]["goal"]["x"] == 2.0
    waiter = asyncio.create_task(adapter.wait_result(goal_id))
    await asyncio.sleep(0)
    assert not waiter.done()                      # no assumption of success
    await adapter.handle_report("robot-1", "command_ack", {"command_id": goal_id})
    await adapter.handle_report("robot-1", "command_result", {"command_id": goal_id, "status": "ACTIVE"})
    await asyncio.sleep(0)
    assert not waiter.done()                      # progress is not terminal
    await adapter.handle_report("robot-1", "command_result", {"command_id": goal_id, "status": "SUCCEEDED"})
    assert (await waiter).status == GoalStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_other_robots_cannot_report_for_this_goal():
    robot, adapter = FakeRobot(), GatewayNavigationAdapter(resolver=resolver, ack_timeout_s=5)
    adapter.register_connection("robot-1", "c1", robot.send)
    goal_id = await adapter.start(Pose(1, 1, 0), "d1")
    await adapter.handle_report("robot-2", "command_result", {"command_id": goal_id, "status": "SUCCEEDED"})
    await adapter.handle_report("robot-1", "command_result", {"command_id": "unknown", "status": "SUCCEEDED"})
    assert not adapter._goals[goal_id].future.done()


@pytest.mark.asyncio
async def test_disconnect_fails_pending_goal():
    robot, adapter = FakeRobot(), GatewayNavigationAdapter(resolver=resolver, ack_timeout_s=5)
    adapter.register_connection("robot-1", "c1", robot.send)
    goal_id = await adapter.start(Pose(1, 1, 0), "d1")
    adapter.unregister_connection("robot-1", "stale-connection")      # stale id: ignored
    assert not adapter._goals[goal_id].future.done()
    adapter.unregister_connection("robot-1", "c1")
    result = await adapter.wait_result(goal_id)
    assert result.status == GoalStatus.FAILED and "disconnected" in result.failure_reason


@pytest.mark.asyncio
async def test_missing_ack_fails_goal_and_sends_stop():
    robot, adapter = FakeRobot(), GatewayNavigationAdapter(resolver=resolver, ack_timeout_s=0.05)
    adapter.register_connection("robot-1", "c1", robot.send)
    goal_id = await adapter.start(Pose(1, 1, 0), "d1")
    result = await asyncio.wait_for(adapter.wait_result(goal_id), 1)
    assert result.status == GoalStatus.FAILED
    assert [m["payload"]["kind"] for m in robot.sent] == ["goto", "stop"]


@pytest.mark.asyncio
async def test_cancel_sends_stop_and_resolves_cancelled():
    robot, adapter = FakeRobot(), GatewayNavigationAdapter(resolver=resolver, ack_timeout_s=5)
    adapter.register_connection("robot-1", "c1", robot.send)
    goal_id = await adapter.start(Pose(1, 1, 0), "d1")
    await adapter.cancel(goal_id)
    assert robot.sent[-1]["payload"]["kind"] == "stop"
    assert (await adapter.wait_result(goal_id)).status == GoalStatus.CANCELLED


@pytest_asyncio.fixture
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(state_module, "AsyncSessionLocal", factory)
    async with factory() as db:
        db.add_all([
            User(id="admin-1", email="a@test", password_hash="x", name="A", role=UserRole.ADMIN),
            User(id="user-1", email="u@test", password_hash="x", name="U", role=UserRole.USER),
            Robot(id="robot-1", name="R1", api_key_hash="x", status=RobotStatus.ONLINE,
                  control_mode=ControlMode.AUTONOMOUS, safety_state=SafetyState.SAFE),
            MappingSession(id="map-1", robot_id="robot-1", status=MappingSessionStatus.FINISHED),
            Destination(id="dest-1", name="Drop", map_id="map-1", x=2.5, y=3.5, theta=0.75, is_home=False),
            Delivery(id="delivery-1", robot_id="robot-1", destination_id="dest-1",
                     requester_id="user-1", status=DeliveryStatus.IDLE, state_history_json=[]),
        ])
        await db.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_state_machine_dispatch_end_to_end_through_adapter(db_factory):
    robot, adapter = FakeRobot(), GatewayNavigationAdapter(resolver=resolver, ack_timeout_s=5)
    adapter.register_connection("robot-1", "c1", robot.send)
    machine = DeliveryStateMachine(adapter)
    task = asyncio.create_task(machine.dispatch("delivery-1", "admin-1"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if robot.sent:
            break
    cmd = robot.sent[0]["payload"]
    assert cmd["goal"]["x"] == 2.5 and cmd["goal"]["y"] == 3.5
    await adapter.handle_report("robot-1", "command_ack", {"command_id": cmd["command_id"]})
    await adapter.handle_report("robot-1", "command_result",
                                {"command_id": cmd["command_id"], "status": "SUCCEEDED"})
    delivery = await asyncio.wait_for(task, 2)
    assert delivery.status == DeliveryStatus.ARRIVED


@pytest.mark.asyncio
async def test_state_machine_failure_when_robot_reports_failed(db_factory):
    robot, adapter = FakeRobot(), GatewayNavigationAdapter(resolver=resolver, ack_timeout_s=5)
    adapter.register_connection("robot-1", "c1", robot.send)
    machine = DeliveryStateMachine(adapter)
    task = asyncio.create_task(machine.dispatch("delivery-1", "admin-1"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if robot.sent:
            break
    cid = robot.sent[0]["payload"]["command_id"]
    await adapter.handle_report("robot-1", "command_result",
                                {"command_id": cid, "status": "FAILED", "detail": "obstacle"})
    delivery = await asyncio.wait_for(task, 2)
    assert delivery.status != DeliveryStatus.ARRIVED
