"""Pooled connections killed server-side must not surface as request 500s.

Preprod defect (2026-10-06): GET /api/cart returned 500 with
``psycopg.OperationalError: consuming input failed: SSL connection has been
closed unexpectedly`` after Neon's pooler dropped an idle pooled connection.
"""

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

from app.cart_routes import router as cart_router
from app.persistence import ConnectionPool, MigrationRunner

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
async def admin() -> AsyncIterator[psycopg.AsyncConnection]:
    assert DATABASE_URL is not None
    connection = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def schema_url(admin: psycopg.AsyncConnection) -> AsyncIterator[str]:
    assert DATABASE_URL is not None
    schema = f"pool_health_{uuid4().hex}"
    await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield make_conninfo(DATABASE_URL, options=f"-csearch_path={schema},public")
    finally:
        await admin.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )


async def _backend_pid(pool: ConnectionPool) -> int:
    async with pool.connection() as connection:
        return int(connection.info.backend_pid)


async def _terminate(admin: psycopg.AsyncConnection, pid: int) -> None:
    """Kill the pooled backend the way an idle pooler/compute drop does."""
    await admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    for _ in range(100):
        result = await admin.execute(
            "SELECT 1 FROM pg_stat_activity WHERE pid = %s", (pid,)
        )
        if await result.fetchone() is None:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"backend {pid} did not terminate")


async def test_pool_replaces_connection_killed_while_idle(
    admin: psycopg.AsyncConnection, schema_url: str
) -> None:
    pool = ConnectionPool(schema_url, min_size=1, max_size=1)
    await pool.open()
    try:
        dead_pid = await _backend_pid(pool)
        await _terminate(admin, dead_pid)

        async with pool.connection() as connection:
            result = await connection.execute("SELECT 1")
            assert await result.fetchone() == (1,)
            assert connection.info.backend_pid != dead_pid
    finally:
        await pool.close()


async def test_cart_read_succeeds_after_pooled_backend_is_killed(
    admin: psycopg.AsyncConnection, schema_url: str
) -> None:
    pool = ConnectionPool(schema_url, min_size=1, max_size=1)
    await pool.open()
    await MigrationRunner(pool, MIGRATIONS).apply()
    app = FastAPI()
    app.include_router(cart_router)
    app.state.persistence_pool = pool
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            first = await client.get("/api/cart", params={"sessionId": "pool-health-1"})
            assert first.status_code == 200, first.text

            await _terminate(admin, await _backend_pid(pool))

            second = await client.get("/api/cart", params={"sessionId": "pool-health-1"})
            assert second.status_code == 200, second.text
            assert second.json()["success"] is True
    finally:
        await pool.close()


def test_pool_is_configured_with_health_check_and_idle_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PG_POOL_MAX_IDLE_SECONDS", raising=False)
    monkeypatch.delenv("PG_POOL_MAX_LIFETIME_SECONDS", raising=False)
    pool = ConnectionPool("postgresql://unused@127.0.0.1:1/unused")
    inner = pool._pool
    assert inner._check is not None
    assert 0 < inner.max_idle <= 240  # below Neon pooler/compute idle drops
    assert 0 < inner.max_lifetime <= 3600

    monkeypatch.setenv("PG_POOL_MAX_IDLE_SECONDS", "45")
    monkeypatch.setenv("PG_POOL_MAX_LIFETIME_SECONDS", "900")
    tuned = ConnectionPool("postgresql://unused@127.0.0.1:1/unused")
    assert tuned._pool.max_idle == 45
    assert tuned._pool.max_lifetime == 900
