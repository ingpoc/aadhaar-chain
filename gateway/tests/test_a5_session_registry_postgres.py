"""A5 session registry durability against local PostgreSQL (never Render)."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.persistence import ConnectionPool, MigrationRunner
from app.session_auth import (
    create_principal_session_token,
    decode_session_payload,
    parse_session_token,
)
from app.session_registry import (
    SessionRegistryUnavailable,
    bind_session_store,
    list_principal_sessions,
    open_session_store,
    persist_registered_session,
    reset_session_registry,
    revoke_principal_durable,
    revoke_sid_durable,
    unbind_session_store,
)


LOCAL_DATABASE_URL = "postgresql://gurusharan@127.0.0.1:5432/postgres"
MIGRATIONS = Path(__file__).parents[1] / "migrations"

pytestmark = [pytest.mark.asyncio]


def _local_database_url() -> str:
    url = os.getenv("DATABASE_URL") or LOCAL_DATABASE_URL
    host = (urlparse(url).hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail(
            "Refusing non-local DATABASE_URL for A5 session registry tests "
            "(never migrate Render from this suite)."
        )
    return url


@pytest_asyncio.fixture
async def postgres_url() -> AsyncIterator[str]:
    admin_url = _local_database_url()
    schema = f"a5_session_{uuid4().hex}"
    admin = await psycopg.AsyncConnection.connect(admin_url, autocommit=True)
    try:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        yield make_conninfo(admin_url, options=f"-csearch_path={schema},public")
    finally:
        await admin.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )
        await admin.close()


async def _bind(postgres_url: str, *, migrate: bool) -> ConnectionPool:
    pool = ConnectionPool(postgres_url, min_size=0, max_size=4)
    await pool.open()
    if migrate:
        await MigrationRunner(pool, MIGRATIONS).apply()
    bind_session_store(pool, require_durable=True)
    await open_session_store()
    return pool


async def _simulate_restart(previous: ConnectionPool, postgres_url: str) -> ConnectionPool:
    unbind_session_store()
    reset_session_registry()
    await previous.close()
    return await _bind(postgres_url, migrate=False)


async def _mint_persisted(*, principal_id: str, audience: str = "ondcbuyer") -> str:
    token = create_principal_session_token(
        principal_id=principal_id,
        audience=audience,
        identity_provider="auth0",
    )
    payload = decode_session_payload(token)
    assert payload is not None
    await persist_registered_session(payload)
    return token


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    unbind_session_store()
    reset_session_registry()
    yield
    unbind_session_store()
    reset_session_registry()


async def test_revoke_sid_survives_new_connection(postgres_url: str) -> None:
    first = await _bind(postgres_url, migrate=True)
    token = await _mint_persisted(principal_id="principal:auth0:a5-durable-sid")
    payload = decode_session_payload(token)
    assert payload is not None
    await revoke_sid_durable(str(payload["sid"]))
    assert parse_session_token(token) is None

    second = await _simulate_restart(first, postgres_url)
    try:
        assert parse_session_token(token) is None
    finally:
        unbind_session_store()
        await second.close()


async def test_revoke_all_survives_new_connection_and_allows_fresh_login(
    postgres_url: str,
) -> None:
    first = await _bind(postgres_url, migrate=True)
    principal = "principal:auth0:a5-durable-twins"
    token_a = await _mint_persisted(principal_id=principal)
    token_b = await _mint_persisted(principal_id=principal)
    await revoke_principal_durable(principal)
    assert parse_session_token(token_a) is None
    assert parse_session_token(token_b) is None

    second = await _simulate_restart(first, postgres_url)
    try:
        assert parse_session_token(token_a) is None
        assert parse_session_token(token_b) is None
        fresh = await _mint_persisted(principal_id=principal)
        assert parse_session_token(fresh) is not None
    finally:
        unbind_session_store()
        await second.close()


async def test_list_sessions_stays_principal_scoped_after_restart(
    postgres_url: str,
) -> None:
    first = await _bind(postgres_url, migrate=True)
    left = "principal:auth0:a5-list-left"
    right = "principal:auth0:a5-list-right"
    token_left = await _mint_persisted(principal_id=left, audience="ondcbuyer")
    await _mint_persisted(principal_id=right, audience="ondcseller")
    listed = list_principal_sessions(left)
    assert len(listed) == 1
    assert listed[0]["principal_id"] == left
    assert listed[0]["sid"] == decode_session_payload(token_left)["sid"]

    second = await _simulate_restart(first, postgres_url)
    try:
        listed = list_principal_sessions(left)
        assert [row["principal_id"] for row in listed] == [left]
        assert all(row["principal_id"] != right for row in listed)
        assert list_principal_sessions(right)
        assert all(row["principal_id"] == right for row in list_principal_sessions(right))
    finally:
        unbind_session_store()
        await second.close()


async def test_write_fails_closed_when_pool_closed(postgres_url: str) -> None:
    pool = await _bind(postgres_url, migrate=True)
    token = await _mint_persisted(principal_id="principal:auth0:a5-closed-pool")
    payload = decode_session_payload(token)
    assert payload is not None
    await pool.close()
    with pytest.raises(SessionRegistryUnavailable):
        await revoke_sid_durable(str(payload["sid"]))
    unbind_session_store()
