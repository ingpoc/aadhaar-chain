from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.agentguard_routes import router as agentguard_router
from app.commerce_compat import CommerceCompatibilityAdapter
from app.commerce_routes import router as commerce_router
from app.commerce_v1 import CommerceV1
from app.commerce_v1_routes import router as commerce_v1_router
from app.persistence import ConnectionPool, MigrationRunner
from app.persistence.agentguard_repository import AgentGuardConflict, AgentGuardNotFound
from app.persistence.ondc_repository import persist_callback_before_ack
from app.seller_agentguard_orchestrator import SellerAgentGuardOrchestrator
from app.session_auth import SESSION_COOKIE_NAME, create_principal_session_token

DATABASE_URL = os.getenv("DATABASE_URL")
MIGRATIONS = Path(__file__).parents[1] / "migrations"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="DATABASE_URL is required for PostgreSQL integration tests",
    ),
]


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[ConnectionPool]:
    assert DATABASE_URL is not None
    schema = f"seller_agentguard_{uuid4().hex}"
    admin = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)
    await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    database_url = make_conninfo(DATABASE_URL, options=f"-csearch_path={schema},public")
    connection_pool = ConnectionPool(database_url, min_size=0, max_size=8)
    await connection_pool.open()
    await MigrationRunner(connection_pool, MIGRATIONS).apply()
    try:
        yield connection_pool
    finally:
        await connection_pool.close()
        await admin.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )
        await admin.close()


async def test_concurrent_seller_ensure_returns_one_durable_agent(
    pool: ConnectionPool,
) -> None:
    principal_id = "principal:seller:concurrent-bootstrap"
    first, second = await asyncio.gather(
        SellerAgentGuardOrchestrator(pool).ensure_agent(principal_id=principal_id),
        SellerAgentGuardOrchestrator(pool).ensure_agent(principal_id=principal_id),
    )

    assert first["agent"]["agent_id"] == second["agent"]["agent_id"]
    async with pool.connection() as connection:
        result = await connection.execute(
            "SELECT COUNT(*) FROM agentguard_agents WHERE principal_id = %s",
            (principal_id,),
        )
        assert await result.fetchone() == (1,)


async def test_seller_catalog_publish_and_archive_are_durable_exact_effects(
    pool: ConnectionPool,
) -> None:
    principal_id = "principal:seller:durable"
    orchestrator = SellerAgentGuardOrchestrator(pool)
    ensured = await orchestrator.ensure_agent(principal_id=principal_id)
    assert ensured["agent"]["role"] == "seller"
    assert ensured["mandate"]["status"] == "active"

    compiled = await orchestrator.compile_mandate(
        principal_id=principal_id,
        limits={"auto_approve_max_inr": {"seller.catalog.publish": 0}},
        allowed_actions=["seller.catalog.publish", "seller.catalog.archive"],
    )
    confirmed = await orchestrator.confirm_mandate(
        principal_id=principal_id,
        mandate_id=compiled["mandate"]["mandate_id"],
    )
    assert confirmed["mandate"]["status"] == "active"

    payload = {
        "title": "Durable Seller Atta",
        "price_inr": 91,
        "inventory": 4,
    }
    decision = await orchestrator.evaluate(
        principal_id=principal_id,
        action="seller.catalog.publish",
        amount_inr=0,
        resource_id="seller-durable-atta",
        counterparty_id=None,
        payload=payload,
        correlation_id="seller-correlation-1",
    )
    first = await orchestrator.execute(
        principal_id=principal_id,
        decision_id=decision["decision_id"],
        approval_id=None,
        action="seller.catalog.publish",
        amount_inr=0,
        resource_id="seller-durable-atta",
        idempotency_key="seller-publish-1",
        correlation_id="seller-correlation-1",
        payload=payload,
    )
    replay = await orchestrator.execute(
        principal_id=principal_id,
        decision_id=decision["decision_id"],
        approval_id=None,
        action="seller.catalog.publish",
        amount_inr=0,
        resource_id="seller-durable-atta",
        idempotency_key="seller-publish-1",
        correlation_id="seller-correlation-1",
        payload=payload,
    )

    assert first["receipt"]["receipt_id"] == replay["receipt"]["receipt_id"]
    assert first["result"]["item"]["status"] == "published"

    archive_decision = await orchestrator.evaluate(
        principal_id=principal_id,
        action="seller.catalog.archive",
        amount_inr=0,
        resource_id="seller-durable-atta",
        counterparty_id=None,
        payload={"item_id": "seller-durable-atta"},
        correlation_id="seller-archive-correlation-1",
    )
    archived = await orchestrator.execute(
        principal_id=principal_id,
        decision_id=archive_decision["decision_id"],
        approval_id=None,
        action="seller.catalog.archive",
        amount_inr=0,
        resource_id="seller-durable-atta",
        idempotency_key="seller-archive-1",
        correlation_id="seller-archive-correlation-1",
        payload={"item_id": "seller-durable-atta"},
    )
    archive_replay = await orchestrator.execute(
        principal_id=principal_id,
        decision_id=archive_decision["decision_id"],
        approval_id=None,
        action="seller.catalog.archive",
        amount_inr=0,
        resource_id="seller-durable-atta",
        idempotency_key="seller-archive-1",
        correlation_id="seller-archive-correlation-1",
        payload={"item_id": "seller-durable-atta"},
    )
    assert archived["receipt"]["receipt_id"] == archive_replay["receipt"]["receipt_id"]
    assert archived["result"]["item"]["status"] == "archived"

    async with pool.connection() as connection:
        result = await connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM commerce_inventory WHERE sku = %s),
                (SELECT COUNT(*) FROM agentguard_execution_intents),
                (SELECT COUNT(*) FROM agentguard_receipts)
            """,
            ("seller-durable-atta",),
        )
        assert await result.fetchone() == (1, 2, 2)

    with pytest.raises(AgentGuardConflict, match="changed after evaluation"):
        await orchestrator.execute(
            principal_id=principal_id,
            decision_id=decision["decision_id"],
            approval_id=None,
            action="seller.catalog.publish",
            amount_inr=0,
            resource_id="seller-durable-atta",
            idempotency_key="seller-publish-1",
            correlation_id="seller-correlation-1",
            payload={**payload, "price_inr": 99},
        )


async def test_seller_routes_use_postgres_and_require_write_contracts(
    pool: ConnectionPool,
) -> None:
    principal_id = "principal:seller:http"
    app = FastAPI()
    app.state.persistence_pool = pool
    app.include_router(agentguard_router)
    token = create_principal_session_token(
        principal_id=principal_id,
        audience="ondcseller",
        identity_provider="demo",
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set(SESSION_COOKIE_NAME, token)
        ensured = await client.post(
            "/api/agentguard/agents/ensure", json={"role": "seller"}
        )
        assert ensured.status_code == 200
        agent_id = ensured.json()["data"]["agent"]["agent_id"]

        missing_key = await client.post(
            "/api/agentguard/actions/execute",
            json={
                "action": "seller.catalog.publish",
                "amount_inr": 0,
                "resource_id": "http-item",
                "payload": {"title": "HTTP Item", "price_inr": 10, "inventory": 1},
            },
        )
        assert missing_key.status_code == 422

        missing_correlation = await client.post(
            "/api/agentguard/actions/execute",
            headers={"Idempotency-Key": "seller-http-write-1"},
            json={
                "action": "seller.catalog.publish",
                "amount_inr": 0,
                "resource_id": "http-item",
                "payload": {"title": "HTTP Item", "price_inr": 10, "inventory": 1},
            },
        )
        assert missing_correlation.status_code == 422
        assert "X-Correlation-ID" in missing_correlation.json()["detail"]

        paused = await client.post(f"/api/agentguard/agents/{agent_id}/pause", json={})
        assert paused.status_code == 200
        denied = await client.post(
            "/api/agentguard/actions/evaluate",
            headers={"X-Correlation-ID": "seller-http-correlation"},
            json={
                "action": "seller.catalog.publish",
                "amount_inr": 0,
                "resource_id": "http-item",
                "payload": {"title": "HTTP Item", "price_inr": 10, "inventory": 1},
            },
        )
        assert denied.status_code == 200
        assert denied.json()["data"]["decision"] == "deny"
        assert denied.headers["X-Correlation-ID"] == "seller-http-correlation"


async def test_seller_order_accept_uses_compatibility_order_shape(
    pool: ConnectionPool,
) -> None:
    seller_id = "principal:seller:accept"
    buyer_id = "principal:buyer:accept"
    commerce = CommerceV1(pool)
    await commerce.upsert_inventory(
        seller_id=seller_id,
        sku="accept-item",
        title="Accept item",
        unit_price_paise=5_000,
        available_quantity=2,
    )
    cart = await commerce.create_cart(
        principal_id=buyer_id,
        seller_id=seller_id,
        idempotency_key="accept-cart",
    )
    cart = await commerce.set_cart_line(
        principal_id=buyer_id,
        cart_id=cart["cart_id"],
        sku="accept-item",
        quantity=1,
        expected_version=cart["version"],
        idempotency_key="accept-line",
    )
    quote = await commerce.preview_checkout(
        principal_id=buyer_id,
        cart_id=cart["cart_id"],
        expected_version=cart["version"],
        idempotency_key="accept-preview",
    )
    prepared = await commerce.prepare_checkout(
        principal_id=buyer_id,
        quote_id=quote["quote_id"],
        idempotency_key="accept-prepare",
        request={"proof": "seller-accept"},
    )
    await commerce.record_payment_result(
        principal_id=buyer_id,
        payment_attempt_id=prepared["payment_attempt"]["payment_attempt_id"],
        status="succeeded",
        provider_reference="sandbox:accept-order",
    )

    orchestrator = SellerAgentGuardOrchestrator(pool)
    await orchestrator.ensure_agent(principal_id=seller_id)
    order_id = prepared["order"]["order_id"]
    decision = await orchestrator.evaluate(
        principal_id=seller_id,
        action="seller.order.accept",
        amount_inr=0,
        resource_id=order_id,
        counterparty_id=None,
        payload={"order_id": order_id},
        correlation_id="seller-accept-correlation",
    )
    executed = await orchestrator.execute(
        principal_id=seller_id,
        decision_id=decision["decision_id"],
        approval_id=None,
        action="seller.order.accept",
        amount_inr=0,
        resource_id=order_id,
        idempotency_key="seller-accept-effect",
        correlation_id="seller-accept-correlation",
        payload={"order_id": order_id},
    )

    assert executed["result"]["order"]["status"] == "confirmed"
    assert executed["result"]["order"]["seller_id"] == seller_id

    logistics_transaction_id = str(uuid4())
    await persist_callback_before_ack(
        pool,
        subscriber_id="preprod-bpp.taptap.in",
        transaction_id=logistics_transaction_id,
        message_id=str(uuid4()),
        action="on_search",
        correlation_id=logistics_transaction_id,
        raw_envelope={
            "context": {
                "domain": "ONDC:LOG10",
                "action": "on_search",
                "core_version": "1.2.5",
                "transaction_id": logistics_transaction_id,
                "bpp_id": "preprod-bpp.taptap.in",
                "bpp_uri": "https://preprod-bpp.taptap.in/ondc",
            },
            "message": {
                "catalog": {
                    "bpp/providers": [
                        {
                            "id": "P1",
                            "descriptor": {"name": "TapTap Logistics"},
                            "fulfillments": [{"id": "F1", "type": "Delivery"}],
                            "items": [
                                {
                                    "id": "I1",
                                    "descriptor": {
                                        "code": "P2P",
                                        "name": "Intercity Courier",
                                    },
                                    "category_id": "Immediate Delivery",
                                    "fulfillment_id": "F1",
                                    "price": {"currency": "INR", "value": "59.00"},
                                    "time": {"duration": "PT60M"},
                                }
                            ],
                        }
                    ]
                }
            },
        },
        redacted_payload={"signature_verified": True, "core_version": "1.2.5"},
    )
    await CommerceCompatibilityAdapter(pool).transition_order(order_id, "preparing")
    dispatch_payload = {
        "order_id": order_id,
        "status": "shipped",
        "tracking_id": "TAPTAP-123",
        "logistics_transaction_id": logistics_transaction_id,
    }
    dispatch_decision = await orchestrator.evaluate(
        principal_id=seller_id,
        action="seller.fulfilment.commit",
        amount_inr=0,
        resource_id=order_id,
        counterparty_id=None,
        payload=dispatch_payload,
        correlation_id="seller-dispatch-correlation",
    )
    dispatched = await orchestrator.execute(
        principal_id=seller_id,
        decision_id=dispatch_decision["decision_id"],
        approval_id=None,
        action="seller.fulfilment.commit",
        amount_inr=0,
        resource_id=order_id,
        idempotency_key="seller-dispatch-effect",
        correlation_id="seller-dispatch-correlation",
        payload=dispatch_payload,
    )

    fulfilment = dispatched["result"]["order"]["fulfilment"]
    assert fulfilment["provider_name"] == "TapTap Logistics"
    assert fulfilment["tracking_id"] == "TAPTAP-123"
    assert fulfilment["logistics"] == {
        "transaction_id": logistics_transaction_id,
        "bpp_id": "preprod-bpp.taptap.in",
        "bpp_uri": "https://preprod-bpp.taptap.in/ondc",
        "provider_id": "P1",
        "provider_name": "TapTap Logistics",
        "item_id": "I1",
        "fulfillment_id": "F1",
        "core_version": "1.2.5",
        "category_id": "Immediate Delivery",
        "item_code": "P2P",
        "price": {"currency": "INR", "value": "59.00"},
        "tat": "PT60M",
        "signature_verified": True,
    }


async def test_seller_refund_uses_one_durable_financial_effect(
    pool: ConnectionPool,
) -> None:
    seller_id = "principal:seller:refund"
    buyer_id = "principal:buyer:refund"
    commerce = CommerceV1(pool)
    await commerce.upsert_inventory(
        seller_id=seller_id,
        sku="refund-item",
        title="Refund item",
        unit_price_paise=10_000,
        available_quantity=2,
    )
    cart = await commerce.create_cart(
        principal_id=buyer_id,
        seller_id=seller_id,
        idempotency_key="refund-cart",
    )
    cart = await commerce.set_cart_line(
        principal_id=buyer_id,
        cart_id=cart["cart_id"],
        sku="refund-item",
        quantity=1,
        expected_version=cart["version"],
        idempotency_key="refund-line",
    )
    quote = await commerce.preview_checkout(
        principal_id=buyer_id,
        cart_id=cart["cart_id"],
        expected_version=cart["version"],
        idempotency_key="refund-preview",
    )
    prepared = await commerce.prepare_checkout(
        principal_id=buyer_id,
        quote_id=quote["quote_id"],
        idempotency_key="refund-prepare",
        request={"proof": "seller-refund"},
    )
    await commerce.record_payment_result(
        principal_id=buyer_id,
        payment_attempt_id=prepared["payment_attempt"]["payment_attempt_id"],
        status="succeeded",
        provider_reference="sandbox:refund-order",
    )

    orchestrator = SellerAgentGuardOrchestrator(pool)
    await orchestrator.ensure_agent(principal_id=seller_id)
    decision = await orchestrator.evaluate(
        principal_id=seller_id,
        action="seller.refund.issue",
        amount_inr=89,
        resource_id=prepared["order"]["order_id"],
        counterparty_id=None,
        payload={"order_id": prepared["order"]["order_id"]},
        correlation_id="seller-refund-correlation",
    )
    first = await orchestrator.execute(
        principal_id=seller_id,
        decision_id=decision["decision_id"],
        approval_id=None,
        action="seller.refund.issue",
        amount_inr=89,
        resource_id=prepared["order"]["order_id"],
        idempotency_key="seller-refund-effect",
        correlation_id="seller-refund-correlation",
        payload={"order_id": prepared["order"]["order_id"]},
    )
    replay = await orchestrator.execute(
        principal_id=seller_id,
        decision_id=decision["decision_id"],
        approval_id=None,
        action="seller.refund.issue",
        amount_inr=89,
        resource_id=prepared["order"]["order_id"],
        idempotency_key="seller-refund-effect",
        correlation_id="seller-refund-correlation",
        payload={"order_id": prepared["order"]["order_id"]},
    )

    assert first["receipt"]["receipt_id"] == replay["receipt"]["receipt_id"]
    assert first["result"]["refund"]["status"] == "succeeded"
    assert first["result"]["refund"]["amount_paise"] == 8_900
    projected = await CommerceCompatibilityAdapter(pool).get_order(
        prepared["order"]["order_id"]
    )
    assert projected["refunded_amount_inr"] == 89
    assert projected["refund_status"] == "succeeded"
    assert projected["refund_authorization"] == {
        "receipt_id": first["receipt"]["receipt_id"],
        "outcome": "succeeded",
        "amount_inr": 89,
        "recorded_at": first["receipt"]["created_at"],
    }
    async with pool.connection() as connection:
        counts = await connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM commerce_refunds),
                (SELECT COUNT(*) FROM commerce_ledger_transactions WHERE posting_type = 'refund'),
                (SELECT COUNT(*) FROM agentguard_execution_intents WHERE operation = 'seller.refund.issue'),
                (SELECT COUNT(*) FROM agentguard_receipts)
            """
        )
        assert await counts.fetchone() == (1, 1, 1, 1)


def _seller_cookie(principal_id: str) -> str:
    return create_principal_session_token(
        principal_id=principal_id,
        audience="ondcseller",
        identity_provider="auth0",
    )


def _buyer_cookie(principal_id: str) -> str:
    return create_principal_session_token(
        principal_id=principal_id,
        audience="ondcbuyer",
        identity_provider="auth0",
    )


async def test_postgres_seller_store_get_empty_is_200_not_404(
    pool: ConnectionPool,
) -> None:
    principal_id = "principal:auth0:store-empty"
    app = FastAPI()
    app.state.persistence_pool = pool
    app.include_router(commerce_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _seller_cookie(principal_id))
        empty = await client.get("/api/demo-commerce/seller/store")
        assert empty.status_code == 200, empty.text
        assert empty.json()["data"]["store"]["status"] == "draft"
        saved = await client.put(
            "/api/demo-commerce/seller/store",
            json={
                "store_name": "Durable Store",
                "city": "Hyderabad",
                "pin": "500001",
                "fulfilment_sla_hours": 12,
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"]["store"]["status"] == "ready"
        loaded = await client.get("/api/demo-commerce/seller/store")
        assert loaded.json()["data"]["store"]["store_name"] == "Durable Store"


async def test_same_principal_buyer_checkout_lists_on_seller_and_short_id(
    pool: ConnectionPool,
) -> None:
    principal_id = "principal:auth0:two-sided-pg"
    app = FastAPI()
    app.state.persistence_pool = pool
    app.include_router(commerce_router)
    app.include_router(commerce_v1_router)
    app.include_router(agentguard_router)
    seller_token = _seller_cookie(principal_id)
    buyer_token = _buyer_cookie(principal_id)
    sku = "loop-pg-atta"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set(SESSION_COOKIE_NAME, seller_token)
        ensure = await client.post(
            "/api/agentguard/agents/ensure", json={"role": "seller"}
        )
        assert ensure.status_code == 200, ensure.text
        published = await client.post(
            "/api/agentguard/actions/execute",
            headers={
                "Idempotency-Key": "pg-loop-publish",
                "X-Correlation-ID": "corr:pg-loop-publish",
            },
            json={
                "action": "seller.catalog.publish",
                "amount_inr": 0,
                "resource_id": sku,
                "payload": {
                    "title": "Sampoorna Whole Wheat Atta 1kg",
                    "price_inr": 89,
                    "inventory": 6,
                    "seller_name": "Sampoorna Groceries",
                },
            },
        )
        assert published.status_code == 200, published.text
        assert published.json()["data"]["decision"] == "allow"

        client.cookies.set(SESSION_COOKIE_NAME, buyer_token)
        compiled = await client.post(
            "/api/agentguard/mandates/compile",
            json={"role": "buyer", "limits": {"max_order_paise": 1_000_000}},
        )
        assert compiled.status_code == 200, compiled.text
        mandate_id = compiled.json()["data"]["mandate"]["mandate_id"]
        confirmed = await client.post(
            f"/api/agentguard/mandates/{mandate_id}/confirm", json={}
        )
        assert confirmed.status_code == 200, confirmed.text
        cart = await client.post(
            "/api/commerce/v1/carts",
            headers={"Idempotency-Key": "pg-loop-cart"},
            json={"seller_id": principal_id},
        )
        assert cart.status_code == 201, cart.text
        cart_body = cart.json()["data"]["cart"]
        line = await client.put(
            f"/api/commerce/v1/carts/{cart_body['cart_id']}/lines/{sku}",
            headers={"Idempotency-Key": "pg-loop-line"},
            json={"quantity": 2, "expected_version": cart_body["version"]},
        )
        assert line.status_code == 200, line.text
        cart_body = line.json()["data"]["cart"]
        preview = await client.post(
            f"/api/commerce/v1/carts/{cart_body['cart_id']}/checkout-preview",
            headers={"Idempotency-Key": "pg-loop-preview"},
            json={"expected_version": cart_body["version"]},
        )
        assert preview.status_code == 200, preview.text
        quote = preview.json()["data"]["quote"]
        evaluation = await client.post(
            "/api/agentguard/actions/evaluate",
            json={
                "action": "buyer.checkout.commit",
                "amount_inr": 178,
                "resource_id": quote["quote_id"],
                "payload": {"quote_id": quote["quote_id"]},
            },
        )
        assert evaluation.status_code == 200, evaluation.text
        decision = evaluation.json()["data"]
        checkout = await client.post(
            "/api/agentguard/actions/execute",
            headers={
                "Idempotency-Key": "pg-loop-checkout",
                "X-Correlation-ID": "corr:pg-loop-checkout",
            },
            json={
                "action": "buyer.checkout.commit",
                "amount_inr": 178,
                "resource_id": quote["quote_id"],
                "decision_id": decision["decision_id"],
                "payload": {
                    "quote_id": quote["quote_id"],
                    "payment_outcome": "succeeded",
                },
            },
        )
        assert checkout.status_code == 200, checkout.text
        order = checkout.json()["data"]["result"]["order"]
        order_id = order["order_id"]
        display_id = order.get("display_id") or order_id.replace("-", "")[:8].upper()

        client.cookies.set(SESSION_COOKIE_NAME, seller_token)
        listed = await client.get("/api/demo-commerce/seller/orders")
        assert listed.status_code == 200, listed.text
        ids = {row["order_id"] for row in listed.json()["data"]["orders"]}
        assert order_id in ids
        by_uuid = await client.get(f"/api/demo-commerce/seller/orders/{order_id}")
        assert by_uuid.status_code == 200, by_uuid.text
        by_short = await client.get(f"/api/demo-commerce/seller/orders/{display_id}")
        assert by_short.status_code == 200, by_short.text
        assert by_short.json()["data"]["order"]["order_id"] == order_id


async def test_seller_refund_over_limit_and_missing_order_are_server_enforced(
    pool: ConnectionPool,
) -> None:
    seller_id = "principal:auth0:refund-pg"
    buyer_id = "principal:auth0:refund-pg-buyer"
    commerce = CommerceV1(pool)
    await commerce.upsert_inventory(
        seller_id=seller_id,
        sku="refund-pg-item",
        title="Refund item",
        unit_price_paise=80_000,
        available_quantity=2,
    )
    cart = await commerce.create_cart(
        principal_id=buyer_id,
        seller_id=seller_id,
        idempotency_key="refund-pg-cart",
    )
    cart = await commerce.set_cart_line(
        principal_id=buyer_id,
        cart_id=cart["cart_id"],
        sku="refund-pg-item",
        quantity=1,
        expected_version=cart["version"],
        idempotency_key="refund-pg-line",
    )
    quote = await commerce.preview_checkout(
        principal_id=buyer_id,
        cart_id=cart["cart_id"],
        expected_version=cart["version"],
        idempotency_key="refund-pg-preview",
    )
    prepared = await commerce.prepare_checkout(
        principal_id=buyer_id,
        quote_id=quote["quote_id"],
        idempotency_key="refund-pg-prepare",
        request={"proof": "seller-refund-pg"},
    )
    await commerce.record_payment_result(
        principal_id=buyer_id,
        payment_attempt_id=prepared["payment_attempt"]["payment_attempt_id"],
        status="succeeded",
        provider_reference="sandbox:refund-pg",
    )
    order_id = str(prepared["order"]["order_id"])
    orchestrator = SellerAgentGuardOrchestrator(pool)
    await orchestrator.ensure_agent(principal_id=seller_id)

    over = await orchestrator.execute(
        principal_id=seller_id,
        decision_id=None,
        approval_id=None,
        action="seller.refund.issue",
        amount_inr=9000,
        resource_id=order_id,
        idempotency_key="refund-pg-over",
        correlation_id="corr-refund-pg-over",
        payload={"order_id": order_id},
    )
    assert over["decision"] == "need_approval"
    assert over["reason_code"] == "approval_required_amount"
    assert over.get("result") is None
    projected = await CommerceCompatibilityAdapter(pool).get_order(order_id)
    assert projected.get("refunded_amount_inr") in (0, 0.0, None)

    stepped = await orchestrator.execute(
        principal_id=seller_id,
        decision_id=None,
        approval_id=None,
        action="seller.refund.issue",
        amount_inr=6000,
        resource_id=order_id,
        idempotency_key="refund-pg-step",
        correlation_id="corr-refund-pg-step",
        payload={"order_id": order_id},
    )
    assert stepped["decision"] == "need_approval"
    approved = await orchestrator.execute(
        principal_id=seller_id,
        decision_id=stepped["decision_id"],
        approval_id=stepped["approval"]["approval_id"],
        action="seller.refund.issue",
        amount_inr=6000,
        resource_id=order_id,
        idempotency_key="refund-pg-approved",
        correlation_id="corr-refund-pg-step",
        payload={"order_id": order_id},
    )
    assert approved["decision"] == "allow"
    replay = await orchestrator.execute(
        principal_id=seller_id,
        decision_id=stepped["decision_id"],
        approval_id=stepped["approval"]["approval_id"],
        action="seller.refund.issue",
        amount_inr=6000,
        resource_id=order_id,
        idempotency_key="refund-pg-approved",
        correlation_id="corr-refund-pg-step",
        payload={"order_id": order_id},
    )
    assert replay["receipt"]["receipt_id"] == approved["receipt"]["receipt_id"]

    with pytest.raises(AgentGuardNotFound, match="Seller order not found"):
        await orchestrator.execute(
            principal_id=seller_id,
            decision_id=None,
            approval_id=None,
            action="seller.refund.issue",
            amount_inr=1000,
            resource_id="7BA6FE24",
            idempotency_key="refund-pg-missing",
            correlation_id="corr-refund-pg-missing",
            payload={"order_id": "7BA6FE24"},
        )
    activity = await orchestrator.ensure_agent(principal_id=seller_id)
    refund_receipts = [
        row
        for row in activity["receipts"]
        if row.get("action") == "seller.refund.issue"
    ]
    assert any(row.get("outcome") == "succeeded" for row in refund_receipts)
    assert any(row.get("outcome") == "denied" for row in refund_receipts)

    from app.checkout_orchestrator import CheckoutOrchestrator

    await CheckoutOrchestrator(pool).set_agent_status(
        principal_id=seller_id,
        agent_id=activity["agent"]["agent_id"],
        status="paused",
    )
    with pytest.raises(AgentGuardConflict, match="Seller decision denied"):
        await orchestrator.execute(
            principal_id=seller_id,
            decision_id=None,
            approval_id=None,
            action="seller.refund.issue",
            amount_inr=100,
            resource_id=order_id,
            idempotency_key="refund-pg-paused",
            correlation_id="corr-refund-pg-paused",
            payload={"order_id": order_id},
        )
    paused_activity = await orchestrator.ensure_agent(principal_id=seller_id)
    assert any(
        row.get("action") == "seller.refund.issue" and row.get("outcome") == "paused"
        for row in paused_activity["receipts"]
    )
