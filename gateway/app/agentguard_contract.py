"""Shared AgentGuard contract constants and canonicalization helpers."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Optional

from pydantic import BaseModel

SCHEMA_VERSION = "1"

AGENTGUARD_ACTIONS = (
    "buyer.checkout.commit",
    "buyer.order.cancel",
    "buyer.return.submit",
    "buyer.remedy.accept",
    "seller.catalog.publish",
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
    "seller.price.change",
    "seller.inventory.commit",
    "seller.order.accept",
    "seller.order.reject",
    "seller.fulfilment.commit",
    "seller.remedy.promise",
    "seller.refund.issue",
]
Role = Literal["buyer", "seller"]


class PrincipalRef(BaseModel):
    schema_version: str = SCHEMA_VERSION
    principal_id: str
    role: Optional[Role] = None
    wallet_address: Optional[str] = None


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
