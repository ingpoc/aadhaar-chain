from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.agentguard_routes import router as agentguard_router
from app.checkout_orchestrator import CheckoutOrchestrator
from app.commerce_v1 import CommerceV1
from app.persistence import ConnectionPool, MigrationRunner
from app.persistence.agentguard_repository import (
    AgentGuardConflict,
    AgentGuardRepository,
)
from app.receipt_signing import verify_receipt
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
async def isolated_database_url() -> AsyncIterator[str]:
    assert DATABASE_URL is not None
    schema = f"cf1_buyer_journey_{uuid4().hex}"
    admin = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)
    await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    database_url = make_conninfo(DATABASE_URL, options=f"-csearch_path={schema},public")
    migration_pool = ConnectionPool(database_url, min_size=0, max_size=4)
    await migration_pool.open()
    await MigrationRunner(migration_pool, MIGRATIONS).apply()
    await migration_pool.close()
    try:
        yield database_url
    finally:
        await admin.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )
        await admin.close()


async def _open_pool(database_url: str) -> ConnectionPool:
    pool = ConnectionPool(database_url, min_size=0, max_size=8)
    await pool.open()
    return pool


async def _confirmed_mandate(
    orchestrator: CheckoutOrchestrator,
    *,
    principal_id: str,
    max_order_paise: int,
) -> dict[str, Any]:
    compiled = await orchestrator.compile_mandate(
        principal_id=principal_id,
        limits={"max_order_paise": max_order_paise},
    )
    return await orchestrator.confirm_mandate(
        principal_id=principal_id,
        mandate_id=compiled["mandate"]["mandate_id"],
    )


async def _quote(
    commerce: CommerceV1,
    *,
    principal_id: str,
    suffix: str,
    unit_price_paise: int = 10_001,
    quantity: int = 2,
) -> dict[str, Any]:
    seller_id = f"seller-{suffix}"
    sku = f"sku-{suffix}"
    await commerce.upsert_inventory(
        seller_id=seller_id,
        sku=sku,
        title=f"CF1 item {suffix}",
        unit_price_paise=unit_price_paise,
        available_quantity=20,
    )
    cart = await commerce.create_cart(
        principal_id=principal_id,
        seller_id=seller_id,
        idempotency_key=f"cart-{suffix}",
    )
    cart = await commerce.set_cart_line(
        principal_id=principal_id,
        cart_id=cart["cart_id"],
        sku=sku,
        quantity=quantity,
        expected_version=cart["version"],
        idempotency_key=f"line-{suffix}",
    )
    return await commerce.preview_checkout(
        principal_id=principal_id,
        cart_id=cart["cart_id"],
        expected_version=cart["version"],
        idempotency_key=f"preview-{suffix}",
    )


async def test_cf1_exact_approval_pause_mutation_revoke_and_restart_recovery(
    isolated_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "principal:cf1-restart"
    pool = await _open_pool(isolated_database_url)
    orchestrator = CheckoutOrchestrator(pool)
    commerce = CommerceV1(pool)
    confirmed = await _confirmed_mandate(
        orchestrator,
        principal_id=principal_id,
        max_order_paise=15_000,
    )
    assert confirmed["mandate"]["status"] == "active"

    quote = await _quote(commerce, principal_id=principal_id, suffix="approved")
    assert quote["subtotal_paise"] == 20_002
    assert quote["landed_total_paise"] == 20_002
    decision = await orchestrator.evaluate_checkout(
        principal_id=principal_id,
        quote_id=quote["quote_id"],
    )
    assert decision["decision"] == "need_approval"
    assert decision["bound_action"]["landed_total_paise"] == 20_002
    assert decision["approval"]["request_hash"] == decision["request_hash"]

    agent_id = decision["agent"]["agent_id"]
    await orchestrator.set_agent_status(
        principal_id=principal_id,
        agent_id=agent_id,
        status="paused",
    )
    with pytest.raises(AgentGuardConflict, match="agent is paused"):
        await orchestrator.execute_checkout(
            principal_id=principal_id,
            quote_id=quote["quote_id"],
            decision_id=decision["decision_id"],
            approval_id=decision["approval"]["approval_id"],
            idempotency_key="execute-approved",
            correlation_id="correlation-approved",
        )
    await orchestrator.set_agent_status(
        principal_id=principal_id,
        agent_id=agent_id,
        status="active",
    )
    # Pause invalidates outstanding approval capability. Resuming authority does
    # not resurrect it; the exact quote must be evaluated again.
    decision = await orchestrator.evaluate_checkout(
        principal_id=principal_id,
        quote_id=quote["quote_id"],
    )

    mutated_quote = await _quote(
        commerce,
        principal_id=principal_id,
        suffix="mutated",
        unit_price_paise=10_002,
    )
    with pytest.raises(AgentGuardConflict, match="checkout no longer matches"):
        await orchestrator.execute_checkout(
            principal_id=principal_id,
            quote_id=mutated_quote["quote_id"],
            decision_id=decision["decision_id"],
            approval_id=decision["approval"]["approval_id"],
            idempotency_key="execute-mutated",
            correlation_id="correlation-mutated",
        )

    async def crash_before_receipt(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated process loss before receipt commit")

    with monkeypatch.context() as crash:
        crash.setattr(AgentGuardRepository, "record_receipt", crash_before_receipt)
        with pytest.raises(RuntimeError, match="simulated process loss"):
            await orchestrator.execute_checkout(
                principal_id=principal_id,
                quote_id=quote["quote_id"],
                decision_id=decision["decision_id"],
                approval_id=decision["approval"]["approval_id"],
                idempotency_key="execute-approved",
                correlation_id="correlation-approved",
            )
    await pool.close()

    restarted_pool = await _open_pool(isolated_database_url)
    try:
        restarted = CheckoutOrchestrator(restarted_pool)
        recovered = await restarted.execute_checkout(
            principal_id=principal_id,
            quote_id=quote["quote_id"],
            decision_id=decision["decision_id"],
            approval_id=decision["approval"]["approval_id"],
            idempotency_key="execute-approved",
            correlation_id="correlation-approved",
        )
        replay = await restarted.execute_checkout(
            principal_id=principal_id,
            quote_id=quote["quote_id"],
            decision_id=decision["decision_id"],
            approval_id=decision["approval"]["approval_id"],
            idempotency_key="execute-approved",
            correlation_id="correlation-approved",
        )
        assert recovered["result"]["order"]["status"] == "paid"
        assert replay["receipt"]["receipt_id"] == recovered["receipt"]["receipt_id"]
        assert verify_receipt(recovered["receipt"]) == {
            "valid": True,
            "reason": "verified",
            "issuer_key_id": recovered["receipt"]["issuer_key_id"],
        }

        tampered = {**recovered["receipt"], "outcome": "payment_unknown"}
        assert verify_receipt(tampered)["valid"] is False

        async with restarted_pool.connection() as connection:
            counts = await connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM commerce_orders),
                    (SELECT COUNT(*) FROM commerce_payment_attempts),
                    (SELECT COUNT(*) FROM commerce_ledger_transactions),
                    (SELECT COUNT(*) FROM agentguard_execution_intents),
                    (SELECT COUNT(*) FROM agentguard_receipts)
                """
            )
            assert await counts.fetchone() == (1, 1, 1, 1, 1)

        await restarted.set_agent_status(
            principal_id=principal_id,
            agent_id=agent_id,
            status="revoked",
        )
        with pytest.raises(AgentGuardConflict, match="agent is revoked"):
            await restarted.evaluate_checkout(
                principal_id=principal_id,
                quote_id=mutated_quote["quote_id"],
            )
    finally:
        await restarted_pool.close()


async def test_cf1_failed_unknown_and_reconciled_payment_truth(
    isolated_database_url: str,
) -> None:
    principal_id = "principal:cf1-payment-truth"
    pool = await _open_pool(isolated_database_url)
    try:
        orchestrator = CheckoutOrchestrator(pool)
        commerce = CommerceV1(pool)
        await _confirmed_mandate(
            orchestrator,
            principal_id=principal_id,
            max_order_paise=0,
        )

        unknown_quote = await _quote(
            commerce, principal_id=principal_id, suffix="unknown"
        )
        unknown_decision = await orchestrator.evaluate_checkout(
            principal_id=principal_id,
            quote_id=unknown_quote["quote_id"],
        )
        unknown = await orchestrator.execute_checkout(
            principal_id=principal_id,
            quote_id=unknown_quote["quote_id"],
            decision_id=unknown_decision["decision_id"],
            approval_id=unknown_decision["approval"]["approval_id"],
            idempotency_key="execute-unknown",
            correlation_id="correlation-unknown",
            payment_outcome="unknown",
        )
        assert unknown["reason_code"] == "PAYMENT_STATUS_UNKNOWN"
        assert unknown["required_action"] == "contact_support"
        assert unknown["receipt"]["outcome"] == "payment_unknown"
        assert unknown["result"]["order"]["status"] == "payment_unknown"

        replay_unknown = await orchestrator.execute_checkout(
            principal_id=principal_id,
            quote_id=unknown_quote["quote_id"],
            decision_id=unknown_decision["decision_id"],
            approval_id=unknown_decision["approval"]["approval_id"],
            idempotency_key="execute-unknown",
            correlation_id="correlation-unknown",
            payment_outcome="unknown",
        )
        assert (
            replay_unknown["receipt"]["receipt_id"] == unknown["receipt"]["receipt_id"]
        )

        reconciled_state = await commerce.reconcile_payment(
            principal_id=principal_id,
            payment_attempt_id=unknown["result"]["payment_attempt"][
                "payment_attempt_id"
            ],
            outcome="succeeded",
            detail={"proof": "cf1-reconciliation"},
        )
        assert reconciled_state["payment_attempt"]["status"] == "reconciled"
        reconciled = await orchestrator.execute_checkout(
            principal_id=principal_id,
            quote_id=unknown_quote["quote_id"],
            decision_id=unknown_decision["decision_id"],
            approval_id=unknown_decision["approval"]["approval_id"],
            idempotency_key="execute-unknown",
            correlation_id="correlation-unknown",
        )
        assert reconciled["reason_code"] == "EXECUTED_AND_VERIFIED"
        assert reconciled["result"]["payment_attempt"]["status"] == "reconciled"
        assert reconciled["result"]["order"]["status"] == "paid"

        failed_quote = await _quote(
            commerce, principal_id=principal_id, suffix="failed"
        )
        failed_decision = await orchestrator.evaluate_checkout(
            principal_id=principal_id,
            quote_id=failed_quote["quote_id"],
        )
        with pytest.raises(AgentGuardConflict, match="Payment failed"):
            await orchestrator.execute_checkout(
                principal_id=principal_id,
                quote_id=failed_quote["quote_id"],
                decision_id=failed_decision["decision_id"],
                approval_id=failed_decision["approval"]["approval_id"],
                idempotency_key="execute-failed",
                correlation_id="correlation-failed",
                payment_outcome="failed",
            )
        async with pool.connection() as connection:
            failure = await connection.execute(
                """
                SELECT
                    intent.status,
                    receipt.status,
                    receipt.payload->>'outcome',
                    payment.status,
                    orders.status
                FROM agentguard_execution_intents AS intent
                JOIN agentguard_receipts AS receipt ON receipt.intent_id = intent.intent_id
                JOIN commerce_payment_attempts AS payment
                  ON payment.principal_id = intent.principal_id
                 AND payment.payment_attempt_id::text =
                     receipt.payload->'result'->'payment_attempt'->>'payment_attempt_id'
                JOIN commerce_orders AS orders ON orders.order_id = payment.order_id
                WHERE intent.principal_id = %s AND intent.idempotency_key = %s
                """,
                (principal_id, "execute-failed"),
            )
            assert await failure.fetchone() == (
                "failed",
                "failed",
                "payment_failed",
                "failed",
                "payment_failed",
            )
    finally:
        await pool.close()


async def test_cf1_stored_receipt_verifies_for_owning_principal_only(
    isolated_database_url: str,
) -> None:
    principal_id = "principal:cf1-receipt-owner"
    pool = await _open_pool(isolated_database_url)
    try:
        orchestrator = CheckoutOrchestrator(pool)
        commerce = CommerceV1(pool)
        await _confirmed_mandate(
            orchestrator,
            principal_id=principal_id,
            max_order_paise=50_000,
        )
        quote = await _quote(commerce, principal_id=principal_id, suffix="receipt")
        decision = await orchestrator.evaluate_checkout(
            principal_id=principal_id,
            quote_id=quote["quote_id"],
        )
        executed = await orchestrator.execute_checkout(
            principal_id=principal_id,
            quote_id=quote["quote_id"],
            decision_id=decision["decision_id"],
            approval_id=None,
            idempotency_key="execute-receipt",
            correlation_id="correlation-receipt",
        )

        api = FastAPI()
        api.state.persistence_pool = pool
        api.include_router(agentguard_router)
        owner_cookie = create_principal_session_token(
            principal_id=principal_id,
            identity_provider="demo",
            display_name="CF1 receipt owner",
            audience="ondcbuyer",
        )
        other_cookie = create_principal_session_token(
            principal_id="principal:cf1-other",
            identity_provider="demo",
            display_name="CF1 other principal",
            audience="ondcbuyer",
        )
        async with AsyncClient(
            transport=ASGITransport(app=api), base_url="http://test"
        ) as client:
            verified = await client.post(
                "/api/agentguard/receipts/verify",
                cookies={SESSION_COOKIE_NAME: owner_cookie},
                json={"receipt_id": executed["receipt"]["receipt_id"]},
            )
            hidden = await client.post(
                "/api/agentguard/receipts/verify",
                cookies={SESSION_COOKIE_NAME: other_cookie},
                json={"receipt_id": executed["receipt"]["receipt_id"]},
            )

        assert verified.status_code == 200
        assert verified.json()["data"]["valid"] is True
        assert verified.json()["data"]["reason"] == "verified"
        assert hidden.status_code == 404
    finally:
        await pool.close()
