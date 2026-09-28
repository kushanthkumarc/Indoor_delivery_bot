"""
rosbridge WebSocket Client — Module 12.

Maintains a persistent connection to the robot, handles automatic reconnection,
and routes incoming messages to the appropriate handlers.

Requirements: 6.7, 7.5, 7.6, 3.1
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Awaitable, Any

import websockets
from websockets.exceptions import ConnectionClosed

from app.core.config import settings
from app.dashboard.service import status_service
from app.safety.manager import safety_manager

logger = logging.getLogger(__name__)

# Callback type for message routers
MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class RosbridgeClient:
    """
    Persistent WebSocket client to the robot's rosbridge server.
    """
    
    def __init__(self, url: str | None = None) -> None:
        self.url = url or getattr(settings, "ROSBRIDGE_URL", "ws://localhost:9090")
        self._ws = None
        self._handlers: list[MessageHandler] = []
        self._reconnect_task: asyncio.Task | None = None
        
        # Determine robot_id from DB or config in a real app, assuming static for now
        self.robot_id = "default-robot-id"  
        
        logger.info("RosbridgeClient created with URL: %s", self.url)

    def register_handler(self, handler: MessageHandler) -> None:
        """Register a callback for incoming messages."""
        self._handlers.append(handler)
        
    async def send(self, message: str) -> None:
        """Send a raw JSON string to the robot."""
        if not self._ws or not self._ws.open:
            logger.error("RosbridgeClient: cannot send, WS disconnected")
            # In a real app we might throw a specific exception here
            return
            
        try:
            await self._ws.send(message)
        except Exception as e:
            logger.error("RosbridgeClient: send failed: %s", str(e))

    async def connect(self) -> None:
        """Connect to rosbridge and keep listening."""
        backoff = 1.0
        max_backoff = 60.0
        
        while True:
            try:
                logger.info("RosbridgeClient connecting to %s...", self.url)
                async with websockets.connect(self.url) as ws:
                    self._ws = ws
                    logger.info("RosbridgeClient connected to %s", self.url)
                    
                    # Notify safety manager (Requirement 7.6)
                    await safety_manager.on_ws_reconnect(self.robot_id)
                    
                    # Reset backoff on successful connect
                    backoff = 1.0
                    
                    # Message loop
                    async for message in ws:
                        await self._route_message(message)
                        
            except ConnectionClosed as e:
                logger.warning("RosbridgeClient connection closed: %s", e)
            except Exception as e:
                logger.error("RosbridgeClient connection error: %s", e)
                
            # Notify safety manager of disconnect (Requirement 7.5)
            await safety_manager.on_ws_disconnect(self.robot_id)
            
            # Backoff and reconnect
            logger.info("RosbridgeClient reconnecting in %.1f seconds...", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    async def _route_message(self, raw_message: str | bytes) -> None:
        """Route incoming JSON message to all registered handlers."""
        try:
            msg = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.warning("RosbridgeClient: received invalid JSON")
            return
            
        # 1. Intercept heartbeat directly (Req 3.1)
        if msg.get("topic") == "/heartbeat":
            await status_service.update_heartbeat(self.robot_id)
            
        # 2. Forward to all registered handlers (Nav2 bridge, mapping session, etc)
        for handler in self._handlers:
            try:
                await handler(msg)
            except Exception as e:
                logger.exception("RosbridgeClient: handler error for message")
                
    def start(self) -> None:
        """Start the client as a background task."""
        self._reconnect_task = asyncio.create_task(self.connect(), name="rosbridge_client")
        
    def stop(self) -> None:
        """Stop the client and cancel reconnects."""
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
