"""
Electa Systems — ORM Models
PostgreSQL schema covering all governance entities.
All timestamps are integer Unix epoch (UTC).
Audit logs are append-only; rows are never updated or deleted.
"""

import enum
import time
import uuid

from sqlalchemy import (
    BigInteger, Boolean, Column, Enum, Float, ForeignKey,
    Index, Integer, JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from database import Base

# JSON maps to Postgres JSON in production; to TEXT in SQLite for tests.
JSONB = JSON


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> int:
    return int(time.time())


# ── Enumerations ──────────────────────────────────────────────────────────────

class UserRole(str, enum.Enum):
    SHAREHOLDER = "shareholder"
    BOARD       = "board"
    REGULATOR   = "regulator"
    ADMIN       = "admin"


class ProposalStatus(str, enum.Enum):
    PENDING   = "pending"
    OPEN      = "open"
    CLOSED    = "closed"
    CANCELLED = "cancelled"


class ProposalType(str, enum.Enum):
    RESOLUTION     = "resolution"
    ELECTION       = "election"
    BYLAW_AMENDMENT = "bylaw_amendment"
    MERGER         = "merger"
    OTHER          = "other"


class ThresholdType(str, enum.Enum):
    SIMPLE_MAJORITY = "simple_majority"
    SUPERMAJORITY   = "supermajority"
    UNANIMOUS       = "unanimous"
    CUSTOM          = "custom"


class VoteChoice(str, enum.Enum):
    YES     = "YES"
    NO      = "NO"
    ABSTAIN = "ABSTAIN"


class ActionType(str, enum.Enum):
    VOTE     = "vote"
    APPROVE  = "approve"
    REJECT   = "reject"
    DELEGATE = "delegate"


class AuditAction(str, enum.Enum):
    PROPOSAL_CREATED  = "proposal.created"
    PROPOSAL_CLOSED   = "proposal.closed"
    PROPOSAL_CANCELLED = "proposal.cancelled"
    VOTE_CAST         = "vote.cast"
    VOTE_DELEGATED    = "vote.delegated"
    QUORUM_REACHED    = "quorum.reached"
    RESULT_COMPUTED   = "result.computed"
    WEBHOOK_DELIVERED = "webhook.delivered"
    WEBHOOK_FAILED    = "webhook.failed"


# ── Tables ────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    external_id  = Column(String(128), unique=True, nullable=True, index=True,
                          comment="Bloomberg UUID / Refinitiv PERMID / internal ref")
    name         = Column(String(256), nullable=False)
    role         = Column(Enum(UserRole), nullable=False, default=UserRole.SHAREHOLDER)
    public_key   = Column(Text, nullable=True, comment="PEM-encoded Ed25519 public key")
    api_key_hash = Column(String(128), nullable=True, index=True)
    is_active    = Column(Boolean, nullable=False, default=True)
    created_at   = Column(BigInteger, nullable=False, default=_now)
    metadata_    = Column("metadata", JSONB, nullable=True)

    ownerships    = relationship("Ownership", back_populates="user", lazy="selectin")
    votes         = relationship("Vote", foreign_keys="Vote.voter_id",
                                 back_populates="voter", lazy="noload")
    audit_entries = relationship("AuditLog", back_populates="actor", lazy="noload")


class Entity(Base):
    """A corporation, fund, or other legal entity subject to governance."""
    __tablename__ = "entities"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    ticker       = Column(String(16), unique=True, nullable=True, index=True)
    name         = Column(String(256), nullable=False)
    lei          = Column(String(20), unique=True, nullable=True,
                          comment="ISO 17442 Legal Entity Identifier")
    jurisdiction = Column(String(64), nullable=True)
    total_shares = Column(Float, nullable=False, default=0.0)
    is_active    = Column(Boolean, nullable=False, default=True)
    created_at   = Column(BigInteger, nullable=False, default=_now)
    metadata_    = Column("metadata", JSONB, nullable=True)

    proposals  = relationship("Proposal", back_populates="entity", lazy="noload")
    ownerships = relationship("Ownership", back_populates="entity", lazy="selectin")


class Ownership(Base):
    """Share ownership — maps users to entities with effective voting weight."""
    __tablename__ = "ownership"
    __table_args__ = (
        UniqueConstraint("user_id", "entity_id", name="uq_ownership_user_entity"),
        Index("ix_ownership_entity", "entity_id"),
    )

    id                   = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id              = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    entity_id            = Column(UUID(as_uuid=False), ForeignKey("entities.id"), nullable=False)
    shares               = Column(Float, nullable=False, default=0.0)
    role_weight_multiplier = Column(Float, nullable=False, default=1.0,
                                    comment="Board members may carry additional weight per bylaws")
    updated_at           = Column(BigInteger, nullable=False, default=_now)

    user   = relationship("User", back_populates="ownerships")
    entity = relationship("Entity", back_populates="ownerships")


class Proposal(Base):
    __tablename__ = "proposals"
    __table_args__ = (
        Index("ix_proposal_entity_status", "entity_id", "status"),
    )

    id                   = Column(String(32), primary_key=True,
                                  comment="Human-readable: P-001, P-002 …")
    entity_id            = Column(UUID(as_uuid=False), ForeignKey("entities.id"), nullable=False)
    title                = Column(String(512), nullable=False)
    description          = Column(Text, nullable=True)
    proposal_type        = Column(Enum(ProposalType), nullable=False,
                                  default=ProposalType.RESOLUTION)
    status               = Column(Enum(ProposalStatus), nullable=False,
                                  default=ProposalStatus.PENDING)
    threshold_type       = Column(Enum(ThresholdType), nullable=False,
                                  default=ThresholdType.SIMPLE_MAJORITY)
    custom_threshold_pct = Column(Float, nullable=True)
    quorum_pct           = Column(Float, nullable=False, default=0.51)
    opens_at             = Column(BigInteger, nullable=False, default=_now)
    closes_at            = Column(BigInteger, nullable=True)
    created_by           = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    created_at           = Column(BigInteger, nullable=False, default=_now)
    metadata_            = Column("metadata", JSONB, nullable=True)

    # Cached at close time
    result_yes_weight    = Column(Float, nullable=True)
    result_no_weight     = Column(Float, nullable=True)
    result_abstain_weight = Column(Float, nullable=True)
    result_total_weight  = Column(Float, nullable=True)
    result_quorum_met    = Column(Boolean, nullable=True)
    result_passed        = Column(Boolean, nullable=True)
    result_computed_at   = Column(BigInteger, nullable=True)

    entity = relationship("Entity", back_populates="proposals", lazy="selectin")
    votes  = relationship("Vote", back_populates="proposal", lazy="noload")


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (
        UniqueConstraint("proposal_id", "voter_id",
                         name="uq_vote_proposal_voter",
                         comment="One vote per voter per proposal"),
        Index("ix_vote_proposal", "proposal_id"),
        Index("ix_vote_voter",    "voter_id"),
    )

    id               = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    proposal_id      = Column(String(32), ForeignKey("proposals.id"), nullable=False)
    voter_id         = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    action_type      = Column(Enum(ActionType), nullable=False, default=ActionType.VOTE)
    choice           = Column(Enum(VoteChoice), nullable=True)
    weight           = Column(Float, nullable=False,
                              comment="Effective shares × role multiplier")
    delegated_from_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=True)
    signature        = Column(Text, nullable=True,
                              comment="Base64url Ed25519 signature of canonical payload")
    signature_verified = Column(Boolean, nullable=True)
    timestamp        = Column(BigInteger, nullable=False, default=_now)
    ip_address       = Column(String(45), nullable=True)
    metadata_        = Column("metadata", JSONB, nullable=True)

    proposal       = relationship("Proposal", back_populates="votes")
    voter          = relationship("User", foreign_keys=[voter_id], back_populates="votes")
    delegated_from = relationship("User", foreign_keys=[delegated_from_id])


class AuditLog(Base):
    """Append-only audit trail. Rows are never updated or deleted."""
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_proposal", "proposal_id"),
        Index("ix_audit_entity",   "entity_id"),
        Index("ix_audit_ts",       "timestamp"),
    )

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    action      = Column(Enum(AuditAction), nullable=False)
    actor_id    = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=True)
    entity_id   = Column(UUID(as_uuid=False), nullable=True)
    proposal_id = Column(String(32), nullable=True)
    vote_id     = Column(UUID(as_uuid=False), nullable=True)
    payload     = Column(JSONB, nullable=True)
    timestamp   = Column(BigInteger, nullable=False, default=_now)
    ip_address  = Column(String(45), nullable=True)

    actor = relationship("User", back_populates="audit_entries", lazy="noload")


class WebhookEndpoint(Base):
    """Registered downstream webhook consumers."""
    __tablename__ = "webhook_endpoints"

    id               = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    owner_id         = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    url              = Column(Text, nullable=False)
    secret           = Column(String(128), nullable=True,
                              comment="HMAC-SHA256 signing secret")
    event_filter     = Column(Text, nullable=True,
                              comment="Comma-separated glob patterns, e.g. 'governance.vote.*'")
    is_active        = Column(Boolean, nullable=False, default=True)
    created_at       = Column(BigInteger, nullable=False, default=_now)
    last_delivery_at = Column(BigInteger, nullable=True)
    delivery_failures = Column(Integer, nullable=False, default=0)
    metadata_        = Column("metadata", JSONB, nullable=True)

    deliveries = relationship("WebhookDelivery", back_populates="endpoint", lazy="noload")


class WebhookDelivery(Base):
    """Immutable delivery log for every webhook dispatch attempt."""
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_wh_delivery_endpoint", "endpoint_id"),
        Index("ix_wh_delivery_ts",       "attempted_at"),
    )

    id             = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    endpoint_id    = Column(UUID(as_uuid=False), ForeignKey("webhook_endpoints.id"), nullable=False)
    event_type     = Column(String(128), nullable=False)
    payload        = Column(JSONB, nullable=False)
    http_status    = Column(Integer, nullable=True)
    success        = Column(Boolean, nullable=False, default=False)
    attempt_number = Column(Integer, nullable=False, default=1)
    attempted_at   = Column(BigInteger, nullable=False, default=_now)
    error_message  = Column(Text, nullable=True)

    endpoint = relationship("WebhookEndpoint", back_populates="deliveries")
