"""Isolated SQLite lifecycle tests using a test-local scripted adapter."""
import asyncio
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base, ControlMode, Delivery, DeliveryStatus, Destination, Event, MappingSession,
    MappingSessionStatus, Robot, RobotStatus, SafetyState, User, UserRole,
)
import app.delivery.state_machine as state_module
from app.delivery.state_machine import DeliveryStateMachine, InvalidTransitionError
from app.nav2.bridge import GoalResult, GoalStatus


class ScriptedTestNavigation:
    """Test-only controlled transport; no implementation is shipped by the app."""

    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.goals = {}
        self.start_count = 0

    async def start(self, pose, delivery_id):
        loop = asyncio.get_running_loop()
        goal_id = f"test-goal-{self.start_count + 1}"
        future = loop.create_future()
        outcome = self.outcomes.pop(0) if self.outcomes else GoalStatus.SUCCEEDED
        self.goals[goal_id] = (pose, outcome, future)
        self.start_count += 1
        if outcome is not None:
            loop.call_soon(future.set_result, GoalResult(goal_id, outcome))
        return goal_id

    async def wait_result(self, goal_id):
        return await self.goals[goal_id][2]

    async def cancel(self, goal_id):
        future = self.goals[goal_id][2]
        if not future.done():
            future.set_result(GoalResult(goal_id, GoalStatus.CANCELLED))


@pytest_asyncio.fixture
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(state_module, "AsyncSessionLocal", factory)
    async with factory() as db:
        db.add_all([
            User(id="admin-1", email="admin@test", password_hash="x", name="Admin", role=UserRole.ADMIN),
            User(id="user-1", email="user@test", password_hash="x", name="User", role=UserRole.USER),
            User(id="user-2", email="other@test", password_hash="x", name="Other", role=UserRole.USER),
            Robot(id="robot-1", name="R1", api_key_hash="x", status=RobotStatus.ONLINE,
                  control_mode=ControlMode.AUTONOMOUS, safety_state=SafetyState.SAFE),
            MappingSession(id="map-1", robot_id="robot-1", status=MappingSessionStatus.FINISHED),
            Destination(id="dest-1", name="Drop", map_id="map-1", x=2.5, y=3.5, theta=0.75, is_home=False),
            Destination(id="home-1", name="Home", map_id="map-1", x=-4.0, y=6.0, theta=1.2, is_home=True),
            Delivery(id="delivery-1", robot_id="robot-1", destination_id="dest-1",
                     requester_id="user-1", status=DeliveryStatus.IDLE, state_history_json=[]),
        ])
        await db.commit()
    yield factory
    await engine.dispose()


async def get_delivery(factory, delivery_id="delivery-1"):
    async with factory() as db:
        return await db.get(Delivery, delivery_id)


@pytest.mark.asyncio
async def test_full_lifecycle_records_audit_and_uses_configured_home(db_factory):
    adapter = ScriptedTestNavigation([GoalStatus.SUCCEEDED, GoalStatus.SUCCEEDED])
    machine = DeliveryStateMachine(adapter)
    arrived = await machine.dispatch("delivery-1", "admin-1")
    assert arrived.status == DeliveryStatus.ARRIVED
    returned = await machine.on_pickup_confirmed("delivery-1", "user-1")
    assert returned.status == DeliveryStatus.IDLE
    assert adapter.start_count == 2
    assert adapter.goals[returned.nav_goal_id][0].x == -4.0
    assert adapter.goals[returned.nav_goal_id][0].y == 6.0
    async with db_factory() as db:
        saved = await db.get(Delivery, "delivery-1")
        assert [entry["to"] for entry in saved.state_history_json] == [
            "GO_TO_DESTINATION", "ARRIVED", "DELIVERED", "RETURN_HOME", "HOME", "IDLE"
        ]
        assert all(entry["timestamp"] and entry["actor_id"] and entry["reason"]
                   for entry in saved.state_history_json)
        events = (await db.execute(select(Event))).scalars().all()
        assert len(events) == 7
        dispatch_event = next(e for e in events if e.payload_json.get("to") == "GO_TO_DESTINATION")
        assert dispatch_event.user_id == "admin-1"
        assert dispatch_event.payload_json["from"] == "IDLE"


@pytest.mark.asyncio
async def test_navigation_failure_and_timeout_fail_closed(db_factory, monkeypatch):
    failed = DeliveryStateMachine(ScriptedTestNavigation([GoalStatus.FAILED]))
    result = await failed.dispatch("delivery-1", "admin-1")
    assert result.status == DeliveryStatus.EMERGENCY_STOP
    assert result.completed_at is None
    assert result.state_history_json[-1]["to"] == "EMERGENCY_STOP"
    assert "physical execution unconfirmed" in result.state_history_json[-1]["reason"]

    async with db_factory() as db:
        delivery = await db.get(Delivery, "delivery-1")
        delivery.status = DeliveryStatus.IDLE
        delivery.state_history_json = []
        await db.commit()
    monkeypatch.setattr(state_module.settings, "DELIVERY_STATE_TIMEOUT_S", 0.01)
    timed = DeliveryStateMachine(ScriptedTestNavigation([None]))
    result = await timed.dispatch("delivery-1", "admin-1")
    assert result.status == DeliveryStatus.EMERGENCY_STOP
    assert result.completed_at is None


@pytest.mark.asyncio
async def test_missing_home_blocks_pickup_return_goal(db_factory):
    async with db_factory() as db:
        delivery = await db.get(Delivery, "delivery-1")
        delivery.status = DeliveryStatus.ARRIVED
        home = await db.get(Destination, "home-1")
        await db.delete(home)
        await db.commit()
    adapter = ScriptedTestNavigation()
    with pytest.raises(ValueError, match="Home destination"):
        await DeliveryStateMachine(adapter).on_pickup_confirmed("delivery-1", "user-1")
    assert adapter.start_count == 0
    assert (await get_delivery(db_factory)).status == DeliveryStatus.ARRIVED


@pytest.mark.asyncio
async def test_invalid_transition_and_repeated_dispatch_do_not_start_duplicate_goal(db_factory):
    adapter = ScriptedTestNavigation([None])
    machine = DeliveryStateMachine(adapter)
    dispatch = asyncio.create_task(machine.dispatch("delivery-1", "admin-1"))
    while adapter.start_count == 0:
        await asyncio.sleep(0)
    duplicate = await machine.dispatch("delivery-1", "admin-1")
    assert duplicate.status == DeliveryStatus.GO_TO_DESTINATION
    assert adapter.start_count == 1
    with pytest.raises(InvalidTransitionError):
        await machine.on_pickup_confirmed("delivery-1", "user-1")
    await machine.cancel("delivery-1", "admin-1")
    assert (await dispatch).status == DeliveryStatus.EMERGENCY_STOP


@pytest.mark.asyncio
async def test_cancellation_race_does_not_overwrite_terminal_cancel_state(db_factory):
    adapter = ScriptedTestNavigation([None])
    machine = DeliveryStateMachine(adapter)
    dispatch = asyncio.create_task(machine.dispatch("delivery-1", "admin-1"))
    while adapter.start_count == 0:
        await asyncio.sleep(0)
    canceled = await machine.cancel("delivery-1", "admin-1")
    result = await dispatch
    assert canceled.status == DeliveryStatus.EMERGENCY_STOP
    assert result.status == DeliveryStatus.EMERGENCY_STOP
    assert result.state_history_json[-1]["reason"].endswith("physical stop unconfirmed")


@pytest.mark.asyncio
async def test_pickup_permission_and_route_robot_map_validation(db_factory, monkeypatch):
    import app.delivery.router as routes

    monkeypatch.setattr(routes, "AsyncSessionLocal", db_factory)
    async with db_factory() as db:
        owner = await db.get(User, "user-2")
    with pytest.raises(HTTPException) as denied:
        await routes.confirm_pickup("delivery-1", owner)
    assert denied.value.status_code == 403

    async with db_factory() as db:
        robot = await db.get(Robot, "robot-1")
        robot.status = RobotStatus.OFFLINE
        await db.commit()
    with pytest.raises(HTTPException) as unavailable:
        routes._validate_robot_for_dispatch(robot)
    assert unavailable.value.status_code == 409

    with pytest.raises(HTTPException) as incompatible:
        await routes._validate_destination_map("map-1", "another-robot")
    assert incompatible.value.status_code == 409


@pytest.mark.asyncio
async def test_dispatch_api_uses_only_explicitly_injected_test_transport(db_factory, monkeypatch):
    import app.delivery.router as routes
    monkeypatch.setattr(routes, "AsyncSessionLocal", db_factory)
    adapter = ScriptedTestNavigation([GoalStatus.SUCCEEDED])
    monkeypatch.setattr(routes, "delivery_state_machine", DeliveryStateMachine(
        adapter, freshness_check=lambda _robot_id, _last_seen: "ONLINE"
    ))
    async with db_factory() as db:
        admin = await db.get(User, "admin-1")
    response = await routes.dispatch_delivery("delivery-1", admin)
    assert response == {
        "id": "delivery-1", "status": "ARRIVED",
        "physical_execution_confirmed": False,
    }
    assert adapter.start_count == 1


@pytest.mark.asyncio
async def test_stale_gateway_cannot_dispatch(db_factory):
    adapter = ScriptedTestNavigation()
    machine = DeliveryStateMachine(adapter, freshness_check=lambda _robot_id, _last_seen: "STALE")
    with pytest.raises(InvalidTransitionError, match="stale or offline"):
        await machine.dispatch("delivery-1", "admin-1")
    assert adapter.start_count == 0


@pytest.mark.asyncio
async def test_transition_event_failure_rolls_back_and_cancels_started_goal(db_factory, monkeypatch):
    adapter = ScriptedTestNavigation([None])

    async def fail_event(*_args, **_kwargs):
        raise RuntimeError("isolated event persistence failure")

    monkeypatch.setattr(state_module, "log_event", fail_event)
    with pytest.raises(RuntimeError, match="event persistence"):
        await DeliveryStateMachine(adapter).dispatch("delivery-1", "admin-1")
    assert (await get_delivery(db_factory)).status == DeliveryStatus.IDLE
    assert adapter.start_count == 1
    goal_id = next(iter(adapter.goals))
    assert adapter.goals[goal_id][2].result().status == GoalStatus.CANCELLED


@pytest.mark.asyncio
async def test_mapped_source_requires_loading_before_destination(db_factory):
    async with db_factory() as db:
        delivery = await db.get(Delivery, "delivery-1")
        delivery.source_id = "home-1"
        await db.commit()
    adapter = ScriptedTestNavigation()
    machine = DeliveryStateMachine(adapter)
    source = await machine.dispatch("delivery-1", "admin-1")
    assert source.status == DeliveryStatus.AT_SOURCE
    assert adapter.start_count == 1
    assert adapter.goals[source.nav_goal_id][0].x == -4.0
    with pytest.raises(InvalidTransitionError):
        await machine.dispatch("delivery-1", "admin-1")
    arrived = await machine.dispatch("delivery-1", "user-1", loading_confirmed=True)
    assert arrived.status == DeliveryStatus.ARRIVED
    assert adapter.start_count == 2
    assert adapter.goals[arrived.nav_goal_id][0].x == 2.5
    assert [h["to"] for h in arrived.state_history_json] == [
        "GO_TO_SOURCE", "AT_SOURCE", "GO_TO_DESTINATION", "ARRIVED"]
    completed = await machine.on_pickup_confirmed("delivery-1", "user-1")
    assert completed.status == DeliveryStatus.DELIVERED
    assert completed.completed_at is not None
    assert adapter.start_count == 2  # Receipt never starts an unrequested return trip.


@pytest.mark.asyncio
async def test_loading_is_rejected_before_source_arrival(db_factory):
    adapter = ScriptedTestNavigation()
    with pytest.raises(InvalidTransitionError):
        await DeliveryStateMachine(adapter).dispatch("delivery-1", "user-1", loading_confirmed=True)
    assert adapter.start_count == 0


@pytest.mark.asyncio
async def test_source_loading_permission(db_factory, monkeypatch):
    import app.delivery.router as routes
    monkeypatch.setattr(routes, "AsyncSessionLocal", db_factory)
    async with db_factory() as db:
        other = await db.get(User, "user-2")
    with pytest.raises(HTTPException) as denied:
        await routes.confirm_loading("delivery-1", other)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_source_receipt_does_not_require_navigation_transport(db_factory, monkeypatch):
    import app.delivery.router as routes
    monkeypatch.setattr(routes, "AsyncSessionLocal", db_factory)
    monkeypatch.setattr(routes, "delivery_state_machine", None)
    async with db_factory() as db:
        delivery = await db.get(Delivery, "delivery-1")
        delivery.source_id = "home-1"
        delivery.status = DeliveryStatus.ARRIVED
        owner = await db.get(User, "user-1")
        other = await db.get(User, "user-2")
        await db.commit()
    with pytest.raises(HTTPException) as denied:
        await routes.confirm_pickup("delivery-1", other)
    assert denied.value.status_code == 403
    result = await routes.confirm_pickup("delivery-1", owner)
    assert result["status"] == "DELIVERED"
    assert result["physical_execution_confirmed"] is False
    assert (await get_delivery(db_factory)).completed_at is not None
    with pytest.raises(HTTPException) as repeated:
        await routes.confirm_pickup("delivery-1", owner)
    assert repeated.value.status_code == 409


@pytest.mark.asyncio
async def test_route_creation_persists_locations_while_robot_offline(db_factory, monkeypatch):
    import app.delivery.router as routes
    from app.db.models import UserRobotAccess
    monkeypatch.setattr(routes, "AsyncSessionLocal", db_factory)
    async with db_factory() as db:
        user = await db.get(User, "user-1")
        robot = await db.get(Robot, "robot-1")
        robot.status = RobotStatus.OFFLINE
        robot.control_mode = ControlMode.STOPPED
        db.add(UserRobotAccess(user_id=user.id, robot_id=robot.id, granted_by="admin-1"))
        await db.commit()
    created = await routes.create_delivery(routes.CreateDeliveryRequest(
        source_id="home-1", destination_id="dest-1", robot_id="robot-1"), user)
    assert created["source_id"] == "home-1"
    assert created["source_name"] == "Home"
    assert created["destination_name"] == "Drop"
    assert created["status"] == "IDLE"
    assert created["physical_execution_confirmed"] is False
    listed = await routes.get_my_deliveries(user)
    assert any(d["id"] == created["id"] and d["source_name"] == "Home" for d in listed)


@pytest.mark.asyncio
@pytest.mark.parametrize("source,destination,code", [("home-1", "home-1", 422), ("missing", "dest-1", 404)])
async def test_invalid_route_rejected(db_factory, monkeypatch, source, destination, code):
    import app.delivery.router as routes
    monkeypatch.setattr(routes, "AsyncSessionLocal", db_factory)
    async with db_factory() as db:
        admin = await db.get(User, "admin-1")
    with pytest.raises(HTTPException) as error:
        await routes.create_delivery(routes.CreateDeliveryRequest(
            source_id=source, destination_id=destination, robot_id="robot-1"), admin)
    assert error.value.status_code == code


@pytest.mark.asyncio
async def test_cross_map_route_and_ungranted_user_rejected(db_factory, monkeypatch):
    import app.delivery.router as routes
    monkeypatch.setattr(routes, "AsyncSessionLocal", db_factory)
    async with db_factory() as db:
        admin = await db.get(User, "admin-1")
        user = await db.get(User, "user-2")
        db.add(MappingSession(id="map-2", robot_id="robot-1", status=MappingSessionStatus.FINISHED))
        db.add(Destination(id="cross-map", name="Other floor", map_id="map-2", x=1, y=2, theta=0))
        await db.commit()
    for actor, source, code in [(admin, "cross-map", 409), (user, "home-1", 403)]:
        with pytest.raises(HTTPException) as error:
            await routes.create_delivery(routes.CreateDeliveryRequest(
                source_id=source, destination_id="dest-1", robot_id="robot-1"), actor)
        assert error.value.status_code == code
