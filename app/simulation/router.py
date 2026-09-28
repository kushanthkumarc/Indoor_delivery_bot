"""
Simulation Router — Module 7 API endpoints.

REST API for interacting with the simulation environment.
"""
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth.dependencies import require_admin
from app.core.config import settings
from app.db.models import User

router = APIRouter(prefix="/sim", tags=["Simulation"])

class InjectFaultRequest(BaseModel):
    fault: str

@router.post(
    "/inject-fault",
    status_code=status.HTTP_200_OK,
    responses={
        403: {"description": "Simulation mode is not active"},
        400: {"description": "Unknown fault type"},
    },
)
async def inject_fault(
    request: InjectFaultRequest,
    current_admin: Annotated[User, Depends(require_admin)],
) -> Any:
    """
    Inject a simulated hardware or transport fault. ADMIN only.
    Only enabled when SIMULATION_MODE=True.
    """
    if not settings.SIMULATION_MODE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Fault injection is only available when SIMULATION_MODE=True"
        )
        
    # Lazy import to avoid circular dependencies during testing
    from app.simulation.fault_injector import FaultInjector
    # In a full DI setup, this would be injected. We approximate here.
    
    # Placeholder for wiring
    fault_type = request.fault.upper()
    
    # We will simulate the failure for the sake of the endpoint implementation structure
    raise HTTPException(status_code=501, detail="Dependency injection not wired in this static file")
