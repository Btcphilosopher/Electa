"""
Electa Systems — Pydantic Schemas
Request bodies, response models, and canonical event payloads.
"""

import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


# ── Users ─────────────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    role: str = Field("shareholder")
    external_id: Optional[str] = None
    public_key: Optional[str] = Field(
        None, description="PEM-encoded Ed25519 public key for signature verification"
    )
    metadata: Optional[Dict[str, Any]] = None


class UserResponse(BaseModel):
    id: str
    name: str
    role: str
    external_id: Optional[str]
    public_key: Optional[str]
    is_active: bool
    created_at: int
    api_key: Optional[str] = None  # surfaced once at creation only

    model_config = {"from_attributes": True}


# ── Entities ──────────────────────────────────────────────────────────────────

class EntityCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    ticker: Optional[str] = Field(None, max_length=16)
    lei: Optional[str] = Field(None, max_length=20)
    jurisdiction: Optional[str] = None
    total_shares: float = Field(..., gt=0)
    metadata: Optional[Dict[str, Any]] = None


class EntityResponse(BaseModel):
    id: str
    name: str
    ticker: Optional[str]
    lei: Optional[str]
    jurisdiction: Optional[str]
    total_shares: float
    is_active: bool
    created_at: int

    model_config = {"from_attributes": True}


class OwnershipSet(BaseModel):
    user_id: str
    entity_id: str
    shares: float = Field(..., ge=0)
    role_weight_multiplier: float = Field(1.0, ge=0.0)


class OwnershipResponse(BaseModel):
    id: str
    user_id: str
    entity_id: str
    shares: float
    role_weight_multiplier: float
    updated_at: int

    model_config = {"from_attributes": True}


# ── Proposals ─────────────────────────────────────────────────────────────────

class ProposalCreate(BaseModel):
    id: Optional[str] = Field(None, description="Human-readable ID, e.g. P-001.")
    entity_id: str
    title: str = Field(..., min_length=3, max_length=512)
    description: Optional[str] = None
    proposal_type: str = Field("resolution")
    threshold_type: str = Field("simple_majority")
    custom_threshold_pct: Optional[float] = Field(None, ge=0.0, le=1.0)
    quorum_pct: float = Field(0.51, ge=0.0, le=1.0)
    opens_at: Optional[int] = None
    closes_at: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_custom_threshold(self):
        if self.threshold_type == "custom" and self.custom_threshold_pct is None:
            raise ValueError(
                "custom_threshold_pct is required when threshold_type is 'custom'"
            )
        return self


class ProposalStatusUpdate(BaseModel):
    status: str = Field(..., description="open | closed | cancelled")


class ProposalResult(BaseModel):
    proposal_id: str
    status: str
    yes_weight: Optional[float]
    no_weight: Optional[float]
    abstain_weight: Optional[float]
    total_weight: Optional[float]
    total_eligible_weight: Optional[float]
    participation_pct: Optional[float]
    quorum_met: Optional[bool]
    passed: Optional[bool]
    computed_at: Optional[int]


class ProposalResponse(BaseModel):
    id: str
    entity_id: str
    title: str
    description: Optional[str]
    proposal_type: str
    status: str
    threshold_type: str
    custom_threshold_pct: Optional[float]
    quorum_pct: float
    opens_at: int
    closes_at: Optional[int]
    created_by: str
    created_at: int
    result: Optional[ProposalResult] = None

    model_config = {"from_attributes": True}


# ── Votes ─────────────────────────────────────────────────────────────────────

class VoteCast(BaseModel):
    proposal_id: str
    voter_id: str
    action_type: str = Field("vote", description="vote | approve | reject | delegate")
    choice: Optional[str] = Field(None, description="YES | NO | ABSTAIN")
    delegate_to_id: Optional[str] = Field(None, description="Target user ID for delegation")
    signature: Optional[str] = Field(None, description="Base64url Ed25519 signature")
    metadata: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_choice_and_delegation(self):
        if self.action_type == "delegate":
            if not self.delegate_to_id:
                raise ValueError("delegate_to_id is required for delegation actions")
        elif self.action_type in ("vote", "approve", "reject"):
            if self.choice is None:
                raise ValueError(f"choice is required for action_type='{self.action_type}'")
            if self.action_type == "approve":
                self.choice = "YES"
            elif self.action_type == "reject":
                self.choice = "NO"
        return self


class VoteResponse(BaseModel):
    id: str
    proposal_id: str
    voter_id: str
    action_type: str
    choice: Optional[str]
    weight: float
    delegated_from_id: Optional[str]
    signature_verified: Optional[bool]
    timestamp: int

    model_config = {"from_attributes": True}


# ── Events ────────────────────────────────────────────────────────────────────

class GovernanceEvent(BaseModel):
    """Canonical structured event emitted by the Electa event bus."""
    event: str = Field(..., description="e.g. governance.vote.cast")
    entity: str
    proposal_id: str
    actor: Optional[str] = None
    vote: Optional[str] = None
    weight: Optional[float] = None
    details: Optional[Dict[str, Any]] = None
    timestamp: int = Field(default_factory=lambda: int(time.time()))


# ── Webhooks ──────────────────────────────────────────────────────────────────

class WebhookCreate(BaseModel):
    owner_id: str
    url: str
    secret: Optional[str] = Field(None, description="Shared secret for HMAC-SHA256 signing")
    event_filter: Optional[str] = Field(
        None,
        description="Comma-separated glob patterns. E.g. 'governance.vote.*,governance.result.*'"
    )
    metadata: Optional[Dict[str, Any]] = None


class WebhookResponse(BaseModel):
    id: str
    owner_id: str
    url: str
    event_filter: Optional[str]
    is_active: bool
    created_at: int
    last_delivery_at: Optional[int]
    delivery_failures: int

    model_config = {"from_attributes": True}


# ── Audit ─────────────────────────────────────────────────────────────────────

class AuditLogEntry(BaseModel):
    id: str
    action: str
    actor_id: Optional[str]
    entity_id: Optional[str]
    proposal_id: Optional[str]
    vote_id: Optional[str]
    payload: Optional[Dict[str, Any]]
    timestamp: int

    model_config = {"from_attributes": True}


# ── Generic responses ─────────────────────────────────────────────────────────

class MessageResponse(BaseModel):
    message: str
    detail: Any = None
