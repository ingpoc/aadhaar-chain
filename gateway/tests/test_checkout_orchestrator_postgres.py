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

from app.checkout_orchestrator import CheckoutOrchestrator
from app.commerce_v1 import CommerceV1
from app.agentguard_routes import router as agentguard_router
from app.commerce_v1_routes import router as commerce_v1_router
from app.persistence import ConnectionPool, MigrationRunner
from app.persistence.agentguard_repository import (
    AgentGuardConflict,
    AgentGuardNotFound,
    AgentGuardRepository,
)
from app.receipt_signing import verify_receipt
from app.session_auth import SESSION_COOKIE_NAME, create_principal_session_token
from config import settings

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
    schema = f"checkout_orchestrator_{uuid4().hex}"
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


async def test_over_limit_exact_approval_checkout_replays_one_paid_order(
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "principal:checkout-e2e"
    commerce = CommerceV1(pool)
    orchestrator = CheckoutOrchestrator(pool)
    compiled = await orchestrator.compile_mandate(
        principal_id=principal_id, limits={"max_order_paise": 15_000}
    )
    await commerce.upsert_inventory(
        seller_id="seller-checkout",
        sku="atta-checkout",
        title="Checkout Atta",
        unit_price_paise=10_000,
        available_quantity=5,
    )
    cart = await commerce.create_cart(
        principal_id=principal_id,
        seller_id="seller-checkout",
        idempotency_key="cart-e2e",
    )
    cart = await commerce.set_cart_line(
        principal_id=principal_id,
        cart_id=cart["cart_id"],
        sku="atta-checkout",
        quantity=2,
        expected_version=cart["version"],
        idempotency_key="line-e2e",
    )
    quote = await commerce.preview_checkout(
        principal_id=principal_id,
        cart_id=cart["cart_id"],
        expected_version=cart["version"],
        idempotency_key="preview-e2e",
    )
    with pytest.raises(AgentGuardNotFound, match="mandate"):
        await orchestrator.evaluate_checkout(
            principal_id=principal_id, quote_id=quote["quote_id"]
        )
    await orchestrator.confirm_mandate(
        principal_id=principal_id,
        mandate_id=compiled["mandate"]["mandate_id"],
    )
    decision = await orchestrator.evaluate_checkout(
        principal_id=principal_id, quote_id=quote["quote_id"]
    )
    assert decision["decision"] == "need_approval"
    assert decision["bound_action"]["landed_total_paise"] == 20_000
    other_decision = await orchestrator.evaluate_checkout(
        principal_id=principal_id, quote_id=quote["quote_id"]
    )

    with pytest.raises(AgentGuardConflict, match="approval does not match"):
        await orchestrator.execute_checkout(
            principal_id=principal_id,
            quote_id=quote["quote_id"],
            decision_id=decision["decision_id"],
            approval_id=other_decision["approval"]["approval_id"],
            idempotency_key="checkout-wrong-approval",
            correlation_id="correlation-wrong-approval",
        )

    with pytest.raises(AgentGuardConflict, match="exact approval"):
        await orchestrator.execute_checkout(
            principal_id=principal_id,
            quote_id=quote["quote_id"],
            decision_id=decision["decision_id"],
            approval_id=None,
            idempotency_key="checkout-e2e",
            correlation_id="correlation-e2e",
        )

    async def crash_before_receipt(*_args, **_kwargs):
        raise RuntimeError("simulated crash before receipt commit")

    with monkeypatch.context() as crash:
        crash.setattr(AgentGuardRepository, "record_receipt", crash_before_receipt)
        with pytest.raises(RuntimeError, match="simulated crash"):
            await orchestrator.execute_checkout(
                principal_id=principal_id,
                quote_id=quote["quote_id"],
                decision_id=decision["decision_id"],
                approval_id=decision["approval"]["approval_id"],
                idempotency_key="checkout-e2e",
                correlation_id="correlation-e2e",
            )

    first = await orchestrator.execute_checkout(
        principal_id=principal_id,
        quote_id=quote["quote_id"],
        decision_id=decision["decision_id"],
        approval_id=decision["approval"]["approval_id"],
        idempotency_key="checkout-e2e",
        correlation_id="correlation-e2e",
    )
    replay = await orchestrator.execute_checkout(
        principal_id=principal_id,
        quote_id=quote["quote_id"],
        decision_id=decision["decision_id"],
        approval_id=decision["approval"]["approval_id"],
        idempotency_key="checkout-e2e",
        correlation_id="correlation-e2e",
    )

    assert first["result"]["order"]["status"] == "paid"
    assert replay["receipt"]["receipt_id"] == first["receipt"]["receipt_id"]
    assert verify_receipt(first["receipt"])["valid"] is True
    async with pool.connection() as connection:
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


async def test_http_mandate_preview_decision_execute_and_replay(
    pool: ConnectionPool,
) -> None:
    principal_id = "principal:http-checkout"
    await CommerceV1(pool).upsert_inventory(
        seller_id="seller-http-checkout",
        sku="rice-http-checkout",
        title="Checkout Rice",
        unit_price_paise=12_500,
        available_quantity=3,
    )
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(commerce_v1_router)
    api.include_router(agentguard_router)
    token = create_principal_session_token(
        principal_id=principal_id,
        audience="ondcbuyer",
        identity_provider="demo",
    )
    transport = ASGITransport(app=api)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: token},
    ) as client:
        mandate = await client.post(
            "/api/agentguard/mandates/compile",
            json={"role": "buyer", "limits": {"max_order_paise": 10_000}},
        )
        assert mandate.status_code == 200
        mandate_record = mandate.json()["data"]["mandate"]
        assert mandate_record["status"] == "draft"

        cart_response = await client.post(
            "/api/commerce/v1/carts",
            headers={"Idempotency-Key": "http-cart"},
            json={"seller_id": "seller-http-checkout"},
        )
        cart = cart_response.json()["data"]["cart"]
        line_response = await client.put(
            f"/api/commerce/v1/carts/{cart['cart_id']}/lines/rice-http-checkout",
            headers={"Idempotency-Key": "http-line"},
            json={"quantity": 1, "expected_version": cart["version"]},
        )
        cart = line_response.json()["data"]["cart"]
        preview_response = await client.post(
            f"/api/commerce/v1/carts/{cart['cart_id']}/checkout-preview",
            headers={"Idempotency-Key": "http-preview"},
            json={"expected_version": cart["version"]},
        )
        quote = preview_response.json()["data"]["quote"]

        before_confirmation = await client.post(
            "/api/agentguard/actions/evaluate",
            json={
                "action": "buyer.checkout.commit",
                "amount_inr": 0,
                "resource_id": quote["quote_id"],
                "payload": {"quote_id": quote["quote_id"]},
            },
        )
        assert before_confirmation.status_code == 404
        confirmed = await client.post(
            f"/api/agentguard/mandates/{mandate_record['mandate_id']}/confirm",
            json={},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["data"]["mandate"]["status"] == "active"

        evaluation = await client.post(
            "/api/agentguard/actions/evaluate",
            json={
                "action": "buyer.checkout.commit",
                "amount_inr": 0,
                "resource_id": quote["quote_id"],
                "payload": {"quote_id": quote["quote_id"]},
            },
        )
        assert evaluation.status_code == 200
        decision = evaluation.json()["data"]
        assert decision["decision"] == "need_approval"
        assert decision["bound_action"]["landed_total_paise"] == 12_500

        execute_body = {
            "action": "buyer.checkout.commit",
            "resource_id": quote["quote_id"],
            "decision_id": decision["decision_id"],
            "approval_id": decision["approval"]["approval_id"],
            "payload": {"quote_id": quote["quote_id"]},
        }
        first = await client.post(
            "/api/agentguard/actions/execute",
            headers={
                "Idempotency-Key": "http-checkout",
                "X-Correlation-ID": "correlation-http-checkout",
            },
            json=execute_body,
        )
        replay = await client.post(
            "/api/agentguard/actions/execute",
            headers={
                "Idempotency-Key": "http-checkout",
                "X-Correlation-ID": "correlation-http-checkout",
            },
            json=execute_body,
        )
        assert first.status_code == replay.status_code == 200
        assert first.json()["data"]["result"]["order"]["status"] == "paid"
        assert (
            first.json()["data"]["receipt"]["receipt_id"]
            == replay.json()["data"]["receipt"]["receipt_id"]
        )
        verified = await client.post(
            "/api/agentguard/receipts/verify",
            json={"receipt_id": first.json()["data"]["receipt"]["receipt_id"]},
        )
        assert verified.status_code == 200
        assert verified.json()["data"]["valid"] is True

        async def execute_outcome(outcome: str):
            outcome_cart = (
                await client.post(
                    "/api/commerce/v1/carts",
                    headers={"Idempotency-Key": f"{outcome}-cart"},
                    json={"seller_id": "seller-http-checkout"},
                )
            ).json()["data"]["cart"]
            outcome_cart = (
                await client.put(
                    f"/api/commerce/v1/carts/{outcome_cart['cart_id']}/lines/rice-http-checkout",
                    headers={"Idempotency-Key": f"{outcome}-line"},
                    json={"quantity": 1, "expected_version": outcome_cart["version"]},
                )
            ).json()["data"]["cart"]
            outcome_quote = (
                await client.post(
                    f"/api/commerce/v1/carts/{outcome_cart['cart_id']}/checkout-preview",
                    headers={"Idempotency-Key": f"{outcome}-preview"},
                    json={"expected_version": outcome_cart["version"]},
                )
            ).json()["data"]["quote"]
            outcome_decision = (
                await client.post(
                    "/api/agentguard/actions/evaluate",
                    json={
                        "action": "buyer.checkout.commit",
                        "amount_inr": 0,
                        "resource_id": outcome_quote["quote_id"],
                        "payload": {"quote_id": outcome_quote["quote_id"]},
                    },
                )
            ).json()["data"]
            body = {
                "action": "buyer.checkout.commit",
                "resource_id": outcome_quote["quote_id"],
                "decision_id": outcome_decision["decision_id"],
                "approval_id": outcome_decision["approval"]["approval_id"],
                "payload": {
                    "quote_id": outcome_quote["quote_id"],
                    "payment_outcome": outcome,
                },
            }
            return await client.post(
                "/api/agentguard/actions/execute",
                headers={
                    "Idempotency-Key": f"{outcome}-checkout",
                    "X-Correlation-ID": f"correlation-{outcome}",
                },
                json=body,
            ), body

        unknown, unknown_body = await execute_outcome("unknown")
        assert unknown.status_code == 202
        assert unknown.json()["data"]["reason_code"] == "PAYMENT_STATUS_UNKNOWN"
        assert unknown.json()["data"]["receipt"]["outcome"] == "payment_unknown"
        unknown_replay = await client.post(
            "/api/agentguard/actions/execute",
            headers={
                "Idempotency-Key": "unknown-checkout",
                "X-Correlation-ID": "correlation-unknown",
            },
            json=unknown_body,
        )
        assert unknown_replay.status_code == 202
        assert (
            unknown_replay.json()["data"]["receipt"]["receipt_id"]
            == unknown.json()["data"]["receipt"]["receipt_id"]
        )
        await CommerceV1(pool).reconcile_payment(
            principal_id=principal_id,
            payment_attempt_id=unknown.json()["data"]["result"]["payment_attempt"][
                "payment_attempt_id"
            ],
            outcome="succeeded",
            detail={"simulated_reconciliation": True},
        )
        reconciled = await client.post(
            "/api/agentguard/actions/execute",
            headers={
                "Idempotency-Key": "unknown-checkout",
                "X-Correlation-ID": "correlation-unknown",
            },
            json=unknown_body,
        )
        assert reconciled.status_code == 200
        assert reconciled.json()["data"]["reason_code"] == "EXECUTED_AND_VERIFIED"
        assert reconciled.json()["data"]["result"]["order"]["status"] == "paid"

        failed, _failed_body = await execute_outcome("failed")
        assert failed.status_code == 409
        assert "Payment failed" in failed.json()["detail"]
        async with pool.connection() as connection:
            failed_record = await connection.execute(
                """
                SELECT intent.status, receipt.status, receipt.payload->>'outcome'
                FROM agentguard_execution_intents AS intent
                JOIN agentguard_receipts AS receipt
                  ON receipt.intent_id = intent.intent_id
                WHERE intent.principal_id = %s AND intent.idempotency_key = %s
                """,
                (principal_id, "failed-checkout"),
            )
            assert await failed_record.fetchone() == (
                "failed",
                "failed",
                "payment_failed",
            )


async def test_legacy_buyer_mandate_shape_uses_postgres_control_plane(
    pool: ConnectionPool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))

    def reject_file_fork(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("PostgreSQL-selected AgentGuard touched file state")

    monkeypatch.setattr(
        "app.agentguard_routes.agentguard.compile_mandate", reject_file_fork
    )
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(agentguard_router)
    token = create_principal_session_token(
        principal_id="principal:legacy-buyer",
        audience="ondcbuyer",
        identity_provider="demo",
    )
    async with AsyncClient(
        transport=ASGITransport(app=api),
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: token},
    ) as client:
        compiled = await client.post(
            "/api/agentguard/mandates/compile",
            json={
                "role": "buyer",
                "template": "buyer_shop_v1",
                "limits": {"auto_approve_max_inr": {"buyer.checkout.commit": 4_321}},
            },
        )
        assert compiled.status_code == 200
        mandate = compiled.json()["data"]["mandate"]
        assert (
            mandate["limits"]["auto_approve_max_inr"]["buyer.checkout.commit"] == 4_321
        )
        confirmed = await client.post(
            f"/api/agentguard/mandates/{mandate['mandate_id']}/confirm", json={}
        )
        assert confirmed.status_code == 200
        ensured = await client.post(
            "/api/agentguard/agents/ensure", json={"role": "buyer"}
        )
        assert ensured.status_code == 200
        assert ensured.json()["data"]["mandate"]["status"] == "active"
        current = await client.get("/api/agentguard/agents/current?role=buyer")
        assert current.status_code == 200
        agent_id = current.json()["data"]["agent"]["agent_id"]
        paused = await client.post(
            f"/api/agentguard/agents/{agent_id}/pause", json={}
        )
        assert paused.status_code == 200
        assert paused.json()["data"]["agent"]["status"] == "paused"
        resumed = await client.post(
            f"/api/agentguard/agents/{agent_id}/resume", json={}
        )
        assert resumed.status_code == 200
        assert resumed.json()["data"]["agent"]["status"] == "active"
