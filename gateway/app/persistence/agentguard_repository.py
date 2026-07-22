"""Transactional PostgreSQL repository for durable AgentGuard state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .transaction import UnitOfWork


class AgentGuardConflict(RuntimeError):
    """A durable AgentGuard invariant rejected the requested transition."""


class AgentGuardNotFound(LookupError):
    """No record exists in the caller's principal scope."""


class AgentGuardPermissionDenied(PermissionError):
    """A record exists but belongs to another principal."""


class AgentGuardRepository:
    """Principal-scoped AgentGuard operations inside one active unit of work."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    @property
    def _connection(self) -> Any:
        connection = self._unit_of_work.connection
        if connection is None:
            raise RuntimeError("AgentGuardRepository requires an active UnitOfWork")
        return connection

    async def create_agent(
        self,
        *,
        agent_id: str,
        principal_id: str,
        role: str,
        payload: dict[str, Any] | None = None,
        status: str = "active",
    ) -> dict[str, Any]:
        return await self._insert_one(
            """
            INSERT INTO agentguard_agents (
                agent_id, principal_id, role, status, payload
            ) VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            (agent_id, principal_id, role, status, Jsonb(payload or {})),
        )

    async def get_agent(
        self, *, principal_id: str, agent_id: str
    ) -> dict[str, Any] | None:
        return await self._fetch_one(
            """
            SELECT * FROM agentguard_agents
            WHERE principal_id = %s AND agent_id = %s
            """,
            (principal_id, agent_id),
        )

    async def set_agent_status(
        self,
        *,
        principal_id: str,
        agent_id: str,
        status: str,
    ) -> dict[str, Any]:
        if status not in {"active", "paused", "revoked"}:
            raise ValueError(f"unsupported agent status: {status}")
        record = await self._fetch_one(
            """
            UPDATE agentguard_agents
            SET status = %s, updated_at = NOW()
            WHERE principal_id = %s AND agent_id = %s
              AND (status <> 'revoked' OR %s = 'revoked')
            RETURNING *
            """,
            (status, principal_id, agent_id, status),
        )
        if record is None:
            await self._raise_missing_or_foreign(
                "agentguard_agents", "agent_id", agent_id, principal_id
            )
        if status in {"paused", "revoked"}:
            await self._connection.execute(
                """
                UPDATE agentguard_approvals
                SET status = CASE WHEN %s = 'revoked' THEN 'revoked' ELSE 'expired' END
                WHERE principal_id = %s AND agent_id = %s AND status = 'issued'
                """,
                (status, principal_id, agent_id),
            )
        return record

    async def create_mandate_version(
        self,
        *,
        mandate_id: str,
        version: int,
        principal_id: str,
        agent_id: str,
        payload: dict[str, Any],
        status: str = "active",
        activate: bool = True,
    ) -> dict[str, Any]:
        record = await self._insert_one(
            """
            INSERT INTO agentguard_mandate_versions (
                mandate_id, version, principal_id, agent_id, status, payload
            ) VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (mandate_id, version, principal_id, agent_id, status, Jsonb(payload)),
        )
        if activate:
            updated = await self._fetch_one(
                """
                UPDATE agentguard_agents
                SET current_mandate_id = %s,
                    current_mandate_version = %s,
                    updated_at = NOW()
                WHERE principal_id = %s AND agent_id = %s
                      AND status <> 'revoked'
                RETURNING agent_id
                """,
                (mandate_id, version, principal_id, agent_id),
            )
            if updated is None:
                raise AgentGuardConflict(
                    "cannot activate a mandate for a missing or revoked agent"
                )
            await self._connection.execute(
                """
                UPDATE agentguard_approvals
                SET status = 'expired'
                WHERE principal_id = %s AND agent_id = %s AND status = 'issued'
                      AND (mandate_id, mandate_version) <> (%s, %s)
                """,
                (principal_id, agent_id, mandate_id, version),
            )
        return record

    async def get_mandate_version(
        self,
        *,
        principal_id: str,
        mandate_id: str,
        version: int,
    ) -> dict[str, Any] | None:
        return await self._fetch_one(
            """
            SELECT * FROM agentguard_mandate_versions
            WHERE principal_id = %s AND mandate_id = %s AND version = %s
            """,
            (principal_id, mandate_id, version),
        )

    async def get_latest_mandate_for_agent(
        self, *, principal_id: str, agent_id: str
    ) -> dict[str, Any] | None:
        return await self._fetch_one(
            """
            SELECT * FROM agentguard_mandate_versions
            WHERE principal_id = %s AND agent_id = %s
            ORDER BY version DESC
            LIMIT 1
            """,
            (principal_id, agent_id),
        )

    async def record_decision(
        self,
        *,
        decision_id: str,
        principal_id: str,
        agent_id: str,
        mandate_id: str,
        mandate_version: int,
        status: str,
        policy: dict[str, Any],
        risk: dict[str, Any],
        required_action: str,
        expiry: datetime | None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist the explicit DecisionV2 envelope and its queryable identity."""
        return await self._insert_one(
            """
            INSERT INTO agentguard_decisions (
                decision_id, principal_id, agent_id, mandate_id, mandate_version,
                status, policy, risk, required_action, expiry, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                decision_id,
                principal_id,
                agent_id,
                mandate_id,
                mandate_version,
                status,
                Jsonb(policy),
                Jsonb(risk),
                required_action,
                expiry,
                Jsonb(payload or {}),
            ),
        )

    async def get_decision(
        self, *, principal_id: str, decision_id: str
    ) -> dict[str, Any] | None:
        return await self._fetch_one(
            """
            SELECT * FROM agentguard_decisions
            WHERE principal_id = %s AND decision_id = %s
            """,
            (principal_id, decision_id),
        )

    async def issue_approval(
        self,
        *,
        approval_id: str,
        principal_id: str,
        decision_id: str,
        agent_id: str,
        mandate_id: str,
        mandate_version: int,
        request_hash: str,
        expires_at: datetime,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._insert_one(
            """
            INSERT INTO agentguard_approvals (
                approval_id, principal_id, decision_id, agent_id, mandate_id,
                mandate_version, request_hash, expires_at, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                approval_id,
                principal_id,
                decision_id,
                agent_id,
                mandate_id,
                mandate_version,
                request_hash,
                expires_at,
                Jsonb(payload or {}),
            ),
        )

    async def get_approval(
        self, *, principal_id: str, approval_id: str
    ) -> dict[str, Any] | None:
        return await self._fetch_one(
            """
            SELECT * FROM agentguard_approvals
            WHERE principal_id = %s AND approval_id = %s
            """,
            (principal_id, approval_id),
        )

    async def consume_approval(
        self,
        *,
        principal_id: str,
        approval_id: str,
        request_hash: str,
    ) -> dict[str, Any]:
        """Atomically consume an approval; a concurrent caller cannot also win."""
        record = await self._fetch_one(
            """
            UPDATE agentguard_approvals AS approval
            SET status = 'consumed', consumed_at = NOW()
            FROM agentguard_agents AS agent
            WHERE approval.principal_id = %s
              AND approval.approval_id = %s
              AND approval.request_hash = %s
              AND approval.status = 'issued'
              AND approval.expires_at > NOW()
              AND agent.principal_id = approval.principal_id
              AND agent.agent_id = approval.agent_id
              AND agent.status = 'active'
              AND agent.current_mandate_id = approval.mandate_id
              AND agent.current_mandate_version = approval.mandate_version
            RETURNING approval.*
            """,
            (principal_id, approval_id, request_hash),
        )
        if record is not None:
            return record
        approval = await self.get_approval(
            principal_id=principal_id, approval_id=approval_id
        )
        if approval is None:
            await self._raise_missing_or_foreign(
                "agentguard_approvals", "approval_id", approval_id, principal_id
            )
        if approval["request_hash"] != request_hash:
            raise AgentGuardConflict("approval request hash mismatch")
        if approval["status"] == "issued" and approval["expires_at"] <= utcnow():
            await self._connection.execute(
                """
                UPDATE agentguard_approvals
                SET status = 'expired'
                WHERE principal_id = %s AND approval_id = %s AND status = 'issued'
                """,
                (principal_id, approval_id),
            )
            approval["status"] = "expired"
        raise AgentGuardConflict(f"approval is not consumable: {approval['status']}")

    async def create_execution_intent(
        self,
        *,
        intent_id: str,
        principal_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        decision_id: str | None = None,
        approval_id: str | None = None,
        payload: dict[str, Any] | None = None,
        status: str = "pending",
    ) -> tuple[dict[str, Any], bool]:
        """Create or replay an intent, rejecting key reuse with a new hash."""
        record = await self._fetch_one(
            """
            INSERT INTO agentguard_execution_intents (
                intent_id, principal_id, operation, idempotency_key,
                request_hash, decision_id, approval_id, status, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (principal_id, operation, idempotency_key)
            DO UPDATE SET updated_at = agentguard_execution_intents.updated_at
            WHERE agentguard_execution_intents.request_hash = EXCLUDED.request_hash
            RETURNING *, (xmax = 0) AS created
            """,
            (
                intent_id,
                principal_id,
                operation,
                idempotency_key,
                request_hash,
                decision_id,
                approval_id,
                status,
                Jsonb(payload or {}),
            ),
        )
        if record is None:
            raise AgentGuardConflict(
                "idempotency key was reused with a different request hash"
            )
        created = bool(record.pop("created"))
        return record, created

    async def get_execution_intent(
        self, *, principal_id: str, intent_id: str
    ) -> dict[str, Any] | None:
        return await self._fetch_one(
            """
            SELECT * FROM agentguard_execution_intents
            WHERE principal_id = %s AND intent_id = %s
            """,
            (principal_id, intent_id),
        )

    async def set_execution_intent_status(
        self,
        *,
        principal_id: str,
        intent_id: str,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"pending", "approved", "executing", "succeeded", "failed"}:
            raise ValueError(f"unsupported execution intent status: {status}")
        record = await self._fetch_one(
            """
            UPDATE agentguard_execution_intents
            SET status = %s, result = %s, updated_at = NOW()
            WHERE principal_id = %s AND intent_id = %s
            RETURNING *
            """,
            (
                status,
                Jsonb(result) if result is not None else None,
                principal_id,
                intent_id,
            ),
        )
        if record is None:
            await self._raise_missing_or_foreign(
                "agentguard_execution_intents", "intent_id", intent_id, principal_id
            )
        return record

    async def record_receipt(
        self,
        *,
        receipt_id: str,
        principal_id: str,
        agent_id: str,
        mandate_id: str,
        mandate_version: int,
        status: str,
        payload: dict[str, Any],
        decision_id: str | None = None,
        approval_id: str | None = None,
        intent_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._insert_one(
            """
            INSERT INTO agentguard_receipts (
                receipt_id, principal_id, agent_id, mandate_id, mandate_version,
                decision_id, approval_id, intent_id, status, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                receipt_id,
                principal_id,
                agent_id,
                mandate_id,
                mandate_version,
                decision_id,
                approval_id,
                intent_id,
                status,
                Jsonb(payload),
            ),
        )

    async def get_receipt(
        self, *, principal_id: str, receipt_id: str
    ) -> dict[str, Any] | None:
        return await self._fetch_one(
            """
            SELECT * FROM agentguard_receipts
            WHERE principal_id = %s AND receipt_id = %s
            """,
            (principal_id, receipt_id),
        )

    async def list_receipts(
        self, *, principal_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT * FROM agentguard_receipts
                WHERE principal_id = %s
                ORDER BY created_at DESC, receipt_id DESC
                LIMIT %s
                """,
                (principal_id, limit),
            )
            return list(await cursor.fetchall())

    async def _insert_one(
        self, statement: str, parameters: tuple[Any, ...]
    ) -> dict[str, Any]:
        record = await self._fetch_one(statement, parameters)
        if record is None:  # pragma: no cover - INSERT RETURNING always returns.
            raise RuntimeError("AgentGuard insert returned no record")
        return record

    async def _fetch_one(
        self, statement: str, parameters: tuple[Any, ...]
    ) -> dict[str, Any] | None:
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(statement, parameters)
            return await cursor.fetchone()

    async def _raise_missing_or_foreign(
        self,
        table: str,
        id_column: str,
        record_id: str,
        principal_id: str,
    ) -> None:
        # Table and column are repository-owned constants, never caller input.
        record = await self._fetch_one(
            f"SELECT principal_id FROM {table} WHERE {id_column} = %s",  # noqa: S608
            (record_id,),
        )
        if record is None:
            raise AgentGuardNotFound(f"AgentGuard record not found: {record_id}")
        if record["principal_id"] != principal_id:
            raise AgentGuardPermissionDenied("AgentGuard principal mismatch")
        raise AgentGuardConflict(f"AgentGuard transition rejected: {record_id}")


def utcnow() -> datetime:
    """One timezone-aware clock helper for repository callers and tests."""
    return datetime.now(timezone.utc)
