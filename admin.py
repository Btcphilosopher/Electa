"""
Electa Systems — Admin Router
System-level operations: statistics, audit search, share cap validation,
overdue proposal detection, manual scheduler trigger, and participation reports.

In production, add an authentication dependency to each route to restrict
access to UserRole.ADMIN.
"""

import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import (
    AuditLog, Entity, Ownership, Proposal, ProposalStatus,
    User, Vote, WebhookEndpoint,
)
from models.schemas import AuditLogEntry
from services.event_bus import event_bus
from services.scheduler import proposal_scheduler

router = APIRouter()


# ── System statistics ─────────────────────────────────────────────────────────

@router.get("/stats", summary="High-level system statistics")
async def system_stats(db: AsyncSession = Depends(get_db)):
    """Aggregate counts for operations dashboards and integration health checks."""

    async def count(model):
        r = await db.execute(select(func.count()).select_from(model))
        return r.scalar_one()

    open_p = (await db.execute(
        select(func.count()).select_from(Proposal)
        .where(Proposal.status == ProposalStatus.OPEN)
    )).scalar_one()

    closed_p = (await db.execute(
        select(func.count()).select_from(Proposal)
        .where(Proposal.status == ProposalStatus.CLOSED)
    )).scalar_one()

    passed_p = (await db.execute(
        select(func.count()).select_from(Proposal)
        .where(Proposal.result_passed == True)  # noqa: E712
    )).scalar_one()

    total_weight = (await db.execute(
        select(func.sum(Vote.weight)).select_from(Vote)
    )).scalar_one()

    active_webhooks = (await db.execute(
        select(func.count()).select_from(WebhookEndpoint)
        .where(WebhookEndpoint.is_active == True)  # noqa: E712
    )).scalar_one()

    return {
        "timestamp": int(time.time()),
        "entities": await count(Entity),
        "users": await count(User),
        "proposals": {
            "open": open_p,
            "closed": closed_p,
            "passed": passed_p,
        },
        "votes_cast": await count(Vote),
        "total_weight_cast": float(total_weight or 0.0),
        "audit_log_entries": await count(AuditLog),
        "active_webhooks": active_webhooks,
        "event_bus_subscribers": event_bus.subscriber_count(),
    }


# ── Audit log search ──────────────────────────────────────────────────────────

@router.get("/audit", response_model=List[AuditLogEntry],
            summary="Search the global audit trail")
async def search_audit(
    action: Optional[str] = Query(None),
    actor_id: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    proposal_id: Optional[str] = Query(None),
    since: Optional[int] = Query(None, description="Unix timestamp lower bound"),
    until: Optional[int] = Query(None, description="Unix timestamp upper bound"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if proposal_id:
        stmt = stmt.where(AuditLog.proposal_id == proposal_id)
    if since:
        stmt = stmt.where(AuditLog.timestamp >= since)
    if until:
        stmt = stmt.where(AuditLog.timestamp <= until)
    stmt = stmt.order_by(AuditLog.timestamp.desc()).offset(skip).limit(limit)
    result = await db.execute(stmt)
    return [AuditLogEntry.model_validate(e) for e in result.scalars().all()]


# ── Share cap validation ──────────────────────────────────────────────────────

@router.get("/entities/{entity_id}/ownership/validate",
            summary="Validate total ownership does not exceed total_shares")
async def validate_ownership(entity_id: str, db: AsyncSession = Depends(get_db)):
    """
    Checks that the sum of all ownership records does not exceed the entity's
    declared total_shares. Used by compliance systems before proposal creation.
    """
    entity = await db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404, "Entity not found.")

    total_assigned = float((await db.execute(
        select(func.sum(Ownership.shares)).where(Ownership.entity_id == entity_id)
    )).scalar_one() or 0.0)

    over_cap = total_assigned > entity.total_shares
    return {
        "entity_id": entity_id,
        "entity_name": entity.name,
        "total_shares": entity.total_shares,
        "total_assigned": total_assigned,
        "unassigned": max(entity.total_shares - total_assigned, 0.0),
        "over_cap": over_cap,
        "pct_assigned": round(total_assigned / entity.total_shares * 100, 2)
            if entity.total_shares > 0 else 0.0,
        "valid": not over_cap,
    }


# ── Overdue proposals ─────────────────────────────────────────────────────────

@router.get("/proposals/overdue",
            summary="List open proposals past their closes_at deadline")
async def overdue_proposals(db: AsyncSession = Depends(get_db)):
    """Returns proposals the scheduler should have already closed."""
    now = int(time.time())
    result = await db.execute(
        select(Proposal).where(
            Proposal.status == ProposalStatus.OPEN,
            Proposal.closes_at != None,   # noqa: E711
            Proposal.closes_at < now,
        ).order_by(Proposal.closes_at.asc())
    )
    overdue = result.scalars().all()
    return {
        "count": len(overdue),
        "proposals": [
            {"id": p.id, "title": p.title, "entity_id": p.entity_id,
             "closes_at": p.closes_at,
             "overdue_by_seconds": now - p.closes_at}
            for p in overdue
        ],
    }


# ── Manual scheduler trigger ──────────────────────────────────────────────────

@router.post("/scheduler/run", summary="Manually trigger one scheduler tick")
async def trigger_scheduler():
    """Forces an immediate scan for overdue proposals."""
    await proposal_scheduler._tick()
    return {"message": "Scheduler tick complete."}


# ── Recent events ─────────────────────────────────────────────────────────────

@router.get("/events/recent", summary="Recent events from replay buffer")
async def admin_recent_events(
    limit: int = Query(100, ge=1, le=200),
    filter: Optional[str] = Query(None),
):
    events = event_bus.recent_events(limit=limit)
    if filter:
        events = [e for e in events if e.event.startswith(filter)]
    return {
        "count": len(events),
        "subscriber_count": event_bus.subscriber_count(),
        "events": [e.model_dump() for e in events],
    }


# ── Voter participation report ────────────────────────────────────────────────

@router.get("/proposals/{proposal_id}/participation",
            summary="Per-voter participation breakdown for a proposal")
async def participation_report(
    proposal_id: str, db: AsyncSession = Depends(get_db),
):
    """
    Full per-voter breakdown: name, role, shares, effective weight,
    voted/not-voted, choice. Suitable for post-vote compliance reporting.
    """
    proposal = await db.get(Proposal, proposal_id)
    if not proposal:
        raise HTTPException(404, "Proposal not found.")

    ownership_result = await db.execute(
        select(Ownership, User).join(User, Ownership.user_id == User.id)
        .where(Ownership.entity_id == proposal.entity_id)
    )
    owners = ownership_result.all()
    total_eligible = sum(
        float(o.shares * o.role_weight_multiplier) for o, _ in owners
    )

    votes_result = await db.execute(
        select(Vote).where(Vote.proposal_id == proposal_id)
    )
    votes = {v.voter_id: v for v in votes_result.scalars().all()}

    participation = []
    for ownership, user in owners:
        eff = float(ownership.shares * ownership.role_weight_multiplier)
        v = votes.get(user.id)
        participation.append({
            "user_id": user.id,
            "user_name": user.name,
            "role": user.role.value,
            "shares": ownership.shares,
            "effective_weight": eff,
            "pct_of_total": round(eff / total_eligible * 100, 2)
                if total_eligible > 0 else 0.0,
            "voted": v is not None,
            "action_type": v.action_type.value if v else None,
            "choice": v.choice.value if v and v.choice else None,
            "voted_weight": float(v.weight) if v else None,
        })

    total_voted_weight = sum(
        float(v.weight) for v in votes.values() if v.choice is not None
    )
    return {
        "proposal_id": proposal_id,
        "proposal_title": proposal.title,
        "proposal_status": proposal.status.value,
        "total_eligible_weight": total_eligible,
        "total_voted_weight": total_voted_weight,
        "participation_pct": round(total_voted_weight / total_eligible * 100, 2)
            if total_eligible > 0 else 0.0,
        "voter_count": sum(1 for p in participation if p["voted"]),
        "eligible_count": len(participation),
        "participants": sorted(participation,
                               key=lambda x: x["effective_weight"], reverse=True),
    }
