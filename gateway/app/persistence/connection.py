"""Lazy PostgreSQL connection-pool ownership."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

try:
    from psycopg_pool import AsyncConnectionPool
except ImportError:  # Lets migration discovery run without the optional DB extra.
    AsyncConnectionPool = None  # type: ignore[assignment,misc]


# Neon's pooler and compute drop idle connections server-side; retire pooled
# connections well before that and health-check each one at checkout.
DEFAULT_MAX_IDLE_SECONDS = 120.0
DEFAULT_MAX_LIFETIME_SECONDS = 1800.0


def _seconds_from_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class ConnectionPool:
    """A lazily opened pool configured exclusively from ``DATABASE_URL``."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        min_size: int = 1,
        max_size: int = 10,
        max_idle: float | None = None,
        max_lifetime: float | None = None,
    ) -> None:
        self.database_url = database_url or os.getenv("DATABASE_URL")
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required for PostgreSQL persistence")
        if AsyncConnectionPool is None:
            raise RuntimeError(
                "PostgreSQL pooling requires the psycopg pool extra; install gateway requirements"
            )
        self._pool: Any = AsyncConnectionPool(
            conninfo=self.database_url,
            min_size=min_size,
            max_size=max_size,
            open=False,
            # Discard connections the server closed while idle (e.g. Neon
            # "SSL connection has been closed unexpectedly") before use.
            check=AsyncConnectionPool.check_connection,
            max_idle=max_idle
            or _seconds_from_env("PG_POOL_MAX_IDLE_SECONDS", DEFAULT_MAX_IDLE_SECONDS),
            max_lifetime=max_lifetime
            or _seconds_from_env(
                "PG_POOL_MAX_LIFETIME_SECONDS", DEFAULT_MAX_LIFETIME_SECONDS
            ),
        )

    async def open(self) -> None:
        if self.is_open:
            return
        await self._pool.open()
        try:
            await self._pool.wait()
        except BaseException:
            await self._pool.close()
            raise

    async def close(self) -> None:
        if self.is_open:
            await self._pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Any]:
        async with self._pool.connection() as connection:
            yield connection

    @property
    def is_open(self) -> bool:
        return not self._pool.closed


def live_connection_pool(pool: Any) -> ConnectionPool | None:
    """Treat only an open gateway pool as PostgreSQL persistence."""
    return pool if isinstance(pool, ConnectionPool) and pool.is_open else None
