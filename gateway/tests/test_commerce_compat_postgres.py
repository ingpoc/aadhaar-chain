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

from app.cart_routes import router as cart_router
from app.commerce_routes import router as commerce_router
from app.commerce_v1 import CommerceV1
from app.persistence import ConnectionPool, MigrationRunner
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
    monkeypatch.setattr(settings, "aadhaar_chain_env", "demo")
    pool = ConnectionPool(postgres_url, min_size=0, max_size=8)
    await pool.open()
    await MigrationRunner(pool, MIGRATIONS).apply()
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(commerce_router)
    api.include_router(cart_router)

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
            rice = await client.get("/api/demo-commerce/buyer/search?q=rice")
            assert rice.status_code == 200
            titles = [item["title"] for item in rice.json()["data"]["items"]]
            assert any("Atta" in title for title in titles)
            grocery = await client.get("/api/demo-commerce/buyer/search?q=grocery")
            assert grocery.status_code == 200
            assert grocery.json()["data"]["count"] >= 1

            session_id = "ondc-session-pg-billing"
            draft = await client.get(f"/api/cart/buyer/{session_id}")
            assert draft.status_code == 200, draft.text
            assert draft.json()["session"]["buyer"] == {}
            empty_save = await client.put(f"/api/cart/buyer/{session_id}", json={})
            assert empty_save.status_code == 200, empty_save.text
            billed = await client.patch(
                f"/api/cart/buyer/{session_id}",
                json={
                    "name": "PG Buyer",
                    "email": "pg@example.com",
                    "phone": "+919111111111",
                },
            )
            assert billed.status_code == 200, billed.text
            loaded = await client.get(f"/api/cart?sessionId={session_id}")
            assert loaded.status_code == 200
            assert loaded.json()["session"]["buyer"]["name"] == "PG Buyer"

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
            assert response.json()["data"]["issue"]["status"] == "acknowledged"
            assert response.json()["data"]["issue"]["version"] == 2
            duplicate_response = await client.post(
                f"/api/demo-commerce/test-fixtures/seller/issues/{issue_id}/respond",
                json={"response": "Duplicate response"},
            )
            assert duplicate_response.status_code == 409
            remedy = await client.post(
                f"/api/demo-commerce/test-fixtures/seller/issues/{issue_id}/remedy",
                json={"data": {"type": "refund", "amount_inr": 25}},
            )
            assert remedy.status_code == 200, remedy.text
            assert remedy.json()["data"]["issue"]["status"] == "resolution_proposed"
            assert remedy.json()["data"]["issue"]["version"] == 3

            buyer_orders = await client.get(
                "/api/demo-commerce/buyer/orders",
                cookies={SESSION_COOKIE_NAME: buyer_cookie},
            )
            seller_orders = await client.get(
                "/api/demo-commerce/seller/orders",
                cookies={SESSION_COOKIE_NAME: seller_cookie},
            )
            short_id = order_id.replace("-", "")[:8].upper()
            seller_by_uuid = await client.get(
                f"/api/demo-commerce/seller/orders/{order_id}",
                cookies={SESSION_COOKIE_NAME: seller_cookie},
            )
            seller_by_short = await client.get(
                f"/api/demo-commerce/seller/orders/{short_id}",
                cookies={SESSION_COOKIE_NAME: seller_cookie},
            )
            foreign_seller = await client.get(
                "/api/demo-commerce/seller/orders",
                cookies={
                    SESSION_COOKIE_NAME: create_principal_session_token(
                        principal_id="principal:auth0:other-seller",
                        audience="ondcseller",
                        identity_provider="auth0",
                    )
                },
            )
            foreign_short = await client.get(
                f"/api/demo-commerce/seller/orders/{short_id}",
                cookies={
                    SESSION_COOKIE_NAME: create_principal_session_token(
                        principal_id="principal:auth0:other-seller",
                        audience="ondcseller",
                        identity_provider="auth0",
                    )
                },
            )
            buyer_issues = await client.get(
                "/api/demo-commerce/buyer/issues",
                cookies={SESSION_COOKIE_NAME: buyer_cookie},
            )

        assert buyer_orders.status_code == seller_orders.status_code == 200
        assert buyer_orders.json()["data"]["orders"][0]["order_id"] == order_id
        assert seller_orders.json()["data"]["orders"][0]["order_id"] == order_id
        assert seller_by_uuid.status_code == seller_by_short.status_code == 200
        assert seller_by_uuid.json()["data"]["order"]["order_id"] == order_id
        assert seller_by_short.json()["data"]["order"]["order_id"] == order_id
        assert foreign_seller.status_code == 200
        assert foreign_seller.json()["data"]["count"] == 0
        assert foreign_short.status_code == 404
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


async def test_buyer_order_is_visible_to_seller_list_and_short_id(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merchant owner and staff operator both see a buyer-created order."""
    monkeypatch.setattr(settings, "aadhaar_chain_env", "demo")
    pool = ConnectionPool(postgres_url, min_size=0, max_size=8)
    await pool.open()
    await MigrationRunner(pool, MIGRATIONS).apply()
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(commerce_router)
    seller_id = "principal:auth0:merchant-atta"
    operator_id = "principal:auth0:hermes-operator"
    stranger_id = "principal:auth0:other-shop"
    buyer_id = "principal:auth0:buyer-atta"
    commerce = CommerceV1(pool)
    await commerce.upsert_store(
        seller_id=seller_id,
        body={
            "store_name": "Sampoorna Groceries",
            "city": "Bengaluru",
            "pin": "560001",
        },
    )
    await commerce.invite_staff(
        seller_id=seller_id,
        actor_principal_id=seller_id,
        body={
            "member_principal_id": operator_id,
            "role": "manager",
            "status": "active",
        },
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=api), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/demo-commerce/test-fixtures/seller/items",
                json={
                    "title": "Sampoorna Whole Wheat Atta 1kg",
                    "price_inr": 89,
                    "inventory": 6,
                    "seller_id": seller_id,
                    "seller_name": "Sampoorna Groceries",
                },
            )
            assert created.status_code == 200, created.text
            item_id = created.json()["data"]["item"]["item_id"]
            published = await client.post(
                f"/api/demo-commerce/test-fixtures/seller/items/{item_id}/publish"
            )
            assert published.status_code == 200, published.text
            order = await client.post(
                "/api/demo-commerce/test-fixtures/buyer/orders",
                headers={"Idempotency-Key": "staff-visibility-order"},
                json={
                    "item_id": item_id,
                    "quantity": 2,
                    "buyer_id": buyer_id,
                    "payment_mode": "success",
                },
            )
            assert order.status_code == 200, order.text
            order_id = order.json()["data"]["order"]["order_id"]
            short_id = order_id.replace("-", "")[:8].upper()
            assert short_id == order_id.replace("-", "")[:8].upper()

            async def _seller_views(principal: str) -> None:
                listed = await client.get(
                    "/api/demo-commerce/seller/orders",
                    cookies={SESSION_COOKIE_NAME: _seller_cookie(principal)},
                )
                assert listed.status_code == 200, listed.text
                ids = {row["order_id"] for row in listed.json()["data"]["orders"]}
                assert order_id in ids
                catalog = await client.get(
                    "/api/demo-commerce/seller/items",
                    cookies={SESSION_COOKIE_NAME: _seller_cookie(principal)},
                )
                assert catalog.status_code == 200, catalog.text
                assert catalog.json()["data"]["count"] >= 1
                by_uuid = await client.get(
                    f"/api/demo-commerce/seller/orders/{order_id}",
                    cookies={SESSION_COOKIE_NAME: _seller_cookie(principal)},
                )
                by_short = await client.get(
                    f"/api/demo-commerce/seller/orders/{short_id}",
                    cookies={SESSION_COOKIE_NAME: _seller_cookie(principal)},
                )
                by_lower = await client.get(
                    f"/api/demo-commerce/seller/orders/{short_id.lower()}",
                    cookies={SESSION_COOKIE_NAME: _seller_cookie(principal)},
                )
                assert by_uuid.status_code == by_short.status_code == by_lower.status_code == 200
                assert by_uuid.json()["data"]["order"]["order_id"] == order_id
                assert by_short.json()["data"]["order"]["order_id"] == order_id
                assert by_lower.json()["data"]["order"]["order_id"] == order_id

            await _seller_views(seller_id)
            await _seller_views(operator_id)

            stranger_list = await client.get(
                "/api/demo-commerce/seller/orders",
                cookies={SESSION_COOKIE_NAME: _seller_cookie(stranger_id)},
            )
            stranger_get = await client.get(
                f"/api/demo-commerce/seller/orders/{short_id}",
                cookies={SESSION_COOKIE_NAME: _seller_cookie(stranger_id)},
            )
            buyer_get = await client.get(
                f"/api/demo-commerce/seller/orders/{short_id}",
                cookies={SESSION_COOKIE_NAME: _buyer_cookie(buyer_id)},
            )
            assert stranger_list.status_code == 200
            assert stranger_list.json()["data"]["count"] == 0
            assert stranger_get.status_code == 404
            assert stranger_get.json()["detail"] == "order not found"
            assert buyer_get.status_code == 403
    finally:
        await pool.close()


async def test_shared_seller_id_binds_operator_without_staff_row(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "aadhaar_chain_env", "demo")
    merchant_id = "principal:auth0:google-merchant"
    operator_id = "principal:auth0:agentmail-operator"
    buyer_id = "principal:auth0:shared-buyer"
    monkeypatch.setattr(settings, "commerce_shared_seller_ids", merchant_id)
    pool = ConnectionPool(postgres_url, min_size=0, max_size=8)
    await pool.open()
    await MigrationRunner(pool, MIGRATIONS).apply()
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(commerce_router)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=api), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/demo-commerce/test-fixtures/seller/items",
                json={
                    "title": "Sampoorna Whole Wheat Atta 1kg",
                    "price_inr": 89,
                    "inventory": 4,
                    "seller_id": merchant_id,
                },
            )
            assert created.status_code == 200, created.text
            item_id = created.json()["data"]["item"]["item_id"]
            assert (
                await client.post(
                    f"/api/demo-commerce/test-fixtures/seller/items/{item_id}/publish"
                )
            ).status_code == 200
            order = await client.post(
                "/api/demo-commerce/test-fixtures/buyer/orders",
                headers={"Idempotency-Key": "shared-seller-order"},
                json={
                    "item_id": item_id,
                    "quantity": 2,
                    "buyer_id": buyer_id,
                    "payment_mode": "success",
                },
            )
            assert order.status_code == 200, order.text
            order_id = order.json()["data"]["order"]["order_id"]
            short_id = order_id.replace("-", "")[:8].upper()
            listed = await client.get(
                "/api/demo-commerce/seller/orders",
                cookies={SESSION_COOKIE_NAME: _seller_cookie(operator_id)},
            )
            by_short = await client.get(
                f"/api/demo-commerce/seller/orders/{short_id}",
                cookies={SESSION_COOKIE_NAME: _seller_cookie(operator_id)},
            )
            catalog = await client.get(
                "/api/demo-commerce/seller/items",
                cookies={SESSION_COOKIE_NAME: _seller_cookie(operator_id)},
            )
            assert listed.status_code == 200, listed.text
            assert order_id in {
                row["order_id"] for row in listed.json()["data"]["orders"]
            }
            assert by_short.status_code == 200, by_short.text
            assert by_short.json()["data"]["order"]["order_id"] == order_id
            assert catalog.json()["data"]["count"] >= 1
    finally:
        await pool.close()


