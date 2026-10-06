"""In-process fan-out of live updates to connected dashboard WebSocket clients."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _SendsText(Protocol):
    async def send_text(self, data: str) -> None: ...


class Broadcaster:
    """Tracks connected WebSocket clients and pushes JSON messages to all of them."""

    def __init__(self) -> None:
        self._clients: set[_SendsText] = set()
        self._lock = asyncio.Lock()

    async def register(self, client: _SendsText) -> None:
        async with self._lock:
            self._clients.add(client)

    async def unregister(self, client: _SendsText) -> None:
        async with self._lock:
            self._clients.discard(client)

    async def broadcast(self, message: dict[str, Any]) -> None:
        data = json.dumps(message)
        async with self._lock:
            clients = list(self._clients)

        dead: list[_SendsText] = []
        for client in clients:
            try:
                await client.send_text(data)
            except Exception:
                dead.append(client)

        if dead:
            async with self._lock:
                for client in dead:
                    self._clients.discard(client)
