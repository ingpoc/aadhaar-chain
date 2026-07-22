from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.commerce_v1 import CommerceConflict, CommerceV1, IdempotencyConflict
from app.commerce_v1_routes import router as commerce_v1_router
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


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 22, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


@pytest_asyncio.fixture
async def postgres_url() -> AsyncIterator[str]:
    assert DATABASE_URL is not None
    schema = f"commerce_test_{uuid4().hex}"
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
) -> AsyncIterator[tuple[CommerceV1, ConnectionPool, Clock]]:
    pool = ConnectionPool(postgres_url, min_size=0, max_size=8)
    await pool.open()
    await MigrationRunner(pool, MIGRATIONS).apply()
    clock = Clock()
    try:
        yield CommerceV1(pool, clock=clock), pool, clock
    finally:
        await pool.close()


async def _cart_and_quote(
    service: CommerceV1,
    *,
    principal: str = "principal:buyer-1",
    seller: str = "seller-1",
    sku: str = "atta-2kg",
    price: int = 12_500,
    stock: int = 10,
    quantity: int = 2,
    ttl_seconds: int = 300,
) -> tuple[dict, dict]:
    await service.upsert_inventory(
        seller_id=seller,
        sku=sku,
        title="Atta 2kg",
        unit_price_paise=price,
        available_quantity=stock,
    )
    cart = await service.create_cart(principal_id=principal, seller_id=seller)
    cart = await service.set_cart_line(
        principal_id=principal,
        cart_id=cart["cart_id"],
        sku=sku,
        quantity=quantity,
        expected_version=cart["version"],
    )
    quote = await service.preview_checkout(
        principal_id=principal,
        cart_id=cart["cart_id"],
        expected_version=cart["version"],
        ttl_seconds=ttl_seconds,
    )
    return cart, quote


async def _count(pool: ConnectionPool, table: str) -> int:
    async with pool.connection() as connection:
        result = await connection.execute(f"SELECT COUNT(*) FROM {table}")
        return (await result.fetchone())[0]


async def test_migration_applies_once_and_reruns(postgres_url: str) -> None:
    pool = ConnectionPool(postgres_url, min_size=0, max_size=2)
    await pool.open()
    try:
        runner = MigrationRunner(pool, MIGRATIONS)
        expected = [migration.number for migration in runner.discover_migrations()]
        assert await runner.apply() == expected
        assert await runner.apply() == []
    finally:
        await pool.close()


async def test_commerce_v1_http_cart_preview_replay_and_tenant_isolation(
    commerce: tuple[CommerceV1, ConnectionPool, Clock],
) -> None:
    service, pool, _clock = commerce
    await service.upsert_inventory(
        seller_id="seller-http",
        sku="atta-http",
        title="Atta HTTP",
        unit_price_paise=10_000,
        available_quantity=4,
    )
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(commerce_v1_router)
    token = create_principal_session_token(
        principal_id="principal:http-buyer",
        audience="ondcbuyer",
        identity_provider="demo",
    )
    other_token = create_principal_session_token(
        principal_id="principal:other-buyer",
        audience="ondcbuyer",
        identity_provider="demo",
    )
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        missing_key = await client.post(
            "/api/commerce/v1/carts",
            cookies={SESSION_COOKIE_NAME: token},
            json={"seller_id": "seller-http"},
        )
        assert missing_key.status_code == 422

        headers = {
            "Idempotency-Key": "cart-http-1",
            "X-Correlation-ID": "correlation-http-1",
        }
        first = await client.post(
            "/api/commerce/v1/carts",
            headers=headers,
            cookies={SESSION_COOKIE_NAME: token},
            json={"seller_id": "seller-http"},
        )
        replay = await client.post(
            "/api/commerce/v1/carts",
            headers=headers,
            cookies={SESSION_COOKIE_NAME: token},
            json={"seller_id": "seller-http"},
        )
        assert first.status_code == replay.status_code == 201
        assert first.headers["X-Correlation-ID"] == "correlation-http-1"
        cart = first.json()["data"]["cart"]
        assert replay.json()["data"]["cart"]["cart_id"] == cart["cart_id"]

        updated = await client.put(
            f"/api/commerce/v1/carts/{cart['cart_id']}/lines/atta-http",
            headers={"Idempotency-Key": "line-http-1"},
            cookies={SESSION_COOKIE_NAME: token},
            json={"quantity": 2, "expected_version": cart["version"]},
        )
        assert updated.status_code == 200
        updated_cart = updated.json()["data"]["cart"]
        preview = await client.post(
            f"/api/commerce/v1/carts/{cart['cart_id']}/checkout-preview",
            headers={"Idempotency-Key": "preview-http-1"},
            cookies={SESSION_COOKIE_NAME: token},
            json={"expected_version": updated_cart["version"]},
        )
        assert preview.status_code == 200
        assert preview.json()["data"]["quote"]["landed_total_paise"] == 20_000

        crossed = await client.get(
            f"/api/commerce/v1/carts/{cart['cart_id']}",
            cookies={SESSION_COOKIE_NAME: other_token},
        )
        assert crossed.status_code == 404


async def test_successful_purchase_is_durable_and_ledger_balanced(commerce) -> None:
    service, pool, _ = commerce
    _, quote = await _cart_and_quote(service)
    prepared = await service.prepare_checkout(
        principal_id="principal:buyer-1",
        quote_id=quote["quote_id"],
        idempotency_key="checkout-1",
    )
    result = await service.record_payment_result(
        principal_id="principal:buyer-1",
        payment_attempt_id=prepared["payment_attempt"]["payment_attempt_id"],
        status="succeeded",
        provider_reference="simulated-success-1",
    )

    assert result["order"]["status"] == "paid"
    assert result["payment_attempt"]["status"] == "succeeded"
    async with pool.connection() as connection:
        inventory = await connection.execute(
            "SELECT available_quantity, reserved_quantity FROM commerce_inventory"
        )
        assert await inventory.fetchone() == (8, 0)
        balances = await connection.execute(
            """
            SELECT ledger_transaction_id,
                   SUM(amount_paise) FILTER (WHERE side = 'debit'),
                   SUM(amount_paise) FILTER (WHERE side = 'credit')
            FROM commerce_ledger_entries GROUP BY ledger_transaction_id
            """
        )
        rows = await balances.fetchall()
        assert len(rows) == 1
        assert rows[0][1:] == (25_000, 25_000)
    async with pool.connection() as connection:
        with pytest.raises(psycopg.errors.RaiseException, match="unbalanced"):
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO commerce_ledger_transactions (
                        ledger_transaction_id, order_id, payment_attempt_id, posting_type
                    ) VALUES (%s, %s, %s, 'reconciliation')
                    """,
                    (
                        uuid4(),
                        result["order"]["order_id"],
                        result["payment_attempt"]["payment_attempt_id"],
                    ),
                )


async def test_duplicate_checkout_concurrency_creates_one_set_and_hash_mismatch_conflicts(
    commerce,
) -> None:
    service, pool, _ = commerce
    _, quote = await _cart_and_quote(service)

    async def prepare() -> dict:
        return await service.prepare_checkout(
            principal_id="principal:buyer-1",
            quote_id=quote["quote_id"],
            idempotency_key="same-key",
            request={"delivery": "standard"},
        )

    first, second = await asyncio.gather(prepare(), prepare())
    assert first == second
    assert await _count(pool, "commerce_orders") == 1
    assert await _count(pool, "commerce_inventory_reservations") == 1
    assert await _count(pool, "commerce_payment_attempts") == 1
    await service.record_payment_result(
        principal_id="principal:buyer-1",
        payment_attempt_id=first["payment_attempt"]["payment_attempt_id"],
        status="succeeded",
    )
    assert await _count(pool, "commerce_ledger_transactions") == 1

    with pytest.raises(IdempotencyConflict, match="request hash mismatch"):
        await service.prepare_checkout(
            principal_id="principal:buyer-1",
            quote_id=quote["quote_id"],
            idempotency_key="same-key",
            request={"delivery": "express"},
        )


async def test_cart_version_price_change_and_quote_expiry_fail_and_release(
    commerce,
) -> None:
    service, pool, clock = commerce
    cart, quote = await _cart_and_quote(service, ttl_seconds=1)
    with pytest.raises(CommerceConflict, match="stale cart version"):
        await service.set_cart_line(
            principal_id="principal:buyer-1",
            cart_id=cart["cart_id"],
            sku="atta-2kg",
            quantity=1,
            expected_version=1,
        )

    await service.upsert_inventory(
        seller_id="seller-1",
        sku="atta-2kg",
        title="Atta 2kg",
        unit_price_paise=13_000,
        available_quantity=10,
    )
    with pytest.raises(CommerceConflict, match="quote changed"):
        await service.prepare_checkout(
            principal_id="principal:buyer-1",
            quote_id=quote["quote_id"],
            idempotency_key="changed-price",
        )

    _, expiring = await _cart_and_quote(service, sku="rice-1kg", ttl_seconds=1)
    clock.now += timedelta(seconds=2)
    with pytest.raises(CommerceConflict, match="quote expired"):
        await service.prepare_checkout(
            principal_id="principal:buyer-1",
            quote_id=expiring["quote_id"],
            idempotency_key="expired",
        )
    async with pool.connection() as connection:
        result = await connection.execute(
            "SELECT reserved_quantity FROM commerce_inventory WHERE sku = 'rice-1kg'"
        )
        assert (await result.fetchone())[0] == 0


async def test_failed_payment_releases_inventory_and_unknown_can_reconcile(
    commerce,
) -> None:
    service, pool, _ = commerce
    _, failed_quote = await _cart_and_quote(service, sku="failed-item")
    failed = await service.prepare_checkout(
        principal_id="principal:buyer-1",
        quote_id=failed_quote["quote_id"],
        idempotency_key="failed",
    )
    failed_result = await service.record_payment_result(
        principal_id="principal:buyer-1",
        payment_attempt_id=failed["payment_attempt"]["payment_attempt_id"],
        status="failed",
    )
    assert failed_result["order"]["status"] == "payment_failed"

    _, unknown_quote = await _cart_and_quote(service, sku="unknown-item")
    unknown = await service.prepare_checkout(
        principal_id="principal:buyer-1",
        quote_id=unknown_quote["quote_id"],
        idempotency_key="unknown",
    )
    unknown_result = await service.record_payment_result(
        principal_id="principal:buyer-1",
        payment_attempt_id=unknown["payment_attempt"]["payment_attempt_id"],
        status="unknown",
    )
    assert unknown_result["payment_attempt"]["status"] == "unknown"
    reconciled = await service.reconcile_payment(
        principal_id="principal:buyer-1",
        payment_attempt_id=unknown["payment_attempt"]["payment_attempt_id"],
        outcome="succeeded",
    )
    assert reconciled["payment_attempt"]["status"] == "reconciled"
    assert reconciled["order"]["status"] == "paid"
    assert await _count(pool, "commerce_ledger_transactions") == 1
    with pytest.raises(CommerceConflict, match="not pending"):
        await service.record_payment_result(
            principal_id="principal:buyer-1",
            payment_attempt_id=unknown["payment_attempt"]["payment_attempt_id"],
            status="succeeded",
        )


async def test_restart_persistence_and_ledger_is_immutable(postgres_url: str) -> None:
    first_pool = ConnectionPool(postgres_url, min_size=0, max_size=4)
    await first_pool.open()
    await MigrationRunner(first_pool, MIGRATIONS).apply()
    service = CommerceV1(first_pool)
    _, quote = await _cart_and_quote(service)
    prepared = await service.prepare_checkout(
        principal_id="principal:buyer-1",
        quote_id=quote["quote_id"],
        idempotency_key="restart",
    )
    await service.record_payment_result(
        principal_id="principal:buyer-1",
        payment_attempt_id=prepared["payment_attempt"]["payment_attempt_id"],
        status="succeeded",
    )
    await first_pool.close()

    second_pool = ConnectionPool(postgres_url, min_size=0, max_size=2)
    await second_pool.open()
    try:
        assert await MigrationRunner(second_pool, MIGRATIONS).apply() == []
        assert await _count(second_pool, "commerce_orders") == 1
        async with second_pool.connection() as connection:
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                async with connection.transaction():
                    await connection.execute(
                        "UPDATE commerce_ledger_entries SET amount_paise = amount_paise + 1"
                    )
    finally:
        await second_pool.close()
