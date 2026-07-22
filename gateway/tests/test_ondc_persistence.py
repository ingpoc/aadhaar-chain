from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.ondc_routes import router as ondc_router
from app.ondc_bpp import router as ondc_bpp_router
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
        expected = [migration.number for migration in runner.discover_migrations()]
        assert await runner.apply() == expected
        assert await runner.apply() == []
        async with pool.connection() as connection:
            result = await connection.execute(
                """
                SELECT migration_number FROM schema_migrations
                ORDER BY migration_number
                """
            )
            assert [row[0] for row in await result.fetchall()] == expected
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
        assert loaded["envelope"] == {"context": {"message_id": "message-1"}}
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
        assert existing["envelope"] == {"context": {"message_id": "message-1"}}
    finally:
        await restarted.close()


async def test_live_callback_persists_before_ack_and_deduplicates(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = await _pool(postgres_url)
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(ondc_router)
    payload = {
        "context": {
            "transaction_id": "transaction-live-1",
            "message_id": "message-live-1",
            "bpp_id": "seller.example",
        },
        "message": {"catalog": {"descriptor": {"name": "Seller"}}},
    }

    def reject_file_fork(_entry: dict[str, object]) -> None:
        raise AssertionError("PostgreSQL-selected callback wrote file state")

    monkeypatch.setattr("app.ondc_routes.ondc_store.append_inbox", reject_file_fork)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=api), base_url="http://test"
        ) as client:
            first = await client.post("/ondc/on_search", json=payload)
            replay = await client.post("/ondc/on_search", json=payload)
            mutated = await client.post(
                "/ondc/on_search",
                json={**payload, "message": {"catalog": {"id": "changed"}}},
            )

        assert first.status_code == replay.status_code == 200
        assert first.json()["message"]["ack"]["status"] == "ACK"
        assert replay.json()["message"]["ack"]["status"] == "ACK"
        assert mutated.status_code == 409
        assert mutated.json()["message"]["ack"]["status"] == "NACK"

        async with pool.connection() as connection:
            result = await connection.execute(
                """
                SELECT COUNT(*), MIN(state), MIN(correlation_id)
                FROM ondc_inbox
                WHERE transaction_id = %s AND message_id = %s
                """,
                ("transaction-live-1", "message-live-1"),
            )
            count, state, correlation_id = await result.fetchone()
        assert count == 1
        assert state == "pending"
        assert correlation_id == "transaction-live-1"
    finally:
        await pool.close()


async def test_live_callback_nacks_before_persistence_on_invalid_correlation(
    postgres_url: str,
) -> None:
    pool = await _pool(postgres_url)
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(ondc_router)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=api), base_url="http://test"
        ) as client:
            response = await client.post(
                "/ondc/on_search",
                json={
                    "context": {
                        "message_id": "message-without-transaction",
                        "bpp_id": "seller.example",
                    }
                },
            )

        assert response.status_code == 400
        assert response.json()["message"]["ack"]["status"] == "NACK"
        async with pool.connection() as connection:
            result = await connection.execute("SELECT COUNT(*) FROM ondc_inbox")
            assert (await result.fetchone())[0] == 0
    finally:
        await pool.close()


async def test_live_confirm_uses_durable_outbox_and_one_effect_replay(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = await _pool(postgres_url)
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(ondc_router)
    signed_post = AsyncMock(
        return_value=(200, {"message": {"ack": {"status": "ACK"}}}, {})
    )
    monkeypatch.setattr("app.ondc_routes._ondc_configured", lambda: True)
    monkeypatch.setattr("app.ondc_routes._signed_post", signed_post)

    def reject_file_fork(_entry: dict[str, object]) -> None:
        raise AssertionError("PostgreSQL-selected outbox wrote file state")

    monkeypatch.setattr("app.ondc_routes.ondc_store.append_outbox", reject_file_fork)
    body = {
        "transaction_id": "transaction-confirm-1",
        "message_id": "message-confirm-1",
        "bpp_id": "seller.example",
        "bpp_uri": "https://seller.example/ondc",
        "order": {"id": "order-1"},
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=api), base_url="http://test"
        ) as client:
            first = await client.post("/api/ondc/confirm", json=body)
            replay = await client.post("/api/ondc/confirm", json=body)

        assert first.status_code == replay.status_code == 200
        assert first.json()["data"]["dispatched"] is True
        assert replay.json()["data"]["dispatched"] is False
        assert replay.json()["data"]["deduplicated"] is True
        assert first.json()["data"]["outbox_id"] == replay.json()["data"]["outbox_id"]
        assert signed_post.await_count == 1

        async with pool.connection() as connection:
            result = await connection.execute(
                """
                SELECT state, retry_count, envelope
                FROM ondc_outbox
                WHERE transaction_id = %s AND message_id = %s
                """,
                ("transaction-confirm-1", "message-confirm-1"),
            )
            state, retry_count, envelope = await result.fetchone()
        assert state == "delivered"
        assert retry_count == 0
        assert envelope["message"]["order"] == {"id": "order-1"}
    finally:
        await pool.close()


async def test_live_confirm_failure_remains_retryable(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = await _pool(postgres_url)
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(ondc_router)
    monkeypatch.setattr("app.ondc_routes._ondc_configured", lambda: True)
    monkeypatch.setattr(
        "app.ondc_routes._signed_post",
        AsyncMock(side_effect=RuntimeError("network unavailable")),
    )
    body = {
        "transaction_id": "transaction-confirm-retry",
        "message_id": "message-confirm-retry",
        "bpp_id": "seller.example",
        "bpp_uri": "https://seller.example/ondc",
        "order": {"id": "order-retry"},
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=api), base_url="http://test"
        ) as client:
            response = await client.post("/api/ondc/confirm", json=body)

        assert response.status_code == 502
        async with pool.connection() as connection:
            result = await connection.execute(
                """
                SELECT state, retry_count, last_error, envelope
                FROM ondc_outbox
                WHERE transaction_id = %s AND message_id = %s
                """,
                ("transaction-confirm-retry", "message-confirm-retry"),
            )
            state, retry_count, last_error, envelope = await result.fetchone()
        assert state == "pending"
        assert retry_count == 1
        assert last_error == "network unavailable"
        assert envelope["message"]["order"] == {"id": "order-retry"}
    finally:
        await pool.close()


async def test_dead_letter_requires_commitment_and_requeues_durably(
    postgres_url: str,
) -> None:
    pool = await _pool(postgres_url)
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(ondc_router)
    try:
        _, persisted = await persist_callback_before_ack(pool, **_callback())
        async with UnitOfWork(pool) as unit_of_work:
            repository = ONDCRepository(unit_of_work)
            claimed = (
                await repository.claim_inbox(worker_id="dead-letter-test")
            )[0]
            dead = await repository.schedule_retry(
                "inbox",
                claimed["inbox_id"],
                claimed["lease_token"],
                error="permanent",
                max_attempts=1,
            )
        async with AsyncClient(
            transport=ASGITransport(app=api), base_url="http://test"
        ) as client:
            mismatch = await client.post(
                f"/api/ondc/inbox/dead-letter/{dead['inbox_id']}/requeue",
                headers={
                    "Idempotency-Key": "recover-dead-letter",
                    "X-Correlation-ID": "dead-letter-test",
                },
                json={"event_commitment": "0" * 64},
            )
            recovered = await client.post(
                f"/api/ondc/inbox/dead-letter/{dead['inbox_id']}/requeue",
                headers={
                    "Idempotency-Key": "recover-dead-letter",
                    "X-Correlation-ID": "dead-letter-test",
                },
                json={"event_commitment": persisted["event_commitment"]},
            )
            replay = await client.post(
                f"/api/ondc/inbox/dead-letter/{dead['inbox_id']}/requeue",
                headers={
                    "Idempotency-Key": "recover-dead-letter",
                    "X-Correlation-ID": "dead-letter-test",
                },
                json={"event_commitment": persisted["event_commitment"]},
            )
        assert mismatch.status_code == 409
        assert recovered.status_code == replay.status_code == 200
        assert recovered.json()["data"]["state"] == "pending"
        assert replay.json()["data"]["state"] == "pending"
        assert recovered.json()["data"]["retry_count"] == 1
    finally:
        await pool.close()


async def test_postgres_diagnostics_and_recovery_drain_never_read_files(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = await _pool(postgres_url)
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(ondc_router)
    outbox = {
        "subscriber_id": "buyer.example",
        "transaction_id": "transaction-drain",
        "message_id": "message-drain",
        "action": "confirm",
        "destination": "https://seller.example/confirm",
        "correlation_id": "transaction-drain",
        "raw_envelope": {
            "context": {
                "transaction_id": "transaction-drain",
                "message_id": "message-drain",
            },
            "message": {"order": {"id": "order-drain"}},
        },
        "redacted_payload": {"status": "queued"},
    }
    async with UnitOfWork(pool) as unit_of_work:
        await ONDCRepository(unit_of_work).enqueue_outbox(**outbox)

    def reject_file_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("PostgreSQL diagnostics consulted file state")

    monkeypatch.setattr("app.ondc_routes.ondc_store.list_outbox", reject_file_read)
    monkeypatch.setattr("app.ondc_routes.ondc_store.list_inbox", reject_file_read)
    monkeypatch.setattr(
        "app.ondc_routes._signed_post",
        AsyncMock(return_value=(200, {"message": {"ack": {"status": "ACK"}}}, "")),
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=api), base_url="http://test"
        ) as client:
            diagnostics = await client.get(
                "/api/ondc/outbox", params={"transaction_id": "transaction-drain"}
            )
            orders = await client.get(
                "/api/ondc/orders", params={"transaction_id": "transaction-drain"}
            )
            drained = await client.post(
                "/api/ondc/outbox/drain",
                headers={
                    "Idempotency-Key": "restart-drain",
                    "X-Correlation-ID": "restart-test",
                },
                json={"worker_id": "restart-worker", "limit": 10},
            )
        assert diagnostics.status_code == orders.status_code == drained.status_code == 200
        assert diagnostics.json()["data"]["items"][0]["state"] == "pending"
        assert orders.json()["data"]["items"][0]["order"]["id"] == "order-drain"
        assert drained.json()["data"]["claimed"] == 1
        assert drained.json()["data"]["items"][0]["state"] == "delivered"
    finally:
        await pool.close()


async def test_bpp_command_persists_before_ack_and_replay_has_one_effect(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = await _pool(postgres_url)
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(ondc_bpp_router)
    callback = AsyncMock()
    monkeypatch.setattr("app.ondc_bpp.settings.ondc_enabled", True)
    monkeypatch.setattr("app.ondc_bpp._post_on_action", callback)
    payload = {
        "context": {
            "bap_id": "buyer.example",
            "bap_uri": "https://buyer.example/ondc",
            "transaction_id": "bpp-transaction",
            "message_id": "bpp-message",
        },
        "message": {"order": {"items": [{"id": "sku-1"}]}},
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=api), base_url="http://test"
        ) as client:
            first = await client.post("/ondc/select", json=payload)
            replay = await client.post("/ondc/select", json=payload)
            mutated = await client.post(
                "/ondc/select",
                json={**payload, "message": {"order": {"items": [{"id": "sku-2"}]}}},
            )
        assert first.status_code == replay.status_code == 200
        assert mutated.status_code == 409
        callback.assert_awaited_once()
        async with pool.connection() as connection:
            result = await connection.execute(
                """
                SELECT COUNT(*), MIN(action) FROM ondc_inbox
                WHERE transaction_id = 'bpp-transaction'
                """
            )
            assert await result.fetchone() == (1, "select")
    finally:
        await pool.close()
