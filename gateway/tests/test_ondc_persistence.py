from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.persistence import ConnectionPool, MigrationRunner, UnitOfWork
from app.persistence.ondc_repository import (
    CorrelationMismatch,
    EnvelopeCommitmentMismatch,
    ONDCRepository,
    persist_callback_before_ack,
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
    schema = f"ondc_persistence_test_{uuid4().hex}"
    admin = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)
    try:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        yield make_conninfo(DATABASE_URL, options=f"-csearch_path={schema},public")
    finally:
        await admin.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )
        await admin.close()


async def _pool(postgres_url: str) -> ConnectionPool:
    pool = ConnectionPool(postgres_url, min_size=0, max_size=8)
    await pool.open()
    await MigrationRunner(pool, MIGRATIONS).apply()
    return pool


def _callback(**overrides: object) -> dict[str, object]:
    callback: dict[str, object] = {
        "subscriber_id": "seller.example",
        "transaction_id": "transaction-1",
        "message_id": "message-1",
        "action": "on_search",
        "correlation_id": "trace-1",
        "raw_envelope": {"context": {"message_id": "message-1"}},
        "redacted_payload": {"status": "ACK", "item_count": 2},
    }
    callback.update(overrides)
    return callback


async def test_migration_applies_once_and_reruns(postgres_url: str) -> None:
    pool = ConnectionPool(postgres_url, min_size=0, max_size=2)
    await pool.open()
    try:
        runner = MigrationRunner(pool, MIGRATIONS)
        assert await runner.apply() == [1, 2, 3, 30]
        assert await runner.apply() == []
        async with pool.connection() as connection:
            result = await connection.execute(
                """
                SELECT migration_number FROM schema_migrations
                ORDER BY migration_number
                """
            )
            assert [row[0] for row in await result.fetchall()] == [1, 2, 3, 30]
            result = await connection.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'ondc_inbox'
                """
            )
            columns = {row[0] for row in await result.fetchall()}
            assert "raw_envelope_commitment" in columns
            assert "raw_envelope" not in columns
    finally:
        await pool.close()


async def test_duplicate_callback_returns_existing_after_durable_commit(
    postgres_url: str,
) -> None:
    pool = await _pool(postgres_url)
    try:
        results = await asyncio.gather(
            persist_callback_before_ack(pool, **_callback()),
            persist_callback_before_ack(pool, **_callback()),
        )
        assert sorted(created for created, _ in results) == [False, True]
        assert len({row["inbox_id"] for _, row in results}) == 1

        first = results[0][1]
        replay_created, replay = await persist_callback_before_ack(pool, **_callback())
        assert replay_created is False
        assert replay["inbox_id"] == first["inbox_id"]
        assert replay["event_commitment"] == first["event_commitment"]
        async with pool.connection() as connection:
            count = await connection.execute("SELECT count(*) FROM ondc_inbox")
            assert (await count.fetchone())[0] == 1
    finally:
        await pool.close()


async def test_replay_rejects_correlation_or_envelope_mismatch(
    postgres_url: str,
) -> None:
    pool = await _pool(postgres_url)
    try:
        await persist_callback_before_ack(pool, **_callback())
        with pytest.raises(CorrelationMismatch, match="correlation_id"):
            await persist_callback_before_ack(
                pool, **_callback(correlation_id="different-trace")
            )
        with pytest.raises(EnvelopeCommitmentMismatch, match="raw envelope"):
            await persist_callback_before_ack(
                pool,
                **_callback(
                    raw_envelope={"context": {"message_id": "message-1"}, "x": 1}
                ),
            )
        with pytest.raises(ValueError, match="sensitive key"):
            await persist_callback_before_ack(
                pool, **_callback(redacted_payload={"status": "ACK", "token": "no"})
            )
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            async with UnitOfWork(pool) as unit_of_work:
                assert unit_of_work.connection is not None
                await unit_of_work.connection.execute(
                    """
                    UPDATE ondc_inbox
                    SET raw_envelope_commitment = repeat('0', 64)
                    """
                )
    finally:
        await pool.close()


async def test_concurrent_claim_has_one_winner(postgres_url: str) -> None:
    pool = await _pool(postgres_url)
    try:
        await persist_callback_before_ack(pool, **_callback())

        async def claim(worker: str) -> list[dict[str, object]]:
            async with UnitOfWork(pool) as unit_of_work:
                return await ONDCRepository(unit_of_work).claim_inbox(
                    worker_id=worker, lease_seconds=30
                )

        claims = await asyncio.gather(claim("worker-a"), claim("worker-b"))
        assert sorted(len(claimed) for claimed in claims) == [0, 1]
    finally:
        await pool.close()


async def test_expired_lease_can_be_recovered(postgres_url: str) -> None:
    pool = await _pool(postgres_url)
    try:
        await persist_callback_before_ack(pool, **_callback())
        async with UnitOfWork(pool) as unit_of_work:
            first = (
                await ONDCRepository(unit_of_work).claim_inbox(
                    worker_id="crashed-worker", lease_seconds=60
                )
            )[0]

        async with pool.connection() as connection:
            await connection.execute(
                """
                UPDATE ondc_inbox
                SET lease_expires_at = NOW() - INTERVAL '1 second'
                WHERE inbox_id = %s
                """,
                (first["inbox_id"],),
            )
            await connection.commit()

        async with UnitOfWork(pool) as unit_of_work:
            recovered = (
                await ONDCRepository(unit_of_work).claim_inbox(
                    worker_id="recovery-worker", lease_seconds=30
                )
            )[0]
        assert recovered["inbox_id"] == first["inbox_id"]
        assert recovered["lease_token"] != first["lease_token"]
        assert recovered["lease_owner"] == "recovery-worker"
    finally:
        await pool.close()


async def test_retry_backoff_then_dead_letter(postgres_url: str) -> None:
    pool = await _pool(postgres_url)
    try:
        await persist_callback_before_ack(pool, **_callback())
        before = datetime.now(timezone.utc)
        async with UnitOfWork(pool) as unit_of_work:
            repository = ONDCRepository(unit_of_work)
            claimed = (
                await repository.claim_inbox(worker_id="worker-a", lease_seconds=30)
            )[0]
            retry = await repository.schedule_retry(
                "inbox",
                claimed["inbox_id"],
                claimed["lease_token"],
                error="temporary failure",
                max_attempts=2,
                base_delay_seconds=30,
            )
        assert retry["state"] == "pending"
        assert retry["retry_count"] == 1
        assert retry["next_attempt_at"] > before

        async with UnitOfWork(pool) as unit_of_work:
            assert (
                await ONDCRepository(unit_of_work).claim_inbox(worker_id="too-early")
                == []
            )

        async with pool.connection() as connection:
            await connection.execute("UPDATE ondc_inbox SET next_attempt_at = NOW()")
            await connection.commit()
        async with UnitOfWork(pool) as unit_of_work:
            repository = ONDCRepository(unit_of_work)
            claimed = (
                await repository.claim_inbox(worker_id="worker-b", lease_seconds=30)
            )[0]
            dead = await repository.schedule_retry(
                "inbox",
                claimed["inbox_id"],
                claimed["lease_token"],
                error="permanent failure",
                max_attempts=2,
            )
        assert dead["state"] == "dead_letter"
        assert dead["retry_count"] == 2
        assert dead["last_error"] == "permanent failure"
    finally:
        await pool.close()


async def test_inbox_survives_pool_restart(postgres_url: str) -> None:
    first_pool = await _pool(postgres_url)
    _, persisted = await persist_callback_before_ack(first_pool, **_callback())
    await first_pool.close()

    second_pool = ConnectionPool(postgres_url, min_size=0, max_size=2)
    await second_pool.open()
    try:
        assert await MigrationRunner(second_pool, MIGRATIONS).apply() == []
        async with UnitOfWork(second_pool) as unit_of_work:
            loaded = await ONDCRepository(unit_of_work).get(
                "inbox", persisted["inbox_id"]
            )
        assert loaded is not None
        assert loaded["event_commitment"] == persisted["event_commitment"]
        assert loaded["redacted_payload"] == {"status": "ACK", "item_count": 2}
    finally:
        await second_pool.close()


async def test_outbox_delivery_is_idempotent_across_restart(
    postgres_url: str,
) -> None:
    pool = await _pool(postgres_url)
    outbox = {
        **_callback(action="search"),
        "destination": "https://seller.example/search",
    }
    try:
        async with UnitOfWork(pool) as unit_of_work:
            created, first = await ONDCRepository(unit_of_work).enqueue_outbox(**outbox)
        async with UnitOfWork(pool) as unit_of_work:
            replayed, replay = await ONDCRepository(unit_of_work).enqueue_outbox(
                **outbox
            )
        assert created is True
        assert replayed is False
        assert replay["outbox_id"] == first["outbox_id"]

        async with UnitOfWork(pool) as unit_of_work:
            repository = ONDCRepository(unit_of_work)
            claimed = (
                await repository.claim_outbox(worker_id="sender", lease_seconds=30)
            )[0]
            delivered = await repository.mark_delivered(
                "outbox", claimed["outbox_id"], claimed["lease_token"]
            )
            repeated = await repository.mark_delivered(
                "outbox", claimed["outbox_id"], claimed["lease_token"]
            )
        assert delivered["state"] == repeated["state"] == "delivered"
    finally:
        await pool.close()

    restarted = ConnectionPool(postgres_url, min_size=0, max_size=2)
    await restarted.open()
    try:
        async with UnitOfWork(restarted) as unit_of_work:
            created, existing = await ONDCRepository(unit_of_work).enqueue_outbox(
                **outbox
            )
        assert created is False
        assert existing["state"] == "delivered"
    finally:
        await restarted.close()
