"""Shared AgentGuard contract constants and canonicalization helpers."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, field_validator

SCHEMA_VERSION = "1"
DECISION_SCHEMA_VERSION = "2"

AGENTGUARD_ACTIONS = (
    "buyer.checkout.commit",
    "buyer.order.cancel",
    "buyer.return.submit",
    "buyer.remedy.accept",
    "seller.catalog.publish",
    "seller.catalog.archive",
    "seller.price.change",
    "seller.inventory.commit",
    "seller.order.accept",
    "seller.order.reject",
    "seller.fulfilment.commit",
    "seller.remedy.promise",
    "seller.refund.issue",
)

LEGACY_ACTION_ALIASES = {
    "refund": "seller.refund.issue",
    "checkout": "buyer.checkout.commit",
}

DECISION_REASONS = (
    "within_policy",
    "approval_required_amount",
    "approval_required_counterparty",
    "approval_required_action",
    "agent_paused",
    "agent_revoked",
    "mandate_missing",
    "mandate_expired",
    "policy_version_stale",
    "action_not_allowed",
    "resource_out_of_scope",
    "counterparty_out_of_scope",
    "amount_exceeded",
    "quantity_exceeded",
    "frequency_exceeded",
    "aggregate_exceeded",
    "approval_expired",
    "approval_mismatch",
    "approval_consumed",
    "request_expired",
    "nonce_replayed",
    "principal_mismatch",
    "execution_unknown",
)

AgentGuardAction = Literal[
    "buyer.checkout.commit",
    "buyer.order.cancel",
    "buyer.return.submit",
    "buyer.remedy.accept",
    "seller.catalog.publish",
    "seller.catalog.archive",
    "seller.price.change",
    "seller.inventory.commit",
    "seller.order.accept",
    "seller.order.reject",
    "seller.fulfilment.commit",
    "seller.remedy.promise",
    "seller.refund.issue",
]
Role = Literal["buyer", "seller"]
DecisionValue = Literal["allow", "need_approval", "deny"]
RequiredAction = Literal["none", "review", "strong_authentication", "contact_support"]
RiskLevel = Literal["read_only", "low", "medium", "high", "critical"]


class PrincipalRef(BaseModel):
    schema_version: str = SCHEMA_VERSION
    principal_id: str
    role: Optional[Role] = None
    wallet_address: Optional[str] = None


class DecisionV2(BaseModel):
    """Additive live decision envelope shared by every AgentGuard client."""

    schema_version: Literal["2"] = DECISION_SCHEMA_VERSION
    decision_id: str
    policy_id: str
    decision: DecisionValue
    reason_code: str
    human_reason: str
    required_action: RequiredAction
    risk_level: RiskLevel
    policy_version: int
    expires_at: str
    request_hash: Optional[str] = None
    approval: Optional[dict[str, Any]] = None
    receipt: Optional[dict[str, Any]] = None

    @field_validator("reason_code")
    @classmethod
    def _known_reason(cls, value: str) -> str:
        if value not in DECISION_REASONS:
            raise ValueError("Unknown AgentGuard decision reason")
        return value


def normalize_action(action: str) -> Optional[str]:
    candidate = LEGACY_ACTION_ALIASES.get(action, action)
    return candidate if candidate in AGENTGUARD_ACTIONS else None


def principal_id_from_wallet(wallet_address: str) -> str:
    return f"wallet:{wallet_address}"


def principal_from_wallet(wallet_address: str, role: Optional[Role] = None) -> PrincipalRef:
    return PrincipalRef(
        principal_id=principal_id_from_wallet(wallet_address),
        role=role,
        wallet_address=wallet_address,
    )


def canonicalize(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
