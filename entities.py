"""
Electa Systems — Entities Router
POST /entities                    Register a new entity (company/fund)
GET  /entities                    List entities
GET  /entities/{id}               Get entity
PUT  /entities/ownership          Upsert share ownership
GET  /entities/{id}/ownership     Get ownership table
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import Entity, Ownership, User
from models.schemas import (
    EntityCreate, EntityResponse, MessageResponse,
    OwnershipResponse, OwnershipSet,
)

router = APIRouter()


@router.post("", response_model=EntityResponse, status_code=201)
async def create_entity(body: EntityCreate, db: AsyncSession = Depends(get_db)):
    if body.ticker:
        existing = await db.execute(
            select(Entity).where(Entity.ticker == body.ticker)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(409,
                f"Entity with ticker '{body.ticker}' already exists.")

    entity = Entity(name=body.name, ticker=body.ticker, lei=body.lei,
                    jurisdiction=body.jurisdiction, total_shares=body.total_shares,
                    metadata_=body.metadata)
    db.add(entity)
    await db.flush()
    return EntityResponse.model_validate(entity)


@router.get("", response_model=List[EntityResponse])
async def list_entities(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Entity).where(Entity.is_active == True)  # noqa: E712
        .offset(skip).limit(limit).order_by(Entity.created_at.desc())
    )
    return [EntityResponse.model_validate(e) for e in result.scalars().all()]


@router.get("/{entity_id}", response_model=EntityResponse)
async def get_entity(entity_id: str, db: AsyncSession = Depends(get_db)):
    entity = await db.get(Entity, entity_id)
    if not entity or not entity.is_active:
        raise HTTPException(404, "Entity not found.")
    return EntityResponse.model_validate(entity)


@router.put("/ownership", response_model=OwnershipResponse)
async def upsert_ownership(body: OwnershipSet, db: AsyncSession = Depends(get_db)):
    """Upsert the share count for a user in an entity."""
    if not await db.get(User, body.user_id):
        raise HTTPException(404, "User not found.")
    if not await db.get(Entity, body.entity_id):
        raise HTTPException(404, "Entity not found.")

    result = await db.execute(
        select(Ownership).where(
            Ownership.user_id == body.user_id,
            Ownership.entity_id == body.entity_id,
        )
    )
    ownership = result.scalar_one_or_none()
    if ownership:
        ownership.shares = body.shares
        ownership.role_weight_multiplier = body.role_weight_multiplier
    else:
        ownership = Ownership(user_id=body.user_id, entity_id=body.entity_id,
                              shares=body.shares,
                              role_weight_multiplier=body.role_weight_multiplier)
        db.add(ownership)
    await db.flush()
    return OwnershipResponse.model_validate(ownership)


@router.get("/{entity_id}/ownership", response_model=List[OwnershipResponse])
async def get_ownership_table(entity_id: str, db: AsyncSession = Depends(get_db)):
    if not await db.get(Entity, entity_id):
        raise HTTPException(404, "Entity not found.")
    result = await db.execute(
        select(Ownership).where(Ownership.entity_id == entity_id)
    )
    return [OwnershipResponse.model_validate(o) for o in result.scalars().all()]
