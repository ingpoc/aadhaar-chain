"""
PostgreSQL persistence foundation for AadhaarChain.

Provides connection pooling, transaction management, and migration infrastructure.

Migration number ranges:
- 001-009: Core infrastructure (schema_migrations, idempotency_records, audit_events)
- 010-019: AgentGuard
- 020-029: Commerce/payment/ledger
- 030-039: ONDC
"""

from .connection import ConnectionPool
from .transaction import Transaction, UnitOfWork
from .migrations import MigrationRunner
from .repositories import AuditRepository, IdempotencyConflict, IdempotencyRepository

__all__ = [
    "ConnectionPool",
    "Transaction",
    "UnitOfWork",
    "MigrationRunner",
    "IdempotencyRepository",
    "IdempotencyConflict",
    "AuditRepository",
]
