"""
Electa Systems — Votes Router
POST /votes                       Cast a vote / approve / reject / delegate
GET  /votes/{proposal_id}         List votes for a proposal
GET  /votes/{proposal_id}/audit   Audit trail for a proposal
"""

import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_db
from models.db_models import (
    ActionType, AuditAction, AuditLog, Proposal,
    ProposalStatus, User, Vote, VoteChoice,
)
from models.schemas import AuditLogEntry, VoteCast, VoteResponse
from services.decision_engine import build_vote_event, get_voter_weight
from services.event_bus import event_bus
from utils.audit import append_audit
from utils.crypto import canonical_vote_payload, verify_signature

router = APIRouter()


@router.post("", response_model=VoteResponse, status_code=201)
async def cast_vote(
    body: VoteCast, request: Request, db: AsyncSession = Depends(get_db),
):
    # ── Validate proposal ─────────────────────────────────────────────────────
    proposal = await db.get(Proposal, body.proposal_id)
    if not proposal:
        raise HTTPException(404, "Proposal not found.")
    if proposal.status != ProposalStatus.OPEN:
        raise HTTPException(409,
            f"Proposal is not open for voting (status: {proposal.status.value}).")
    now = int(time.time())
    if proposal.closes_at and now > proposal.closes_at:
        raise HTTPException(409, "Voting period has ended.")

    # ── Validate voter ────────────────────────────────────────────────────────
    voter = await db.get(User, body.voter_id)
    if not voter or not voter.is_active:
        raise HTTPException(404, "Voter not found or inactive.")

    # ── Duplicate vote check ──────────────────────────────────────────────────
    existing = (await db.execute(
        select(Vote).where(Vote.proposal_id == body.proposal_id,
                           Vote.voter_id == body.voter_id)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "This voter has already cast a vote on this proposal.")

    # ── Resolve base weight ───────────────────────────────────────────────────
    weight = await get_voter_weight(db, body.voter_id, proposal.entity_id)
    if weight <= 0:
        raise HTTPException(403, "Voter has no ownership stake in this entity.")

    # ── Delegation ────────────────────────────────────────────────────────────
    if body.action_type == "delegate":
        target = await db.get(User, body.delegate_to_id)
        if not target or not target.is_active:
            raise HTTPException(404, "Delegation target not found.")
        if body.delegate_to_id == body.voter_id:
            raise HTTPException(422, "Cannot delegate to yourself.")

        # If target already voted, add weight to their record
        target_vote = (await db.execute(
            select(Vote).where(Vote.proposal_id == body.proposal_id,
                               Vote.voter_id == body.delegate_to_id)
        )).scalar_one_or_none()
        if target_vote:
            target_vote.weight = float(target_vote.weight) + weight
            await db.flush()

        delegation = Vote(
            proposal_id=body.proposal_id, voter_id=body.voter_id,
            action_type=ActionType.DELEGATE, choice=None, weight=weight,
            metadata_={"delegate_to": body.delegate_to_id},
        )
        db.add(delegation)
        await db.flush()

        await append_audit(db, AuditAction.VOTE_DELEGATED,
                           payload={"voter_id": body.voter_id,
                                    "delegate_to": body.delegate_to_id,
                                    "weight": weight},
                           actor_id=body.voter_id, proposal_id=body.proposal_id,
                           vote_id=delegation.id)
        await event_bus.publish(
            build_vote_event(proposal, body.voter_id, voter.name,
                             choice=None, weight=weight, action_type="delegate")
        )
        return VoteResponse.model_validate(delegation)

    # ── Add delegated weight ──────────────────────────────────────────────────
    delegated = (await db.execute(
        select(func.sum(Vote.weight)).where(
            Vote.proposal_id == body.proposal_id,
            Vote.action_type == ActionType.DELEGATE,
            Vote.metadata_["delegate_to"].as_string() == body.voter_id,
        )
    )).scalar_one_or_none()
    weight += float(delegated or 0.0)

    # ── Crypto verification ───────────────────────────────────────────────────
    signature_verified: Optional[bool] = None
    if body.signature and voter.public_key:
        canonical = canonical_vote_payload(
            body.proposal_id, body.voter_id, body.choice, now
        )
        signature_verified = verify_signature(voter.public_key, body.signature, canonical)
        if not signature_verified and settings.REQUIRE_CRYPTO_SIGNATURES:
            raise HTTPException(403, "Cryptographic signature verification failed.")

    # ── Validate choice ───────────────────────────────────────────────────────
    try:
        choice = VoteChoice(body.choice)
    except ValueError:
        raise HTTPException(422,
            f"Invalid choice '{body.choice}'. Valid: YES, NO, ABSTAIN")
    try:
        action_type = ActionType(body.action_type)
    except ValueError:
        action_type = ActionType.VOTE

    # ── Persist vote ──────────────────────────────────────────────────────────
    vote = Vote(
        proposal_id=body.proposal_id, voter_id=body.voter_id,
        action_type=action_type, choice=choice, weight=weight,
        signature=body.signature, signature_verified=signature_verified,
        timestamp=now,
        ip_address=request.client.host if request.client else None,
        metadata_=body.metadata,
    )
    db.add(vote)
    await db.flush()

    await append_audit(db, AuditAction.VOTE_CAST,
                       payload={"vote_id": vote.id, "voter_id": body.voter_id,
                                "choice": choice.value, "weight": weight,
                                "signature_verified": signature_verified},
                       actor_id=body.voter_id, proposal_id=body.proposal_id,
                       vote_id=vote.id,
                       ip_address=request.client.host if request.client else None)

    await event_bus.publish(
        build_vote_event(proposal, body.voter_id, voter.name,
                         choice=choice.value, weight=weight,
                         action_type=action_type.value)
    )
    return VoteResponse.model_validate(vote)


@router.get("/{proposal_id}", response_model=List[VoteResponse])
async def list_votes(
    proposal_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    if not await db.get(Proposal, proposal_id):
        raise HTTPException(404, "Proposal not found.")
    result = await db.execute(
        select(Vote).where(Vote.proposal_id == proposal_id)
        .offset(skip).limit(limit).order_by(Vote.timestamp.asc())
    )
    return [VoteResponse.model_validate(v) for v in result.scalars().all()]


@router.get("/{proposal_id}/audit", response_model=List[AuditLogEntry])
async def get_proposal_audit(
    proposal_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AuditLog).where(AuditLog.proposal_id == proposal_id)
        .offset(skip).limit(limit).order_by(AuditLog.timestamp.asc())
    )
    return [AuditLogEntry.model_validate(e) for e in result.scalars().all()]
