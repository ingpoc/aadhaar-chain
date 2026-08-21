"""Razorpay Test Mode payment rail for AgentGuard / ONDC Buyer (A7 sandbox).

Fail closed on Live keys. Never call api.razorpay.com with ``rzp_live_``.
When test keys are absent, callers keep the simulated CommerceV1 payment path.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from config import settings

RAZORPAY_TEST_KEY_PREFIX = "rzp_test_"
RAZORPAY_LIVE_KEY_PREFIX = "rzp_live_"
RAZORPAY_API_BASE = "https://api.razorpay.com/v1"
PROVIDER = "razorpay_test"
CURRENCY = "INR"


class RazorpayConfigError(RuntimeError):
    """Razorpay Test Mode is misconfigured; do not start the client."""

    status_code = 503


class RazorpayLiveKeyRefused(RazorpayConfigError):
    """Live keys are forbidden in this sandbox. Stay on rzp_test_ only."""


class RazorpayNotConfigured(RazorpayConfigError):
    """Test keys are missing; payment remains simulated."""


class RazorpaySignatureError(ValueError):
    """Checkout or webhook HMAC verification failed."""

    status_code = 400


class RazorpayApiError(RuntimeError):
    """Razorpay Test API rejected the request."""

    status_code = 502


def _trimmed(value: str | None) -> str:
    return str(value or "").strip()


def configured_key_id() -> str:
    return _trimmed(settings.razorpay_key_id)


def configured_key_secret() -> str:
    return _trimmed(settings.razorpay_key_secret)


def configured_webhook_secret() -> str:
    return _trimmed(settings.razorpay_webhook_secret)


def _refuse_live_or_unknown_key(key_id: str) -> None:
    if key_id.startswith(RAZORPAY_LIVE_KEY_PREFIX) or not key_id.startswith(
        RAZORPAY_TEST_KEY_PREFIX
    ):
        raise RazorpayLiveKeyRefused(
            "Razorpay Live keys are refused. A7 sandbox allows rzp_test_ only."
        )


def resolve_payment_rail() -> dict[str, Any]:
    """Describe the active payment rail without starting an HTTP client.

    Missing keys → simulated (public checkout keeps working).
    A present key that is not ``rzp_test_`` → fail closed, never simulate.
    """
    key_id = configured_key_id()
    if not key_id:
        return {
            "rail": "simulated",
            "simulated": True,
            "mode": None,
            "key_id": None,
            "currency": CURRENCY,
            "message": (
                "Razorpay Test Mode keys are not configured; payment is simulated."
            ),
        }
    _refuse_live_or_unknown_key(key_id)
    if not configured_key_secret():
        raise RazorpayConfigError(
            "RAZORPAY_KEY_SECRET is required when RAZORPAY_KEY_ID is set."
        )
    return {
        "rail": PROVIDER,
        "simulated": False,
        "mode": "test",
        "key_id": key_id,
        "currency": CURRENCY,
        "message": (
            "Razorpay Test Mode. Mock UPI/cards only. No live customer money."
        ),
    }


def razorpay_test_enabled() -> bool:
    return resolve_payment_rail()["rail"] == PROVIDER


def public_payment_config() -> dict[str, Any]:
    """Buyer SPA payload: public key id only, never the secret."""
    return resolve_payment_rail()


def checkout_signature(order_id: str, payment_id: str, secret: str) -> str:
    payload = f"{order_id}|{payment_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_checkout_signature(
    *,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
    secret: str | None = None,
) -> None:
    """Official Checkout verification: HMAC SHA256 of order_id|payment_id."""
    key_secret = _trimmed(secret) if secret is not None else configured_key_secret()
    if not key_secret:
        raise RazorpayConfigError("RAZORPAY_KEY_SECRET is required to verify checkout.")
    expected = checkout_signature(razorpay_order_id, razorpay_payment_id, key_secret)
    if not hmac.compare_digest(expected, _trimmed(razorpay_signature)):
        raise RazorpaySignatureError("invalid Razorpay checkout signature")


def verify_webhook_signature(
    *,
    body: bytes | str,
    signature: str,
    secret: str | None = None,
) -> None:
    """HMAC SHA256 of the raw webhook body with the webhook secret."""
    webhook_secret = (
        _trimmed(secret) if secret is not None else configured_webhook_secret()
    )
    if not webhook_secret:
        raise RazorpayConfigError(
            "RAZORPAY_WEBHOOK_SECRET is required to verify Razorpay webhooks."
        )
    message = body.encode("utf-8") if isinstance(body, str) else body
    expected = hmac.new(
        webhook_secret.encode("utf-8"), message, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, _trimmed(signature)):
        raise RazorpaySignatureError("invalid Razorpay webhook signature")


def require_webhook_secret() -> str:
    secret = configured_webhook_secret()
    if not secret:
        raise RazorpayConfigError(
            "RAZORPAY_WEBHOOK_SECRET is required to verify Razorpay webhooks."
        )
    return secret


class RazorpayTestClient:
    """Minimal Orders + Refunds client. Constructor refuses Live keys."""

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        *,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    ) -> None:
        trimmed_id = _trimmed(key_id)
        trimmed_secret = _trimmed(key_secret)
        if not trimmed_id:
            raise RazorpayNotConfigured(
                "RAZORPAY_KEY_ID is required to start the Razorpay Test client."
            )
        _refuse_live_or_unknown_key(trimmed_id)
        if not trimmed_secret:
            raise RazorpayConfigError(
                "RAZORPAY_KEY_SECRET is required to start the Razorpay Test client."
            )
        self.key_id = trimmed_id
        self._key_secret = trimmed_secret
        self._transport = transport

    @classmethod
    def from_settings(
        cls,
        *,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    ) -> RazorpayTestClient:
        rail = resolve_payment_rail()
        if rail["rail"] != PROVIDER:
            raise RazorpayNotConfigured(str(rail["message"]))
        return cls(
            configured_key_id(),
            configured_key_secret(),
            transport=transport,
        )

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=RAZORPAY_API_BASE,
            auth=(self.key_id, self._key_secret),
            transport=self._transport,
            timeout=20.0,
        )

    async def create_order(
        self,
        *,
        amount_paise: int,
        receipt: str,
        notes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if amount_paise <= 0:
            raise RazorpayApiError("Razorpay order amount must be positive paise")
        payload = {
            "amount": int(amount_paise),
            "currency": CURRENCY,
            "receipt": receipt[:40],
            "payment_capture": 1,
        }
        if notes:
            payload["notes"] = notes
        return await self._post("/orders", payload)

    async def refund(
        self,
        *,
        payment_id: str,
        amount_paise: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if amount_paise <= 0:
            raise RazorpayApiError("Razorpay refund amount must be positive paise")
        headers = {"X-Razorpay-Idempotency": idempotency_key[:64]}
        return await self._post(
            f"/payments/{payment_id}/refund",
            {"amount": int(amount_paise)},
            headers=headers,
        )

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        async with self._http() as client:
            response = await client.post(path, json=payload, headers=headers)
        if response.status_code >= 400:
            detail = response.text[:300]
            raise RazorpayApiError(
                f"Razorpay Test API {path} failed ({response.status_code}): {detail}"
            )
        body = response.json()
        if not isinstance(body, dict):
            raise RazorpayApiError("Razorpay Test API returned a non-object payload")
        return body


def require_razorpay_client(
    *,
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
) -> RazorpayTestClient:
    return RazorpayTestClient.from_settings(transport=transport)


def razorpay_payment_id_from_result(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    payment_id = result.get("razorpay_payment_id") or result.get("payment_id")
    value = _trimmed(str(payment_id) if payment_id is not None else "")
    return value or None


__all__ = [
    "CURRENCY",
    "PROVIDER",
    "RAZORPAY_API_BASE",
    "RAZORPAY_LIVE_KEY_PREFIX",
    "RAZORPAY_TEST_KEY_PREFIX",
    "RazorpayApiError",
    "RazorpayConfigError",
    "RazorpayLiveKeyRefused",
    "RazorpayNotConfigured",
    "RazorpaySignatureError",
    "RazorpayTestClient",
    "checkout_signature",
    "public_payment_config",
    "razorpay_payment_id_from_result",
    "razorpay_test_enabled",
    "require_razorpay_client",
    "require_webhook_secret",
    "resolve_payment_rail",
    "verify_checkout_signature",
    "verify_webhook_signature",
]
