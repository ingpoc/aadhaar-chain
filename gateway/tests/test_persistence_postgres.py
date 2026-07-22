from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.persistence import (
    AuditRepository,
    ConnectionPool,
    IdempotencyConflict,
    IdempotencyRepository,
    MigrationRunner,
    UnitOfWork,
)


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
    schema = f"persistence_test_{uuid4().hex}"
    admin = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)
    try:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        yield make_conninfo(
            DATABASE_URL,
            options=f"-csearch_path={schema},public",
        )
    finally:
        await admin.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )
        await admin.close()


async def _open_migrated_pool(postgres_url: str) -> ConnectionPool:
    pool = ConnectionPool(postgres_url, min_size=0, max_size=8)
    await pool.open()
    await MigrationRunner(pool, MIGRATIONS).apply()
    return pool


async def test_migrations_apply_once_and_rerun_cleanly(postgres_url: str) -> None:
    pool = ConnectionPool(postgres_url, min_size=0, max_size=2)
    await pool.open()
    try:
        runner = MigrationRunner(pool, MIGRATIONS)
        expected = [migration.number for migration in runner.discover_migrations()]
        assert await runner.apply() == expected
        assert await runner.apply() == []

        async with pool.connection() as connection:
            result = await connection.execute(
                "SELECT migration_number FROM schema_migrations ORDER BY migration_number"
            )
            assert [row[0] for row in await result.fetchall()] == expected
    finally:
        await pool.close()


async def test_concurrent_same_key_creates_one_record(postgres_url: str) -> None:
    pool = await _open_migrated_pool(postgres_url)
    try:
        async def create() -> bool:
            async with UnitOfWork(pool) as unit_of_work:
                created, _ = await IdempotencyRepository(
                    unit_of_work
                ).create_or_get("principal-1", "checkout", "key-1", "hash-1")
                return created

        created = await asyncio.gather(create(), create())
        assert sorted(created) == [False, True]

        async with pool.connection() as connection:
            count = await connection.execute(
                "SELECT COUNT(*) FROM idempotency_records"
            )
            assert (await count.fetchone())[0] == 1
    finally:
        await pool.close()


async def test_same_key_with_different_hash_conflicts(postgres_url: str) -> None:
    pool = await _open_migrated_pool(postgres_url)
    try:
        async with UnitOfWork(pool) as unit_of_work:
            await IdempotencyRepository(unit_of_work).create_or_get(
                "principal-1", "checkout", "key-1", "hash-1"
            )

        with pytest.raises(IdempotencyConflict, match="request hash mismatch"):
            async with UnitOfWork(pool) as unit_of_work:
                await IdempotencyRepository(unit_of_work).create_or_get(
                    "principal-1", "checkout", "key-1", "hash-2"
                )
    finally:
        await pool.close()


async def test_unit_of_work_rollback_is_atomic(postgres_url: str) -> None:
    pool = await _open_migrated_pool(postgres_url)
    try:
        with pytest.raises(RuntimeError, match="force rollback"):
            async with UnitOfWork(pool) as unit_of_work:
                await IdempotencyRepository(unit_of_work).create_or_get(
                    "principal-1", "checkout", "key-1", "hash-1"
                )
                await AuditRepository(unit_of_work).append(
                    "event-1", "checkout.started", principal_id="principal-1"
                )
                raise RuntimeError("force rollback")

        async with pool.connection() as connection:
            records = await connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM idempotency_records),
                    (SELECT COUNT(*) FROM audit_events)
                """
            )
            assert await records.fetchone() == (0, 0)
    finally:
        await pool.close()


async def test_records_survive_pool_restart(postgres_url: str) -> None:
    first_pool = await _open_migrated_pool(postgres_url)
    async with UnitOfWork(first_pool) as unit_of_work:
        await IdempotencyRepository(unit_of_work).create_or_get(
            "principal-1", "checkout", "key-1", "hash-1"
        )
        await AuditRepository(unit_of_work).append(
            "event-1",
            "checkout.started",
            principal_id="principal-1",
            payload={"attempt": 1},
        )
    await first_pool.close()

    second_pool = ConnectionPool(postgres_url, min_size=0, max_size=2)
    await second_pool.open()
    try:
        assert await MigrationRunner(second_pool, MIGRATIONS).apply() == []
        async with UnitOfWork(second_pool) as unit_of_work:
            created, record = await IdempotencyRepository(
                unit_of_work
            ).create_or_get("principal-1", "checkout", "key-1", "hash-1")
            events = await AuditRepository(unit_of_work).query_by_principal(
                "principal-1"
            )
        assert created is False
        assert record["request_hash"] == "hash-1"
        assert events[0]["payload"] == {"attempt": 1}
    finally:
        await second_pool.close()


async def test_audit_events_reject_mutation(postgres_url: str) -> None:
    pool = await _open_migrated_pool(postgres_url)
    try:
        async with UnitOfWork(pool) as unit_of_work:
            await AuditRepository(unit_of_work).append("event-1", "checkout.started")

        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            async with UnitOfWork(pool) as unit_of_work:
                assert unit_of_work.connection is not None
                await unit_of_work.connection.execute(
                    "UPDATE audit_events SET event = 'changed' WHERE event_id = 'event-1'"
                )
    finally:
        await pool.close()
