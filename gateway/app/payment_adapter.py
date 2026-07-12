"""Deterministic simulated payment adapter for the local commerce demo."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class PaymentAdapter:
    def __init__(self) -> None:
        self._payments: dict[str, dict[str, Any]] = {}
        self._refunds: dict[str, dict[str, Any]] = {}

    def charge(
        self,
        *,
        idempotency_key: str,
        amount_inr: int,
        mode: str = "success",
        reference_id: str,
    ) -> dict[str, Any]:
        if idempotency_key in self._payments:
            return self._payments[idempotency_key]

        status = {
            "success": "succeeded",
            "decline": "declined",
            "timeout": "unknown",
            "unknown": "unknown",
        }.get(mode, "unknown")
        result = {
            "adapter": "simulated_payment_v1",
            "payment_id": f"pay_{idempotency_key}",
            "reference_id": reference_id,
            "amount_inr": amount_inr,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._payments[idempotency_key] = result
        return result

    def reconcile(self, *, idempotency_key: str) -> dict[str, Any]:
        payment = self._payments.get(idempotency_key)
        if not payment:
            return {"status": "unknown", "reason": "payment_not_found"}
        if payment["status"] == "unknown":
            payment = {**payment, "status": "succeeded", "reconciled": True}
            self._payments[idempotency_key] = payment
        return payment

    def refund(
        self,
        *,
        idempotency_key: str,
        payment_id: str,
        amount_inr: int,
        mode: str = "success",
    ) -> dict[str, Any]:
        if idempotency_key in self._refunds:
            return self._refunds[idempotency_key]
        status = {
            "success": "succeeded",
            "decline": "declined",
            "timeout": "unknown",
            "unknown": "unknown",
        }.get(mode, "unknown")
        result = {
            "adapter": "simulated_payment_v1",
            "refund_id": f"refund_{idempotency_key}",
            "payment_id": payment_id,
            "amount_inr": amount_inr,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._refunds[idempotency_key] = result
        return result


payment_adapter = PaymentAdapter()
