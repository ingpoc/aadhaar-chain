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
    schema = f"commerce_compat_test_{uuid4().hex}"
    admin = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)
    try:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        yield make_conninfo(DATABASE_URL, options=f"-csearch_path={schema},public")
    finally:
        await admin.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )
        await admin.close()


async def test_demo_commerce_is_a_postgres_compatibility_adapter(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = ConnectionPool(postgres_url, min_size=0, max_size=8)
    await pool.open()
    await MigrationRunner(pool, MIGRATIONS).apply()
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(commerce_router)

    def reject_file_fork(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("PostgreSQL-selected commerce touched file state")

    for name in (
        "create_item",
        "publish_item",
        "search_items",
        "create_order",
        "list_buyer_orders",
        "list_seller_orders",
    ):
        monkeypatch.setattr(f"app.commerce_routes.commerce_demo.{name}", reject_file_fork)

    seller_id = "principal:auth0:seller-1"
    buyer_id = "principal:auth0:buyer-1"
    buyer_cookie = create_principal_session_token(
        principal_id=buyer_id,
        audience="ondcbuyer",
        identity_provider="auth0",
    )
    seller_cookie = create_principal_session_token(
        principal_id=seller_id,
        audience="ondcseller",
        identity_provider="auth0",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=api), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/demo-commerce/test-fixtures/seller/items",
                json={
                    "title": "Durable Atta 1kg",
                    "description": "Stone-ground wheat",
                    "price_inr": 125,
                    "inventory": 4,
                    "seller_id": seller_id,
                    "seller_name": "Durable Foods",
                    "delivery_areas": ["560001"],
                },
            )
            assert created.status_code == 200, created.text
            item_id = created.json()["data"]["item"]["item_id"]

            published = await client.post(
                f"/api/demo-commerce/test-fixtures/seller/items/{item_id}/publish"
            )
            assert published.status_code == 200, published.text
            search = await client.get("/api/demo-commerce/buyer/search?q=atta")
            assert search.status_code == 200
            assert search.json()["data"]["items"][0]["delivery_areas"] == ["560001"]

            order = await client.post(
                "/api/demo-commerce/test-fixtures/buyer/orders",
                headers={"Idempotency-Key": "compat-order-1"},
                json={
                    "item_id": item_id,
                    "quantity": 2,
                    "buyer_id": buyer_id,
                    "payment_mode": "success",
                },
            )
            assert order.status_code == 200, order.text
            order_id = order.json()["data"]["order"]["order_id"]
            replay = await client.post(
                "/api/demo-commerce/test-fixtures/buyer/orders",
                headers={"Idempotency-Key": "compat-order-1"},
                json={
                    "item_id": item_id,
                    "quantity": 2,
                    "buyer_id": buyer_id,
                    "payment_mode": "success",
                },
            )
            assert replay.status_code == 200, replay.text
            assert replay.json()["data"]["order"]["order_id"] == order_id

            issue = await client.post(
                f"/api/demo-commerce/test-fixtures/buyer/orders/{order_id}/issues",
                json={"reason": "fulfillment", "description": "Parcel delayed"},
            )
            assert issue.status_code == 200, issue.text
            issue_id = issue.json()["data"]["issue"]["issue_id"]
            response = await client.post(
                f"/api/demo-commerce/test-fixtures/seller/issues/{issue_id}/respond",
                json={"response": "Investigating now"},
            )
            assert response.status_code == 200, response.text

            buyer_orders = await client.get(
                "/api/demo-commerce/buyer/orders",
                cookies={SESSION_COOKIE_NAME: buyer_cookie},
            )
            seller_orders = await client.get(
                "/api/demo-commerce/seller/orders",
                cookies={SESSION_COOKIE_NAME: seller_cookie},
            )
            buyer_issues = await client.get(
                "/api/demo-commerce/buyer/issues",
                cookies={SESSION_COOKIE_NAME: buyer_cookie},
            )

        assert buyer_orders.status_code == seller_orders.status_code == 200
        assert buyer_orders.json()["data"]["orders"][0]["order_id"] == order_id
        assert seller_orders.json()["data"]["orders"][0]["order_id"] == order_id
        assert buyer_orders.json()["data"]["orders"][0]["amount_inr"] == 250
        assert buyer_issues.json()["data"]["issues"][0]["response"] == "Investigating now"

        async with pool.connection() as connection:
            inventory = await connection.execute(
                "SELECT available_quantity, reserved_quantity FROM commerce_inventory"
            )
            assert await inventory.fetchone() == (2, 0)
            orders = await connection.execute("SELECT COUNT(*) FROM commerce_orders")
            assert (await orders.fetchone())[0] == 1
    finally:
        await pool.close()
