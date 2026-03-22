"""
Electa Systems — Webhook Service
Delivers GovernanceEvents to registered HTTP endpoints.
Supports HMAC-SHA256 payload signing and exponential-backoff retries.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import AsyncSessionLocal
from models.db_models import WebhookDelivery, WebhookEndpoint
from models.schemas import GovernanceEvent

logger = logging.getLogger("electa.webhook")


def _sign_payload(secret: str, body: bytes) -> str:
    """HMAC-SHA256 hex digest for payload signing."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _matches_filter(event_type: str, event_filter: Optional[str]) -> bool:
    """Glob-style filter: 'governance.vote.*' matches 'governance.vote.cast'."""
    if not event_filter:
        return True
    for pattern in [p.strip() for p in event_filter.split(",") if p.strip()]:
        if pattern == event_type:
            return True
        if pattern.endswith("*") and event_type.startswith(pattern[:-1]):
            return True
    return False


async def _deliver_once(
    client: httpx.AsyncClient,
    endpoint: WebhookEndpoint,
    event: GovernanceEvent,
    attempt: int,
    db: AsyncSession,
) -> bool:
    payload_dict = event.model_dump()
    body = json.dumps(payload_dict, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Electa-Event":            event.event,
        "X-Electa-Timestamp":        str(event.timestamp),
        "X-Electa-Delivery-Attempt": str(attempt),
    }
    if endpoint.secret:
        headers["X-Electa-Signature"] = f"sha256={_sign_payload(endpoint.secret, body)}"

    success, http_status, error_msg = False, None, None
    try:
        resp = await client.post(endpoint.url, content=body, headers=headers,
                                 timeout=settings.WEBHOOK_TIMEOUT_SECONDS)
        http_status = resp.status_code
        success = 200 <= http_status < 300
        if not success:
            error_msg = f"HTTP {http_status}: {resp.text[:200]}"
    except httpx.TimeoutException:
        error_msg = "Request timed out"
    except httpx.RequestError as exc:
        error_msg = f"Request error: {exc}"
    except Exception as exc:
        error_msg = f"Unexpected error: {exc}"

    db.add(WebhookDelivery(
        endpoint_id=endpoint.id, event_type=event.event, payload=payload_dict,
        http_status=http_status, success=success,
        attempt_number=attempt, error_message=error_msg,
    ))
    if success:
        endpoint.last_delivery_at = int(time.time())
        endpoint.delivery_failures = 0
    else:
        endpoint.delivery_failures = (endpoint.delivery_failures or 0) + 1
        logger.warning("Webhook failed: endpoint=%s event=%s attempt=%d error=%s",
                       endpoint.id, event.event, attempt, error_msg)
    await db.flush()
    return success


async def dispatch_event_to_webhooks(event: GovernanceEvent):
    """Fan-out to all matching active endpoints. Called as a background task."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(WebhookEndpoint).where(WebhookEndpoint.is_active == True)  # noqa: E712
        )
        endpoints: List[WebhookEndpoint] = result.scalars().all()

    matching = [e for e in endpoints if _matches_filter(event.event, e.event_filter)]
    if not matching:
        return

    async with httpx.AsyncClient(follow_redirects=False) as client:
        for endpoint in matching:
            for attempt in range(1, settings.WEBHOOK_MAX_RETRIES + 1):
                async with AsyncSessionLocal() as db:
                    ep = await db.get(WebhookEndpoint, endpoint.id)
                    if ep is None or not ep.is_active:
                        break
                    success = await _deliver_once(client, ep, event, attempt, db)
                    await db.commit()
                if success:
                    break
                if attempt < settings.WEBHOOK_MAX_RETRIES:
                    backoff = settings.WEBHOOK_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    await asyncio.sleep(backoff)
