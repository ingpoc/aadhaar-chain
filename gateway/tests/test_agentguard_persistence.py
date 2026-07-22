from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.persistence import ConnectionPool, MigrationRunner, UnitOfWork
from app.persistence.agentguard_repository import (
    AgentGuardConflict,
    AgentGuardPermissionDenied,
    AgentGuardRepository,
    utcnow,
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
    schema = f"agentguard_test_{uuid4().hex}"
    admin = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)
    try:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        yield make_conninfo(DATABASE_URL, options=f"-csearch_path={schema},public")
    finally:
        await admin.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )
        await admin.close()


async def _open_migrated_pool(
    postgres_url: str, *, max_size: int = 8
) -> ConnectionPool:
    pool = ConnectionPool(postgres_url, min_size=0, max_size=max_size)
    await pool.open()
    await MigrationRunner(pool, MIGRATIONS).apply()
    return pool


async def _create_authority(
    pool: ConnectionPool,
    *,
    suffix: str,
    principal_id: str = "principal-owner",
) -> dict[str, str]:
    ids = {
        "principal_id": principal_id,
        "agent_id": f"agent-{suffix}",
        "mandate_id": f"mandate-{suffix}",
        "decision_id": f"decision-{suffix}",
        "approval_id": f"approval-{suffix}",
    }
    async with UnitOfWork(pool) as unit_of_work:
        repository = AgentGuardRepository(unit_of_work)
        await repository.create_agent(
            agent_id=ids["agent_id"],
            principal_id=principal_id,
            role="buyer",
            payload={"name": "Buyer agent"},
        )
        await repository.create_mandate_version(
            mandate_id=ids["mandate_id"],
            version=1,
            principal_id=principal_id,
            agent_id=ids["agent_id"],
            payload={"allowed_actions": ["checkout"]},
        )
        await repository.record_decision(
            decision_id=ids["decision_id"],
            principal_id=principal_id,
            agent_id=ids["agent_id"],
            mandate_id=ids["mandate_id"],
            mandate_version=1,
            status="approval_required",
            policy={"policy_id": "checkout-v2", "version": 2},
            risk={"level": "medium", "score": 0.52},
            required_action="human_approval",
            expiry=utcnow() + timedelta(minutes=10),
            payload={"reason_codes": ["amount_threshold"]},
        )
        await repository.issue_approval(
            approval_id=ids["approval_id"],
            principal_id=principal_id,
            decision_id=ids["decision_id"],
            agent_id=ids["agent_id"],
            mandate_id=ids["mandate_id"],
            mandate_version=1,
            request_hash=f"hash-{suffix}",
            expires_at=utcnow() + timedelta(minutes=10),
            payload={"operation": "checkout"},
        )
    return ids


async def test_migration_rerun_is_clean(postgres_url: str) -> None:
    pool = ConnectionPool(postgres_url, min_size=0, max_size=2)
    await pool.open()
    try:
        runner = MigrationRunner(pool, MIGRATIONS)
        assert await runner.apply() == [1, 2, 3, 10]
        assert await runner.apply() == []
        async with pool.connection() as connection:
            result = await connection.execute(
                "SELECT migration_number FROM schema_migrations ORDER BY migration_number"
            )
            assert [row[0] for row in await result.fetchall()] == [1, 2, 3, 10]
    finally:
        await pool.close()


async def test_records_survive_pool_restart_and_preserve_decision_v2(
    postgres_url: str,
) -> None:
    first_pool = await _open_migrated_pool(postgres_url)
    ids = await _create_authority(first_pool, suffix="restart")
    async with UnitOfWork(first_pool) as unit_of_work:
        repository = AgentGuardRepository(unit_of_work)
        await repository.record_receipt(
            receipt_id="receipt-restart",
            principal_id=ids["principal_id"],
            agent_id=ids["agent_id"],
            mandate_id=ids["mandate_id"],
            mandate_version=1,
            decision_id=ids["decision_id"],
            approval_id=ids["approval_id"],
            status="approved",
            payload={"resource_id": "order-1"},
        )
    await first_pool.close()

    second_pool = ConnectionPool(postgres_url, min_size=0, max_size=2)
    await second_pool.open()
    try:
        assert await MigrationRunner(second_pool, MIGRATIONS).apply() == []
        async with UnitOfWork(second_pool) as unit_of_work:
            repository = AgentGuardRepository(unit_of_work)
            decision = await repository.get_decision(
                principal_id=ids["principal_id"], decision_id=ids["decision_id"]
            )
            receipt = await repository.get_receipt(
                principal_id=ids["principal_id"], receipt_id="receipt-restart"
            )
        assert decision is not None
        assert decision["decision_id"] == ids["decision_id"]
        assert decision["policy"] == {"policy_id": "checkout-v2", "version": 2}
        assert decision["risk"] == {"level": "medium", "score": 0.52}
        assert decision["required_action"] == "human_approval"
        assert decision["expiry"] is not None
        assert receipt is not None
        assert receipt["payload"] == {"resource_id": "order-1"}
    finally:
        await second_pool.close()


async def test_mandate_versions_are_immutable(postgres_url: str) -> None:
    pool = await _open_migrated_pool(postgres_url)
    try:
        ids = await _create_authority(pool, suffix="immutable")
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            async with UnitOfWork(pool) as unit_of_work:
                assert unit_of_work.connection is not None
                await unit_of_work.connection.execute(
                    """
                    UPDATE agentguard_mandate_versions
                    SET payload = '{"changed": true}'::JSONB
                    WHERE principal_id = %s AND mandate_id = %s AND version = 1
                    """,
                    (ids["principal_id"], ids["mandate_id"]),
                )
    finally:
        await pool.close()


async def test_cross_principal_reads_and_consume_are_denied(postgres_url: str) -> None:
    pool = await _open_migrated_pool(postgres_url)
    try:
        ids = await _create_authority(pool, suffix="scope")
        async with UnitOfWork(pool) as unit_of_work:
            repository = AgentGuardRepository(unit_of_work)
            assert (
                await repository.get_agent(
                    principal_id="principal-attacker", agent_id=ids["agent_id"]
                )
                is None
            )
            assert (
                await repository.get_decision(
                    principal_id="principal-attacker", decision_id=ids["decision_id"]
                )
                is None
            )
        with pytest.raises(AgentGuardPermissionDenied, match="principal mismatch"):
            async with UnitOfWork(pool) as unit_of_work:
                await AgentGuardRepository(unit_of_work).consume_approval(
                    principal_id="principal-attacker",
                    approval_id=ids["approval_id"],
                    request_hash="hash-scope",
                )
    finally:
        await pool.close()


async def test_concurrent_approval_consume_has_exactly_one_winner(
    postgres_url: str,
) -> None:
    pool = await _open_migrated_pool(postgres_url)
    try:
        ids = await _create_authority(pool, suffix="race")

        async def consume() -> str:
            try:
                async with UnitOfWork(pool) as unit_of_work:
                    await AgentGuardRepository(unit_of_work).consume_approval(
                        principal_id=ids["principal_id"],
                        approval_id=ids["approval_id"],
                        request_hash="hash-race",
                    )
                return "winner"
            except AgentGuardConflict:
                return "conflict"

        assert sorted(await asyncio.gather(consume(), consume())) == [
            "conflict",
            "winner",
        ]
        async with UnitOfWork(pool) as unit_of_work:
            approval = await AgentGuardRepository(unit_of_work).get_approval(
                principal_id=ids["principal_id"], approval_id=ids["approval_id"]
            )
        assert approval is not None
        assert approval["status"] == "consumed"
        assert approval["consumed_at"] is not None
    finally:
        await pool.close()


@pytest.mark.parametrize(
    ("status", "approval_status"),
    [("paused", "expired"), ("revoked", "revoked")],
)
async def test_pause_and_revoke_invalidate_pending_approval(
    postgres_url: str, status: str, approval_status: str
) -> None:
    pool = await _open_migrated_pool(postgres_url)
    try:
        ids = await _create_authority(pool, suffix=status)
        async with UnitOfWork(pool) as unit_of_work:
            await AgentGuardRepository(unit_of_work).set_agent_status(
                principal_id=ids["principal_id"],
                agent_id=ids["agent_id"],
                status=status,
            )
        with pytest.raises(
            AgentGuardConflict, match=f"not consumable: {approval_status}"
        ):
            async with UnitOfWork(pool) as unit_of_work:
                await AgentGuardRepository(unit_of_work).consume_approval(
                    principal_id=ids["principal_id"],
                    approval_id=ids["approval_id"],
                    request_hash=f"hash-{status}",
                )
        if status == "revoked":
            with pytest.raises(AgentGuardConflict, match="transition rejected"):
                async with UnitOfWork(pool) as unit_of_work:
                    await AgentGuardRepository(unit_of_work).set_agent_status(
                        principal_id=ids["principal_id"],
                        agent_id=ids["agent_id"],
                        status="active",
                    )
    finally:
        await pool.close()


async def test_execution_intent_idempotency_and_hash_conflict(
    postgres_url: str,
) -> None:
    pool = await _open_migrated_pool(postgres_url)
    try:
        ids = await _create_authority(pool, suffix="intent")
        async with UnitOfWork(pool) as unit_of_work:
            repository = AgentGuardRepository(unit_of_work)
            first, first_created = await repository.create_execution_intent(
                intent_id="intent-original",
                principal_id=ids["principal_id"],
                operation="checkout",
                idempotency_key="request-42",
                request_hash="request-hash-a",
                decision_id=ids["decision_id"],
                approval_id=ids["approval_id"],
                payload={"cart_id": "cart-1"},
            )
        async with UnitOfWork(pool) as unit_of_work:
            replay, replay_created = await AgentGuardRepository(
                unit_of_work
            ).create_execution_intent(
                intent_id="intent-ignored-on-replay",
                principal_id=ids["principal_id"],
                operation="checkout",
                idempotency_key="request-42",
                request_hash="request-hash-a",
                decision_id=ids["decision_id"],
                approval_id=ids["approval_id"],
                payload={"cart_id": "cart-1"},
            )
        assert first_created is True
        assert replay_created is False
        assert first["intent_id"] == replay["intent_id"] == "intent-original"

        with pytest.raises(AgentGuardConflict, match="different request hash"):
            async with UnitOfWork(pool) as unit_of_work:
                await AgentGuardRepository(unit_of_work).create_execution_intent(
                    intent_id="intent-conflict",
                    principal_id=ids["principal_id"],
                    operation="checkout",
                    idempotency_key="request-42",
                    request_hash="request-hash-b",
                )

        other = await _create_authority(
            pool, suffix="intent-other", principal_id="principal-other"
        )
        async with UnitOfWork(pool) as unit_of_work:
            other_intent, created = await AgentGuardRepository(
                unit_of_work
            ).create_execution_intent(
                intent_id="intent-other-principal",
                principal_id=other["principal_id"],
                operation="checkout",
                idempotency_key="request-42",
                request_hash="request-hash-b",
                decision_id=other["decision_id"],
                approval_id=other["approval_id"],
            )
        assert created is True
        assert other_intent["principal_id"] == "principal-other"
    finally:
        await pool.close()
