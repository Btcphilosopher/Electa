"""
Electa Systems — Events Router
GET /events/stream    Server-Sent Events (SSE) real-time stream
WS  /events/ws        WebSocket stream
GET /events/recent    Replay buffer snapshot
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from services.event_bus import event_bus

logger = logging.getLogger("electa.events")
router = APIRouter()


@router.get("/stream", summary="SSE real-time governance event stream")
async def sse_stream(
    replay: bool = Query(False, description="Replay buffered events before going live"),
    filter: Optional[str] = Query(None, description="Event prefix filter"),
):
    """
    Opens a Server-Sent Events stream. Each message is a JSON-encoded
    GovernanceEvent. Compatible with EventSource and financial data terminals.
    """
    async def generator():
        async with event_bus.subscribe(replay=replay) as queue:
            yield 'data: {"event":"stream.connected","system":"Electa Systems"}\n\n'
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if event is None:
                    break
                if filter and not event.event.startswith(filter):
                    continue
                payload = json.dumps(event.model_dump(), separators=(",", ":"))
                yield f"id: {event.timestamp}\ndata: {payload}\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@router.websocket("/ws")
async def websocket_stream(
    websocket: WebSocket,
    replay: bool = Query(False),
    filter: Optional[str] = Query(None),
):
    """
    WebSocket stream of governance events. Each message is a JSON string
    matching the GovernanceEvent schema. Clients may send:
      {"action": "filter", "prefix": "governance.vote"}
    to dynamically update their filter.
    """
    await websocket.accept()
    active_filter = filter

    async def _listen():
        nonlocal active_filter
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                    if msg.get("action") == "filter":
                        active_filter = msg.get("prefix")
                        await websocket.send_json(
                            {"event": "stream.filter_updated",
                             "filter": active_filter})
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass

    listen_task = asyncio.create_task(_listen())
    try:
        async with event_bus.subscribe(replay=replay) as queue:
            await websocket.send_json(
                {"event": "stream.connected", "system": "Electa Systems",
                 "replay": replay})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    await websocket.send_json({"event": "heartbeat"})
                    continue
                if event is None:
                    break
                if active_filter and not event.event.startswith(active_filter):
                    continue
                await websocket.send_json(event.model_dump())
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error("WebSocket error: %s", exc)
    finally:
        listen_task.cancel()
        try:
            await websocket.close()
        except Exception:
            pass


@router.get("/recent", summary="Recent events from replay buffer")
async def recent_events(
    limit: int = Query(50, ge=1, le=200),
    filter: Optional[str] = Query(None),
):
    events = event_bus.recent_events(limit=limit)
    if filter:
        events = [e for e in events if e.event.startswith(filter)]
    return {"count": len(events), "events": [e.model_dump() for e in events]}
