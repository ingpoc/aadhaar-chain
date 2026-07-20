"""Deterministic simulated payment and reconciliation tests."""
from __future__ import annotations

from app.payment_adapter import PaymentAdapter


def test_payment_and_refund_success_are_idempotent() -> None:
    adapter = PaymentAdapter()
    payment = adapter.charge(
        idempotency_key="payment-success-1",
        amount_inr=1200,
        reference_id="order-1",
    )
    duplicate = adapter.charge(
        idempotency_key="payment-success-1",
        amount_inr=9999,
        reference_id="order-different",
    )
    refund = adapter.refund(
        idempotency_key="refund-success-1",
        payment_id=payment["payment_id"],
        amount_inr=1200,
    )
    duplicate_refund = adapter.refund(
        idempotency_key="refund-success-1",
        payment_id="different-payment",
        amount_inr=1,
    )

    assert payment["status"] == "succeeded"
    assert duplicate == payment
    assert refund["status"] == "succeeded"
    assert duplicate_refund == refund


def test_unknown_payment_reconciles_without_double_charge() -> None:
    adapter = PaymentAdapter()
    pending = adapter.charge(
        idempotency_key="payment-unknown-1",
        amount_inr=1200,
        mode="timeout",
        reference_id="order-unknown",
    )

    assert pending["status"] == "unknown"
    reconciled = adapter.reconcile(idempotency_key="payment-unknown-1")
    assert reconciled["status"] == "succeeded"
    assert reconciled["reconciled"] is True
    assert adapter.reconcile(idempotency_key="payment-unknown-1") == reconciled
    assert adapter.reconcile(idempotency_key="missing-payment") == {
        "status": "unknown",
        "reason": "payment_not_found",
    }
