"""
Simulation Fault Injector — Module 7.5

Injects faults into the system testing error recovery logic.
Requirements: 8.5
"""
from __future__ import annotations

import logging
from typing import Any

from app.safety.manager import safety_manager

logger = logging.getLogger(__name__)


class FaultInjector:
    """
    Injects simulated hardware and transport faults.
    """
    
    def __init__(self, mapping_service: Any, goal_tracker: Any) -> None:
        self._mapping_service = mapping_service
        self._goal_tracker = goal_tracker
        
    async def inject_slam_lost(self, session_id: str = "mock-session-id") -> None:
        """Force the SLAM state to LOST."""
        logger.info("Injecting fault: SLAM_LOST")
        if hasattr(self._mapping_service, "handle_slam_status_update"):
            await self._mapping_service.handle_slam_status_update(session_id, "LOST")
            
    async def inject_nav2_timeout(self) -> None:
        """Force all active navigation goals to timeout immediately."""
        logger.info("Injecting fault: NAV2_TIMEOUT")
        if hasattr(self._goal_tracker, "_goals"):
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            # Expire deadlines immediately
            async with self._goal_tracker._lock:
                for goal in self._goal_tracker._goals.values():
                    goal.deadline = now
            # Trigger timeout check
            await self._goal_tracker.check_timeouts()
            
    async def inject_emergency_stop(self) -> None:
        """Trigger global emergency stop."""
        logger.info("Injecting fault: EMERGENCY_STOP")
        await safety_manager.trigger_emergency_stop(
            source="fault_injector", 
            reason="Simulated hardware fault"
        )
