"""Transactional repositories for the shared persistence foundation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .transaction import UnitOfWork


class IdempotencyConflict(RuntimeError):
    """The same idempotency key was reused with a different request hash."""


class IdempotencyRepository:
    """Store and retrieve principal-scoped idempotency records."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    @property
    def _connection(self) -> Any:
        connection = self._unit_of_work.connection
        if connection is None:
            raise RuntimeError("IdempotencyRepository requires an active UnitOfWork")
        return connection

    async def create_or_get(
        self,
        principal_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        resource: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Create a pending record or return the matching existing record.

        The insert and follow-up select are deliberately separate statements. Under
        PostgreSQL READ COMMITTED, a losing concurrent ``ON CONFLICT`` insert can
        then see the winner's committed row in the second statement.
        """
        columns = """
            id, principal_id, operation, idempotency_key, request_hash,
            status, response, resource, correlation_id, created_at, updated_at
        """
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                INSERT INTO idempotency_records (
                    principal_id, operation, idempotency_key, request_hash,
                    resource, correlation_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (principal_id, operation, idempotency_key) DO NOTHING
                RETURNING {columns}
                """,
                (
                    principal_id,
                    operation,
                    idempotency_key,
                    request_hash,
                    resource,
                    correlation_id,
                ),
            )
            record = await cursor.fetchone()
            created = record is not None
            if record is None:
                await cursor.execute(
                    f"""
                    SELECT {columns}
                    FROM idempotency_records
                    WHERE principal_id = %s
                      AND operation = %s
                      AND idempotency_key = %s
                    """,
                    (principal_id, operation, idempotency_key),
                )
                record = await cursor.fetchone()

        if record is None:
            raise RuntimeError("conflicting idempotency record was not visible")
        if record["request_hash"] != request_hash:
            raise IdempotencyConflict(
                "idempotency key request hash mismatch for "
                f"{principal_id}/{operation}/{idempotency_key}"
            )
        return created, record

    async def update_response(
        self,
        principal_id: str,
        operation: str,
        idempotency_key: str,
        status: str,
        response: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Persist the outcome for an existing idempotency record."""
        if status not in {"pending", "success", "failure"}:
            raise ValueError(f"unsupported idempotency status: {status}")
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                UPDATE idempotency_records
                SET status = %s, response = %s, updated_at = NOW()
                WHERE principal_id = %s
                  AND operation = %s
                  AND idempotency_key = %s
                RETURNING id, principal_id, operation, idempotency_key,
                          request_hash, status, response, resource,
                          correlation_id, created_at, updated_at
                """,
                (
                    status,
                    Jsonb(response) if response is not None else None,
                    principal_id,
                    operation,
                    idempotency_key,
                ),
            )
            return await cursor.fetchone()


class AuditRepository:
    """Append and query immutable audit events."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    @property
    def _connection(self) -> Any:
        connection = self._unit_of_work.connection
        if connection is None:
            raise RuntimeError("AuditRepository requires an active UnitOfWork")
        return connection

    async def append(
        self,
        event_id: str,
        event: str,
        principal_id: str | None = None,
        actor: str | None = None,
        resource: str | None = None,
        correlation_id: str | None = None,
        payload: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Append one audit event; no update or delete operations are exposed."""
        timestamp = occurred_at or datetime.now(timezone.utc)
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                INSERT INTO audit_events (
                    event_id, event, principal_id, actor, resource,
                    correlation_id, payload, occurred_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING event_id, event, principal_id, actor, resource,
                          correlation_id, payload, occurred_at
                """,
                (
                    event_id,
                    event,
                    principal_id,
                    actor,
                    resource,
                    correlation_id,
                    Jsonb(payload or {}),
                    timestamp,
                ),
            )
            record = await cursor.fetchone()
        if record is None:  # pragma: no cover - INSERT RETURNING always returns.
            raise RuntimeError("audit insert returned no record")
        return record

    create = append

    async def query_by_principal(
        self, principal_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT event_id, event, principal_id, actor, resource,
                       correlation_id, payload, occurred_at
                FROM audit_events
                WHERE principal_id = %s
                ORDER BY occurred_at DESC, event_id DESC
                LIMIT %s
                """,
                (principal_id, limit),
            )
            return list(await cursor.fetchall())

    async def query_by_correlation(
        self, correlation_id: str
    ) -> list[dict[str, Any]]:
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT event_id, event, principal_id, actor, resource,
                       correlation_id, payload, occurred_at
                FROM audit_events
                WHERE correlation_id = %s
                ORDER BY occurred_at ASC, event_id ASC
                """,
                (correlation_id,),
            )
            return list(await cursor.fetchall())
