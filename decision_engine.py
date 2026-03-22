"""
Electa Systems — Decision Engine
Computes weighted governance results, enforces quorum, and applies
threshold logic (simple majority, supermajority, unanimous, custom).
"""

import logging
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.db_models import (
    Ownership, Proposal, ProposalStatus, ThresholdType, Vote, VoteChoice,
)
from models.schemas import GovernanceEvent, ProposalResult

logger = logging.getLogger("electa.decision_engine")


@dataclass
class TallyResult:
    yes_weight: float
    no_weight: float
    abstain_weight: float
    total_cast_weight: float
    total_eligible_weight: float
    participation_pct: float
    quorum_met: bool
    passed: bool
    threshold_applied: str
    required_pct: float


async def get_total_eligible_weight(db: AsyncSession, entity_id: str) -> float:
    """Sum of (shares × role_multiplier) for all owners of an entity."""
    result = await db.execute(
        select(func.sum(Ownership.shares * Ownership.role_weight_multiplier))
        .where(Ownership.entity_id == entity_id)
    )
    return float(result.scalar_one_or_none() or 0.0)


async def get_voter_weight(db: AsyncSession, user_id: str, entity_id: str) -> float:
    """Effective voting weight for a user on a specific entity."""
    result = await db.execute(
        select((Ownership.shares * Ownership.role_weight_multiplier).label("weight"))
        .where(Ownership.user_id == user_id, Ownership.entity_id == entity_id)
    )
    row = result.first()
    return float(row.weight) if row else 0.0


async def tally_proposal(db: AsyncSession, proposal: Proposal) -> TallyResult:
    """Aggregate all cast votes and apply governance rules."""
    votes_result = await db.execute(
        select(Vote).where(Vote.proposal_id == proposal.id)
    )
    votes: List[Vote] = votes_result.scalars().all()

    yes_weight     = sum(v.weight for v in votes if v.choice == VoteChoice.YES)
    no_weight      = sum(v.weight for v in votes if v.choice == VoteChoice.NO)
    abstain_weight = sum(v.weight for v in votes if v.choice == VoteChoice.ABSTAIN)
    total_cast     = yes_weight + no_weight + abstain_weight

    total_eligible   = await get_total_eligible_weight(db, proposal.entity_id)
    participation_pct = (total_cast / total_eligible) if total_eligible > 0 else 0.0
    quorum_met        = participation_pct >= proposal.quorum_pct

    # Threshold — abstentions excluded from pass/fail denominator
    effective_denom = yes_weight + no_weight
    tt = proposal.threshold_type

    if tt == ThresholdType.SIMPLE_MAJORITY:
        required_pct    = settings.DEFAULT_MAJORITY_PCT
        threshold_label = "simple_majority (>50%)"
        passed = effective_denom > 0 and yes_weight / effective_denom > required_pct

    elif tt == ThresholdType.SUPERMAJORITY:
        required_pct    = settings.DEFAULT_SUPERMAJORITY_PCT
        threshold_label = "supermajority (≥66.67%)"
        passed = effective_denom > 0 and yes_weight / effective_denom >= required_pct

    elif tt == ThresholdType.UNANIMOUS:
        required_pct    = 1.0
        threshold_label = "unanimous (100%)"
        passed = no_weight == 0.0 and yes_weight > 0.0

    elif tt == ThresholdType.CUSTOM:
        required_pct    = proposal.custom_threshold_pct or settings.DEFAULT_MAJORITY_PCT
        threshold_label = f"custom ({required_pct:.1%})"
        passed = effective_denom > 0 and yes_weight / effective_denom >= required_pct

    else:
        required_pct    = settings.DEFAULT_MAJORITY_PCT
        threshold_label = "simple_majority (default)"
        passed = effective_denom > 0 and yes_weight / effective_denom > required_pct

    if not quorum_met:
        passed = False

    logger.info(
        "Tally [%s]: YES=%.0f NO=%.0f ABS=%.0f | eligible=%.0f | "
        "participation=%.1f%% | quorum=%s | passed=%s",
        proposal.id, yes_weight, no_weight, abstain_weight,
        total_eligible, participation_pct * 100, quorum_met, passed,
    )

    return TallyResult(
        yes_weight=yes_weight, no_weight=no_weight, abstain_weight=abstain_weight,
        total_cast_weight=total_cast, total_eligible_weight=total_eligible,
        participation_pct=participation_pct, quorum_met=quorum_met, passed=passed,
        threshold_applied=threshold_label, required_pct=required_pct,
    )


async def close_and_compute(
    db: AsyncSession, proposal: Proposal
) -> Tuple[TallyResult, ProposalResult]:
    """Close the proposal, persist the result, and return the tally."""
    tally = await tally_proposal(db, proposal)

    proposal.status                = ProposalStatus.CLOSED
    proposal.result_yes_weight     = tally.yes_weight
    proposal.result_no_weight      = tally.no_weight
    proposal.result_abstain_weight = tally.abstain_weight
    proposal.result_total_weight   = tally.total_cast_weight
    proposal.result_quorum_met     = tally.quorum_met
    proposal.result_passed         = tally.passed
    proposal.result_computed_at    = int(time.time())
    await db.flush()

    schema_result = ProposalResult(
        proposal_id=proposal.id, status="closed",
        yes_weight=tally.yes_weight, no_weight=tally.no_weight,
        abstain_weight=tally.abstain_weight, total_weight=tally.total_cast_weight,
        total_eligible_weight=tally.total_eligible_weight,
        participation_pct=round(tally.participation_pct, 4),
        quorum_met=tally.quorum_met, passed=tally.passed,
        computed_at=proposal.result_computed_at,
    )
    return tally, schema_result


def build_vote_event(
    proposal: Proposal, voter_id: str, voter_name: str,
    choice: Optional[str], weight: float, action_type: str,
) -> GovernanceEvent:
    event_type = {
        "vote":     "governance.vote.cast",
        "approve":  "governance.vote.cast",
        "reject":   "governance.vote.cast",
        "delegate": "governance.vote.delegated",
    }.get(action_type, "governance.vote.cast")

    return GovernanceEvent(
        event=event_type,
        entity=proposal.entity.name if proposal.entity else proposal.entity_id,
        proposal_id=proposal.id, actor=voter_name,
        vote=choice, weight=weight,
        details={"action_type": action_type, "proposal_title": proposal.title},
    )


def build_result_event(proposal: Proposal, tally: TallyResult) -> GovernanceEvent:
    return GovernanceEvent(
        event="governance.result.computed",
        entity=proposal.entity.name if proposal.entity else proposal.entity_id,
        proposal_id=proposal.id,
        details={
            "passed": tally.passed, "quorum_met": tally.quorum_met,
            "yes_weight": tally.yes_weight, "no_weight": tally.no_weight,
            "abstain_weight": tally.abstain_weight,
            "participation_pct": round(tally.participation_pct, 4),
            "threshold_applied": tally.threshold_applied,
        },
    )
