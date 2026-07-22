"""Lazy PostgreSQL connection-pool ownership."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

try:
    from psycopg_pool import AsyncConnectionPool
except ImportError:  # Lets migration discovery run without the optional DB extra.
    AsyncConnectionPool = None  # type: ignore[assignment,misc]


class ConnectionPool:
    """A lazily opened pool configured exclusively from ``DATABASE_URL``."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        min_size: int = 1,
        max_size: int = 10,
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
