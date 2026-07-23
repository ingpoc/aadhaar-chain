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
from app.commerce_routes import router as commerce_router
from app.persistence import ConnectionPool, MigrationRunner
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
async def postgres_url() -> AsyncIterator[str]:
    assert DATABASE_URL is not None
    schema = f"cf2_cf3_lifecycle_{uuid4().hex}"
    admin = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)
    try:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        yield make_conninfo(DATABASE_URL, options=f"-csearch_path={schema},public")
    finally:
        await admin.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )
        await admin.close()


async def _compile_and_confirm(
    client: AsyncClient,
    *,
    cookie: str,
    role: str,
    allowed_actions: list[str],
) -> None:
    compiled = await client.post(
        "/api/agentguard/mandates/compile",
        cookies={SESSION_COOKIE_NAME: cookie},
        json={
            "role": role,
            "allowed_actions": allowed_actions,
            "limits": {"auto_approve_max_inr": {"buyer.checkout.commit": 10_000}},
        },
    )
    assert compiled.status_code == 200, compiled.text
    mandate_id = compiled.json()["data"]["mandate"]["mandate_id"]
    confirmed = await client.post(
        f"/api/agentguard/mandates/{mandate_id}/confirm",
        cookies={SESSION_COOKIE_NAME: cookie},
        json={},
    )
    assert confirmed.status_code == 200, confirmed.text


async def _execute(
    client: AsyncClient,
    *,
    cookie: str,
    action: str,
    resource_id: str,
    idempotency_key: str,
    payload: dict[str, object],
) -> dict[str, object]:
    response = await client.post(
        "/api/agentguard/actions/execute",
        cookies={SESSION_COOKIE_NAME: cookie},
        headers={
            "Idempotency-Key": idempotency_key,
            "X-Correlation-ID": f"correlation:{idempotency_key}",
        },
        json={
            "action": action,
            "amount_inr": 0,
            "resource_id": resource_id,
            "idempotency_key": idempotency_key,
            "payload": payload,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["schema_version"] == "2"
    assert data["receipt"]["schema_version"] == "2"
    return data


async def test_two_sided_fulfilment_return_and_remedy_slice(
    postgres_url: str,
) -> None:
    pool = ConnectionPool(postgres_url, min_size=0, max_size=8)
    await pool.open()
    await MigrationRunner(pool, MIGRATIONS).apply()
    app = FastAPI()
    app.state.persistence_pool = pool
    app.include_router(commerce_router)
    app.include_router(agentguard_router)

    seller_id = "principal:auth0:cf3-seller"
    buyer_id = "principal:auth0:cf2-buyer"
    seller_cookie = create_principal_session_token(
        principal_id=seller_id,
        audience="ondcseller",
        identity_provider="auth0",
    )
    buyer_cookie = create_principal_session_token(
        principal_id=buyer_id,
        audience="ondcbuyer",
        identity_provider="auth0",
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            item = await client.post(
                "/api/demo-commerce/test-fixtures/seller/items",
                json={
                    "title": "CF2 CF3 lifecycle item",
                    "price_inr": 250,
                    "inventory": 3,
                    "seller_id": seller_id,
                },
            )
            assert item.status_code == 200, item.text
            item_id = item.json()["data"]["item"]["item_id"]
            await client.post(
                f"/api/demo-commerce/test-fixtures/seller/items/{item_id}/publish"
            )
            order = await client.post(
                "/api/demo-commerce/test-fixtures/buyer/orders",
                json={
                    "item_id": item_id,
                    "quantity": 1,
                    "buyer_id": buyer_id,
                    "payment_mode": "success",
                },
            )
            assert order.status_code == 200, order.text
            order_id = order.json()["data"]["order"]["order_id"]

            await _compile_and_confirm(
                client,
                cookie=seller_cookie,
                role="seller",
                allowed_actions=[
                    "seller.order.accept",
                    "seller.fulfilment.commit",
                    "seller.remedy.promise",
                ],
            )
            await _compile_and_confirm(
                client,
                cookie=buyer_cookie,
                role="buyer",
                allowed_actions=[
                    "buyer.checkout.commit",
                    "buyer.order.cancel",
                    "buyer.return.submit",
                    "buyer.remedy.accept",
                ],
            )

            cancellable = await client.post(
                "/api/demo-commerce/test-fixtures/buyer/orders",
                json={
                    "item_id": item_id,
                    "quantity": 1,
                    "buyer_id": buyer_id,
                    "payment_mode": "success",
                },
            )
            assert cancellable.status_code == 200, cancellable.text
            cancellable_order_id = cancellable.json()["data"]["order"]["order_id"]
            cancelled = await _execute(
                client,
                cookie=buyer_cookie,
                action="buyer.order.cancel",
                resource_id=cancellable_order_id,
                idempotency_key=f"{cancellable_order_id}:cancel",
                payload={
                    "order_id": cancellable_order_id,
                    "reason": "Buyer changed their mind",
                },
            )
            assert cancelled["result"]["order"]["status"] == "cancelled"

            accepted = await _execute(
                client,
                cookie=seller_cookie,
                action="seller.order.accept",
                resource_id=order_id,
                idempotency_key=f"{order_id}:accept",
                payload={"order_id": order_id},
            )
            assert accepted["result"]["order"]["status"] == "confirmed"

            prepared = await _execute(
                client,
                cookie=seller_cookie,
                action="seller.fulfilment.commit",
                resource_id=order_id,
                idempotency_key=f"{order_id}:prepare",
                payload={
                    "order_id": order_id,
                    "status": "preparing",
                    "status_message": "Preparing the order",
                },
            )
            assert prepared["result"]["order"]["status"] == "preparing"

            dispatched = await _execute(
                client,
                cookie=seller_cookie,
                action="seller.fulfilment.commit",
                resource_id=order_id,
                idempotency_key=f"{order_id}:dispatch",
                payload={
                    "order_id": order_id,
                    "status": "shipped",
                    "tracking_id": "TRACK-CF23",
                    "provider_name": "Contract Courier",
                },
            )
            assert dispatched["result"]["order"]["status"] == "shipped"
            assert (
                dispatched["result"]["order"]["fulfilment"]["tracking_id"]
                == "TRACK-CF23"
            )

            completed = await _execute(
                client,
                cookie=seller_cookie,
                action="seller.fulfilment.commit",
                resource_id=order_id,
                idempotency_key=f"{order_id}:complete",
                payload={"order_id": order_id, "status": "delivered"},
            )
            assert completed["result"]["order"]["status"] == "delivered"

            buyer_order = await client.get(
                f"/api/demo-commerce/buyer/orders/{order_id}",
                cookies={SESSION_COOKIE_NAME: buyer_cookie},
            )
            assert buyer_order.status_code == 200, buyer_order.text
            assert (
                buyer_order.json()["data"]["order"]["fulfilment"]["tracking_id"]
                == "TRACK-CF23"
            )

            return_result = await _execute(
                client,
                cookie=buyer_cookie,
                action="buyer.return.submit",
                resource_id=order_id,
                idempotency_key=f"{order_id}:return",
                payload={"order_id": order_id, "reason": "Item arrived damaged"},
            )
            assert return_result["result"]["return"]["status"] == "requested"

            issue = await client.post(
                f"/api/demo-commerce/buyer/orders/{order_id}/issues",
                cookies={SESSION_COOKIE_NAME: buyer_cookie},
                json={
                    "reason": "post_delivery",
                    "description": "Package was damaged in transit",
                },
            )
            assert issue.status_code == 200, issue.text
            issue_id = issue.json()["data"]["issue"]["issue_id"]

            remedy = await _execute(
                client,
                cookie=seller_cookie,
                action="seller.remedy.promise",
                resource_id=issue_id,
                idempotency_key=f"{issue_id}:remedy",
                payload={
                    "issue_id": issue_id,
                    "type": "replacement",
                    "message": "A replacement will be sent",
                },
            )
            assert remedy["result"]["issue"]["status"] == "resolution_proposed"

            accepted_remedy = await _execute(
                client,
                cookie=buyer_cookie,
                action="buyer.remedy.accept",
                resource_id=issue_id,
                idempotency_key=f"{issue_id}:accept",
                payload={"issue_id": issue_id},
            )
            assert accepted_remedy["result"]["issue"]["status"] == "closed"

            verified = await client.post(
                "/api/agentguard/receipts/verify",
                cookies={SESSION_COOKIE_NAME: buyer_cookie},
                json={"receipt": accepted_remedy["receipt"]},
            )
            assert verified.status_code == 200, verified.text
            assert verified.json()["data"]["valid"] is True

            buyer_issues = await client.get(
                f"/api/demo-commerce/buyer/issues?order_id={order_id}",
                cookies={SESSION_COOKIE_NAME: buyer_cookie},
            )
            outcome = buyer_issues.json()["data"]["issues"][0]["outcome_receipt"]
            assert outcome["receipt_id"] == accepted_remedy["receipt"]["receipt_id"]

            current_agent = await client.get(
                "/api/agentguard/agents/current?role=buyer",
                cookies={SESSION_COOKIE_NAME: buyer_cookie},
            )
            agent_id = current_agent.json()["data"]["agent"]["agent_id"]
            paused = await client.post(
                f"/api/agentguard/agents/{agent_id}/pause",
                cookies={SESSION_COOKIE_NAME: buyer_cookie},
                json={},
            )
            assert paused.status_code == 200, paused.text
            denied = await client.post(
                "/api/agentguard/actions/evaluate",
                cookies={SESSION_COOKIE_NAME: buyer_cookie},
                headers={"X-Correlation-ID": "cf23-paused-buyer"},
                json={
                    "action": "buyer.order.cancel",
                    "amount_inr": 0,
                    "resource_id": order_id,
                    "payload": {"order_id": order_id},
                },
            )
            assert denied.status_code == 200, denied.text
            assert denied.json()["data"]["schema_version"] == "2"
            assert denied.json()["data"]["decision"] == "deny"
            assert denied.json()["data"]["reason_code"] == "agent_paused"
    finally:
        await pool.close()
