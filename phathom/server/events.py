"""In-process pub/sub for live UI updates.

The extension background relays ``live`` messages to popup/side-panel/dashboard;
this hub is the server-side fan-out point (also used by tests and, later, SSE).
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("phathom.server.events")


class Hub:
    def __init__(self, maxsize: int = 200) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    async def publish(self, event: dict) -> None:
        dead = []
        for q in self._subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
            except Exception:  # noqa: BLE001
                dead.append(q)
        for q in dead:
            self._subs.discard(q)
