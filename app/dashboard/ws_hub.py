"""
WebSocket Dashboard Hub — Module 2.

Manages all connected ADMIN dashboard WebSocket clients and broadcasts
robot state events to them.

Requirements: 3.2, 3.7
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket

if TYPE_CHECKING:
    from app.dashboard.service import RobotStatusService

logger = logging.getLogger(__name__)


class DashboardWSHub:
    """
    Connection manager for ADMIN dashboard WebSocket clients.

    - Tracks all active connections.
    - Sends the current robot state snapshot immediately on connect (Req 3.7).
    - Broadcasts events to all connected clients (Req 3.2).
    """

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        # Injected by router after service is constructed to avoid circular deps
        self._status_service: "RobotStatusService | None" = None

    def set_status_service(self, service: "RobotStatusService") -> None:
        self._status_service = service

    async def connect(self, websocket: WebSocket) -> None:
        """
        Register a new WebSocket connection and immediately push the current
        robot state snapshot to it (Requirement 3.7).

        Note: the socket must already be accepted by the caller — this
        method only registers it with the hub and sends the snapshot.
        """
        self._connections.add(websocket)
        logger.info("Dashboard client connected. Total: %d", len(self._connections))

        if self._status_service is not None:
            try:
                await self.send_snapshot(websocket)
            except Exception:
                logger.exception("Failed to send initial snapshot to new client")

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket from the active connection set."""
        self._connections.discard(websocket)
        logger.info("Dashboard client disconnected. Total: %d", len(self._connections))

    async def broadcast(self, event_type: str, payload: dict[str, Any]) -> None:
        """
        Push an event to every connected dashboard client.

        Stale connections are silently removed (Requirement 3.2).
        """
        if not self._connections:
            return

        message = {"type": event_type, "payload": payload}
        dead: set[WebSocket] = set()

        await asyncio.gather(
            *[self._safe_send(ws, message, dead) for ws in list(self._connections)],
            return_exceptions=True,
        )

        for ws in dead:
            self._connections.discard(ws)

    async def _safe_send(
        self,
        websocket: WebSocket,
        message: dict[str, Any],
        dead: set[WebSocket],
    ) -> None:
        try:
            await websocket.send_json(message)
        except Exception:
            dead.add(websocket)

    async def send_snapshot(self, websocket: WebSocket) -> None:
        """
        Send the current full robot state snapshot to a single client.

        Requirement 3.7 — on connect, the server sends the current state.
        """
        if self._status_service is None:
            return
        snapshot = await self._status_service.get_robot_snapshot()
        await websocket.send_json({"type": "robot.status_snapshot", "payload": snapshot})


# Module-level singleton used across the app
hub = DashboardWSHub()
