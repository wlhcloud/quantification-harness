"""Thread-safe broadcast hub bridging sync worker threads and WebSocket clients."""
from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any


class SyncHub:
    def __init__(self) -> None:
        self._queues: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._queues.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._queues.discard(q)

    def publish(self, message: dict[str, Any]) -> None:
        with self._lock:
            for q in list(self._queues):
                try:
                    q.put_nowait(message)
                except queue.Full:  # slow client: drop rather than block producers
                    pass

    def close(self) -> None:
        with self._lock:
            self._queues.clear()


async def websocket_loop(websocket, hub: SyncHub, initial: dict[str, Any]) -> None:
    """Accept a websocket, send the current snapshot, then fan out hub messages."""
    from fastapi import WebSocketDisconnect

    await websocket.accept()
    q = hub.subscribe()
    try:
        await websocket.send_json({"type": "snapshot", **initial})
        while True:
            try:
                message = q.get_nowait()
                await websocket.send_json(message)
            except queue.Empty:
                await asyncio.sleep(0.1)
    except (WebSocketDisconnect, RuntimeError):
        hub.unsubscribe(q)
    except Exception:  # noqa: BLE001
        hub.unsubscribe(q)
