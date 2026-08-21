"""Razorpay Test Mode against the existing CommerceV1 payment/ledger."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.commerce_v1 import CommerceV1
from app.commerce_v1_routes import router as commerce_v1_router
from app.persistence import ConnectionPool, MigrationRunner
from app.razorpay_test import (
    RazorpayTestClient,
    checkout_signature,
)
from app.session_auth import SESSION_COOKIE_NAME, create_principal_session_token
from config import settings


DATABASE_URL = os.getenv("DATABASE_URL")
MIGRATIONS = Path(__file__).parents[1] / "migrations"
KEY_ID = "rzp_test_gateway"
KEY_SECRET = "test_key_secret"
WEBHOOK_SECRET = "whsec_test"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="DATABASE_URL is required for PostgreSQL integration tests",
    ),
]


@pytest_asyncio.fixture
async def postgres_url() -> AsyncIterator[str]:
    assert DATABASE_URL is not None
    schema = f"razorpay_test_{uuid4().hex}"
    admin = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)
    try:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        yield make_conninfo(DATABASE_URL, options=f"-csearch_path={schema},public")
    finally:
        await admin.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )
        await admin.close()


@pytest_asyncio.fixture
async def commerce(
    postgres_url: str,
) -> AsyncIterator[tuple[CommerceV1, ConnectionPool]]:
    pool = ConnectionPool(postgres_url, min_size=0, max_size=8)
    await pool.open()
    await MigrationRunner(pool, MIGRATIONS).apply()
    try:
        yield CommerceV1(pool), pool
    finally:
        await pool.close()


def _enable_razorpay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "razorpay_key_id", KEY_ID)
    monkeypatch.setattr(settings, "razorpay_key_secret", KEY_SECRET)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", WEBHOOK_SECRET)


def _mock_client() -> tuple[RazorpayTestClient, list[dict], list[dict]]:
    orders: list[dict] = []
    refunds: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/orders"):
            body = json.loads(request.content)
            order = {
                "id": f"order_test_{len(orders) + 1}",
                "amount": body["amount"],
                "currency": body["currency"],
                "receipt": body.get("receipt"),
                "status": "created",
            }
            orders.append(order)
            return httpx.Response(200, json=order)
        if request.method == "POST" and request.url.path.endswith("/refund"):
            body = json.loads(request.content)
            refund = {
                "id": f"rfnd_test_{len(refunds) + 1}",
                "payment_id": request.url.path.split("/")[3],
                "amount": body["amount"],
                "status": "processed",
            }
            refunds.append(refund)
            return httpx.Response(200, json=refund)
        return httpx.Response(404, json={"error": "unmocked"})

    client = RazorpayTestClient(
        KEY_ID, KEY_SECRET, transport=httpx.MockTransport(handler)
    )
    return client, orders, refunds


async def _prepare_pending_order(
    service: CommerceV1, *, principal: str = "principal:buyer-1"
) -> dict:
    await service.upsert_inventory(
        seller_id="seller-1",
        sku="atta-2kg",
        title="Atta 2kg",
        unit_price_paise=12_500,
        available_quantity=10,
    )
    cart = await service.create_cart(principal_id=principal, seller_id="seller-1")
    cart = await service.set_cart_line(
        principal_id=principal,
        cart_id=cart["cart_id"],
        sku="atta-2kg",
        quantity=2,
        expected_version=cart["version"],
    )
    quote = await service.preview_checkout(
        principal_id=principal,
        cart_id=cart["cart_id"],
        expected_version=cart["version"],
    )
    return await service.prepare_checkout(
        principal_id=principal,
        quote_id=quote["quote_id"],
        idempotency_key="rzp-prepare-1",
    )


def _webhook_body(event: str, *, order_id: str, payment_id: str, amount: int) -> bytes:
    payload = {
        "entity": "event",
        "id": f"evt_{payment_id}",
        "event": event,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "captured" if event == "payment.captured" else "failed",
                }
            }
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


async def test_missing_keys_leave_simulated_execute_path(
    commerce: tuple[CommerceV1, ConnectionPool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, pool = commerce
    monkeypatch.setattr(settings, "razorpay_key_id", None)
    monkeypatch.setattr(settings, "razorpay_key_secret", None)
    prepared = await _prepare_pending_order(service)
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(commerce_v1_router)
    token = create_principal_session_token(
        principal_id="principal:buyer-1",
        audience="ondcbuyer",
        identity_provider="demo",
    )
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        config = await client.get(
            "/api/commerce/v1/payments/config",
            cookies={SESSION_COOKIE_NAME: token},
        )
        assert config.status_code == 200
        rail = config.json()["data"]["payment_rail"]
        assert rail["simulated"] is True
        assert "simulated" in config.json()["message"].lower()
        refused = await client.post(
            f"/api/commerce/v1/orders/{prepared['order']['order_id']}/razorpay/orders",
            cookies={SESSION_COOKIE_NAME: token},
        )
        assert refused.status_code == 503
    result = await service.record_payment_result(
        principal_id="principal:buyer-1",
        payment_attempt_id=prepared["payment_attempt"]["payment_attempt_id"],
        status="succeeded",
        detail={"simulated": True},
    )
    assert result["payment_attempt"]["status"] == "succeeded"
    assert result["order"]["status"] == "paid"
    assert (result["payment_attempt"].get("result") or {}).get("simulated") is True


async def test_create_confirm_webhook_idempotency_and_refund(
    commerce: tuple[CommerceV1, ConnectionPool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, pool = commerce
    _enable_razorpay(monkeypatch)
    mock_client, orders, refunds = _mock_client()
    monkeypatch.setattr(
        "app.commerce_v1.require_razorpay_client", lambda **_kwargs: mock_client
    )
    prepared = await _prepare_pending_order(service)
    order_id = prepared["order"]["order_id"]

    checkout = await service.create_razorpay_checkout_order(
        principal_id="principal:buyer-1",
        order_id=order_id,
        client=mock_client,
    )
    replay = await service.create_razorpay_checkout_order(
        principal_id="principal:buyer-1",
        order_id=order_id,
        client=mock_client,
    )
    assert len(orders) == 1
    assert checkout["razorpay"]["key"] == KEY_ID
    assert checkout["razorpay"]["amount"] == 25_000
    assert checkout["razorpay"]["currency"] == "INR"
    assert checkout["razorpay"]["order_id"] == "order_test_1"
    assert replay["razorpay"]["order_id"] == "order_test_1"
    assert "secret" not in json.dumps(checkout)

    signature = checkout_signature("order_test_1", "pay_test_1", KEY_SECRET)
    confirmed = await service.confirm_razorpay_checkout(
        principal_id="principal:buyer-1",
        order_id=order_id,
        razorpay_order_id="order_test_1",
        razorpay_payment_id="pay_test_1",
        razorpay_signature=signature,
    )
    assert confirmed["payment_attempt"]["status"] == "succeeded"
    assert confirmed["order"]["status"] == "paid"
    replay_confirm = await service.confirm_razorpay_checkout(
        principal_id="principal:buyer-1",
        order_id=order_id,
        razorpay_order_id="order_test_1",
        razorpay_payment_id="pay_test_1",
        razorpay_signature=signature,
    )
    assert replay_confirm["payment_attempt"]["status"] == "succeeded"

    body = _webhook_body(
        "payment.captured",
        order_id="order_test_1",
        payment_id="pay_test_1",
        amount=25_000,
    )
    signature_header = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    first_hook = await service.apply_razorpay_webhook(
        body=body, signature=signature_header, event_id="evt_pay_test_1"
    )
    second_hook = await service.apply_razorpay_webhook(
        body=body, signature=signature_header, event_id="evt_pay_test_1"
    )
    assert first_hook["payment_attempt"]["status"] == "succeeded"
    assert second_hook["duplicate"] is True
    async with pool.connection() as connection:
        ledger = await connection.execute(
            "SELECT COUNT(*) FROM commerce_ledger_transactions WHERE posting_type = 'payment'"
        )
        assert (await ledger.fetchone())[0] == 1

    refunded = await service.issue_refund(
        seller_id="seller-1",
        order_id=order_id,
        amount_paise=25_000,
        idempotency_key="refund-rzp-1",
        correlation_id="corr-refund-rzp-1",
    )
    assert refunded["refund"]["status"] == "succeeded"
    assert len(refunds) == 1
    replay_refund = await service.issue_refund(
        seller_id="seller-1",
        order_id=order_id,
        amount_paise=25_000,
        idempotency_key="refund-rzp-1",
        correlation_id="corr-refund-rzp-1",
    )
    assert replay_refund["refund"]["refund_id"] == refunded["refund"]["refund_id"]
    assert len(refunds) == 1


async def test_failed_webhook_is_idempotent_on_pending_order(
    commerce: tuple[CommerceV1, ConnectionPool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, pool = commerce
    _enable_razorpay(monkeypatch)
    mock_client, _orders, _refunds = _mock_client()
    prepared = await _prepare_pending_order(service, principal="principal:fail-buyer")
    order_id = prepared["order"]["order_id"]
    await service.create_razorpay_checkout_order(
        principal_id="principal:fail-buyer",
        order_id=order_id,
        client=mock_client,
    )
    body = _webhook_body(
        "payment.failed",
        order_id="order_test_1",
        payment_id="pay_failed_1",
        amount=25_000,
    )
    signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    first = await service.apply_razorpay_webhook(
        body=body, signature=signature, event_id="evt_pay_failed_1"
    )
    second = await service.apply_razorpay_webhook(
        body=body, signature=signature, event_id="evt_pay_failed_1"
    )
    assert first["payment_attempt"]["status"] == "failed"
    assert first["order"]["status"] == "payment_failed"
    assert second["duplicate"] is True
    async with pool.connection() as connection:
        ledger = await connection.execute("SELECT COUNT(*) FROM commerce_ledger_transactions")
        assert (await ledger.fetchone())[0] == 0


async def test_http_create_confirm_webhook_and_failed_signature(
    commerce: tuple[CommerceV1, ConnectionPool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, pool = commerce
    _enable_razorpay(monkeypatch)
    mock_client, _orders, _refunds = _mock_client()
    monkeypatch.setattr(
        "app.commerce_v1.require_razorpay_client", lambda **_kwargs: mock_client
    )
    prepared = await _prepare_pending_order(service, principal="principal:http-buyer")
    order_id = prepared["order"]["order_id"]

    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(commerce_v1_router)
    token = create_principal_session_token(
        principal_id="principal:http-buyer",
        audience="ondcbuyer",
        identity_provider="demo",
    )
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        config = await client.get(
            "/api/commerce/v1/payments/config",
            cookies={SESSION_COOKIE_NAME: token},
        )
        assert config.status_code == 200
        rail = config.json()["data"]["payment_rail"]
        assert rail["rail"] == "razorpay_test"
        assert rail["key_id"] == KEY_ID
        assert rail["simulated"] is False

        created = await client.post(
            f"/api/commerce/v1/orders/{order_id}/razorpay/orders",
            cookies={SESSION_COOKIE_NAME: token},
        )
        assert created.status_code == 200
        razorpay_order_id = created.json()["data"]["razorpay"]["order_id"]
        bad = await client.post(
            f"/api/commerce/v1/orders/{order_id}/razorpay/confirm",
            cookies={SESSION_COOKIE_NAME: token},
            json={
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": "pay_test_http",
                "razorpay_signature": "deadbeef",
            },
        )
        assert bad.status_code == 400

        good_sig = checkout_signature(razorpay_order_id, "pay_test_http", KEY_SECRET)
        confirmed = await client.post(
            f"/api/commerce/v1/orders/{order_id}/razorpay/confirm",
            cookies={SESSION_COOKIE_NAME: token},
            json={
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": "pay_test_http",
                "razorpay_signature": good_sig,
            },
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["data"]["payment_attempt"]["status"] == "succeeded"

        failed_body = _webhook_body(
            "payment.failed",
            order_id="order_unknown",
            payment_id="pay_miss",
            amount=25_000,
        )
        failed_sig = hmac.new(
            WEBHOOK_SECRET.encode(), failed_body, hashlib.sha256
        ).hexdigest()
        ignored = await client.post(
            "/api/commerce/v1/payments/razorpay/webhook",
            content=failed_body,
            headers={
                "X-Razorpay-Signature": failed_sig,
                "X-Razorpay-Event-Id": "evt_unknown",
                "Content-Type": "application/json",
            },
        )
        assert ignored.status_code == 200
        assert ignored.json()["data"]["ignored"] is True


async def test_live_keys_fail_closed_on_create_order(
    commerce: tuple[CommerceV1, ConnectionPool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, pool = commerce
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_live_nope")
    monkeypatch.setattr(settings, "razorpay_key_secret", "live_secret")
    monkeypatch.setattr(settings, "razorpay_webhook_secret", WEBHOOK_SECRET)
    prepared = await _prepare_pending_order(service)
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(commerce_v1_router)
    token = create_principal_session_token(
        principal_id="principal:buyer-1",
        audience="ondcbuyer",
        identity_provider="demo",
    )
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/commerce/v1/orders/{prepared['order']['order_id']}/razorpay/orders",
            cookies={SESSION_COOKIE_NAME: token},
        )
        assert response.status_code == 503
        assert "Live keys are refused" in response.json()["detail"]
