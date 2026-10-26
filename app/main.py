import asyncio
import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text

from app.admin.router import router as admin_router
from app.auth.router import router as auth_router
from app.dashboard.router import router as dashboard_router
from app.delivery.router import router as delivery_router
from app.destinations.router import router as destinations_router
from app.events.router import router as events_router
from app.mapping.router import router as mapping_router
from app.safety.router import router as safety_router
from app.auth.service import hash_password
from app.core.config import settings, validate_production_settings
from app.db.models import User, UserRole
from app.db.redis import close_redis, get_redis
from app.db.session import AsyncSessionLocal, close_database

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Indoor Delivery Robot Backend",
    version="0.1.0",
    debug=settings.DEBUG,
)

_cors_origins = [origin.strip() for origin in settings.CORS_ORIGINS.split(",") if origin.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(events_router)
app.include_router(mapping_router)
app.include_router(dashboard_router)
app.include_router(destinations_router)
app.include_router(safety_router)
app.include_router(delivery_router)

# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def on_startup() -> None:
    stage = "configuration validation"
    try:
        validate_production_settings()
        stage = "database connectivity check"
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        stage = "Redis connectivity check"
        if not await (await get_redis()).ping():
            raise ConnectionError("Redis ping did not succeed")

        if settings.RUN_MIGRATIONS_ON_STARTUP:
            stage = "startup migration"
            from app.core.startup import run_in_thread
            await asyncio.get_running_loop().run_in_executor(None, run_in_thread)
        stage = "admin bootstrap"
        await _seed_first_admin()
        stage = "safety state synchronization"
        from app.safety.manager import safety_manager
        await safety_manager.sync_from_db()
        from app.dashboard.ws_hub import hub
        safety_manager.set_hub(hub)
        from app.dashboard.service import status_service
        status_service.start_monitor()

        if settings.ROBOT_COMMAND_CHANNEL_ENABLED:
            from app.delivery import router as delivery_module
            from app.delivery.state_machine import DeliveryStateMachine
            from app.gateway.navigation import gateway_navigation
            delivery_module.delivery_state_machine = DeliveryStateMachine(
                gateway_navigation, freshness_check=status_service.connection_freshness)
            logger.info("Robot command channel enabled (goal commands over gateway socket)")
        if settings.ROSBRIDGE_ENABLED:
            from app.rosbridge.client import RosbridgeClient
            client = RosbridgeClient()
            client.start()
            app.state.rosbridge_client = client
        else:
            logger.info("ROS bridge startup disabled; robot connectivity remains telemetry-driven")
        logger.info("Application startup completed")
    except Exception as exc:
        logger.error("Application startup failed during %s (%s); service is not ready",
                     stage, type(exc).__name__)
        raise RuntimeError(f"Application startup failed during {stage}") from None


async def _seed_first_admin() -> None:
    """
    Create the first ADMIN account from env vars if no admin exists yet.
    Requirement 1.1
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(
                User.role == UserRole.ADMIN, User.is_active.is_(True)
            ).limit(1)
        )
        if result.scalar_one_or_none() is not None:
            return  # Admin already exists

        configured = set(settings.model_fields_set) | set(os.environ)
        if not {"ADMIN_EMAIL", "ADMIN_PASSWORD"}.issubset(configured):
            raise RuntimeError(
                "No active ADMIN exists; provision the first administrator with "
                "ADMIN_EMAIL and ADMIN_PASSWORD before startup"
            )

        admin = User(
            email=settings.ADMIN_EMAIL,
            password_hash=hash_password(settings.ADMIN_PASSWORD),
            name="Admin",
            role=UserRole.ADMIN,
            is_active=True,
        )
        session.add(admin)
        await session.commit()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    # Stop heartbeat monitor
    from app.dashboard.service import status_service
    status_service.stop_monitor()
    
    if hasattr(app.state, "rosbridge_client") and app.state.rosbridge_client:
        app.state.rosbridge_client.stop()
        
    try:
        await close_redis()
    except Exception as exc:
        logger.error("Redis shutdown cleanup failed (%s)", type(exc).__name__)
    await close_database()


# ---------------------------------------------------------------------------
# Exception Handlers
# ---------------------------------------------------------------------------
from fastapi import Request
from fastapi.responses import JSONResponse
from app.delivery.state_machine import InvalidTransitionError

@app.exception_handler(InvalidTransitionError)
async def invalid_transition_exception_handler(request: Request, exc: InvalidTransitionError):
    return JSONResponse(
        status_code=422,
        content={"error": "INVALID_TRANSITION", "message": str(exc)},
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
async def readiness() -> dict:
    """Dependency readiness; unlike /health this checks MySQL and Redis."""
    checks = {"database": False, "redis": False}
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        pass
    try:
        checks["redis"] = bool(await (await get_redis()).ping())
    except Exception:
        pass
    if not all(checks.values()):
        raise HTTPException(status_code=503, detail={"status": "not_ready", "checks": checks})
    return {"status": "ready", "checks": checks}
