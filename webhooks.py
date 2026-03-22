"""
Electa Systems — Webhooks Router
POST   /webhooks                  Register a webhook endpoint
GET    /webhooks                  List webhooks
GET    /webhooks/{id}             Get webhook
DELETE /webhooks/{id}             Deactivate webhook
GET    /webhooks/{id}/deliveries  Delivery history
POST   /webhooks/test             Send a test ping
"""

import asyncio
import time
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import User, WebhookDelivery, WebhookEndpoint
from models.schemas import GovernanceEvent, MessageResponse, WebhookCreate, WebhookResponse
from services.webhook_service import dispatch_event_to_webhooks

router = APIRouter()


@router.post("", response_model=WebhookResponse, status_code=201)
async def register_webhook(body: WebhookCreate, db: AsyncSession = Depends(get_db)):
    if not await db.get(User, body.owner_id):
        raise HTTPException(404, "Owner user not found.")
    endpoint = WebhookEndpoint(owner_id=body.owner_id, url=body.url,
                               secret=body.secret, event_filter=body.event_filter,
                               metadata_=body.metadata)
    db.add(endpoint)
    await db.flush()
    return WebhookResponse.model_validate(endpoint)


@router.get("", response_model=List[WebhookResponse])
async def list_webhooks(
    owner_id: str = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(WebhookEndpoint).where(WebhookEndpoint.is_active == True)  # noqa: E712
    if owner_id:
        stmt = stmt.where(WebhookEndpoint.owner_id == owner_id)
    result = await db.execute(stmt.offset(skip).limit(limit))
    return [WebhookResponse.model_validate(e) for e in result.scalars().all()]


@router.get("/{endpoint_id}", response_model=WebhookResponse)
async def get_webhook(endpoint_id: str, db: AsyncSession = Depends(get_db)):
    ep = await db.get(WebhookEndpoint, endpoint_id)
    if not ep:
        raise HTTPException(404, "Webhook endpoint not found.")
    return WebhookResponse.model_validate(ep)


@router.delete("/{endpoint_id}", response_model=MessageResponse)
async def deactivate_webhook(endpoint_id: str, db: AsyncSession = Depends(get_db)):
    ep = await db.get(WebhookEndpoint, endpoint_id)
    if not ep:
        raise HTTPException(404, "Webhook endpoint not found.")
    ep.is_active = False
    return MessageResponse(message=f"Webhook '{endpoint_id}' deactivated.")


@router.get("/{endpoint_id}/deliveries")
async def list_deliveries(
    endpoint_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    if not await db.get(WebhookEndpoint, endpoint_id):
        raise HTTPException(404, "Webhook endpoint not found.")
    result = await db.execute(
        select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint_id)
        .offset(skip).limit(limit).order_by(WebhookDelivery.attempted_at.desc())
    )
    return {
        "endpoint_id": endpoint_id,
        "deliveries": [
            {"id": d.id, "event_type": d.event_type, "http_status": d.http_status,
             "success": d.success, "attempt_number": d.attempt_number,
             "attempted_at": d.attempted_at, "error_message": d.error_message}
            for d in result.scalars().all()
        ],
    }


@router.post("/test", response_model=MessageResponse)
async def send_test_event(
    endpoint_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    ep = await db.get(WebhookEndpoint, endpoint_id)
    if not ep or not ep.is_active:
        raise HTTPException(404, "Webhook endpoint not found.")
    test_event = GovernanceEvent(
        event="governance.test.ping", entity="Electa-Systems",
        proposal_id="TEST-000", actor="system",
        details={"message": "Test delivery from Electa Systems GEA."},
        timestamp=int(time.time()),
    )
    asyncio.create_task(dispatch_event_to_webhooks(test_event))
    return MessageResponse(message=f"Test event dispatched to '{endpoint_id}'.")
