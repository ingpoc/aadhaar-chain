from __future__ import annotations

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
from app.commerce_v1 import CommerceV1
from app.persistence import ConnectionPool, MigrationRunner
from app.persistence.agentguard_repository import AgentGuardConflict
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


async def test_seller_catalog_publish_is_durable_exact_and_one_effect(
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
        allowed_actions=["seller.catalog.publish"],
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
        assert await result.fetchone() == (1, 1, 1)

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
