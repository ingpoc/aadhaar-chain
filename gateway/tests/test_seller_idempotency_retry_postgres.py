"""Seller protected-action idempotency must release failed or stale attempts.

Preprod defect (2026-10-06): a dispatch whose effect raised left its
execution intent ``executing`` forever, so a corrected retry under the same
Seller idempotency key failed with "idempotency key was reused with a
different request hash".
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.commerce_compat import CommerceCompatibilityAdapter
from app.commerce_v1 import CommerceV1
from app.persistence import ConnectionPool, MigrationRunner
from app.persistence.agentguard_repository import AgentGuardConflict
from app.persistence.ondc_repository import persist_callback_before_ack
from app.seller_agentguard_orchestrator import SellerAgentGuardOrchestrator

DATABASE_URL = os.getenv("DATABASE_URL")
MIGRATIONS = Path(__file__).parents[1] / "migrations"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="DATABASE_URL is required for PostgreSQL integration tests",
    ),
]

DISPATCH_KEY_PREFIX = "seller.fulfilment.commit:"


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[ConnectionPool]:
    assert DATABASE_URL is not None
    schema = f"seller_idem_retry_{uuid4().hex}"
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


async def _preparing_order(pool: ConnectionPool, tag: str) -> tuple[str, str]:
    seller_id = f"principal:seller:{tag}"
    buyer_id = f"principal:buyer:{tag}"
    commerce = CommerceV1(pool)
    await commerce.upsert_inventory(
        seller_id=seller_id,
        sku=f"{tag}-item",
        title="Retry item",
        unit_price_paise=5_000,
        available_quantity=2,
    )
    cart = await commerce.create_cart(
        principal_id=buyer_id, seller_id=seller_id, idempotency_key=f"{tag}-cart"
    )
    cart = await commerce.set_cart_line(
        principal_id=buyer_id,
        cart_id=cart["cart_id"],
        sku=f"{tag}-item",
        quantity=1,
        expected_version=cart["version"],
        idempotency_key=f"{tag}-line",
    )
    quote = await commerce.preview_checkout(
        principal_id=buyer_id,
        cart_id=cart["cart_id"],
        expected_version=cart["version"],
        idempotency_key=f"{tag}-preview",
    )
    prepared = await commerce.prepare_checkout(
        principal_id=buyer_id,
        quote_id=quote["quote_id"],
        idempotency_key=f"{tag}-prepare",
        request={"proof": tag},
    )
    await commerce.record_payment_result(
        principal_id=buyer_id,
        payment_attempt_id=prepared["payment_attempt"]["payment_attempt_id"],
        status="succeeded",
        provider_reference=f"sandbox:{tag}",
    )
    order_id = str(prepared["order"]["order_id"])
    orchestrator = SellerAgentGuardOrchestrator(pool)
    await orchestrator.ensure_agent(principal_id=seller_id)
    await orchestrator.execute(
        principal_id=seller_id,
        decision_id=None,
        approval_id=None,
        action="seller.order.accept",
        amount_inr=0,
        resource_id=order_id,
        idempotency_key=f"seller.order.accept:{order_id}",
        correlation_id=f"seller-protected:seller.order.accept:{order_id}",
        payload={"order_id": order_id},
    )
    await CommerceCompatibilityAdapter(pool).transition_order(order_id, "preparing")
    return seller_id, order_id


async def _signed_logistics_offer(
    pool: ConnectionPool, transaction_id: str | None = None
) -> str:
    transaction_id = transaction_id or str(uuid4())
    await persist_callback_before_ack(
        pool,
        subscriber_id="preprod-bpp.taptap.in",
        transaction_id=transaction_id,
        message_id=str(uuid4()),
        action="on_search",
        correlation_id=transaction_id,
        raw_envelope={
            "context": {
                "domain": "ONDC:LOG10",
                "action": "on_search",
                "core_version": "1.2.5",
                "transaction_id": transaction_id,
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
                                    "descriptor": {"code": "P2P", "name": "Courier"},
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
    return transaction_id


async def _dispatch(
    orchestrator: SellerAgentGuardOrchestrator,
    *,
    seller_id: str,
    order_id: str,
    payload_extra: dict,
) -> dict:
    """Mirror the Seller app: fixed key per order, correlation derived from it."""
    key = f"{DISPATCH_KEY_PREFIX}{order_id}"
    return await orchestrator.execute(
        principal_id=seller_id,
        decision_id=None,
        approval_id=None,
        action="seller.fulfilment.commit",
        amount_inr=0,
        resource_id=order_id,
        idempotency_key=key,
        correlation_id=f"seller-protected:{key}",
        payload={
            "order_id": order_id,
            "status": "shipped",
            "tracking_id": "TRK-RETRY-1",
            "status_message": "The seller dispatched this order to the courier.",
            **payload_extra,
        },
    )


async def _intent(pool: ConnectionPool, seller_id: str, order_id: str) -> tuple:
    async with pool.connection() as connection:
        result = await connection.execute(
            """
            SELECT status, result IS NOT NULL
            FROM agentguard_execution_intents
            WHERE principal_id = %s AND operation = 'seller.fulfilment.commit'
              AND idempotency_key = %s
            """,
            (seller_id, f"{DISPATCH_KEY_PREFIX}{order_id}"),
        )
        return await result.fetchone()


async def _order_status(pool: ConnectionPool, order_id: str) -> str:
    order = await CommerceCompatibilityAdapter(pool).get_order(order_id)
    return str(order["status"])


async def test_failed_dispatch_releases_key_so_corrected_retry_succeeds(
    pool: ConnectionPool,
) -> None:
    seller_id, order_id = await _preparing_order(pool, "retry-failed")
    orchestrator = SellerAgentGuardOrchestrator(pool)

    with pytest.raises(AgentGuardConflict, match="offer matched"):
        await _dispatch(
            orchestrator,
            seller_id=seller_id,
            order_id=order_id,
            payload_extra={"logistics_transaction_id": str(uuid4())},
        )
    assert await _intent(pool, seller_id, order_id) == ("failed", True)
    assert await _order_status(pool, order_id) == "preparing"

    corrected = await _dispatch(
        orchestrator,
        seller_id=seller_id,
        order_id=order_id,
        payload_extra={"provider_name": "Delhivery"},
    )
    assert corrected["decision"] == "allow"
    assert corrected["result"]["order"]["status"] == "shipped"
    assert await _intent(pool, seller_id, order_id) == ("succeeded", True)

    # A true duplicate of the successful request replays the same receipt.
    duplicate = await _dispatch(
        orchestrator,
        seller_id=seller_id,
        order_id=order_id,
        payload_extra={"provider_name": "Delhivery"},
    )
    assert duplicate["receipt"]["receipt_id"] == corrected["receipt"]["receipt_id"]

    # A different request after success is still a key-reuse conflict.
    with pytest.raises(AgentGuardConflict, match="different request hash"):
        await _dispatch(
            orchestrator,
            seller_id=seller_id,
            order_id=order_id,
            payload_extra={"provider_name": "Shadowfax"},
        )


async def test_failed_dispatch_same_request_retry_also_succeeds(
    pool: ConnectionPool,
) -> None:
    seller_id, order_id = await _preparing_order(pool, "retry-same")
    orchestrator = SellerAgentGuardOrchestrator(pool)
    logistics_transaction_id = str(uuid4())
    with pytest.raises(AgentGuardConflict, match="offer matched"):
        await _dispatch(
            orchestrator,
            seller_id=seller_id,
            order_id=order_id,
            payload_extra={"logistics_transaction_id": logistics_transaction_id},
        )
    assert await _intent(pool, seller_id, order_id) == ("failed", True)

    # The signed offer arrives later; the identical request must now execute.
    await _signed_logistics_offer(pool, logistics_transaction_id)
    retried = await _dispatch(
        orchestrator,
        seller_id=seller_id,
        order_id=order_id,
        payload_extra={"logistics_transaction_id": logistics_transaction_id},
    )
    assert retried["result"]["order"]["status"] == "shipped"
    assert await _intent(pool, seller_id, order_id) == ("succeeded", True)


async def test_stale_executing_dispatch_is_reclaimed_by_corrected_retry(
    pool: ConnectionPool,
) -> None:
    """The preprod ACEA7CE1 shape: intent stuck executing, result NULL, hours old."""
    seller_id, order_id = await _preparing_order(pool, "retry-stale")
    orchestrator = SellerAgentGuardOrchestrator(pool)
    with pytest.raises(AgentGuardConflict, match="offer matched"):
        await _dispatch(
            orchestrator,
            seller_id=seller_id,
            order_id=order_id,
            payload_extra={"logistics_transaction_id": str(uuid4())},
        )
    async with pool.connection() as connection:
        await connection.execute(
            """
            UPDATE agentguard_execution_intents
            SET status = 'executing', result = NULL,
                updated_at = NOW() - INTERVAL '6 hours'
            WHERE principal_id = %s AND idempotency_key = %s
            """,
            (seller_id, f"{DISPATCH_KEY_PREFIX}{order_id}"),
        )
        await connection.commit()

    offer = await _signed_logistics_offer(pool)
    corrected = await _dispatch(
        orchestrator,
        seller_id=seller_id,
        order_id=order_id,
        payload_extra={"logistics_transaction_id": offer},
    )
    assert corrected["result"]["order"]["status"] == "shipped"
    assert corrected["result"]["order"]["fulfilment"]["provider_name"] == (
        "TapTap Logistics"
    )
    assert await _intent(pool, seller_id, order_id) == ("succeeded", True)


async def test_fresh_in_flight_dispatch_still_conflicts_on_different_request(
    pool: ConnectionPool,
) -> None:
    seller_id, order_id = await _preparing_order(pool, "retry-fresh")
    orchestrator = SellerAgentGuardOrchestrator(pool)
    with pytest.raises(AgentGuardConflict, match="offer matched"):
        await _dispatch(
            orchestrator,
            seller_id=seller_id,
            order_id=order_id,
            payload_extra={"logistics_transaction_id": str(uuid4())},
        )
    async with pool.connection() as connection:
        await connection.execute(
            """
            UPDATE agentguard_execution_intents
            SET status = 'executing', result = NULL, updated_at = NOW()
            WHERE principal_id = %s AND idempotency_key = %s
            """,
            (seller_id, f"{DISPATCH_KEY_PREFIX}{order_id}"),
        )
        await connection.commit()

    with pytest.raises(AgentGuardConflict, match="different request hash"):
        await _dispatch(
            orchestrator,
            seller_id=seller_id,
            order_id=order_id,
            payload_extra={"provider_name": "Delhivery"},
        )
    assert await _order_status(pool, order_id) == "preparing"
