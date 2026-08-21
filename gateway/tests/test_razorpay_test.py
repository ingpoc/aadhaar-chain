"""Razorpay Test Mode rail: HMAC verify, live-key refusal, simulated fallback."""
from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from app.payment_adapter import PaymentAdapter
from app.razorpay_test import (
    RAZORPAY_API_BASE,
    RazorpayLiveKeyRefused,
    RazorpayNotConfigured,
    RazorpaySignatureError,
    RazorpayTestClient,
    checkout_signature,
    public_payment_config,
    resolve_payment_rail,
    verify_checkout_signature,
    verify_webhook_signature,
)
from config import settings


def test_checkout_signature_accepts_official_hmac() -> None:
    secret = "test_key_secret"
    order_id = "order_test_abc"
    payment_id = "pay_test_xyz"
    signature = checkout_signature(order_id, payment_id, secret)
    verify_checkout_signature(
        razorpay_order_id=order_id,
        razorpay_payment_id=payment_id,
        razorpay_signature=signature,
        secret=secret,
    )


def test_checkout_signature_rejects_tampered_payload() -> None:
    secret = "test_key_secret"
    signature = checkout_signature("order_test_abc", "pay_test_xyz", secret)
    with pytest.raises(RazorpaySignatureError, match="checkout signature"):
        verify_checkout_signature(
            razorpay_order_id="order_test_abc",
            razorpay_payment_id="pay_FORGED",
            razorpay_signature=signature,
            secret=secret,
        )


def test_webhook_signature_accepts_raw_body_hmac() -> None:
    secret = "whsec_test"
    body = b'{"event":"payment.captured","payload":{}}'
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    verify_webhook_signature(body=body, signature=signature, secret=secret)


def test_webhook_signature_rejects_parsed_or_wrong_secret() -> None:
    secret = "whsec_test"
    body = b'{"event":"payment.captured"}'
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    with pytest.raises(RazorpaySignatureError, match="webhook signature"):
        verify_webhook_signature(
            body=body, signature=signature, secret="other_secret"
        )
    parsed = json.dumps(json.loads(body), indent=2).encode()
    with pytest.raises(RazorpaySignatureError, match="webhook signature"):
        verify_webhook_signature(body=parsed, signature=signature, secret=secret)


def test_live_key_refuses_client_before_any_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_live_not_allowed")
    monkeypatch.setattr(settings, "razorpay_key_secret", "live_secret")

    with pytest.raises(RazorpayLiveKeyRefused, match="Live keys are refused"):
        resolve_payment_rail()
    with pytest.raises(RazorpayLiveKeyRefused, match="Live keys are refused"):
        RazorpayTestClient("rzp_live_not_allowed", "live_secret")
    with pytest.raises(RazorpayLiveKeyRefused):
        RazorpayTestClient("rzp_prod_other", "secret")


def test_missing_keys_keep_simulated_payment_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "razorpay_key_id", None)
    monkeypatch.setattr(settings, "razorpay_key_secret", None)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", None)

    rail = resolve_payment_rail()
    assert rail["rail"] == "simulated"
    assert rail["simulated"] is True
    assert rail["key_id"] is None
    assert "simulated" in rail["message"].lower()
    config = public_payment_config()
    assert "key_secret" not in config
    assert config["key_id"] is None

    adapter = PaymentAdapter()
    payment = adapter.charge(
        idempotency_key="sim-fallback-1",
        amount_inr=500,
        reference_id="order-sim",
    )
    assert payment["adapter"] == "simulated_payment_v1"
    assert payment["status"] == "succeeded"

    with pytest.raises(RazorpayNotConfigured):
        RazorpayTestClient.from_settings()


@pytest.mark.asyncio
async def test_test_client_creates_inr_paise_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(RAZORPAY_API_BASE)
        payload = json.loads(request.content)
        assert payload["amount"] == 25_000
        assert payload["currency"] == "INR"
        return httpx.Response(
            200,
            json={
                "id": "order_test_1",
                "amount": 25_000,
                "currency": "INR",
                "status": "created",
            },
        )

    client = RazorpayTestClient(
        "rzp_test_abc",
        "test_secret",
        transport=httpx.MockTransport(handler),
    )
    created = await client.create_order(
        amount_paise=25_000, receipt="attempt-1", notes={"commerce_order_id": "x"}
    )
    assert created["id"] == "order_test_1"
