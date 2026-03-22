"""
Electa Systems — Startup Hooks
Wires the webhook dispatcher to the event bus at application startup.
"""

import asyncio
import logging

from services.event_bus import event_bus
from services.webhook_service import dispatch_event_to_webhooks

logger = logging.getLogger("electa.startup")


def register_event_hooks():
    async def webhook_hook(event):
        asyncio.create_task(dispatch_event_to_webhooks(event))

    event_bus.register_hook(webhook_hook)
    logger.info("Webhook dispatcher registered as event bus hook.")
