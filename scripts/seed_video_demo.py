"""Add repeatable, labelled fixtures to development or explicitly opted-in staging.

Run inside the app container. Existing accounts and hardware records are preserved.
Staging requires --allow-staging and a unique DEMO_USER_PASSWORD; production is forbidden.
This creates historical samples, not a navigation transport or physical telemetry.
"""
import asyncio
import argparse
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.auth.service import hash_password
from app.core.config import settings, validate_production_settings
from app.db.models import (
    Delivery, DeliveryStatus, Destination, Event, EventSeverity, MappingSession,
    MappingSessionStatus, Robot, RobotStatus, ControlMode, User, UserRobotAccess,
    UserRole,
)
from app.db.session import AsyncSessionLocal, engine


def key(label):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "robot-operator-video-demo/" + label))


def seed_password(allow_staging=False):
    environment = settings.APP_ENV.strip().lower()
    if environment == "development":
        if os.getenv("RENDER", "").lower() == "true":
            raise RuntimeError("Render demo seeding requires APP_ENV=staging and --allow-staging")
        return settings.DEMO_USER_PASSWORD or "VideoDemo-2026!"
    if environment != "staging" or not allow_staging:
        raise RuntimeError("Demo fixtures require development or staging with --allow-staging; production is forbidden")
    # Render still enforces verified TLS and strong application credentials even
    # when APP_ENV=staging. Never turn those checks off for a demonstration.
    validate_production_settings()
    password = settings.DEMO_USER_PASSWORD
    if len(password) < 14 or password in {"VideoDemo-2026!", "change-me-admin-password"}:
        raise RuntimeError("Set a unique DEMO_USER_PASSWORD of at least 14 characters before staging seeding")
    return password


async def main(allow_staging=False):
    password = seed_password(allow_staging)
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        admin = (await db.execute(select(User).where(User.role == UserRole.ADMIN, User.is_active.is_(True)))).scalars().first()
        if admin is None:
            raise RuntimeError("Create an admin before seeding")
        demo = (await db.execute(select(User).where(User.email == "video.demo@example.com"))).scalar_one_or_none()
        if demo is None:
            demo = User(email="video.demo@example.com", name="Video Demo Operator", role=UserRole.USER,
                        is_active=True, password_hash=hash_password(password))
            db.add(demo)
            await db.flush()
        elif demo.role != UserRole.USER or not demo.is_active:
            raise RuntimeError("The demo email already belongs to an inactive or non-USER account; no changes applied")
        robot = await db.get(Robot, key("robot"))
        if robot is None:
            robot = Robot(id=key("robot"), name="DEMO - Campus Courier 01",
                          api_key_hash=hash_password(secrets.token_urlsafe(32)),
                          status=RobotStatus.OFFLINE, control_mode=ControlMode.STOPPED,
                          firmware_version="demo-fixtures-no-hardware")
            db.add(robot)
            await db.flush()
        mapping = await db.get(MappingSession, key("map"))
        if mapping is None:
            mapping = MappingSession(id=key("map"), robot_id=robot.id,
                status=MappingSessionStatus.FINISHED, elapsed_ms=486000, pose_update_count=245,
                last_pose_json={"x": 0.0, "y": 0.0, "theta": 0.0},
                map_data_ref="demo://campus-ground-floor/synthetic-not-hardware-validated",
                started_at=now-timedelta(hours=2), finished_at=now-timedelta(hours=1))
            db.add(mapping)
            await db.flush()
        places = [("Reception", 0.0, 0.0, 0.0), ("Robotics Lab", 12.4, 3.8, 1.57),
                  ("Library", 7.2, 15.6, 3.14), ("Faculty Office", 18.6, 9.3, 0.0)]
        for name, x, y, theta in places:
            if await db.get(Destination, key(name)) is None:
                db.add(Destination(id=key(name), name="DEMO - " + name, map_id=mapping.id,
                                   x=x, y=y, theta=theta, is_home=name == "Reception"))
        await db.flush()
        user_query = select(User).where(User.role == UserRole.USER, User.is_active.is_(True))
        if settings.APP_ENV.strip().lower() != "development":
            user_query = user_query.where(User.id == demo.id)
        users = (await db.execute(user_query)).scalars().all()
        for user in users:
            grant = (await db.execute(select(UserRobotAccess).where(
                UserRobotAccess.user_id == user.id, UserRobotAccess.robot_id == robot.id))).scalar_one_or_none()
            if grant is None:
                db.add(UserRobotAccess(user_id=user.id, robot_id=robot.id, granted_by=admin.id))
            # Every account gets its own samples so /deliveries/mine remains isolated.
            for index, status in enumerate((DeliveryStatus.IDLE, DeliveryStatus.ARRIVED, DeliveryStatus.DELIVERED)):
                ident = key(str(user.id) + "/" + status.value)
                existing = await db.get(Delivery, ident)
                if existing is not None:
                    if existing.source_id is None:
                        existing.source_id = key("Reception")
                    continue
                created = now - timedelta(minutes=90-index*20)
                states = ["IDLE"] if status == DeliveryStatus.IDLE else ["IDLE", "GO_TO_DESTINATION", "ARRIVED"]
                if status == DeliveryStatus.DELIVERED:
                    states.append("DELIVERED")
                history = [{"from": states[i-1] if i else None, "to": state,
                    "timestamp": (created+timedelta(minutes=i*2)).isoformat(),
                    "actor_id": str(user.id), "reason": "DEMO fixture - simulated history; no physical execution"}
                    for i, state in enumerate(states)]
                db.add(Delivery(id=ident, robot_id=robot.id, requester_id=user.id, source_id=key("Reception"),
                    destination_id=key(places[index+1][0]), status=status,
                    state_history_json=history, created_at=created,
                    delivered_at=created+timedelta(minutes=4) if index else None,
                    completed_at=created+timedelta(minutes=6) if status == DeliveryStatus.DELIVERED else None))
        if await db.get(Event, key("event")) is None:
            db.add(Event(id=key("event"), robot_id=robot.id, user_id=admin.id,
                type="demo.fixtures_created", severity=EventSeverity.INFO,
                payload_json={"demo": True, "physical_execution_confirmed": False,
                              "message": "Synthetic campus map and delivery history for software video demonstration"}))
        await db.commit()
        print(json.dumps({"robot_id": str(robot.id), "map_id": str(mapping.id),
                          "destinations": 4, "user_accounts_with_demo_access": len(users),
                          "demo_email": demo.email, "hardware_simulated": False}))
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-staging", action="store_true", help="Explicitly seed the dedicated demo account on a staging database")
    args = parser.parse_args()
    asyncio.run(main(allow_staging=args.allow_staging))
