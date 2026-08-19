"""PostgreSQL-owned AgentGuard policy and execution for Seller writes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import re
from typing import Any
from uuid import UUID, uuid4

from app import agentguard
from app.agentguard_contract import canonicalize
from app.commerce_compat import CommerceCompatibilityAdapter
from app.commerce_v1 import CommerceNotFound
from app.persistence.agentguard_repository import (
    AgentGuardConflict,
    AgentGuardNotFound,
    AgentGuardRepository,
)
from app.persistence.connection import ConnectionPool
from app.persistence.ondc_repository import ONDCRepository
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


def _duration_seconds(value: Any) -> Decimal:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+(?:\.\d+)?)D)?(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
        r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?",
        str(value or ""),
    )
    if not match or not any(match.groupdict().values()):
        return Decimal("Infinity")
    parts = {key: Decimal(number or "0") for key, number in match.groupdict().items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def _offer_rank(offer: dict[str, Any]) -> tuple[Decimal, Decimal, str, str, str]:
    try:
        price = Decimal(str((offer.get("price") or {}).get("value")))
    except (InvalidOperation, TypeError):
        price = Decimal("Infinity")
    if price < 0:
        price = Decimal("Infinity")
    return (
        price,
        _duration_seconds(offer.get("tat")),
        str(offer.get("bpp_id") or ""),
        str(offer.get("provider_id") or ""),
        str(offer.get("item_id") or ""),
    )


def _verified_logistics_offer(
    records: list[dict[str, Any]], *, bpp_id: str | None = None
) -> dict[str, Any]:
    """Select one signed LOG10 1.2.5 P2P Immediate Delivery offer."""
    offers: list[dict[str, Any]] = []
    for record in records:
        envelope = record.get("envelope") or {}
        context = envelope.get("context") or {}
        if (
            record.get("action") != "on_search"
            or (record.get("redacted_payload") or {}).get("signature_verified")
            is not True
            or context.get("domain") != "ONDC:LOG10"
            or context.get("core_version") != "1.2.5"
            or not context.get("bpp_id")
            or not context.get("bpp_uri")
            or context.get("bpp_id") != record.get("subscriber_id")
            or str(context.get("transaction_id") or "")
            != str(record.get("transaction_id") or "")
            or (bpp_id and context.get("bpp_id") != bpp_id)
        ):
            continue
        catalog = (envelope.get("message") or {}).get("catalog") or {}
        for provider in catalog.get("bpp/providers") or []:
            if not isinstance(provider, dict):
                continue
            fulfillments = {
                fulfillment.get("id"): fulfillment
                for fulfillment in provider.get("fulfillments") or []
                if isinstance(fulfillment, dict)
            }
            for item in provider.get("items") or []:
                if not isinstance(item, dict):
                    continue
                fulfillment = fulfillments.get(item.get("fulfillment_id")) or {}
                if (
                    (item.get("descriptor") or {}).get("code") != "P2P"
                    or item.get("category_id") != "Immediate Delivery"
                    or fulfillment.get("type") != "Delivery"
                ):
                    continue
                provider_name = (provider.get("descriptor") or {}).get(
                    "name"
                ) or fulfillment.get("@ondc/org/provider_name")
                if (
                    not provider_name
                    or not provider.get("id")
                    or not item.get("id")
                    or not item.get("fulfillment_id")
                ):
                    continue
                price = item.get("price")
                offers.append(
                    {
                        "transaction_id": str(record["transaction_id"]),
                        "bpp_id": str(context["bpp_id"]),
                        "bpp_uri": str(context.get("bpp_uri") or ""),
                        "provider_id": str(provider.get("id") or ""),
                        "provider_name": str(provider_name),
                        "item_id": str(item.get("id") or ""),
                        "fulfillment_id": str(item.get("fulfillment_id") or ""),
                        "core_version": "1.2.5",
                        "category_id": "Immediate Delivery",
                        "item_code": "P2P",
                        "price": dict(price) if isinstance(price, dict) else {},
                        "tat": (item.get("time") or {}).get("duration"),
                        "signature_verified": True,
                    }
                )
    if not offers:
        raise AgentGuardConflict(
            "no signed LOG10 1.2.5 P2P Immediate Delivery offer matched"
        )
    return min(offers, key=_offer_rank)


class SellerAgentGuardOrchestrator:
    """Own Seller mandates, decisions, approvals, intents, and receipts in PostgreSQL."""

    policy_id = "seller.ops.limit"
    default_actions = sorted(
        action
        for action in agentguard.AGENTGUARD_ACTIONS
        if action.startswith("seller.")
    )

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool
        self.commerce = CommerceCompatibilityAdapter(pool)

    async def _bind_logistics_offer(self, payload: dict[str, Any]) -> dict[str, Any]:
        transaction_id = str(payload.get("logistics_transaction_id") or "").strip()
        if not transaction_id:
            return payload
        async with UnitOfWork(self.pool) as unit_of_work:
            records = await ONDCRepository(unit_of_work).list_for_transaction(
                "inbox", transaction_id, action="on_search", limit=100
            )
        offer = _verified_logistics_offer(
            records,
            bpp_id=str(payload.get("logistics_bpp_id") or "").strip() or None,
        )
        return {
            **payload,
            "provider_name": offer["provider_name"],
            "logistics": offer,
        }

    @staticmethod
    def agent_id(principal_id: str) -> str:
        return f"agent_seller_{sha256(principal_id.encode()).hexdigest()[:20]}"

    @staticmethod
    def mandate_id(principal_id: str) -> str:
        return f"mandate_seller_{sha256(principal_id.encode()).hexdigest()[:20]}"

    @classmethod
    def _mandate_view(cls, mandate: dict[str, Any] | None) -> dict[str, Any] | None:
        if mandate is None:
            return None
        payload = mandate.get("payload") or {}
        limits = payload.get("auto_approve_max_inr") or {}
        return _jsonable(
            {
                **mandate,
                "template": "seller_ops_v1",
                "allowed_actions": payload.get("allowed_actions")
                or cls.default_actions,
                "limits": {"auto_approve_max_inr": limits},
            }
        )

    @classmethod
    def _policy_view(cls, mandate: dict[str, Any] | None) -> dict[str, Any]:
        payload = (mandate or {}).get("payload") or {}
        return {
            "policy_id": cls.policy_id,
            "role": "seller",
            "allowed_actions": payload.get("allowed_actions") or cls.default_actions,
            "auto_approve_max_inr": payload.get("auto_approve_max_inr") or {},
        }

    async def ensure_agent(self, *, principal_id: str) -> dict[str, Any]:
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
                    role="seller",
                    payload={"name": "Seller commerce agent"},
                )
            mandate = await repository.get_latest_mandate_for_agent(
                principal_id=principal_id, agent_id=agent_id
            )
            if mandate is None:
                await repository.create_mandate_version(
                    mandate_id=mandate_id,
                    version=1,
                    principal_id=principal_id,
                    agent_id=agent_id,
                    payload={
                        "allowed_actions": self.default_actions,
                        "auto_approve_max_inr": {"seller.refund.issue": 5_000},
                        "currency": "INR",
                    },
                )
                agent = await repository.get_agent(
                    principal_id=principal_id, agent_id=agent_id
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
            "policy": self._policy_view(mandate),
            "receipts": _jsonable(receipts),
        }

    async def compile_mandate(
        self,
        *,
        principal_id: str,
        limits: dict[str, Any],
        allowed_actions: list[str] | None,
    ) -> dict[str, Any]:
        current = await self.ensure_agent(principal_id=principal_id)
        agent_record = current["agent"]
        if agent_record["status"] == "revoked":
            raise AgentGuardConflict("agent is revoked")
        actions = allowed_actions or self.default_actions
        invalid = [
            action
            for action in actions
            if agentguard.normalize_action(action) != action
            or not action.startswith("seller.")
        ]
        if invalid:
            raise ValueError(f"unsupported Seller actions: {', '.join(invalid)}")
        automatic = limits.get("auto_approve_max_inr") or {}
        if not isinstance(automatic, dict):
            automatic = {"seller.refund.issue": int(automatic)}
        automatic = {str(key): int(value) for key, value in automatic.items()}
        if any(value < 0 for value in automatic.values()):
            raise ValueError("automatic approval limits must be non-negative")
        agent_id = self.agent_id(principal_id)
        mandate_id = self.mandate_id(principal_id)
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = AgentGuardRepository(unit_of_work)
            latest = await repository.get_latest_mandate_for_agent(
                principal_id=principal_id, agent_id=agent_id
            )
            mandate = await repository.create_mandate_version(
                mandate_id=mandate_id,
                version=int((latest or {}).get("version") or 0) + 1,
                principal_id=principal_id,
                agent_id=agent_id,
                payload={
                    "allowed_actions": actions,
                    "auto_approve_max_inr": automatic,
                    "currency": "INR",
                },
                status="draft",
                activate=False,
            )
        return {"agent": agent_record, "mandate": self._mandate_view(mandate)}

    async def confirm_mandate(
        self, *, principal_id: str, mandate_id: str
    ) -> dict[str, Any]:
        agent_id = self.agent_id(principal_id)
        if mandate_id != self.mandate_id(principal_id):
            raise AgentGuardNotFound("Seller mandate not found")
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = AgentGuardRepository(unit_of_work)
            agent = await repository.get_agent(
                principal_id=principal_id, agent_id=agent_id
            )
            if agent is None:
                raise AgentGuardNotFound("Seller mandate not found")
            latest = await repository.get_latest_mandate_for_agent(
                principal_id=principal_id, agent_id=agent_id
            )
            if latest is None or latest["mandate_id"] != mandate_id:
                raise AgentGuardNotFound("Seller mandate not found")
            if (
                latest["status"] == "active"
                and agent.get("current_mandate_version") == latest["version"]
            ):
                active = latest
            elif latest["status"] == "draft":
                active = await repository.create_mandate_version(
                    mandate_id=mandate_id,
                    version=latest["version"] + 1,
                    principal_id=principal_id,
                    agent_id=agent_id,
                    payload=latest["payload"],
                    status="active",
                    activate=True,
                )
            else:
                raise AgentGuardConflict("latest Seller mandate is not confirmable")
            agent = await repository.get_agent(
                principal_id=principal_id, agent_id=agent_id
            )
        return {"agent": _jsonable(agent), "mandate": self._mandate_view(active)}

    async def evaluate(
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
        if normalized is None or not normalized.startswith("seller."):
            raise ValueError("unsupported Seller protected action")
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = AgentGuardRepository(unit_of_work)
            agent = await repository.get_agent(
                principal_id=principal_id, agent_id=self.agent_id(principal_id)
            )
            if agent is None or agent.get("current_mandate_id") is None:
                raise AgentGuardNotFound("Seller mandate not found")
            mandate = await repository.get_mandate_version(
                principal_id=principal_id,
                mandate_id=agent["current_mandate_id"],
                version=agent["current_mandate_version"],
            )
            if mandate is None:
                raise AgentGuardNotFound("Seller mandate not found")
            mandate_payload = mandate.get("payload") or {}
            allowed_actions = mandate_payload.get("allowed_actions") or []
            status = "allow"
            reason_code = "within_policy"
            human_reason = "Seller action is within the confirmed mandate."
            if agent["status"] != "active":
                status = "deny"
                reason_code = f"agent_{agent['status']}"
                human_reason = f"Seller agent is {agent['status']}."
            elif normalized not in allowed_actions:
                status = "deny"
                reason_code = "action_not_allowed"
                human_reason = "Seller action is not in the confirmed mandate."
            limits = mandate_payload.get("auto_approve_max_inr") or {}
            threshold = int(
                limits.get(normalized, limits.get("seller.refund.issue", 0))
            )
            if status == "allow" and amount_inr > threshold:
                status = "need_approval"
                reason_code = "approval_required_amount"
                over_by = amount_inr - threshold
                human_reason = (
                    f"This refund is INR {over_by} above the automatic refund "
                    "limit and needs one-time approval before execution."
                    if normalized == "seller.refund.issue"
                    else (
                        "This Seller action exceeds the automatic mandate "
                        "limit and needs one-time approval before execution."
                    )
                )
            requires_approval = status == "need_approval"
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

    async def execute(
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
        if normalized is None or not normalized.startswith("seller."):
            raise ValueError("unsupported Seller protected action")
        if decision_id is None:
            evaluated = await self.evaluate(
                principal_id=principal_id,
                action=normalized,
                amount_inr=amount_inr,
                resource_id=resource_id,
                counterparty_id=None,
                payload=payload,
                correlation_id=correlation_id,
            )
            decision_id = evaluated["decision_id"]
            if evaluated["decision"] == "need_approval" and approval_id is None:
                return evaluated
            if evaluated["decision"] != "allow" and approval_id is None:
                await self._record_denied_receipt(
                    principal_id=principal_id,
                    agent=evaluated["agent"],
                    mandate=evaluated["mandate"],
                    decision_id=decision_id,
                    action=normalized,
                    amount_inr=amount_inr,
                    resource_id=resource_id,
                    payload=payload,
                    correlation_id=correlation_id,
                    reason_code=str(evaluated.get("reason_code") or "denied"),
                    human_reason=str(
                        evaluated.get("human_reason")
                        or "Seller decision denied the protected action"
                    ),
                    outcome=(
                        "paused"
                        if str(evaluated.get("reason_code") or "").endswith("paused")
                        else "denied"
                    ),
                )
                raise AgentGuardConflict("Seller decision denied the protected action")
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
                raise AgentGuardNotFound("Seller decision not found")
            if decision["status"] == "need_approval" and approval_id is None:
                raise AgentGuardConflict("exact Seller approval is required")
            if decision["status"] not in {"allow", "need_approval"}:
                raise AgentGuardConflict("Seller decision denied the protected action")
            if decision.get("expiry") and decision["expiry"] <= _utcnow():
                raise AgentGuardConflict("Seller decision expired")
            if (decision.get("payload") or {}).get("request_hash") != request_hash:
                raise AgentGuardConflict("Seller action changed after evaluation")
            agent = await repository.get_agent(
                principal_id=principal_id, agent_id=decision["agent_id"]
            )
            if agent is None or agent["status"] != "active":
                raise AgentGuardConflict(
                    f"Seller agent is {(agent or {}).get('status', 'missing')}"
                )
            if (
                agent.get("current_mandate_id"),
                agent.get("current_mandate_version"),
            ) != (decision["mandate_id"], decision["mandate_version"]):
                raise AgentGuardConflict("Seller decision mandate is stale")
            if approval_id is not None:
                approval = await repository.get_approval(
                    principal_id=principal_id, approval_id=approval_id
                )
                if approval is None or approval["decision_id"] != decision_id:
                    raise AgentGuardConflict(
                        "Seller approval does not match the decision"
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
            needs_exact_approval = (
                decision["status"] == "need_approval"
                or decision["required_action"] == "review"
            )
            if created and needs_exact_approval:
                if approval_id is None:
                    raise AgentGuardConflict("exact Seller approval is required")
                await repository.consume_approval(
                    principal_id=principal_id,
                    approval_id=approval_id,
                    request_hash=request_hash,
                )
            elif needs_exact_approval and approval_id is None:
                raise AgentGuardConflict("exact Seller approval is required")
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
                raise AgentGuardNotFound("Seller mandate not found")

        try:
            effect = await self._execute_effect(
                principal_id=principal_id,
                action=normalized,
                resource_id=resource_id,
                payload=payload,
                amount_inr=amount_inr,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
            )
        except (AgentGuardNotFound, KeyError, CommerceNotFound) as exc:
            if normalized != "seller.refund.issue":
                raise
            await self._fail_missing_order_refund(
                principal_id=principal_id,
                agent=agent,
                mandate=mandate,
                decision_id=decision_id,
                approval_id=approval_id,
                intent_id=intent["intent_id"],
                action=normalized,
                amount_inr=amount_inr,
                resource_id=resource_id,
                payload=payload,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            )
            raise AgentGuardNotFound("Seller order not found") from exc
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
            "human_reason": "Seller action executed under the confirmed mandate.",
            "reason": "Seller action executed under the confirmed mandate.",
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

    async def _execute_effect(
        self,
        *,
        principal_id: str,
        action: str,
        resource_id: str,
        payload: dict[str, Any],
        amount_inr: int,
        idempotency_key: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        if action in {"seller.catalog.publish", "seller.catalog.archive"}:
            operated = await self.commerce.list_operated_seller_ids(principal_id)
            try:
                current = await self.commerce.get_item(resource_id)
            except KeyError:
                if action == "seller.catalog.archive":
                    raise AgentGuardNotFound("Seller catalog item not found") from None
                current = await self.commerce.create_item(
                    {**payload, "item_id": resource_id, "seller_id": principal_id}
                )
            item = current.get("item", current)
            if item["seller_id"] not in operated:
                raise AgentGuardConflict("Seller does not own catalog item")
            return await self.commerce.publish_item(
                resource_id,
                status="published"
                if action == "seller.catalog.publish"
                else "archived",
            )
        if action in {"seller.price.change", "seller.inventory.commit"}:
            operated = await self.commerce.list_operated_seller_ids(principal_id)
            try:
                current = await self.commerce.get_item(resource_id)
            except KeyError:
                raise AgentGuardNotFound("Seller catalog item not found") from None
            item = current.get("item", current)
            if item["seller_id"] not in operated:
                raise AgentGuardConflict("Seller does not own catalog item")
            return await self.commerce.update_item(resource_id, payload)
        if action in {
            "seller.order.accept",
            "seller.order.reject",
            "seller.fulfilment.commit",
        }:
            operated = await self.commerce.list_operated_seller_ids(principal_id)
            try:
                current = await self.commerce.get_order(resource_id)
            except KeyError:
                raise AgentGuardNotFound("Seller order not found") from None
            if current["seller_id"] not in operated:
                raise AgentGuardConflict("Seller does not own order")
            if action == "seller.fulfilment.commit":
                payload = await self._bind_logistics_offer(payload)
            status = {
                "seller.order.accept": "confirmed",
                "seller.order.reject": "cancelled",
                "seller.fulfilment.commit": str(payload.get("status") or "shipped"),
            }[action]
            if (
                action == "seller.fulfilment.commit"
                and current["status"] == "preparing"
                and status == "preparing"
            ):
                return await self.commerce.rebind_rejected_logistics_provider(
                    resource_id, payload
                )
            return await self.commerce.transition_order(
                resource_id, status, payload=payload
            )
        if action == "seller.remedy.promise":
            operated = await self.commerce.list_operated_seller_ids(principal_id)
            issues = await self.commerce.list_issues(seller_ids=operated)
            if resource_id not in {issue["issue_id"] for issue in issues["issues"]}:
                raise AgentGuardNotFound("Seller issue not found")
            return await self.commerce.remedy_issue(resource_id, payload)
        if action == "seller.refund.issue":
            operated = await self.commerce.list_operated_seller_ids(principal_id)
            try:
                current = await self.commerce.get_order(resource_id)
            except (KeyError, CommerceNotFound):
                raise AgentGuardNotFound("Seller order not found") from None
            if current["seller_id"] not in operated:
                raise AgentGuardConflict("Seller does not own order")
            try:
                return await self.commerce.issue_refund(
                    resource_id,
                    seller_id=current["seller_id"],
                    amount_inr=amount_inr,
                    idempotency_key=idempotency_key,
                    correlation_id=correlation_id,
                )
            except (KeyError, CommerceNotFound) as exc:
                raise AgentGuardNotFound("Seller order not found") from exc
        raise ValueError("unsupported Seller protected action")

    async def _record_denied_receipt(
        self,
        *,
        principal_id: str,
        agent: dict[str, Any],
        mandate: dict[str, Any],
        decision_id: str,
        action: str,
        amount_inr: int,
        resource_id: str,
        payload: dict[str, Any],
        correlation_id: str,
        reason_code: str,
        human_reason: str,
        outcome: str,
        approval_id: str | None = None,
        intent_id: str | None = None,
    ) -> dict[str, Any]:
        receipt_id = f"receipt_{uuid4().hex}"
        receipt = sign_receipt(
            {
                "schema_version": "2",
                "receipt_id": receipt_id,
                "principal_id": principal_id,
                "action": action,
                "decision_id": decision_id,
                "approval_id": approval_id,
                "intent_id": intent_id,
                "correlation_id": correlation_id,
                "bound_action": {
                    "action": action,
                    "amount_inr": amount_inr,
                    "resource_id": resource_id,
                    "payload": payload,
                    "correlation_id": correlation_id,
                },
                "result": None,
                "outcome": outcome,
                "reason_code": reason_code,
                "human_reason": human_reason,
                "created_at": _utcnow().isoformat(),
            }
        )
        async with UnitOfWork(self.pool) as unit_of_work:
            await AgentGuardRepository(unit_of_work).record_receipt(
                receipt_id=receipt_id,
                principal_id=principal_id,
                agent_id=agent["agent_id"],
                mandate_id=mandate["mandate_id"],
                mandate_version=mandate["version"],
                decision_id=decision_id,
                approval_id=approval_id,
                intent_id=intent_id,
                status=outcome,
                payload=receipt,
            )
        return receipt

    async def _fail_missing_order_refund(
        self,
        *,
        principal_id: str,
        agent: dict[str, Any],
        mandate: dict[str, Any],
        decision_id: str,
        approval_id: str | None,
        intent_id: str,
        action: str,
        amount_inr: int,
        resource_id: str,
        payload: dict[str, Any],
        correlation_id: str,
        idempotency_key: str,
    ) -> None:
        receipt = await self._record_denied_receipt(
            principal_id=principal_id,
            agent=agent,
            mandate=mandate,
            decision_id=decision_id,
            action=action,
            amount_inr=amount_inr,
            resource_id=resource_id,
            payload=payload,
            correlation_id=correlation_id,
            reason_code="resource_not_found",
            human_reason="Seller order not found",
            outcome="denied",
            approval_id=approval_id,
            intent_id=intent_id,
        )
        async with UnitOfWork(self.pool) as unit_of_work:
            await AgentGuardRepository(unit_of_work).set_execution_intent_status(
                principal_id=principal_id,
                intent_id=intent_id,
                status="failed",
                result={
                    "decision": "deny",
                    "reason_code": "resource_not_found",
                    "human_reason": "Seller order not found",
                    "receipt": receipt,
                    "idempotency_key": idempotency_key,
                },
            )


__all__ = ["SellerAgentGuardOrchestrator"]
