"""AgentGuard control plane — file-backed agents, policies, approvals, receipts.

PII-free: no Aadhaar/PAN/UID fields. Approvals are one-time and consumed atomically.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from config import settings

STATE_FILE = "agentguard-state.json"

Decision = Literal["allow", "need_approval", "deny"]
AgentStatus = Literal["active", "paused"]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class AgentRecord(BaseModel):
    agent_id: str
    wallet_address: str
    name: str = "Store Operations Assistant"
    status: AgentStatus = "active"
    policy_id: Optional[str] = None
    created_at: str
    updated_at: str


class PolicyRecord(BaseModel):
    policy_id: str
    wallet_address: str
    agent_id: str
    template: str = "seller_ops_refund_v1"
    refund_auto_max_inr: int = 5000
    created_at: str


class ApprovalRecord(BaseModel):
    approval_id: str
    wallet_address: str
    agent_id: str
    policy_id: str
    action: str
    amount_inr: int
    resource_id: str
    status: Literal["issued", "consumed", "expired"] = "issued"
    created_at: str
    consumed_at: Optional[str] = None
    nonce: str


class ReceiptRecord(BaseModel):
    receipt_id: str
    wallet_address: str
    agent_id: str
    policy_id: str
    action: str
    amount_inr: int
    resource_id: str
    outcome: Literal["allowed", "approved", "denied", "paused"]
    approval_id: Optional[str] = None
    created_at: str


class AgentGuardState(BaseModel):
    version: int = 1
    agents: dict[str, AgentRecord] = Field(default_factory=dict)
    policies: dict[str, PolicyRecord] = Field(default_factory=dict)
    approvals: dict[str, ApprovalRecord] = Field(default_factory=dict)
    receipts: dict[str, ReceiptRecord] = Field(default_factory=dict)
    # wallet -> agent_id for the default seller ops agent
    wallet_agents: dict[str, str] = Field(default_factory=dict)


def _state_path() -> Path:
    return Path(settings.data_dir).expanduser() / STATE_FILE


def load_state() -> AgentGuardState:
    path = _state_path()
    if not path.is_file():
        return AgentGuardState()
    raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    return AgentGuardState.model_validate(raw)


def save_state(state: AgentGuardState) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)


def ensure_seller_ops_agent(wallet_address: str) -> tuple[AgentRecord, PolicyRecord]:
    """Idempotent: register Store Ops agent + refund template policy for wallet."""
    state = load_state()
    existing_id = state.wallet_agents.get(wallet_address)
    if existing_id and existing_id in state.agents:
        agent = state.agents[existing_id]
        policy = state.policies.get(agent.policy_id or "", None)
        if policy:
            return agent, policy

    now = _utcnow()
    agent_id = _new_id("agent")
    policy_id = _new_id("policy")
    agent = AgentRecord(
        agent_id=agent_id,
        wallet_address=wallet_address,
        name="Store Operations Assistant",
        status="active",
        policy_id=policy_id,
        created_at=now,
        updated_at=now,
    )
    policy = PolicyRecord(
        policy_id=policy_id,
        wallet_address=wallet_address,
        agent_id=agent_id,
        template="seller_ops_refund_v1",
        refund_auto_max_inr=5000,
        created_at=now,
    )
    state.agents[agent_id] = agent
    state.policies[policy_id] = policy
    state.wallet_agents[wallet_address] = agent_id
    save_state(state)
    return agent, policy


def get_agent(agent_id: str) -> Optional[AgentRecord]:
    return load_state().agents.get(agent_id)


def get_agent_for_wallet(wallet_address: str) -> Optional[AgentRecord]:
    state = load_state()
    aid = state.wallet_agents.get(wallet_address)
    return state.agents.get(aid) if aid else None


def get_policy(policy_id: str) -> Optional[PolicyRecord]:
    return load_state().policies.get(policy_id)


def pause_agent(agent_id: str) -> AgentRecord:
    state = load_state()
    agent = state.agents.get(agent_id)
    if not agent:
        raise KeyError(f"Unknown agent: {agent_id}")
    agent = agent.model_copy(update={"status": "paused", "updated_at": _utcnow()})
    state.agents[agent_id] = agent
    save_state(state)
    return agent


def resume_agent(agent_id: str) -> AgentRecord:
    state = load_state()
    agent = state.agents.get(agent_id)
    if not agent:
        raise KeyError(f"Unknown agent: {agent_id}")
    agent = agent.model_copy(update={"status": "active", "updated_at": _utcnow()})
    state.agents[agent_id] = agent
    save_state(state)
    return agent


def evaluate_action(
    *,
    wallet_address: str,
    action: str,
    amount_inr: int,
    resource_id: str,
) -> dict[str, Any]:
    """Evaluate refund (or similar) against policy. Fail closed when paused/missing."""
    agent, policy = ensure_seller_ops_agent(wallet_address)
    # reload after ensure
    agent = get_agent(agent.agent_id) or agent
    policy = get_policy(policy.policy_id) or policy

    if agent.status == "paused":
        receipt = _write_receipt(
            wallet_address=wallet_address,
            agent_id=agent.agent_id,
            policy_id=policy.policy_id,
            action=action,
            amount_inr=amount_inr,
            resource_id=resource_id,
            outcome="paused",
        )
        return {
            "decision": "deny",
            "reason": "Agent is paused.",
            "agent": agent.model_dump(),
            "policy": policy.model_dump(),
            "receipt": receipt.model_dump(),
            "approval": None,
        }

    if action == "checkout":
        # Buyer elevated checkout: amounts above INR 10_000 need one-time approval.
        checkout_auto_max = 10000
        if amount_inr <= checkout_auto_max:
            receipt = _write_receipt(
                wallet_address=wallet_address,
                agent_id=agent.agent_id,
                policy_id=policy.policy_id,
                action=action,
                amount_inr=amount_inr,
                resource_id=resource_id,
                outcome="allowed",
            )
            return {
                "decision": "allow",
                "reason": f"Within auto checkout limit INR {checkout_auto_max}.",
                "agent": agent.model_dump(),
                "policy": {
                    **policy.model_dump(),
                    "checkout_auto_max_inr": checkout_auto_max,
                },
                "receipt": receipt.model_dump(),
                "approval": None,
            }
        approval = _issue_approval(
            wallet_address=wallet_address,
            agent_id=agent.agent_id,
            policy_id=policy.policy_id,
            action=action,
            amount_inr=amount_inr,
            resource_id=resource_id,
        )
        return {
            "decision": "need_approval",
            "reason": (
                f"Checkout INR {amount_inr} exceeds auto limit INR {checkout_auto_max}."
            ),
            "agent": agent.model_dump(),
            "policy": {
                **policy.model_dump(),
                "checkout_auto_max_inr": checkout_auto_max,
            },
            "receipt": None,
            "approval": approval.model_dump(),
        }

    if action != "refund":
        return {
            "decision": "deny",
            "reason": f"Unsupported action: {action}",
            "agent": agent.model_dump(),
            "policy": policy.model_dump(),
            "receipt": None,
            "approval": None,
        }

    if amount_inr < 0:
        return {
            "decision": "deny",
            "reason": "Amount must be non-negative.",
            "agent": agent.model_dump(),
            "policy": policy.model_dump(),
            "receipt": None,
            "approval": None,
        }

    if amount_inr <= policy.refund_auto_max_inr:
        receipt = _write_receipt(
            wallet_address=wallet_address,
            agent_id=agent.agent_id,
            policy_id=policy.policy_id,
            action=action,
            amount_inr=amount_inr,
            resource_id=resource_id,
            outcome="allowed",
        )
        return {
            "decision": "allow",
            "reason": f"Within auto refund limit INR {policy.refund_auto_max_inr}.",
            "agent": agent.model_dump(),
            "policy": policy.model_dump(),
            "receipt": receipt.model_dump(),
            "approval": None,
        }

    approval = _issue_approval(
        wallet_address=wallet_address,
        agent_id=agent.agent_id,
        policy_id=policy.policy_id,
        action=action,
        amount_inr=amount_inr,
        resource_id=resource_id,
    )
    return {
        "decision": "need_approval",
        "reason": (
            f"Refund INR {amount_inr} exceeds auto limit INR {policy.refund_auto_max_inr}."
        ),
        "agent": agent.model_dump(),
        "policy": policy.model_dump(),
        "receipt": None,
        "approval": approval.model_dump(),
    }


def consume_approval(
    *,
    approval_id: str,
    wallet_address: str,
) -> dict[str, Any]:
    """Atomically consume a one-time approval. Replay → raise ConflictError."""
    state = load_state()
    approval = state.approvals.get(approval_id)
    if not approval:
        raise KeyError(f"Unknown approval: {approval_id}")
    if approval.wallet_address != wallet_address:
        raise PermissionError("Approval wallet mismatch.")
    if approval.status == "consumed":
        raise ConflictError("Approval already consumed (replay rejected).")
    if approval.status != "issued":
        raise ConflictError(f"Approval not consumable: {approval.status}")

    now = _utcnow()
    approval = approval.model_copy(update={"status": "consumed", "consumed_at": now})
    state.approvals[approval_id] = approval
    save_state(state)

    receipt = _write_receipt(
        wallet_address=wallet_address,
        agent_id=approval.agent_id,
        policy_id=approval.policy_id,
        action=approval.action,
        amount_inr=approval.amount_inr,
        resource_id=approval.resource_id,
        outcome="approved",
        approval_id=approval_id,
    )
    return {
        "approval": approval.model_dump(),
        "receipt": receipt.model_dump(),
    }


def get_receipt(receipt_id: str) -> Optional[ReceiptRecord]:
    return load_state().receipts.get(receipt_id)


def list_receipts_for_wallet(wallet_address: str) -> list[ReceiptRecord]:
    state = load_state()
    rows = [r for r in state.receipts.values() if r.wallet_address == wallet_address]
    return sorted(rows, key=lambda r: r.created_at, reverse=True)


class ConflictError(Exception):
    """HTTP 409 semantics for replay/consume conflicts."""


def _issue_approval(
    *,
    wallet_address: str,
    agent_id: str,
    policy_id: str,
    action: str,
    amount_inr: int,
    resource_id: str,
) -> ApprovalRecord:
    state = load_state()
    approval = ApprovalRecord(
        approval_id=_new_id("appr"),
        wallet_address=wallet_address,
        agent_id=agent_id,
        policy_id=policy_id,
        action=action,
        amount_inr=amount_inr,
        resource_id=resource_id,
        status="issued",
        created_at=_utcnow(),
        nonce=uuid.uuid4().hex,
    )
    state.approvals[approval.approval_id] = approval
    save_state(state)
    return approval


def _write_receipt(
    *,
    wallet_address: str,
    agent_id: str,
    policy_id: str,
    action: str,
    amount_inr: int,
    resource_id: str,
    outcome: Literal["allowed", "approved", "denied", "paused"],
    approval_id: Optional[str] = None,
) -> ReceiptRecord:
    state = load_state()
    receipt = ReceiptRecord(
        receipt_id=_new_id("rcpt"),
        wallet_address=wallet_address,
        agent_id=agent_id,
        policy_id=policy_id,
        action=action,
        amount_inr=amount_inr,
        resource_id=resource_id,
        outcome=outcome,
        approval_id=approval_id,
        created_at=_utcnow(),
    )
    state.receipts[receipt.receipt_id] = receipt
    save_state(state)
    return receipt
