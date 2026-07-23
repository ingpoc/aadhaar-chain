"""PostgreSQL-backed AgentGuard checkout policy and execution saga."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from app import agentguard
from app.agentguard_contract import canonicalize
from app.commerce_compat import CommerceCompatibilityAdapter
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


_DELIVERY_CONTEXT_KEYS = (
    "name",
    "email",
    "phone",
    "line1",
    "line2",
    "city",
    "state",
    "postalCode",
    "country",
)


def _normalized_delivery_context(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key: str(value[key]).strip()
        for key in _DELIVERY_CONTEXT_KEYS
        if value.get(key) is not None and str(value[key]).strip()
    }


class CheckoutOrchestrator:
    """Own Buyer authority, intent persistence, and commerce execution."""

    operation = "buyer.checkout.commit"
    policy_id = "buyer.checkout.limit"
    default_actions = sorted(
        action
        for action in agentguard.AGENTGUARD_ACTIONS
        if action.startswith("buyer.")
    )

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool
        self.commerce = CommerceV1(pool)
        self.compat = CommerceCompatibilityAdapter(pool)

    @staticmethod
    def agent_id(principal_id: str) -> str:
        return f"agent_buyer_{sha256(principal_id.encode()).hexdigest()[:20]}"

    @staticmethod
    def mandate_id(principal_id: str) -> str:
        return f"mandate_buyer_{sha256(principal_id.encode()).hexdigest()[:20]}"

    @classmethod
    def _mandate_view(cls, mandate: dict[str, Any] | None) -> dict[str, Any] | None:
        if mandate is None:
            return None
        payload = mandate.get("payload") or {}
        maximum = int(payload.get("max_order_paise") or 0)
        return _jsonable(
            {
                **mandate,
                "allowed_actions": payload.get("allowed_actions")
                or cls.default_actions,
                "limits": {
                    "auto_approve_max_inr": {cls.operation: maximum // 100},
                    "max_order_paise": maximum,
                },
            }
        )

    @classmethod
    def _policy_view(cls) -> dict[str, Any]:
        return {
            "policy_id": cls.policy_id,
            "version": 1,
            "allowed_actions": cls.default_actions,
        }

    async def ensure_agent(self, *, principal_id: str) -> dict[str, Any]:
        agent_id = self.agent_id(principal_id)
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
            mandate = await repository.get_latest_mandate_for_agent(
                principal_id=principal_id, agent_id=agent_id
            )
            receipts = await repository.list_receipts(
                principal_id=principal_id, limit=20
            )
        return {
            "agent": _jsonable(agent),
            "mandate": self._mandate_view(mandate),
            "policy": self._policy_view(),
            "receipts": _jsonable(receipts),
        }

    async def compile_mandate(
        self,
        *,
        principal_id: str,
        limits: dict[str, Any],
        allowed_actions: list[str] | None = None,
    ) -> dict[str, Any]:
        max_order_paise = limits.get("max_order_paise")
        if max_order_paise is None:
            automatic = limits.get("auto_approve_max_inr")
            if isinstance(automatic, dict):
                automatic = automatic.get(self.operation)
            if automatic is None:
                automatic = limits.get("checkout_auto_max_inr", 10_000)
            max_order_paise = int(automatic) * 100
        max_order_paise = int(max_order_paise)
        if max_order_paise < 0:
            raise ValueError("max order amount must be non-negative")
        selected_actions = [
            action
            for action in (allowed_actions or self.default_actions)
            if action in self.default_actions
        ]
        if self.operation not in selected_actions:
            selected_actions.insert(0, self.operation)
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
            latest = await repository.get_latest_mandate_for_agent(
                principal_id=principal_id, agent_id=agent_id
            )
            version = int((latest or {}).get("version") or 0) + 1
            mandate = await repository.create_mandate_version(
                mandate_id=mandate_id,
                version=version,
                principal_id=principal_id,
                agent_id=agent_id,
                payload={
                    "allowed_actions": selected_actions,
                    "max_order_paise": max_order_paise,
                    "currency": "INR",
                },
                status="draft",
                activate=False,
            )
            agent = await repository.get_agent(
                principal_id=principal_id, agent_id=agent_id
            )
        return {"agent": _jsonable(agent), "mandate": self._mandate_view(mandate)}

    async def confirm_mandate(
        self, *, principal_id: str, mandate_id: str
    ) -> dict[str, Any]:
        agent_id = self.agent_id(principal_id)
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = AgentGuardRepository(unit_of_work)
            agent = await repository.get_agent(
                principal_id=principal_id, agent_id=agent_id
            )
            if agent is None:
                raise AgentGuardNotFound("buyer mandate not found")
            if (
                agent.get("current_mandate_id") == mandate_id
                and agent.get("current_mandate_version") is not None
            ):
                active = await repository.get_mandate_version(
                    principal_id=principal_id,
                    mandate_id=mandate_id,
                    version=agent["current_mandate_version"],
                )
                if active is not None and active["status"] == "active":
                    return {"agent": _jsonable(agent), "mandate": _jsonable(active)}
            latest = await repository.get_latest_mandate_for_agent(
                principal_id=principal_id, agent_id=agent_id
            )
            if (
                latest is None
                or latest["mandate_id"] != mandate_id
                or latest["status"] != "draft"
            ):
                raise AgentGuardConflict("latest mandate is not confirmable")
            active = await repository.create_mandate_version(
                mandate_id=mandate_id,
                version=latest["version"] + 1,
                principal_id=principal_id,
                agent_id=agent_id,
                payload=latest["payload"],
                status="active",
                activate=True,
            )
            agent = await repository.get_agent(
                principal_id=principal_id, agent_id=agent_id
            )
        return {"agent": _jsonable(agent), "mandate": self._mandate_view(active)}

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
        return {"agent": _jsonable(agent), "mandate": self._mandate_view(mandate)}

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

    async def evaluate_protected_action(
        self,
        *,
        principal_id: str,
        action: str,
        amount_inr: int,
        resource_id: str,
        counterparty_id: str | None,
        payload: dict[str, Any],
        correlation_id: str,
    ) -> dict[str, Any]:
        normalized = agentguard.normalize_action(action)
        if normalized is None or not normalized.startswith("buyer."):
            raise ValueError("unsupported Buyer protected action")
        if normalized == self.operation:
            raise ValueError("checkout must be evaluated against a durable quote")
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = AgentGuardRepository(unit_of_work)
            agent = await repository.get_agent(
                principal_id=principal_id, agent_id=self.agent_id(principal_id)
            )
            if agent is None or agent.get("current_mandate_id") is None:
                raise AgentGuardNotFound("buyer mandate not found")
            mandate = await repository.get_mandate_version(
                principal_id=principal_id,
                mandate_id=agent["current_mandate_id"],
                version=agent["current_mandate_version"],
            )
            if mandate is None:
                raise AgentGuardNotFound("buyer mandate not found")
            mandate_payload = mandate.get("payload") or {}
            allowed_actions = mandate_payload.get("allowed_actions") or []
            status = "allow"
            reason_code = "within_policy"
            human_reason = "Buyer action is within the confirmed mandate."
            if agent["status"] != "active":
                status = "deny"
                reason_code = f"agent_{agent['status']}"
                human_reason = f"Buyer agent is {agent['status']}."
            elif normalized not in allowed_actions:
                status = "deny"
                reason_code = "action_not_allowed"
                human_reason = "Buyer action is not in the confirmed mandate."
            limits = mandate_payload.get("auto_approve_max_inr") or {}
            threshold = int(limits.get(normalized, 0))
            requires_approval = status == "allow" and amount_inr > threshold
            bound_action = {
                "action": normalized,
                "amount_inr": amount_inr,
                "resource_id": resource_id,
                "counterparty_id": counterparty_id,
                "payload": payload,
                "correlation_id": correlation_id,
            }
            request_hash = _hash(bound_action)
            decision_id = f"decision_{uuid4().hex}"
            expiry = _utcnow() + timedelta(minutes=10)
            await repository.record_decision(
                decision_id=decision_id,
                principal_id=principal_id,
                agent_id=agent["agent_id"],
                mandate_id=mandate["mandate_id"],
                mandate_version=mandate["version"],
                status=status,
                policy={"policy_id": self.policy_id, "version": mandate["version"]},
                risk={"level": "high"},
                required_action="review" if requires_approval else "none",
                expiry=expiry,
                payload={
                    "request_hash": request_hash,
                    "bound_action": bound_action,
                    "reason_code": reason_code,
                    "human_reason": human_reason,
                },
            )
            approval = None
            if requires_approval:
                approval = await repository.issue_approval(
                    approval_id=f"approval_{uuid4().hex}",
                    principal_id=principal_id,
                    decision_id=decision_id,
                    agent_id=agent["agent_id"],
                    mandate_id=mandate["mandate_id"],
                    mandate_version=mandate["version"],
                    request_hash=request_hash,
                    expires_at=expiry,
                    payload={"bound_action": bound_action},
                )
        response = {
            "schema_version": "2",
            "decision": status,
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
            "bound_action": bound_action,
            "agent": _jsonable(agent),
            "mandate": self._mandate_view(mandate),
        }
        if approval is not None:
            response["approval"] = _jsonable(approval)
        return response

    async def execute_protected_action(
        self,
        *,
        principal_id: str,
        decision_id: str | None,
        approval_id: str | None,
        action: str,
        amount_inr: int,
        resource_id: str,
        idempotency_key: str,
        correlation_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = agentguard.normalize_action(action)
        if normalized is None or not normalized.startswith("buyer."):
            raise ValueError("unsupported Buyer protected action")
        if normalized == self.operation:
            raise ValueError("checkout must execute against a durable quote")
        if decision_id is None:
            evaluated = await self.evaluate_protected_action(
                principal_id=principal_id,
                action=normalized,
                amount_inr=amount_inr,
                resource_id=resource_id,
                counterparty_id=None,
                payload=payload,
                correlation_id=correlation_id,
            )
            decision_id = evaluated["decision_id"]
            if evaluated.get("approval") is not None and approval_id is None:
                return evaluated
        bound_action = {
            "action": normalized,
            "amount_inr": amount_inr,
            "resource_id": resource_id,
            "counterparty_id": None,
            "payload": payload,
            "correlation_id": correlation_id,
        }
        request_hash = _hash(bound_action)
        intent_id = f"intent_{uuid4().hex}"
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = AgentGuardRepository(unit_of_work)
            decision = await repository.get_decision(
                principal_id=principal_id, decision_id=decision_id
            )
            if decision is None:
                raise AgentGuardNotFound("Buyer decision not found")
            if decision["status"] != "allow":
                raise AgentGuardConflict("Buyer decision denied the protected action")
            if decision.get("expiry") and decision["expiry"] <= _utcnow():
                raise AgentGuardConflict("Buyer decision expired")
            if (decision.get("payload") or {}).get("request_hash") != request_hash:
                raise AgentGuardConflict("Buyer action changed after evaluation")
            agent = await repository.get_agent(
                principal_id=principal_id, agent_id=decision["agent_id"]
            )
            if agent is None or agent["status"] != "active":
                raise AgentGuardConflict(
                    f"Buyer agent is {(agent or {}).get('status', 'missing')}"
                )
            if (
                agent.get("current_mandate_id"),
                agent.get("current_mandate_version"),
            ) != (decision["mandate_id"], decision["mandate_version"]):
                raise AgentGuardConflict("Buyer decision mandate is stale")
            if approval_id is not None:
                approval = await repository.get_approval(
                    principal_id=principal_id, approval_id=approval_id
                )
                if approval is None or approval["decision_id"] != decision_id:
                    raise AgentGuardConflict(
                        "Buyer approval does not match the decision"
                    )
            intent, created = await repository.create_execution_intent(
                intent_id=intent_id,
                principal_id=principal_id,
                operation=normalized,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                decision_id=decision_id,
                approval_id=approval_id,
                payload={"bound_action": bound_action},
                status="pending",
            )
            if not created and intent.get("result") is not None:
                return _jsonable(intent["result"])
            if decision["required_action"] == "review":
                if approval_id is None:
                    raise AgentGuardConflict("exact Buyer approval is required")
                if created:
                    await repository.consume_approval(
                        principal_id=principal_id,
                        approval_id=approval_id,
                        request_hash=request_hash,
                    )
            await repository.set_execution_intent_status(
                principal_id=principal_id,
                intent_id=intent["intent_id"],
                status="executing",
            )
            mandate = await repository.get_mandate_version(
                principal_id=principal_id,
                mandate_id=decision["mandate_id"],
                version=decision["mandate_version"],
            )
            if mandate is None:
                raise AgentGuardConflict("Buyer mandate is unavailable")

        effect = await self._execute_protected_effect(
            principal_id=principal_id,
            action=normalized,
            resource_id=resource_id,
            payload=payload,
        )
        receipt_id = f"receipt_{uuid4().hex}"
        receipt = sign_receipt(
            {
                "schema_version": "2",
                "receipt_id": receipt_id,
                "principal_id": principal_id,
                "action": normalized,
                "decision_id": decision_id,
                "approval_id": approval_id,
                "intent_id": intent["intent_id"],
                "idempotency_key": idempotency_key,
                "correlation_id": correlation_id,
                "bound_action": bound_action,
                "result": effect,
                "outcome": "succeeded",
                "created_at": _utcnow().isoformat(),
            }
        )
        response = {
            "schema_version": "2",
            "decision": "allow",
            "decision_id": decision_id,
            "policy_id": self.policy_id,
            "reason_code": "executed",
            "human_reason": "Buyer action completed and its outcome was verified.",
            "reason": "Buyer action completed and its outcome was verified.",
            "required_action": "none",
            "risk_level": "high",
            "policy_version": mandate["version"],
            "result": effect,
            "receipt": receipt,
        }
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = AgentGuardRepository(unit_of_work)
            await repository.set_execution_intent_status(
                principal_id=principal_id,
                intent_id=intent["intent_id"],
                status="succeeded",
                result=response,
            )
            await repository.record_receipt(
                receipt_id=receipt_id,
                principal_id=principal_id,
                agent_id=agent["agent_id"],
                mandate_id=mandate["mandate_id"],
                mandate_version=mandate["version"],
                decision_id=decision_id,
                approval_id=approval_id,
                intent_id=intent["intent_id"],
                status="succeeded",
                payload=receipt,
            )
        return response

    async def _execute_protected_effect(
        self,
        *,
        principal_id: str,
        action: str,
        resource_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            order = await self.compat.get_order(resource_id, principal_id=principal_id)
        except KeyError:
            order = None
        if action == "buyer.order.cancel":
            if order is None:
                raise AgentGuardNotFound("Buyer order not found")
            return await self.compat.transition_order(
                resource_id, "cancelled", payload=payload
            )
        if action == "buyer.return.submit":
            if order is None:
                raise AgentGuardNotFound("Buyer order not found")
            return await self.compat.create_return(
                resource_id, principal_id=principal_id, body=payload
            )
        if action == "buyer.remedy.accept":
            issues = await self.compat.list_issues(principal_id=principal_id)
            if resource_id not in {issue["issue_id"] for issue in issues["issues"]}:
                raise AgentGuardNotFound("Buyer issue not found")
            return await self.compat.accept_remedy(resource_id)
        raise ValueError("unsupported Buyer protected action")

    async def _bound_quote(
        self,
        repository: CommerceRepository,
        *,
        principal_id: str,
        quote_id: str,
        lock: bool,
        require_open: bool = True,
        delivery_context: dict[str, str] | None = None,
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
        if delivery_context:
            bound["delivery_context_hash"] = _hash(delivery_context)
        return quote, _jsonable(bound)

    async def evaluate_checkout(
        self,
        *,
        principal_id: str,
        quote_id: str,
        delivery_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_delivery_context = _normalized_delivery_context(delivery_context)
        async with UnitOfWork(self.pool) as unit_of_work:
            agentguard = AgentGuardRepository(unit_of_work)
            commerce = CommerceRepository(unit_of_work)
            agent, mandate = await self._authority(agentguard, principal_id)
            quote, bound = await self._bound_quote(
                commerce,
                principal_id=principal_id,
                quote_id=quote_id,
                lock=True,
                delivery_context=normalized_delivery_context,
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
        delivery_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if payment_outcome not in {"succeeded", "failed", "unknown"}:
            raise ValueError("unsupported simulated payment outcome")
        normalized_delivery_context = _normalized_delivery_context(delivery_context)
        async with UnitOfWork(self.pool) as unit_of_work:
            agentguard = AgentGuardRepository(unit_of_work)
            commerce = CommerceRepository(unit_of_work)
            agent, mandate = await self._authority(agentguard, principal_id)
            decision = await agentguard.get_decision(
                principal_id=principal_id, decision_id=decision_id
            )
            if decision is None:
                raise AgentGuardNotFound("decision not found")
            if decision["expiry"] is not None and decision["expiry"] <= _utcnow():
                raise AgentGuardConflict("decision expired")
            _quote, bound = await self._bound_quote(
                commerce,
                principal_id=principal_id,
                quote_id=quote_id,
                lock=True,
                require_open=False,
                delivery_context=normalized_delivery_context,
            )
            request_hash = _hash(bound)
            if decision["payload"].get("request_hash") != request_hash:
                raise AgentGuardConflict("checkout no longer matches the decision")
            if (
                decision["mandate_id"],
                decision["mandate_version"],
            ) != (mandate["mandate_id"], mandate["version"]):
                raise AgentGuardConflict("decision mandate is stale")
            if approval_id:
                approval = await agentguard.get_approval(
                    principal_id=principal_id, approval_id=approval_id
                )
                if approval is None or approval["decision_id"] != decision_id:
                    raise AgentGuardConflict("approval does not match the decision")
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
            if normalized_delivery_context:
                await self.compat.set_delivery_context(
                    str(commerce_result["order"]["order_id"]),
                    principal_id=principal_id,
                    delivery_context=normalized_delivery_context,
                )
            payment_state = await self.commerce.get_payment_state(
                principal_id=principal_id,
                payment_attempt_id=commerce_result["payment_attempt"][
                    "payment_attempt_id"
                ],
            )
            current_status = payment_state["payment_attempt"]["status"]
            if (
                not created
                and current_status == "unknown"
                and intent.get("result") is not None
            ):
                return intent["result"]
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
                (
                    not created
                    and current_status
                    in {"succeeded", "reconciled", "failed", "unknown"}
                )
                or (
                    payment_outcome == "succeeded"
                    and current_status in {"succeeded", "reconciled"}
                )
                or current_status == payment_outcome
            ):
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

        payment_status = verified_result["payment_attempt"]["status"]
        if payment_status in {"succeeded", "reconciled"}:
            intent_status = "succeeded"
            receipt_status = "executed"
            receipt_outcome = "executed"
            reason_code = "EXECUTED_AND_VERIFIED"
            human_reason = "Payment and order state were verified."
            required_action = "none"
        elif payment_status == "failed":
            intent_status = "failed"
            receipt_status = "failed"
            receipt_outcome = "payment_failed"
            reason_code = "PAYMENT_FAILED"
            human_reason = "Payment failed. No successful purchase was recorded."
            required_action = "review"
        else:
            intent_status = "executing"
            receipt_status = "pending"
            receipt_outcome = "payment_unknown"
            reason_code = "PAYMENT_STATUS_UNKNOWN"
            human_reason = (
                "Payment status is unknown. The order is pending reconciliation."
            )
            required_action = "contact_support"

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
                "outcome": receipt_outcome,
                "created_at": _utcnow().isoformat(),
            }
        )
        response = {
            "schema_version": "2",
            "decision": "allow",
            "decision_id": decision_id,
            "policy_id": self.policy_id,
            "reason_code": reason_code,
            "human_reason": human_reason,
            "reason": human_reason,
            "required_action": required_action,
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
                status=intent_status,
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
                status=receipt_status,
                payload=receipt_payload,
            )
        if payment_status == "failed":
            raise AgentGuardConflict(human_reason)
        return response


__all__ = ["CheckoutOrchestrator"]
