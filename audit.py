"""
Electa Systems — Audit Utilities
Append-only audit trail helpers. Rows are never updated or deleted.
"""

import time
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import AuditAction, AuditLog


async def append_audit(
    db: AsyncSession,
    action: AuditAction,
    payload: Optional[Dict[str, Any]] = None,
    actor_id: Optional[str] = None,
    entity_id: Optional[str] = None,
    proposal_id: Optional[str] = None,
    vote_id: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> AuditLog:
    """Insert an immutable audit log entry. Never modifies existing rows."""
    entry = AuditLog(
        action=action,
        actor_id=actor_id,
        entity_id=entity_id,
        proposal_id=proposal_id,
        vote_id=vote_id,
        payload=payload,
        timestamp=int(time.time()),
        ip_address=ip_address,
    )
    db.add(entry)
    await db.flush()
    return entry
