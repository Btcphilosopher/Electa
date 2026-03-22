"""
Electa Systems — Event Bus
Async pub/sub backbone. Fan-outs GovernanceEvents to all registered
consumers: WebSocket streams, SSE clients, and the webhook dispatcher.
Maintains a 200-event ring buffer for late-joining consumers.
"""

import asyncio
import logging
from typing import Callable, Dict, List, Optional
from uuid import uuid4

from models.schemas import GovernanceEvent

logger = logging.getLogger("electa.event_bus")


class EventBus:
    """
    Lightweight in-process pub/sub engine.

    Producers: await event_bus.publish(event)
    Consumers: async with event_bus.subscribe() as queue: ...
    """

    REPLAY_BUFFER_SIZE = 200

    def __init__(self):
        self._queues: Dict[str, asyncio.Queue] = {}
        self._hooks: List[Callable] = []
        self._replay_buffer: List[GovernanceEvent] = []
        self._lock = asyncio.Lock()
        self._running = False

    async def start(self):
        self._running = True
        logger.info("EventBus started.")

    async def stop(self):
        self._running = False
        async with self._lock:
            for q in self._queues.values():
                try:
                    q.put_nowait(None)  # shutdown sentinel
                except asyncio.QueueFull:
                    pass
        logger.info("EventBus stopped.")

    def subscriber_count(self) -> int:
        return len(self._queues)

    def register_hook(self, callback: Callable):
        self._hooks.append(callback)

    def unregister_hook(self, callback: Callable):
        self._hooks = [h for h in self._hooks if h is not callback]

    async def publish(self, event: GovernanceEvent):
        if not self._running:
            logger.warning("EventBus not running; event dropped: %s", event.event)
            return

        async with self._lock:
            self._replay_buffer.append(event)
            if len(self._replay_buffer) > self.REPLAY_BUFFER_SIZE:
                self._replay_buffer = self._replay_buffer[-self.REPLAY_BUFFER_SIZE:]

            dead: List[str] = []
            for sub_id, q in self._queues.items():
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning("Subscriber %s queue full — event dropped.", sub_id)
                except Exception as exc:
                    logger.error("Queue error for %s: %s", sub_id, exc)
                    dead.append(sub_id)
            for sub_id in dead:
                del self._queues[sub_id]

        for hook in list(self._hooks):
            try:
                await hook(event)
            except Exception as exc:
                logger.error("Hook %s raised: %s", hook, exc)

        logger.debug("Published: %s | proposal=%s", event.event, event.proposal_id)

    class _Subscription:
        def __init__(self, bus: "EventBus", replay: bool = False):
            self._bus = bus
            self._sub_id: Optional[str] = None
            self._queue: Optional[asyncio.Queue] = None
            self._replay = replay

        async def __aenter__(self) -> asyncio.Queue:
            self._sub_id = str(uuid4())
            self._queue = asyncio.Queue(maxsize=500)
            async with self._bus._lock:
                self._bus._queues[self._sub_id] = self._queue
                if self._replay:
                    for evt in self._bus._replay_buffer:
                        try:
                            self._queue.put_nowait(evt)
                        except asyncio.QueueFull:
                            break
            logger.debug("Subscriber %s connected.", self._sub_id)
            return self._queue

        async def __aexit__(self, *_):
            async with self._bus._lock:
                self._bus._queues.pop(self._sub_id, None)
            logger.debug("Subscriber %s disconnected.", self._sub_id)

    def subscribe(self, replay: bool = False) -> "_Subscription":
        return self._Subscription(self, replay=replay)

    def recent_events(self, limit: int = 50) -> List[GovernanceEvent]:
        return list(reversed(self._replay_buffer[-limit:]))


event_bus = EventBus()
