from fastapi import FastAPI
from sqlalchemy import select

from app.admin.router import router as admin_router
from app.auth.router import router as auth_router
from app.dashboard.router import router as dashboard_router
from app.delivery.router import router as delivery_router
from app.destinations.router import router as destinations_router
from app.events.router import router as events_router
from app.mapping.router import router as mapping_router
from app.safety.router import router as safety_router
from app.simulation.router import router as sim_router
from app.auth.service import hash_password
from app.core.config import settings
from app.db.models import User, UserRole
from app.db.redis import close_redis
from app.db.session import AsyncSessionLocal

app = FastAPI(
    title="Indoor Delivery Robot Backend",
    version="0.1.0",
    debug=settings.DEBUG,
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

if settings.SIMULATION_MODE:
    app.include_router(sim_router)


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def on_startup() -> None:
    await _seed_first_admin()
    # Sync safety state from DB (Requirement 7.1)
    from app.safety.manager import safety_manager
    await safety_manager.sync_from_db()
    # Wire dashboard hub to safety manager for broadcasting
    from app.dashboard.ws_hub import hub
    safety_manager.set_hub(hub)
    # Start heartbeat timeout monitor (Requirement 3.1)
    from app.dashboard.service import status_service
    status_service.start_monitor()
    
    # Start background tasks based on mode
    if settings.SIMULATION_MODE:
        from app.simulation.mock_slam import MockSlamPublisher
        
        # In a real app we'd inject the actual mapping service instance here
        mock_mapping_service = None
        publisher = MockSlamPublisher(mock_mapping_service)
        publisher.start()
        # Keep a reference so it's not garbage collected
        app.state.mock_slam_publisher = publisher
    else:
        from app.rosbridge.client import RosbridgeClient
        client = RosbridgeClient()
        client.start()
        app.state.rosbridge_client = client


async def _seed_first_admin() -> None:
    """
    Create the first ADMIN account from env vars if no admin exists yet.
    Requirement 1.1
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.role == UserRole.ADMIN).limit(1)
        )
        if result.scalar_one_or_none() is not None:
            return  # Admin already exists

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
    
    if hasattr(app.state, "mock_slam_publisher") and app.state.mock_slam_publisher:
        app.state.mock_slam_publisher.stop()
        
    if hasattr(app.state, "rosbridge_client") and app.state.rosbridge_client:
        app.state.rosbridge_client.stop()
        
    await close_redis()


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
