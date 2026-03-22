"""
Electa Systems — Users Router
POST /users            Create a user (returns one-time API key)
GET  /users            List users
GET  /users/{id}       Get user
DELETE /users/{id}     Deactivate user
"""

import hashlib
import secrets
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import User, UserRole
from models.schemas import MessageResponse, UserCreate, UserResponse

router = APIRouter()


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(body: UserCreate, db: AsyncSession = Depends(get_db)):
    if body.external_id:
        existing = await db.execute(
            select(User).where(User.external_id == body.external_id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(409,
                f"User with external_id '{body.external_id}' already exists.")

    try:
        role = UserRole(body.role)
    except ValueError:
        raise HTTPException(422,
            f"Invalid role '{body.role}'. Valid: {[r.value for r in UserRole]}")

    raw_api_key  = secrets.token_urlsafe(32)
    api_key_hash = hashlib.sha256(raw_api_key.encode()).hexdigest()

    user = User(name=body.name, role=role, external_id=body.external_id,
                public_key=body.public_key, api_key_hash=api_key_hash,
                metadata_=body.metadata)
    db.add(user)
    await db.flush()

    resp = UserResponse.model_validate(user).model_dump()
    resp["api_key"] = raw_api_key  # surfaced once; not stored in plaintext
    return resp


@router.get("", response_model=List[UserResponse])
async def list_users(
    role: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User).where(User.is_active == True)  # noqa: E712
    if role:
        stmt = stmt.where(User.role == role)
    stmt = stmt.offset(skip).limit(limit).order_by(User.created_at.desc())
    result = await db.execute(stmt)
    return [UserResponse.model_validate(u) for u in result.scalars().all()]


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(user_id: str, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(404, "User not found.")
    return UserResponse.model_validate(user)


@router.delete("/{user_id}", response_model=MessageResponse)
async def deactivate_user(user_id: str, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found.")
    user.is_active = False
    return MessageResponse(message=f"User '{user.name}' deactivated.")
