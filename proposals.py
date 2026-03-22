"""
Electa Systems — Proposals Router
POST   /proposals                       Create proposal
GET    /proposals                       List proposals
GET    /proposals/{id}                  Get proposal
PATCH  /proposals/{id}/status           Update status
POST   /proposals/{id}/close            Force-close and compute result
GET    /proposals/{id}/result           Get computed result
"""

import time
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import (
    AuditAction, Entity, Proposal, ProposalStatus,
    ProposalType, ThresholdType, User,
)
from models.schemas import (
    GovernanceEvent, MessageResponse, ProposalCreate,
    ProposalResponse, ProposalResult, ProposalStatusUpdate,
)
from services.decision_engine import build_result_event, close_and_compute
from services.event_bus import event_bus
from utils.audit import append_audit

router = APIRouter()


def _auto_id() -> str:
    return f"P-{uuid.uuid4().hex[:8].upper()}"


async def _get_or_404(proposal_id: str, db: AsyncSession) -> Proposal:
    p = await db.get(Proposal, proposal_id)
    if not p:
        raise HTTPException(404, f"Proposal '{proposal_id}' not found.")
    return p


def _to_response(p: Proposal) -> ProposalResponse:
    result = None
    if p.status == ProposalStatus.CLOSED and p.result_computed_at:
        result = ProposalResult(
            proposal_id=p.id, status="closed",
            yes_weight=p.result_yes_weight, no_weight=p.result_no_weight,
            abstain_weight=p.result_abstain_weight, total_weight=p.result_total_weight,
            total_eligible_weight=None, participation_pct=None,
            quorum_met=p.result_quorum_met, passed=p.result_passed,
            computed_at=p.result_computed_at,
        )
    return ProposalResponse(
        id=p.id, entity_id=p.entity_id, title=p.title, description=p.description,
        proposal_type=p.proposal_type.value, status=p.status.value,
        threshold_type=p.threshold_type.value,
        custom_threshold_pct=p.custom_threshold_pct,
        quorum_pct=p.quorum_pct, opens_at=p.opens_at, closes_at=p.closes_at,
        created_by=p.created_by, created_at=p.created_at, result=result,
    )


@router.post("", response_model=ProposalResponse, status_code=201)
async def create_proposal(
    body: ProposalCreate,
    request: Request,
    creator_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    entity = await db.get(Entity, body.entity_id)
    if not entity:
        raise HTTPException(404, "Entity not found.")

    if creator_id:
        if not await db.get(User, creator_id):
            raise HTTPException(404, "Creator user not found.")
    else:
        creator_id = "00000000-0000-0000-0000-000000000000"

    try:
        proposal_type  = ProposalType(body.proposal_type)
        threshold_type = ThresholdType(body.threshold_type)
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    pid = body.id or _auto_id()
    if await db.get(Proposal, pid):
        raise HTTPException(409, f"Proposal ID '{pid}' already exists.")

    proposal = Proposal(
        id=pid, entity_id=body.entity_id, title=body.title,
        description=body.description, proposal_type=proposal_type,
        threshold_type=threshold_type,
        custom_threshold_pct=body.custom_threshold_pct,
        quorum_pct=body.quorum_pct,
        opens_at=body.opens_at or int(time.time()),
        closes_at=body.closes_at, created_by=creator_id,
        status=ProposalStatus.OPEN, metadata_=body.metadata,
    )
    db.add(proposal)
    await db.flush()

    await append_audit(db, AuditAction.PROPOSAL_CREATED,
                       payload={"proposal_id": pid, "title": body.title},
                       actor_id=creator_id, entity_id=body.entity_id,
                       proposal_id=pid,
                       ip_address=request.client.host if request.client else None)

    await event_bus.publish(GovernanceEvent(
        event="governance.proposal.created", entity=entity.name,
        proposal_id=pid, actor=creator_id,
        details={"title": proposal.title,
                 "proposal_type": proposal_type.value,
                 "threshold_type": threshold_type.value,
                 "closes_at": body.closes_at},
    ))
    return _to_response(proposal)


@router.get("", response_model=List[ProposalResponse])
async def list_proposals(
    entity_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Proposal)
    if entity_id:
        stmt = stmt.where(Proposal.entity_id == entity_id)
    if status:
        try:
            stmt = stmt.where(Proposal.status == ProposalStatus(status))
        except ValueError:
            raise HTTPException(422, f"Invalid status '{status}'.")
    result = await db.execute(
        stmt.offset(skip).limit(limit).order_by(Proposal.created_at.desc())
    )
    return [_to_response(p) for p in result.scalars().all()]


@router.get("/{proposal_id}", response_model=ProposalResponse)
async def get_proposal(proposal_id: str, db: AsyncSession = Depends(get_db)):
    return _to_response(await _get_or_404(proposal_id, db))


@router.patch("/{proposal_id}/status", response_model=ProposalResponse)
async def update_status(
    proposal_id: str, body: ProposalStatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    proposal = await _get_or_404(proposal_id, db)
    try:
        proposal.status = ProposalStatus(body.status)
    except ValueError:
        raise HTTPException(422, f"Invalid status '{body.status}'.")
    await db.flush()
    return _to_response(proposal)


@router.post("/{proposal_id}/close", response_model=ProposalResult)
async def force_close(
    proposal_id: str, request: Request,
    db: AsyncSession = Depends(get_db),
):
    proposal = await _get_or_404(proposal_id, db)
    if proposal.status == ProposalStatus.CLOSED:
        raise HTTPException(409, "Proposal is already closed.")
    if proposal.status == ProposalStatus.CANCELLED:
        raise HTTPException(409, "Proposal is cancelled and cannot be closed.")

    tally, result = await close_and_compute(db, proposal)
    await append_audit(db, AuditAction.RESULT_COMPUTED,
                       payload=result.model_dump(), proposal_id=proposal_id,
                       entity_id=proposal.entity_id,
                       ip_address=request.client.host if request.client else None)
    await event_bus.publish(build_result_event(proposal, tally))
    return result


@router.get("/{proposal_id}/result", response_model=ProposalResult)
async def get_result(proposal_id: str, db: AsyncSession = Depends(get_db)):
    proposal = await _get_or_404(proposal_id, db)
    if proposal.status != ProposalStatus.CLOSED:
        raise HTTPException(400,
            f"Results only available after close. Current: {proposal.status.value}")
    return ProposalResult(
        proposal_id=proposal.id, status="closed",
        yes_weight=proposal.result_yes_weight,
        no_weight=proposal.result_no_weight,
        abstain_weight=proposal.result_abstain_weight,
        total_weight=proposal.result_total_weight,
        total_eligible_weight=None, participation_pct=None,
        quorum_met=proposal.result_quorum_met, passed=proposal.result_passed,
        computed_at=proposal.result_computed_at,
    )
