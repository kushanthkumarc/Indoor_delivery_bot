"""
Simulation mock SLAM publisher — Module 7.3

Publishes mock SLAM poses through the system to simulate movement.
Requirements: 8.3, 8.4
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


class MockSlamPublisher:
    """
    Publishes fake SLAM poses to simulate robot movement.
    """
    
    def __init__(self, mapping_service: Any, delivery_manager: Any = None) -> None:
        self._hz = float(settings.SIM_SLAM_HZ)
        self._path_mode = getattr(settings, "SIM_PATH", "random_walk")
        self._mapping_service = mapping_service
        self._task: asyncio.Task | None = None
        
        # Fake robot state tracking
        self.x = 0.0
        self.y = 0.0
        self.theta = 0.0
        self.session_id = "mock-session-id"

    async def _run(self) -> None:
        """Background loop publishing poses at configured Hz."""
        delay = 1.0 / self._hz if self._hz > 0 else 0.1
        
        logger.info(
            "MockSlamPublisher starting: %.1f Hz, mode=%s", 
            self._hz, self._path_mode
        )
        
        while True:
            try:
                # Update fake pose
                if self._path_mode == "random_walk":
                    self.x += random.uniform(-0.1, 0.1)
                    self.y += random.uniform(-0.1, 0.1)
                    self.theta += random.uniform(-0.05, 0.05)
                else:
                    # Very simple scripted forward movement
                    self.x += 0.05
                    
                pose_update = {
                    "session_id": self.session_id,
                    "x": self.x,
                    "y": self.y,
                    "theta": self.theta,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
                
                # Send to real mapping service handler
                # Note: This is a placeholder since mapping module is not fully implemented
                if hasattr(self._mapping_service, "handle_pose_update"):
                    await self._mapping_service.handle_pose_update(
                        self.session_id, self.x, self.y, self.theta
                    )
                    
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("MockSlamPublisher error")
                
            await asyncio.sleep(delay)
            
    def start(self) -> None:
        """Start the publisher background task."""
        self._task = asyncio.create_task(self._run(), name="mock_slam_publisher")
        
    def stop(self) -> None:
        """Stop the background task."""
        if self._task and not self._task.done():
            self._task.cancel()
