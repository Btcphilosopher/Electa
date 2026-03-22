"""
Electa Systems — Proposal Scheduler
Background task that polls for proposals whose closes_at has elapsed
and auto-closes them via the decision engine.
"""

import asyncio
import logging
import time

from sqlalchemy import select

from database import AsyncSessionLocal
from models.db_models import AuditAction, Proposal, ProposalStatus
from services.decision_engine import build_result_event, close_and_compute
from services.event_bus import event_bus
from utils.audit import append_audit

logger = logging.getLogger("electa.scheduler")


class ProposalScheduler:
    def __init__(self, interval_seconds: float = 30.0):
        self.interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="proposal-scheduler")
        logger.info("ProposalScheduler started (interval=%.0fs).", self.interval)

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ProposalScheduler stopped.")

    async def _loop(self):
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                logger.error("Scheduler tick error: %s", exc, exc_info=True)
            await asyncio.sleep(self.interval)

    async def _tick(self):
        now = int(time.time())
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Proposal).where(
                    Proposal.status == ProposalStatus.OPEN,
                    Proposal.closes_at != None,     # noqa: E711
                    Proposal.closes_at <= now,
                )
            )
            expired: list[Proposal] = result.scalars().all()

        if not expired:
            return
        logger.info("Scheduler: %d proposal(s) to close.", len(expired))
        for p in expired:
            await self._close_proposal(p.id)

    async def _close_proposal(self, proposal_id: str):
        async with AsyncSessionLocal() as db:
            try:
                proposal = await db.get(Proposal, proposal_id)
                if proposal is None or proposal.status != ProposalStatus.OPEN:
                    return
                tally, schema_result = await close_and_compute(db, proposal)
                await append_audit(
                    db, AuditAction.RESULT_COMPUTED,
                    payload={**schema_result.model_dump(), "trigger": "scheduler"},
                    proposal_id=proposal_id, entity_id=proposal.entity_id,
                )
                await db.commit()
                await event_bus.publish(build_result_event(proposal, tally))
                logger.info("Scheduler closed %s: passed=%s", proposal_id, tally.passed)
            except Exception as exc:
                await db.rollback()
                logger.error("Scheduler failed on %s: %s", proposal_id, exc, exc_info=True)


proposal_scheduler = ProposalScheduler()
