"""Durable ONDC inbox/outbox primitives.

The immutable envelope is retained with its commitment so leased workers can
recover after a process restart. A bounded redacted projection remains available
for operational inspection without exposing the full protocol payload.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .connection import ConnectionPool
from .transaction import UnitOfWork

Queue = Literal["inbox", "outbox"]
_REDACTED_LIMIT_BYTES = 4096
_SENSITIVE_KEYS = {
    "address",
    "authorization",
    "email",
    "name",
    "payment",
    "phone",
    "prompt",
    "raw",
    "token",
}


class CorrelationMismatch(RuntimeError):
    """A commitment was reused with different correlation data."""


class EnvelopeCommitmentMismatch(RuntimeError):
    """A deduplicated message did not contain the original raw envelope."""


class LeaseMismatch(RuntimeError):
    """A queue mutation was attempted without the active claim lease."""


def _required(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field} is required")
    return value


def _json_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _commitment(*parts: str) -> str:
    encoded = json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode()
    return sha256(encoded).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _validate_redacted_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("redacted_payload must be an object")

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key.casefold() in _SENSITIVE_KEYS:
                    raise ValueError(f"redacted_payload contains sensitive key: {key}")
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    if len(_json_bytes(payload)) > _REDACTED_LIMIT_BYTES:
        raise ValueError("redacted_payload exceeds 4096 bytes")
    return payload


class ONDCRepository:
    """Operate on ONDC messaging rows inside one explicit unit of work."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    @property
    def _connection(self) -> Any:
        connection = self._unit_of_work.connection
        if connection is None:
            raise RuntimeError("ONDCRepository requires an active UnitOfWork")
        return connection

    async def persist_callback(
        self,
        *,
        subscriber_id: str,
        transaction_id: str,
        message_id: str,
        action: str,
        raw_envelope: Any,
        redacted_payload: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Persist a callback once, returning the prior row for an exact replay."""
        values = self._correlation_values(
            subscriber_id, transaction_id, message_id, action, correlation_id
        )
        event_commitment = _commitment(*values[:4])
        raw_commitment = sha256(_json_bytes(raw_envelope)).hexdigest()
        projection = _validate_redacted_payload(
            {} if redacted_payload is None else redacted_payload
        )
        columns = self._columns("inbox")
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                INSERT INTO ondc_inbox (
                    event_commitment, subscriber_id, transaction_id, message_id,
                    action, correlation_id, raw_envelope_commitment,
                    envelope, redacted_payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING {columns}
                """,
                (
                    event_commitment,
                    *values,
                    raw_commitment,
                    Jsonb(raw_envelope),
                    Jsonb(projection),
                ),
            )
            created = await cursor.fetchone()
            if created is not None:
                return True, created
            await cursor.execute(
                f"SELECT {columns} FROM ondc_inbox WHERE event_commitment = %s",
                (event_commitment,),
            )
            existing = await cursor.fetchone()
        if existing is None:  # Defensive: the unique winner must be visible now.
            raise RuntimeError("deduplicated ONDC callback disappeared")
        self._validate_existing(existing, values, raw_commitment, projection)
        return False, existing

    async def enqueue_outbox(
        self,
        *,
        subscriber_id: str,
        transaction_id: str,
        message_id: str,
        action: str,
        destination: str,
        raw_envelope: Any,
        redacted_payload: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        source_event_commitment: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Create one delivery intent and return it on idempotent retries."""
        values = self._correlation_values(
            subscriber_id, transaction_id, message_id, action, correlation_id
        )
        destination = _required(destination, "destination")
        event_commitment = source_event_commitment or _commitment(
            "outbox", *values[:4], destination
        )
        if not _is_sha256(event_commitment):
            raise ValueError("source_event_commitment must be a SHA-256 hex digest")
        raw_commitment = sha256(_json_bytes(raw_envelope)).hexdigest()
        projection = _validate_redacted_payload(
            {} if redacted_payload is None else redacted_payload
        )
        columns = self._columns("outbox")
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                INSERT INTO ondc_outbox (
                    event_commitment, subscriber_id, transaction_id, message_id,
                    action, correlation_id, destination,
                    raw_envelope_commitment, envelope, redacted_payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_commitment) DO NOTHING
                RETURNING {columns}
                """,
                (
                    event_commitment,
                    *values,
                    destination,
                    raw_commitment,
                    Jsonb(raw_envelope),
                    Jsonb(projection),
                ),
            )
            created = await cursor.fetchone()
            if created is not None:
                return True, created
            await cursor.execute(
                f"SELECT {columns} FROM ondc_outbox WHERE event_commitment = %s",
                (event_commitment,),
            )
            existing = await cursor.fetchone()
        if existing is None:
            raise RuntimeError("deduplicated ONDC outbox event disappeared")
        self._validate_existing(existing, values, raw_commitment, projection)
        if existing["destination"] != destination:
            raise CorrelationMismatch("destination does not match persisted event")
        return False, existing

    async def claim_inbox(
        self, *, worker_id: str, lease_seconds: int = 30, limit: int = 1
    ) -> list[dict[str, Any]]:
        return await self._claim("inbox", worker_id, lease_seconds, limit)

    async def claim_outbox(
        self, *, worker_id: str, lease_seconds: int = 30, limit: int = 1
    ) -> list[dict[str, Any]]:
        return await self._claim("outbox", worker_id, lease_seconds, limit)

    async def claim_outbox_record(
        self, record_id: int, *, worker_id: str, lease_seconds: int = 30
    ) -> dict[str, Any] | None:
        """Claim one freshly staged or expired outbox record for inline delivery."""
        if not worker_id.strip() or lease_seconds < 1:
            raise ValueError("worker_id and positive lease_seconds are required")
        columns = self._columns("outbox")
        lease_token = uuid4()
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                UPDATE ondc_outbox
                SET state = 'processing', lease_token = %s, lease_owner = %s,
                    lease_expires_at = NOW() + make_interval(secs => %s),
                    updated_at = NOW()
                WHERE outbox_id = %s
                  AND next_attempt_at <= NOW()
                  AND (
                    state = 'pending'
                    OR (state = 'processing' AND lease_expires_at <= NOW())
                  )
                RETURNING {columns}
                """,
                (lease_token, worker_id, lease_seconds, record_id),
            )
            return await cursor.fetchone()

    async def mark_delivered(
        self, queue: Queue, record_id: int, lease_token: UUID
    ) -> dict[str, Any]:
        """Complete a claim; repeating the same completion is harmless."""
        table, id_column = self._table(queue)
        columns = self._columns(queue)
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                UPDATE {table}
                SET state = 'delivered', delivered_at = COALESCE(delivered_at, NOW()),
                    updated_at = NOW(), lease_expires_at = NULL, lease_owner = NULL
                WHERE {id_column} = %s AND lease_token = %s
                  AND state = 'processing'
                RETURNING {columns}
                """,
                (record_id, lease_token),
            )
            row = await cursor.fetchone()
            if row is not None:
                return row
            await cursor.execute(
                f"SELECT {columns} FROM {table} WHERE {id_column} = %s",
                (record_id,),
            )
            row = await cursor.fetchone()
        if (
            row is not None
            and row["state"] == "delivered"
            and row["lease_token"] == lease_token
        ):
            return row
        raise LeaseMismatch(f"{queue} record is not held by this lease")

    async def schedule_retry(
        self,
        queue: Queue,
        record_id: int,
        lease_token: UUID,
        *,
        error: str,
        max_attempts: int = 5,
        base_delay_seconds: int = 1,
    ) -> dict[str, Any]:
        """Release a claim with exponential backoff or dead-letter it."""
        if max_attempts < 1 or base_delay_seconds < 0:
            raise ValueError("invalid retry policy")
        table, id_column = self._table(queue)
        columns = self._columns(queue)
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                UPDATE {table}
                SET retry_count = retry_count + 1,
                    state = CASE WHEN retry_count + 1 >= %s
                        THEN 'dead_letter' ELSE 'pending' END,
                    next_attempt_at = CASE WHEN retry_count + 1 >= %s THEN NOW()
                        ELSE NOW() + make_interval(
                            secs => %s * power(2::numeric, retry_count)::double precision
                        ) END,
                    last_error = %s, lease_token = NULL, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = NOW()
                WHERE {id_column} = %s AND lease_token = %s
                  AND state = 'processing'
                RETURNING {columns}
                """,
                (
                    max_attempts,
                    max_attempts,
                    base_delay_seconds,
                    error[:2000],
                    record_id,
                    lease_token,
                ),
            )
            row = await cursor.fetchone()
        if row is None:
            raise LeaseMismatch(f"{queue} record is not held by this lease")
        return row

    async def get(self, queue: Queue, record_id: int) -> dict[str, Any] | None:
        table, id_column = self._table(queue)
        columns = self._columns(queue)
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"SELECT {columns} FROM {table} WHERE {id_column} = %s",
                (record_id,),
            )
            return await cursor.fetchone()

    async def list_for_transaction(
        self,
        queue: Queue,
        transaction_id: str,
        *,
        action: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Load durable protocol records without consulting the legacy file store."""
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        table, _ = self._table(queue)
        columns = self._columns(queue)
        query = f"SELECT {columns} FROM {table} WHERE transaction_id = %s"
        parameters: list[Any] = [_required(transaction_id, "transaction_id")]
        if action is not None:
            query += " AND action = %s"
            parameters.append(_required(action, "action"))
        query += " ORDER BY created_at DESC LIMIT %s"
        parameters.append(limit)
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, parameters)
            return list(await cursor.fetchall())

    async def list_records(
        self,
        queue: Queue,
        *,
        state: str | None = None,
        action: str | None = None,
        transaction_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Inspect durable queue state without falling back to local files."""
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if state is not None and state not in {
            "pending",
            "processing",
            "delivered",
            "dead_letter",
        }:
            raise ValueError("unsupported queue state")
        table, _ = self._table(queue)
        clauses: list[str] = []
        parameters: list[Any] = []
        if state is not None:
            clauses.append("state = %s")
            parameters.append(state)
        if action is not None:
            clauses.append("action = %s")
            parameters.append(_required(action, "action"))
        if transaction_id is not None:
            clauses.append("transaction_id = %s")
            parameters.append(_required(transaction_id, "transaction_id"))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        columns = self._columns(queue)
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"SELECT {columns} FROM {table} {where} "
                "ORDER BY created_at DESC LIMIT %s",
                parameters,
            )
            return list(await cursor.fetchall())

    async def requeue_dead_letter(
        self,
        queue: Queue,
        record_id: int,
        *,
        event_commitment: str,
    ) -> dict[str, Any] | None:
        """Safely return one identified dead letter to the pending queue."""
        if not _is_sha256(event_commitment):
            raise ValueError("event_commitment must be a SHA-256 hex digest")
        table, id_column = self._table(queue)
        columns = self._columns(queue)
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                UPDATE {table}
                SET state = 'pending', next_attempt_at = NOW(),
                    lease_token = NULL, lease_owner = NULL,
                    lease_expires_at = NULL, last_error = NULL,
                    updated_at = NOW()
                WHERE {id_column} = %s
                  AND event_commitment = %s
                  AND state = 'dead_letter'
                RETURNING {columns}
                """,
                (record_id, event_commitment),
            )
            updated = await cursor.fetchone()
            if updated is not None:
                return updated
            await cursor.execute(
                f"""
                SELECT {columns} FROM {table}
                WHERE {id_column} = %s AND event_commitment = %s
                  AND state = 'pending'
                """,
                (record_id, event_commitment),
            )
            return await cursor.fetchone()

    async def _claim(
        self, queue: Queue, worker_id: str, lease_seconds: int, limit: int
    ) -> list[dict[str, Any]]:
        if lease_seconds < 1 or limit < 1:
            raise ValueError("lease_seconds and limit must be positive")
        worker_id = _required(worker_id, "worker_id")
        table, id_column = self._table(queue)
        columns = self._columns(queue, alias="claimed")
        lease_token = uuid4()
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                WITH candidates AS (
                    SELECT {id_column}
                    FROM {table}
                    WHERE next_attempt_at <= NOW()
                      AND (state = 'pending' OR (
                          state = 'processing' AND lease_expires_at <= NOW()
                      ))
                    ORDER BY next_attempt_at, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                ), claimed AS (
                    UPDATE {table} queued
                    SET state = 'processing', lease_token = %s,
                        lease_owner = %s,
                        lease_expires_at = NOW() + make_interval(secs => %s),
                        updated_at = NOW()
                    FROM candidates
                    WHERE queued.{id_column} = candidates.{id_column}
                    RETURNING queued.*
                )
                SELECT {columns} FROM claimed ORDER BY created_at
                """,
                (limit, lease_token, worker_id, lease_seconds),
            )
            return list(await cursor.fetchall())

    @staticmethod
    def _correlation_values(
        subscriber_id: str,
        transaction_id: str,
        message_id: str,
        action: str,
        correlation_id: str | None,
    ) -> tuple[str, str, str, str, str | None]:
        return (
            _required(subscriber_id, "subscriber_id"),
            _required(transaction_id, "transaction_id"),
            _required(message_id, "message_id"),
            _required(action, "action"),
            _required(correlation_id, "correlation_id")
            if correlation_id is not None
            else None,
        )

    @staticmethod
    def _validate_existing(
        existing: dict[str, Any],
        values: tuple[str, str, str, str, str | None],
        raw_commitment: str,
        projection: dict[str, Any],
    ) -> None:
        fields = ("subscriber_id", "transaction_id", "message_id", "action")
        if any(existing[field] != value for field, value in zip(fields, values)):
            raise CorrelationMismatch(
                "event commitment has different correlation fields"
            )
        if existing["correlation_id"] != values[4]:
            raise CorrelationMismatch("correlation_id does not match persisted event")
        if existing["raw_envelope_commitment"] != raw_commitment:
            raise EnvelopeCommitmentMismatch(
                "raw envelope does not match persisted event"
            )
        if existing["redacted_payload"] != projection:
            raise EnvelopeCommitmentMismatch(
                "redacted projection does not match persisted event"
            )

    @staticmethod
    def _table(queue: Queue) -> tuple[str, str]:
        if queue == "inbox":
            return "ondc_inbox", "inbox_id"
        if queue == "outbox":
            return "ondc_outbox", "outbox_id"
        raise ValueError(f"unsupported queue: {queue}")

    @classmethod
    def _columns(cls, queue: Queue, alias: str | None = None) -> str:
        _, id_column = cls._table(queue)
        prefix = f"{alias}." if alias else ""
        names = [
            id_column,
            "event_commitment",
            "subscriber_id",
            "transaction_id",
            "message_id",
            "action",
            "correlation_id",
        ]
        if queue == "outbox":
            names.append("destination")
        names.extend(
            [
            "raw_envelope_commitment",
            "envelope",
            "redacted_payload",
                "state",
                "retry_count",
                "next_attempt_at",
                "lease_token",
                "lease_owner",
                "lease_expires_at",
                "last_error",
                "delivered_at",
                "created_at",
                "updated_at",
            ]
        )
        return ", ".join(prefix + name for name in names)


async def persist_callback_before_ack(
    pool: ConnectionPool, **callback: Any
) -> tuple[bool, dict[str, Any]]:
    """Commit a callback durably before the protocol adapter emits its ACK."""
    async with UnitOfWork(pool) as unit_of_work:
        result = await ONDCRepository(unit_of_work).persist_callback(**callback)
    return result
