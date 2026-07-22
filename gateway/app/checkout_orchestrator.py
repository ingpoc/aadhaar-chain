"""PostgreSQL-backed AgentGuard checkout policy and execution saga."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from app.agentguard_contract import canonicalize
from app.commerce_v1 import CommerceV1
from app.persistence.agentguard_repository import (
    AgentGuardConflict,
    AgentGuardNotFound,
    AgentGuardRepository,
)
from app.persistence.commerce_repository import CommerceRepository
from app.persistence.connection import ConnectionPool
from app.persistence.transaction import UnitOfWork
from app.receipt_signing import sign_receipt


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _hash(payload: dict[str, Any]) -> str:
    return sha256(canonicalize(_jsonable(payload)).encode("utf-8")).hexdigest()


class CheckoutOrchestrator:
    """Own exact checkout authority, intent persistence, and saga resumption."""

    operation = "buyer.checkout.commit"
    policy_id = "buyer.checkout.limit"

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool
        self.commerce = CommerceV1(pool)

    @staticmethod
    def agent_id(principal_id: str) -> str:
        return f"agent_buyer_{sha256(principal_id.encode()).hexdigest()[:20]}"

    @staticmethod
    def mandate_id(principal_id: str) -> str:
        return f"mandate_buyer_{sha256(principal_id.encode()).hexdigest()[:20]}"

    async def compile_mandate(
        self, *, principal_id: str, limits: dict[str, Any]
    ) -> dict[str, Any]:
        max_order_paise = limits.get("max_order_paise")
        if max_order_paise is None:
            max_order_paise = int(limits.get("checkout_auto_max_inr", 10_000)) * 100
        max_order_paise = int(max_order_paise)
        if max_order_paise < 0:
            raise ValueError("max order amount must be non-negative")
        agent_id = self.agent_id(principal_id)
        mandate_id = self.mandate_id(principal_id)
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = AgentGuardRepository(unit_of_work)
            agent = await repository.get_agent(
                principal_id=principal_id, agent_id=agent_id
            )
            if agent is None:
                agent = await repository.create_agent(
                    agent_id=agent_id,
                    principal_id=principal_id,
                    role="buyer",
                    payload={"name": "Buyer commerce agent"},
                )
            if agent["status"] == "revoked":
                raise AgentGuardConflict("agent is revoked")
            version = int(agent.get("current_mandate_version") or 0) + 1
            mandate = await repository.create_mandate_version(
                mandate_id=mandate_id,
                version=version,
                principal_id=principal_id,
                agent_id=agent_id,
                payload={
                    "allowed_actions": [self.operation],
                    "max_order_paise": max_order_paise,
                    "currency": "INR",
                },
            )
            agent = await repository.get_agent(
                principal_id=principal_id, agent_id=agent_id
            )
        return {"agent": _jsonable(agent), "mandate": _jsonable(mandate)}

    async def _authority(
        self, repository: AgentGuardRepository, principal_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        agent = await repository.get_agent(
            principal_id=principal_id, agent_id=self.agent_id(principal_id)
        )
        if agent is None or agent.get("current_mandate_id") is None:
            raise AgentGuardNotFound("buyer mandate not found")
        if agent["status"] != "active":
            raise AgentGuardConflict(f"agent is {agent['status']}")
        mandate = await repository.get_mandate_version(
            principal_id=principal_id,
            mandate_id=agent["current_mandate_id"],
            version=agent["current_mandate_version"],
        )
        if mandate is None or mandate["status"] != "active":
            raise AgentGuardConflict("active mandate not found")
        return agent, mandate

    async def current_mandate(self, *, principal_id: str) -> dict[str, Any]:
        async with UnitOfWork(self.pool) as unit_of_work:
            agent, mandate = await self._authority(
                AgentGuardRepository(unit_of_work), principal_id
            )
        return {"agent": _jsonable(agent), "mandate": _jsonable(mandate)}

    async def set_agent_status(
        self, *, principal_id: str, agent_id: str, status: str
    ) -> dict[str, Any]:
        async with UnitOfWork(self.pool) as unit_of_work:
            agent = await AgentGuardRepository(unit_of_work).set_agent_status(
                principal_id=principal_id, agent_id=agent_id, status=status
            )
        return _jsonable(agent)

    async def get_receipt(
        self, *, principal_id: str, receipt_id: str
    ) -> dict[str, Any]:
        async with UnitOfWork(self.pool) as unit_of_work:
            receipt = await AgentGuardRepository(unit_of_work).get_receipt(
                principal_id=principal_id, receipt_id=receipt_id
            )
        if receipt is None:
            raise AgentGuardNotFound("receipt not found")
        return _jsonable(receipt)

    async def _bound_quote(
        self,
        repository: CommerceRepository,
        *,
        principal_id: str,
        quote_id: str,
        lock: bool,
        require_open: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        quote = await repository.get_quote(UUID(quote_id), principal_id, lock=lock)
        if require_open and quote["status"] != "open":
            raise AgentGuardConflict("quote is not open")
        if require_open and quote["expires_at"] <= _utcnow():
            raise AgentGuardConflict("quote expired")
        bound = {
            "action": self.operation,
            "principal_id": principal_id,
            "quote_id": str(quote["quote_id"]),
            "cart_id": str(quote["cart_id"]),
            "cart_version": quote["cart_version"],
            "seller_id": quote["seller_id"],
            "landed_total_paise": quote["landed_total_paise"],
            "currency": "INR",
            "inventory_commitment": quote["line_snapshot"],
            "expires_at": quote["expires_at"].isoformat(),
        }
        return quote, _jsonable(bound)

    async def evaluate_checkout(
        self, *, principal_id: str, quote_id: str
    ) -> dict[str, Any]:
        try:
            async with UnitOfWork(self.pool) as unit_of_work:
                agentguard = AgentGuardRepository(unit_of_work)
                commerce = CommerceRepository(unit_of_work)
                try:
                    agent, mandate = await self._authority(agentguard, principal_id)
                except AgentGuardNotFound:
                    agent = mandate = None
        except AgentGuardNotFound:  # pragma: no cover - retained for adapter safety
            agent = mandate = None
        if agent is None or mandate is None:
            await self.compile_mandate(principal_id=principal_id, limits={})

        async with UnitOfWork(self.pool) as unit_of_work:
            agentguard = AgentGuardRepository(unit_of_work)
            commerce = CommerceRepository(unit_of_work)
            agent, mandate = await self._authority(agentguard, principal_id)
            quote, bound = await self._bound_quote(
                commerce, principal_id=principal_id, quote_id=quote_id, lock=True
            )
            limit = int(mandate["payload"]["max_order_paise"])
            requires_approval = quote["landed_total_paise"] > limit
            decision = "need_approval" if requires_approval else "allow"
            reason_code = (
                "AMOUNT_EXCEEDS_ORDER_LIMIT"
                if requires_approval
                else "WITHIN_ORDER_LIMIT"
            )
            human_reason = (
                f"This order is INR {(quote['landed_total_paise'] - limit) / 100:.2f} "
                "above your automatic purchase limit."
                if requires_approval
                else "This order is within your automatic purchase limit."
            )
            decision_id = f"decision_{uuid4().hex}"
            request_hash = _hash(bound)
            expiry = min(quote["expires_at"], _utcnow() + timedelta(minutes=10))
            await agentguard.record_decision(
                decision_id=decision_id,
                principal_id=principal_id,
                agent_id=agent["agent_id"],
                mandate_id=mandate["mandate_id"],
                mandate_version=mandate["version"],
                status=decision,
                policy={"policy_id": self.policy_id, "version": mandate["version"]},
                risk={"level": "high"},
                required_action="review" if requires_approval else "none",
                expiry=expiry,
                payload={
                    "request_hash": request_hash,
                    "bound_action": bound,
                    "reason_code": reason_code,
                    "human_reason": human_reason,
                },
            )
            approval = None
            if requires_approval:
                approval = await agentguard.issue_approval(
                    approval_id=f"approval_{uuid4().hex}",
                    principal_id=principal_id,
                    decision_id=decision_id,
                    agent_id=agent["agent_id"],
                    mandate_id=mandate["mandate_id"],
                    mandate_version=mandate["version"],
                    request_hash=request_hash,
                    expires_at=expiry,
                    payload={"bound_action": bound},
                )
        response = {
            "schema_version": "2",
            "decision": decision,
            "decision_id": decision_id,
            "policy_id": self.policy_id,
            "reason_code": reason_code,
            "human_reason": human_reason,
            "reason": human_reason,
            "required_action": "review" if requires_approval else "none",
            "risk_level": "high",
            "policy_version": mandate["version"],
            "expires_at": expiry.isoformat(),
            "request_hash": request_hash,
            "bound_action": bound,
            "agent": _jsonable(agent),
            "mandate": _jsonable(mandate),
        }
        if approval is not None:
            response["approval"] = _jsonable(approval)
        return response

    async def execute_checkout(
        self,
        *,
        principal_id: str,
        quote_id: str,
        decision_id: str,
        approval_id: str | None,
        idempotency_key: str,
        correlation_id: str,
        payment_outcome: str = "succeeded",
    ) -> dict[str, Any]:
        if payment_outcome not in {"succeeded", "failed", "unknown"}:
            raise ValueError("unsupported simulated payment outcome")
        async with UnitOfWork(self.pool) as unit_of_work:
            agentguard = AgentGuardRepository(unit_of_work)
            commerce = CommerceRepository(unit_of_work)
            agent, mandate = await self._authority(agentguard, principal_id)
            decision = await agentguard.get_decision(
                principal_id=principal_id, decision_id=decision_id
            )
            if decision is None:
                raise AgentGuardNotFound("decision not found")
            _quote, bound = await self._bound_quote(
                commerce,
                principal_id=principal_id,
                quote_id=quote_id,
                lock=True,
                require_open=False,
            )
            request_hash = _hash(bound)
            if decision["payload"].get("request_hash") != request_hash:
                raise AgentGuardConflict("checkout no longer matches the decision")
            if (
                decision["mandate_id"],
                decision["mandate_version"],
            ) != (mandate["mandate_id"], mandate["version"]):
                raise AgentGuardConflict("decision mandate is stale")
            intent, created = await agentguard.create_execution_intent(
                intent_id=f"intent_{uuid4().hex}",
                principal_id=principal_id,
                operation=self.operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                decision_id=decision_id,
                approval_id=approval_id,
                payload={"bound_action": bound, "correlation_id": correlation_id},
                status="approved",
            )
            if not created and intent["status"] == "succeeded":
                return intent["result"]
            if not created and intent["status"] == "failed":
                raise AgentGuardConflict(
                    (intent.get("result") or {}).get("error", "checkout failed")
                )
            if created:
                if _quote["status"] != "open" or _quote["expires_at"] <= _utcnow():
                    raise AgentGuardConflict("quote is not open")
                if decision["status"] == "need_approval":
                    if not approval_id:
                        raise AgentGuardConflict("exact approval is required")
                    await agentguard.consume_approval(
                        principal_id=principal_id,
                        approval_id=approval_id,
                        request_hash=request_hash,
                    )
                elif decision["status"] != "allow":
                    raise AgentGuardConflict("decision does not authorize checkout")
                intent = await agentguard.set_execution_intent_status(
                    principal_id=principal_id,
                    intent_id=intent["intent_id"],
                    status="executing",
                )

        try:
            commerce_result = await self.commerce.prepare_checkout(
                principal_id=principal_id,
                quote_id=quote_id,
                idempotency_key=idempotency_key,
                request=bound,
            )
            payment_state = await self.commerce.get_payment_state(
                principal_id=principal_id,
                payment_attempt_id=commerce_result["payment_attempt"][
                    "payment_attempt_id"
                ],
            )
            current_status = payment_state["payment_attempt"]["status"]
            if current_status == "pending":
                payment_result = await self.commerce.record_payment_result(
                    principal_id=principal_id,
                    payment_attempt_id=commerce_result["payment_attempt"][
                        "payment_attempt_id"
                    ],
                    status=payment_outcome,
                    provider_reference=f"sandbox:{idempotency_key}",
                    detail={"simulated": True},
                )
            elif (
                payment_outcome == "succeeded"
                and current_status in {"succeeded", "reconciled"}
            ) or current_status == payment_outcome:
                payment_result = payment_state
            else:
                raise AgentGuardConflict(
                    f"payment is already {current_status}, not {payment_outcome}"
                )
            verified_result = {**commerce_result, **payment_result}
        except Exception as error:
            async with UnitOfWork(self.pool) as unit_of_work:
                await AgentGuardRepository(unit_of_work).set_execution_intent_status(
                    principal_id=principal_id,
                    intent_id=intent["intent_id"],
                    status="failed",
                    result={"error": str(error)},
                )
            raise

        receipt_id = f"receipt_{uuid4().hex}"
        receipt_payload = sign_receipt(
            {
                "schema_version": "2",
                "receipt_id": receipt_id,
                "principal_id": principal_id,
                "action": self.operation,
                "decision_id": decision_id,
                "approval_id": approval_id,
                "intent_id": intent["intent_id"],
                "idempotency_key": idempotency_key,
                "correlation_id": correlation_id,
                "bound_action": bound,
                "result": verified_result,
                "outcome": "executed",
                "created_at": _utcnow().isoformat(),
            }
        )
        response = {
            "schema_version": "2",
            "decision": "allow",
            "decision_id": decision_id,
            "policy_id": self.policy_id,
            "reason_code": "EXECUTED_AND_VERIFIED",
            "human_reason": "Payment and order state were verified.",
            "reason": "Payment and order state were verified.",
            "required_action": "none",
            "risk_level": "high",
            "policy_version": mandate["version"],
            "expires_at": decision["expiry"].isoformat(),
            "result": verified_result,
            "receipt": receipt_payload,
        }
        async with UnitOfWork(self.pool) as unit_of_work:
            agentguard = AgentGuardRepository(unit_of_work)
            await agentguard.set_execution_intent_status(
                principal_id=principal_id,
                intent_id=intent["intent_id"],
                status="succeeded",
                result=response,
            )
            await agentguard.record_receipt(
                receipt_id=receipt_id,
                principal_id=principal_id,
                agent_id=agent["agent_id"],
                mandate_id=mandate["mandate_id"],
                mandate_version=mandate["version"],
                decision_id=decision_id,
                approval_id=approval_id,
                intent_id=intent["intent_id"],
                status="executed",
                payload=receipt_payload,
            )
        return response


__all__ = ["CheckoutOrchestrator"]
