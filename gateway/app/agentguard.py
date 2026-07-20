"""AgentGuard control plane: principals, mandates, approvals, executors, receipts."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

from app.agentguard_contract import (
    AGENTGUARD_ACTIONS,
    SCHEMA_VERSION,
    PrincipalRef,
    canonicalize,
    normalize_action,
    principal_from_wallet,
    principal_id_from_wallet,
    sha256_hex,
)
from app.receipt_signing import sign_receipt, verify_receipt
from config import settings

STATE_FILE = "agentguard-state.json"
APPROVAL_TTL_MINUTES = 15
_STATE_LOCK = RLock()

Decision = Literal["allow", "need_approval", "deny"]
AgentStatus = Literal["active", "paused", "revoked"]
Role = Literal["buyer", "seller"]
MandateStatus = Literal["draft", "active", "revoked", "expired"]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _principal_role_key(principal_id: str, role: Role) -> str:
    return f"{principal_id}:{role}"


class AgentRecord(BaseModel):
    agent_id: str
    principal_id: str = ""
    wallet_address: Optional[str] = None
    role: Role = "seller"
    name: str = "Store Operations Assistant"
    status: AgentStatus = "active"
    mandate_id: Optional[str] = None
    policy_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION
    created_at: str
    updated_at: str


class PolicyRecord(BaseModel):
    policy_id: str
    principal_id: str = ""
    wallet_address: Optional[str] = None
    agent_id: str
    mandate_id: str = ""
    template: str = "seller_ops_v1"
    version: int = 1
    refund_auto_max_inr: int = 5000
    checkout_auto_max_inr: int = 10000
    allowed_actions: list[str] = Field(default_factory=list)
    status: MandateStatus = "active"
    created_at: str


class MandateRecord(BaseModel):
    mandate_id: str
    principal_id: str
    wallet_address: Optional[str] = None
    agent_id: str
    role: Role
    template: str
    status: MandateStatus = "draft"
    version: int = 1
    allowed_actions: list[str]
    limits: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    confirmed_at: Optional[str] = None
    expires_at: Optional[str] = None


class ApprovalRecord(BaseModel):
    approval_id: str
    request_hash: str = ""
    principal_id: str = ""
    wallet_address: Optional[str] = None
    agent_id: str
    action: str
    amount_inr: int
    resource_id: str
    mandate_id: str = ""
    mandate_version: int = 1
    policy_version: int = 1
    nonce: str
    expires_at: str = "9999-12-31T23:59:59+00:00"
    status: Literal["issued", "consumed", "expired"] = "issued"
    created_at: str
    consumed_at: Optional[str] = None


class ReceiptRecord(BaseModel):
    receipt_id: str
    schema_version: str = SCHEMA_VERSION
    principal_id: str = ""
    wallet_address: Optional[str] = None
    agent_id: str
    policy_id: Optional[str] = None
    mandate_id: Optional[str] = None
    mandate_version: Optional[int] = None
    action: str
    amount_inr: int
    resource_id: str
    outcome: Literal["allowed", "approved", "denied", "paused", "executed"]
    reason_code: Optional[str] = None
    approval_id: Optional[str] = None
    request_hash: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    created_at: str
    issuer_key_id: Optional[str] = None
    signature: Optional[str] = None


class AgentGuardState(BaseModel):
    schema_version: str = SCHEMA_VERSION
    version: int = 1
    agents: dict[str, AgentRecord] = Field(default_factory=dict)
    policies: dict[str, PolicyRecord] = Field(default_factory=dict)
    mandates: dict[str, MandateRecord] = Field(default_factory=dict)
    mandate_history: list[dict[str, Any]] = Field(default_factory=list)
    approvals: dict[str, ApprovalRecord] = Field(default_factory=dict)
    receipts: dict[str, ReceiptRecord] = Field(default_factory=dict)
    wallet_agents: dict[str, str] = Field(default_factory=dict)
    principal_agents: dict[str, str] = Field(default_factory=dict)
    consumed_nonces: dict[str, str] = Field(default_factory=dict)


class ConflictError(Exception):
    """HTTP 409 semantics for replay/consume conflicts."""


class ExecutionError(Exception):
    """Raised when a protected action has no registered executor."""


Executor = Callable[[dict[str, Any]], dict[str, Any]]
_EXECUTORS: dict[str, Executor] = {}


def _state_path() -> Path:
    return Path(settings.data_dir).expanduser() / STATE_FILE


def _template_for_role(role: Role) -> str:
    return "buyer_shop_v1" if role == "buyer" else "seller_ops_v1"


def _role_for_action(action: str) -> Role:
    return "buyer" if action.startswith("buyer.") else "seller"


def _template_defaults(template: str, role: Role) -> tuple[list[str], dict[str, Any]]:
    if template == "buyer_shop_v1":
        actions = [action for action in AGENTGUARD_ACTIONS if action.startswith("buyer.")]
        return actions, {"auto_approve_max_inr": {"buyer.checkout.commit": 10000}}
    actions = [action for action in AGENTGUARD_ACTIONS if action.startswith("seller.")]
    return actions, {"auto_approve_max_inr": {"seller.refund.issue": 5000}}


def _normalize_compile_limits(
    role: Role,
    limits: Optional[dict[str, Any]],
    default_limits: dict[str, Any],
) -> dict[str, Any]:
    """Merge client limits; accept nested auto_approve_max_inr or flat refund/checkout keys."""
    merged: dict[str, Any] = {**default_limits, **(limits or {})}
    auto = dict(merged.get("auto_approve_max_inr") or default_limits.get("auto_approve_max_inr") or {})
    if "refund_auto_max_inr" in merged:
        auto["seller.refund.issue"] = int(merged.pop("refund_auto_max_inr"))
    if "checkout_auto_max_inr" in merged:
        auto["buyer.checkout.commit"] = int(merged.pop("checkout_auto_max_inr"))
    if role == "seller" and "seller.refund.issue" not in auto:
        auto["seller.refund.issue"] = int(
            (default_limits.get("auto_approve_max_inr") or {}).get("seller.refund.issue", 5000)
        )
    if role == "buyer" and "buyer.checkout.commit" not in auto:
        auto["buyer.checkout.commit"] = int(
            (default_limits.get("auto_approve_max_inr") or {}).get("buyer.checkout.commit", 10000)
        )
    merged["auto_approve_max_inr"] = auto
    return merged


def _filter_allowed_actions(role: Role, allowed_actions: Optional[list[str]]) -> list[str]:
    defaults, _ = _template_defaults(_template_for_role(role), role)
    if not allowed_actions:
        return defaults
    prefix = "buyer." if role == "buyer" else "seller."
    filtered = [a for a in allowed_actions if a in AGENTGUARD_ACTIONS and a.startswith(prefix)]
    return filtered or defaults

def _state_dict(state: AgentGuardState) -> dict[str, Any]:
    return state.model_dump(mode="json")


def load_state() -> AgentGuardState:
    path = _state_path()
    if not path.is_file():
        return AgentGuardState()
    raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    raw.setdefault("schema_version", SCHEMA_VERSION)
    raw.setdefault("mandates", {})
    raw.setdefault("mandate_history", [])
    raw.setdefault("principal_agents", {})
    raw.setdefault("consumed_nonces", {})
    state = AgentGuardState.model_validate(raw)
    changed = False
    for agent_id, agent in list(state.agents.items()):
        if not agent.principal_id and agent.wallet_address:
            state.agents[agent_id] = agent.model_copy(
                update={"principal_id": principal_id_from_wallet(agent.wallet_address)}
            )
            changed = True
    for policy_id, policy in list(state.policies.items()):
        if not policy.principal_id and policy.wallet_address:
            state.policies[policy_id] = policy.model_copy(
                update={"principal_id": principal_id_from_wallet(policy.wallet_address)}
            )
            changed = True
    for approval_id, approval in list(state.approvals.items()):
        if not approval.principal_id and approval.wallet_address:
            state.approvals[approval_id] = approval.model_copy(
                update={"principal_id": principal_id_from_wallet(approval.wallet_address)}
            )
            changed = True
    for receipt_id, receipt in list(state.receipts.items()):
        if not receipt.principal_id and receipt.wallet_address:
            state.receipts[receipt_id] = receipt.model_copy(
                update={"principal_id": principal_id_from_wallet(receipt.wallet_address)}
            )
            changed = True
    if changed:
        save_state(state)
    return state


def save_state(state: AgentGuardState) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_state_dict(state), indent=2), encoding="utf-8")
    tmp.replace(path)


def ensure_agent(
    *,
    principal_id: str,
    role: Role,
    wallet_address: Optional[str] = None,
    name: Optional[str] = None,
) -> tuple[AgentRecord, MandateRecord, PolicyRecord]:
    state = load_state()
    key = _principal_role_key(principal_id, role)
    existing_id = state.principal_agents.get(key)
    if existing_id and existing_id in state.agents:
        agent = state.agents[existing_id]
        mandate = state.mandates.get(agent.mandate_id or "")
        policy = state.policies.get(agent.policy_id or "")
        if mandate and policy:
            return agent, mandate, policy

    now = _utcnow()
    agent_id = _new_id("agent")
    mandate_id = _new_id("mandate")
    policy_id = _new_id("policy")
    template = _template_for_role(role)
    actions, limits = _template_defaults(template, role)
    agent = AgentRecord(
        agent_id=agent_id,
        principal_id=principal_id,
        wallet_address=wallet_address,
        role=role,
        name=name or ("Shopping Assistant" if role == "buyer" else "Store Operations Assistant"),
        status="active",
        mandate_id=mandate_id,
        policy_id=policy_id,
        created_at=now,
        updated_at=now,
    )
    mandate = MandateRecord(
        mandate_id=mandate_id,
        principal_id=principal_id,
        wallet_address=wallet_address,
        agent_id=agent_id,
        role=role,
        template=template,
        status="active",
        version=1,
        allowed_actions=actions,
        limits=limits,
        created_at=now,
        confirmed_at=now,
    )
    policy = _policy_from_mandate(policy_id, mandate)
    state.agents[agent_id] = agent
    state.mandates[mandate_id] = mandate
    state.policies[policy_id] = policy
    state.principal_agents[key] = agent_id
    if wallet_address and role == "seller":
        state.wallet_agents[wallet_address] = agent_id
    state.mandate_history.append(
        {"event": "created_default", "mandate_id": mandate_id, "principal_id": principal_id, "at": now}
    )
    save_state(state)
    return agent, mandate, policy


def ensure_seller_ops_agent(wallet_address: str) -> tuple[AgentRecord, PolicyRecord]:
    agent, _mandate, policy = ensure_agent(
        principal_id=principal_id_from_wallet(wallet_address),
        role="seller",
        wallet_address=wallet_address,
    )
    return agent, policy


def _policy_from_mandate(policy_id: str, mandate: MandateRecord) -> PolicyRecord:
    auto_limits = mandate.limits.get("auto_approve_max_inr", {})
    return PolicyRecord(
        policy_id=policy_id,
        principal_id=mandate.principal_id,
        wallet_address=mandate.wallet_address,
        agent_id=mandate.agent_id,
        mandate_id=mandate.mandate_id,
        template=mandate.template,
        version=mandate.version,
        refund_auto_max_inr=int(auto_limits.get("seller.refund.issue", 5000)),
        checkout_auto_max_inr=int(auto_limits.get("buyer.checkout.commit", 10000)),
        allowed_actions=mandate.allowed_actions,
        status=mandate.status,
        created_at=mandate.created_at,
    )


def get_agent(agent_id: str) -> Optional[AgentRecord]:
    return load_state().agents.get(agent_id)


def get_agent_for_wallet(wallet_address: str, role: Role = "seller") -> Optional[AgentRecord]:
    state = load_state()
    principal_id = principal_id_from_wallet(wallet_address)
    aid = state.principal_agents.get(_principal_role_key(principal_id, role))
    if not aid and role == "seller":
        aid = state.wallet_agents.get(wallet_address)
    return state.agents.get(aid) if aid else None


def get_current_agent(principal_id: str, role: Role) -> Optional[AgentRecord]:
    state = load_state()
    aid = state.principal_agents.get(_principal_role_key(principal_id, role))
    return state.agents.get(aid) if aid else None


def get_policy(policy_id: str) -> Optional[PolicyRecord]:
    return load_state().policies.get(policy_id)


def compile_mandate(
    *,
    template: str,
    role: Role,
    limits: Optional[dict[str, Any]] = None,
    allowed_actions: Optional[list[str]] = None,
    principal_id: str,
    wallet_address: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> MandateRecord:
    state = load_state()
    agent = state.agents.get(agent_id or "")
    if not agent:
        agent, _mandate, _policy = ensure_agent(
            principal_id=principal_id,
            role=role,
            wallet_address=wallet_address,
        )
        state = load_state()

    _actions, default_limits = _template_defaults(template, role)
    actions = _filter_allowed_actions(role, allowed_actions)
    merged_limits = _normalize_compile_limits(role, limits, default_limits)
    mandate = MandateRecord(
        mandate_id=_new_id("mandate"),
        principal_id=principal_id,
        wallet_address=wallet_address or agent.wallet_address,
        agent_id=agent.agent_id,
        role=role,
        template=template,
        status="draft",
        version=(len([m for m in state.mandates.values() if m.agent_id == agent.agent_id]) + 1),
        allowed_actions=actions,
        limits=merged_limits,
        created_at=_utcnow(),
    )
    state.mandates[mandate.mandate_id] = mandate
    state.mandate_history.append(
        {"event": "compiled", "mandate_id": mandate.mandate_id, "principal_id": principal_id, "at": mandate.created_at}
    )
    save_state(state)
    return mandate


def confirm_mandate(mandate_id: str, principal_id: str) -> MandateRecord:
    with _STATE_LOCK:
        return _confirm_mandate_locked(mandate_id, principal_id)


def _confirm_mandate_locked(mandate_id: str, principal_id: str) -> MandateRecord:
    state = load_state()
    mandate = state.mandates.get(mandate_id)
    if not mandate:
        raise KeyError(f"Unknown mandate: {mandate_id}")
    if mandate.principal_id != principal_id:
        raise PermissionError("Mandate principal mismatch.")
    now = _utcnow()
    mandate = mandate.model_copy(update={"status": "active", "confirmed_at": now})
    agent = state.agents.get(mandate.agent_id)
    if not agent:
        raise KeyError(f"Unknown agent: {mandate.agent_id}")
    policy_id = agent.policy_id or _new_id("policy")
    agent = agent.model_copy(update={"mandate_id": mandate_id, "policy_id": policy_id, "updated_at": now})
    state.agents[agent.agent_id] = agent
    state.mandates[mandate_id] = mandate
    state.policies[policy_id] = _policy_from_mandate(policy_id, mandate)
    _expire_pending_approvals(state, agent.agent_id)
    state.mandate_history.append(
        {"event": "confirmed", "mandate_id": mandate_id, "principal_id": principal_id, "at": now}
    )
    save_state(state)
    return mandate


def get_mandate(mandate_id: str) -> Optional[MandateRecord]:
    return load_state().mandates.get(mandate_id)


def pause_agent(agent_id: str) -> AgentRecord:
    return _set_agent_status(agent_id, "paused")


def resume_agent(agent_id: str) -> AgentRecord:
    return _set_agent_status(agent_id, "active")


def revoke_agent(agent_id: str) -> AgentRecord:
    with _STATE_LOCK:
        return _revoke_agent_locked(agent_id)


def _revoke_agent_locked(agent_id: str) -> AgentRecord:
    state = load_state()
    agent = state.agents.get(agent_id)
    if not agent:
        raise KeyError(f"Unknown agent: {agent_id}")
    now = _utcnow()
    agent = agent.model_copy(update={"status": "revoked", "updated_at": now})
    state.agents[agent_id] = agent
    if agent.mandate_id and agent.mandate_id in state.mandates:
        state.mandates[agent.mandate_id] = state.mandates[agent.mandate_id].model_copy(
            update={"status": "revoked"}
        )
    _expire_pending_approvals(state, agent_id)
    save_state(state)
    return agent


def _expire_pending_approvals(state: AgentGuardState, agent_id: str) -> None:
    for approval_id, approval in list(state.approvals.items()):
        if approval.agent_id == agent_id and approval.status == "issued":
            state.approvals[approval_id] = approval.model_copy(update={"status": "expired"})


def _set_agent_status(agent_id: str, status: AgentStatus) -> AgentRecord:
    with _STATE_LOCK:
        state = load_state()
        agent = state.agents.get(agent_id)
        if not agent:
            raise KeyError(f"Unknown agent: {agent_id}")
        agent = agent.model_copy(update={"status": status, "updated_at": _utcnow()})
        state.agents[agent_id] = agent
        if status != "active":
            _expire_pending_approvals(state, agent_id)
        save_state(state)
        return agent


def evaluate_action(
    *,
    action: str,
    amount_inr: int,
    resource_id: str,
    wallet_address: Optional[str] = None,
    principal_id: Optional[str] = None,
    role: Optional[Role] = None,
    agent_id: Optional[str] = None,
    counterparty_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    write_receipt: bool = True,
) -> dict[str, Any]:
    normalized = normalize_action(action)
    if normalized is None:
        return {
            "decision": "deny",
            "reason": f"Unsupported action: {action}",
            "reason_code": "action_not_allowed",
            "agent": None,
            "policy": None,
            "mandate": None,
            "receipt": None,
            "approval": None,
        }
    role = role or _role_for_action(normalized)
    principal_id = principal_id or (principal_id_from_wallet(wallet_address) if wallet_address else None)
    if not principal_id:
        return _deny_without_agent(normalized, amount_inr, resource_id, "principal_mismatch")

    agent, mandate, policy = _resolve_agent_mandate_policy(
        principal_id=principal_id,
        wallet_address=wallet_address,
        role=role,
        agent_id=agent_id,
    )
    reason_code = _authorization_failure(agent, mandate, normalized, amount_inr)
    request = _action_request(
        principal_id=principal_id,
        wallet_address=wallet_address or agent.wallet_address,
        role=role,
        agent_id=agent.agent_id,
        action=normalized,
        amount_inr=amount_inr,
        resource_id=resource_id,
        counterparty_id=counterparty_id,
        payload=payload,
    )
    request_hash = sha256_hex(canonicalize(request))

    if reason_code in {"agent_paused", "agent_revoked"}:
        receipt = (
            _write_receipt(
                principal_id=principal_id,
                wallet_address=wallet_address or agent.wallet_address,
                agent_id=agent.agent_id,
                policy_id=policy.policy_id,
                mandate_id=mandate.mandate_id,
                mandate_version=mandate.version,
                action=normalized,
                amount_inr=amount_inr,
                resource_id=resource_id,
                outcome="paused" if reason_code == "agent_paused" else "denied",
                reason_code=reason_code,
                request_hash=request_hash,
            )
            if write_receipt
            else None
        )
        return _decision("deny", reason_code, agent, mandate, policy, receipt, None, request_hash)
    if reason_code:
        return _decision("deny", reason_code, agent, mandate, policy, None, None, request_hash)

    auto_max = int(mandate.limits.get("auto_approve_max_inr", {}).get(normalized, 0))
    if amount_inr <= auto_max:
        receipt = (
            _write_receipt(
                principal_id=principal_id,
                wallet_address=wallet_address or agent.wallet_address,
                agent_id=agent.agent_id,
                policy_id=policy.policy_id,
                mandate_id=mandate.mandate_id,
                mandate_version=mandate.version,
                action=normalized,
                amount_inr=amount_inr,
                resource_id=resource_id,
                outcome="allowed",
                reason_code="within_policy",
                request_hash=request_hash,
            )
            if write_receipt
            else None
        )
        return _decision("allow", "within_policy", agent, mandate, policy, receipt, None, request_hash)

    approval = _issue_approval(
        principal_id=principal_id,
        wallet_address=wallet_address or agent.wallet_address,
        agent_id=agent.agent_id,
        action=normalized,
        amount_inr=amount_inr,
        resource_id=resource_id,
        mandate=mandate,
        counterparty_id=counterparty_id,
        payload=payload,
    )
    return _decision(
        "need_approval",
        "approval_required_amount",
        agent,
        mandate,
        policy,
        None,
        approval,
        approval.request_hash,
    )


def _authorization_failure(
    agent: AgentRecord,
    mandate: MandateRecord,
    action: str,
    amount_inr: int,
) -> Optional[str]:
    if agent.status == "paused":
        return "agent_paused"
    if agent.status == "revoked":
        return "agent_revoked"
    if mandate.status != "active":
        return "mandate_missing"
    if mandate.expires_at and _parse_dt(mandate.expires_at) < datetime.now(timezone.utc):
        return "mandate_expired"
    if action not in mandate.allowed_actions:
        return "action_not_allowed"
    if amount_inr < 0:
        return "amount_exceeded"
    return None


def _resolve_agent_mandate_policy(
    *,
    principal_id: str,
    wallet_address: Optional[str],
    role: Role,
    agent_id: Optional[str],
) -> tuple[AgentRecord, MandateRecord, PolicyRecord]:
    if agent_id:
        state = load_state()
        agent = state.agents.get(agent_id)
        if not agent:
            raise KeyError(f"Unknown agent: {agent_id}")
        if agent.principal_id != principal_id:
            raise PermissionError("Agent principal mismatch.")
        mandate = state.mandates.get(agent.mandate_id or "")
        policy = state.policies.get(agent.policy_id or "")
        if not mandate or not policy:
            raise KeyError("Agent mandate missing.")
        return agent, mandate, policy
    return ensure_agent(principal_id=principal_id, role=role, wallet_address=wallet_address)


def _deny_without_agent(action: str, amount_inr: int, resource_id: str, reason_code: str) -> dict[str, Any]:
    return {
        "decision": "deny",
        "reason": reason_code,
        "reason_code": reason_code,
        "agent": None,
        "policy": None,
        "mandate": None,
        "receipt": None,
        "approval": None,
        "request_hash": None,
    }


def _decision(
    decision: Decision,
    reason_code: str,
    agent: AgentRecord,
    mandate: MandateRecord,
    policy: PolicyRecord,
    receipt: Optional[ReceiptRecord],
    approval: Optional[ApprovalRecord],
    request_hash: Optional[str],
) -> dict[str, Any]:
    return {
        "decision": decision,
        "reason": _reason_message(decision, reason_code, approval, policy),
        "reason_code": reason_code,
        "agent": agent.model_dump(),
        "policy": policy.model_dump(),
        "mandate": mandate.model_dump(),
        "receipt": receipt.model_dump() if receipt else None,
        "approval": approval.model_dump() if approval else None,
        "request_hash": request_hash,
    }


def _reason_message(
    decision: Decision,
    reason_code: str,
    approval: Optional[ApprovalRecord],
    policy: PolicyRecord,
) -> str:
    if reason_code == "within_policy":
        return "Within policy."
    if reason_code == "approval_required_amount" and approval:
        limit = (
            policy.checkout_auto_max_inr
            if approval.action == "buyer.checkout.commit"
            else policy.refund_auto_max_inr
        )
        return f"Amount INR {approval.amount_inr} exceeds auto limit INR {limit}."
    if reason_code == "agent_paused":
        return "Agent is paused."
    if reason_code == "agent_revoked":
        return "Agent is revoked."
    if decision == "deny":
        return reason_code
    return "Evaluated."


def _action_request(
    *,
    principal_id: str,
    wallet_address: Optional[str],
    role: Role,
    agent_id: str,
    action: str,
    amount_inr: int,
    resource_id: str,
    nonce: Optional[str] = None,
    expires_at: Optional[str] = None,
    counterparty_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    principal = PrincipalRef(
        principal_id=principal_id,
        role=role,
        wallet_address=wallet_address,
    ).model_dump(exclude_none=True)
    request: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "principal": principal,
        "agent_id": agent_id,
        "action": action,
        "resource_id": resource_id,
        "amount_inr": amount_inr,
    }
    if nonce:
        request["nonce"] = nonce
    if expires_at:
        request["expires_at"] = expires_at
    if counterparty_id:
        request["counterparty_id"] = counterparty_id
    if payload:
        request["payload"] = payload
    return request


def _issue_approval(
    *,
    principal_id: str,
    wallet_address: Optional[str],
    agent_id: str,
    action: str,
    amount_inr: int,
    resource_id: str,
    mandate: MandateRecord,
    counterparty_id: Optional[str],
    payload: Optional[dict[str, Any]],
) -> ApprovalRecord:
    state = load_state()
    nonce = uuid.uuid4().hex
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=APPROVAL_TTL_MINUTES)).isoformat()
    request = _action_request(
        principal_id=principal_id,
        wallet_address=wallet_address,
        role=mandate.role,
        agent_id=agent_id,
        action=action,
        amount_inr=amount_inr,
        resource_id=resource_id,
        nonce=nonce,
        expires_at=expires_at,
        counterparty_id=counterparty_id,
        payload=payload,
    )
    approval = ApprovalRecord(
        approval_id=_new_id("appr"),
        request_hash=sha256_hex(canonicalize(request)),
        principal_id=principal_id,
        wallet_address=wallet_address,
        agent_id=agent_id,
        action=action,
        amount_inr=amount_inr,
        resource_id=resource_id,
        mandate_id=mandate.mandate_id,
        mandate_version=mandate.version,
        policy_version=mandate.version,
        nonce=nonce,
        expires_at=expires_at,
        status="issued",
        created_at=_utcnow(),
    )
    state.approvals[approval.approval_id] = approval
    save_state(state)
    return approval


def consume_approval(
    *,
    approval_id: str,
    wallet_address: Optional[str] = None,
    principal_id: Optional[str] = None,
    action: Optional[str] = None,
    amount_inr: Optional[int] = None,
    resource_id: Optional[str] = None,
    request_hash: Optional[str] = None,
    counterparty_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    validate_request: bool = False,
) -> dict[str, Any]:
    with _STATE_LOCK:
        return _consume_approval_locked(
            approval_id=approval_id,
            wallet_address=wallet_address,
            principal_id=principal_id,
            action=action,
            amount_inr=amount_inr,
            resource_id=resource_id,
            request_hash=request_hash,
            counterparty_id=counterparty_id,
            payload=payload,
            validate_request=validate_request,
        )


def _consume_approval_locked(
    *,
    approval_id: str,
    wallet_address: Optional[str] = None,
    principal_id: Optional[str] = None,
    action: Optional[str] = None,
    amount_inr: Optional[int] = None,
    resource_id: Optional[str] = None,
    request_hash: Optional[str] = None,
    counterparty_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    validate_request: bool = False,
) -> dict[str, Any]:
    state = load_state()
    approval = state.approvals.get(approval_id)
    if not approval:
        raise KeyError(f"Unknown approval: {approval_id}")
    expected_principal = principal_id or (principal_id_from_wallet(wallet_address) if wallet_address else None)
    if expected_principal and approval.principal_id != expected_principal:
        raise PermissionError("Approval principal mismatch.")
    if wallet_address and approval.wallet_address and approval.wallet_address != wallet_address:
        raise PermissionError("Approval wallet mismatch.")
    if approval.status == "consumed":
        raise ConflictError("Approval already consumed (replay rejected).")
    if approval.status != "issued":
        raise ConflictError(f"Approval not consumable: {approval.status}")
    if _parse_dt(approval.expires_at) < datetime.now(timezone.utc):
        approval = approval.model_copy(update={"status": "expired"})
        state.approvals[approval_id] = approval
        save_state(state)
        raise ConflictError("Approval expired.")
    if approval.nonce in state.consumed_nonces:
        raise ConflictError("Approval nonce replay rejected.")
    if action and normalize_action(action) != approval.action:
        raise ConflictError("Approval action mismatch.")
    if amount_inr is not None and amount_inr != approval.amount_inr:
        raise ConflictError("Approval amount mismatch.")
    if resource_id and resource_id != approval.resource_id:
        raise ConflictError("Approval resource mismatch.")
    if request_hash and request_hash != approval.request_hash:
        raise ConflictError("Approval request hash mismatch.")

    agent = state.agents.get(approval.agent_id)
    if not agent or agent.principal_id != approval.principal_id:
        raise ConflictError("Approval agent is no longer valid.")
    if agent.status != "active":
        raise ConflictError(f"Approval invalidated: agent is {agent.status}.")
    if agent.mandate_id != approval.mandate_id:
        raise ConflictError("Approval invalidated by mandate replacement.")
    mandate = state.mandates.get(approval.mandate_id)
    if not mandate or mandate.status != "active":
        raise ConflictError("Approval mandate is no longer active.")
    if mandate.version != approval.mandate_version:
        raise ConflictError("Approval invalidated by mandate version change.")
    policy = state.policies.get(agent.policy_id or "")
    if not policy or policy.status != "active" or policy.version != approval.policy_version:
        raise ConflictError("Approval invalidated by policy version change.")
    if validate_request:
        candidate = _action_request(
            principal_id=approval.principal_id,
            wallet_address=approval.wallet_address,
            role=mandate.role,
            agent_id=approval.agent_id,
            action=normalize_action(action or approval.action) or "",
            amount_inr=approval.amount_inr if amount_inr is None else amount_inr,
            resource_id=resource_id or approval.resource_id,
            nonce=approval.nonce,
            expires_at=approval.expires_at,
            counterparty_id=counterparty_id,
            payload=payload,
        )
        if sha256_hex(canonicalize(candidate)) != approval.request_hash:
            raise ConflictError("Approval canonical request mismatch.")

    now = _utcnow()
    approval = approval.model_copy(update={"status": "consumed", "consumed_at": now})
    state.approvals[approval_id] = approval
    state.consumed_nonces[approval.nonce] = approval_id
    save_state(state)

    receipt = _write_receipt(
        principal_id=approval.principal_id,
        wallet_address=approval.wallet_address,
        agent_id=approval.agent_id,
        policy_id=agent.policy_id,
        mandate_id=approval.mandate_id,
        mandate_version=approval.mandate_version,
        action=approval.action,
        amount_inr=approval.amount_inr,
        resource_id=approval.resource_id,
        outcome="approved",
        reason_code="within_policy",
        approval_id=approval_id,
        request_hash=approval.request_hash,
    )
    return {
        "approval": approval.model_dump(),
        "mandate": mandate.model_dump(),
        "receipt": receipt.model_dump(),
    }


def register_executor(action: str, executor: Executor) -> None:
    normalized = normalize_action(action)
    if not normalized:
        raise ValueError(f"Unsupported action: {action}")
    _EXECUTORS[normalized] = executor


def execute_action(
    *,
    action: str,
    amount_inr: int,
    resource_id: str,
    wallet_address: Optional[str] = None,
    principal_id: Optional[str] = None,
    role: Optional[Role] = None,
    agent_id: Optional[str] = None,
    approval_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    normalized = normalize_action(action)
    if not normalized or normalized not in _EXECUTORS:
        raise ExecutionError("Unsupported protected action.")
    if approval_id:
        consumed = consume_approval(
            approval_id=approval_id,
            wallet_address=wallet_address,
            principal_id=principal_id,
            action=normalized,
            amount_inr=amount_inr,
            resource_id=resource_id,
            payload=payload,
            validate_request=True,
        )
        agent = get_agent(consumed["approval"]["agent_id"])
        mandate = get_mandate(consumed["approval"]["mandate_id"])
        request_hash = consumed["approval"]["request_hash"]
        principal_id = consumed["approval"]["principal_id"]
    else:
        decision = evaluate_action(
            action=normalized,
            amount_inr=amount_inr,
            resource_id=resource_id,
            wallet_address=wallet_address,
            principal_id=principal_id,
            role=role,
            agent_id=agent_id,
            payload=payload,
            write_receipt=False,
        )
        if decision["decision"] != "allow":
            return decision
        agent = AgentRecord.model_validate(decision["agent"])
        mandate = MandateRecord.model_validate(decision["mandate"])
        request_hash = decision["request_hash"]
        principal_id = agent.principal_id

    context = {
        "action": normalized,
        "amount_inr": amount_inr,
        "resource_id": resource_id,
        "principal_id": principal_id,
        "wallet_address": wallet_address or (agent.wallet_address if agent else None),
        "agent_id": agent.agent_id if agent else None,
        "idempotency_key": idempotency_key or request_hash or uuid.uuid4().hex,
        "payload": payload or {},
    }
    result = _EXECUTORS[normalized](context)
    receipt = _write_receipt(
        principal_id=principal_id or "",
        wallet_address=context["wallet_address"],
        agent_id=agent.agent_id if agent else "",
        policy_id=agent.policy_id if agent else None,
        mandate_id=mandate.mandate_id if mandate else None,
        mandate_version=mandate.version if mandate else None,
        action=normalized,
        amount_inr=amount_inr,
        resource_id=resource_id,
        outcome="executed",
        reason_code="within_policy",
        approval_id=approval_id,
        request_hash=request_hash,
        result=result,
    )
    if normalized == "buyer.checkout.commit":
        from app import commerce_demo

        order = result.get("order") if isinstance(result, dict) else None
        order_id = order.get("order_id") if isinstance(order, dict) else None
        if order_id:
            result = {
                **result,
                "order": commerce_demo.record_order_authorization(
                    str(order_id),
                    {
                        "decision": "allow",
                        "reason_code": "exact_approval" if approval_id else "within_policy",
                        "receipt_id": receipt.receipt_id,
                        "approval_id": approval_id,
                        "amount_inr": amount_inr,
                        "recorded_at": receipt.created_at,
                    },
                ),
            }
    return {
        "decision": "allow",
        "reason": "Executed.",
        "reason_code": "within_policy",
        "result": result,
        "receipt": receipt.model_dump(),
    }


def get_receipt(receipt_id: str) -> Optional[ReceiptRecord]:
    return load_state().receipts.get(receipt_id)


def list_receipts_for_wallet(wallet_address: str) -> list[ReceiptRecord]:
    state = load_state()
    rows = [r for r in state.receipts.values() if r.wallet_address == wallet_address]
    return sorted(rows, key=lambda r: r.created_at, reverse=True)


def list_receipts_for_principal(principal_id: str) -> list[ReceiptRecord]:
    state = load_state()
    rows = [r for r in state.receipts.values() if r.principal_id == principal_id]
    return sorted(rows, key=lambda r: r.created_at, reverse=True)


def verify_receipt_payload(receipt: dict[str, Any]) -> dict[str, Any]:
    return verify_receipt(receipt)


def verify_receipt_by_id(receipt_id: str) -> dict[str, Any]:
    receipt = get_receipt(receipt_id)
    if not receipt:
        raise KeyError(f"Unknown receipt: {receipt_id}")
    return verify_receipt(receipt.model_dump())


def _write_receipt(
    *,
    principal_id: str,
    wallet_address: Optional[str],
    agent_id: str,
    policy_id: Optional[str],
    action: str,
    amount_inr: int,
    resource_id: str,
    outcome: Literal["allowed", "approved", "denied", "paused", "executed"],
    mandate_id: Optional[str] = None,
    mandate_version: Optional[int] = None,
    reason_code: Optional[str] = None,
    approval_id: Optional[str] = None,
    request_hash: Optional[str] = None,
    result: Optional[dict[str, Any]] = None,
) -> ReceiptRecord:
    state = load_state()
    receipt = ReceiptRecord(
        receipt_id=_new_id("rcpt"),
        principal_id=principal_id,
        wallet_address=wallet_address,
        agent_id=agent_id,
        policy_id=policy_id,
        mandate_id=mandate_id,
        mandate_version=mandate_version,
        action=action,
        amount_inr=amount_inr,
        resource_id=resource_id,
        outcome=outcome,
        reason_code=reason_code,
        approval_id=approval_id,
        request_hash=request_hash,
        result=result,
        created_at=_utcnow(),
    )
    signed = ReceiptRecord.model_validate(sign_receipt(receipt.model_dump(exclude_none=True)))
    state.receipts[signed.receipt_id] = signed
    save_state(state)
    return signed


def _commerce_executor(action: str) -> Executor:
    def _execute(context: dict[str, Any]) -> dict[str, Any]:
        from app import commerce_demo

        payload = context["payload"]
        principal_id = str(context.get("principal_id") or "")
        if not principal_id:
            raise PermissionError("Authenticated principal required for commerce execution.")
        resource_id = str(context["resource_id"])
        # Client idempotency keys are scoped to the authenticated tenant before
        # they reach the shared commerce store. The same raw key from another
        # principal must never replay a foreign result.
        key = f"{principal_id}:{context['idempotency_key']}"
        if action == "seller.catalog.publish":
            return commerce_demo.publish_item_from_payload(
                payload,
                principal_id=principal_id,
                resource_id=resource_id,
                idempotency_key=key,
            )
        if action == "seller.catalog.archive":
            return commerce_demo.archive_item_from_payload(
                payload,
                principal_id=principal_id,
                resource_id=resource_id,
                idempotency_key=key,
            )
        if action == "buyer.checkout.commit":
            return commerce_demo.create_order_from_payload(
                payload,
                principal_id=principal_id,
                idempotency_key=key,
            )
        if action in {"seller.order.accept", "seller.order.reject", "seller.fulfilment.commit"}:
            return commerce_demo.transition_order_from_payload(
                action,
                payload,
                principal_id=principal_id,
                resource_id=resource_id,
                idempotency_key=key,
            )
        if action == "seller.refund.issue":
            return commerce_demo.refund_from_payload(
                payload,
                principal_id=principal_id,
                resource_id=resource_id,
                amount_inr=context["amount_inr"],
                idempotency_key=key,
            )
        if action in {"buyer.return.submit", "buyer.order.cancel"}:
            return commerce_demo.issue_from_payload(
                action,
                payload,
                principal_id=principal_id,
                resource_id=resource_id,
                idempotency_key=key,
            )
        if action == "buyer.remedy.accept":
            return commerce_demo.accept_remedy_from_payload(
                payload,
                principal_id=principal_id,
                resource_id=resource_id,
                idempotency_key=key,
            )
        if action == "seller.remedy.promise":
            return commerce_demo.remedy_from_payload(
                payload,
                principal_id=principal_id,
                resource_id=resource_id,
                idempotency_key=key,
            )
        raise ExecutionError("Unsupported protected action.")

    return _execute


for _action in AGENTGUARD_ACTIONS:
    register_executor(_action, _commerce_executor(_action))


def compat_principal(wallet_address: str, role: Optional[Role] = None) -> PrincipalRef:
    return principal_from_wallet(wallet_address, role)
