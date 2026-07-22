"""Explicit transaction / unit-of-work primitive."""

from __future__ import annotations

import sys
from types import TracebackType
from typing import Any

from .connection import ConnectionPool


class UnitOfWork:
    """Lease one pooled connection and commit or roll back it atomically."""

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool
        self.connection: Any | None = None
        self._lease: Any | None = None
        self._transaction: Any | None = None

    async def __aenter__(self) -> "UnitOfWork":
        if self.connection is not None:
            raise RuntimeError("UnitOfWork cannot be entered more than once")
        self._lease = self.pool.connection()
        try:
            self.connection = await self._lease.__aenter__()
            self._transaction = self.connection.transaction()
            await self._transaction.__aenter__()
        except BaseException:
            await self._lease.__aexit__(*sys.exc_info())
            self._reset()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self._transaction is None or self._lease is None:
            return False
        try:
            suppress = await self._transaction.__aexit__(exc_type, exc, traceback)
        except BaseException:
            try:
                await self._lease.__aexit__(*sys.exc_info())
            finally:
                self._reset()
            raise
        try:
            await self._lease.__aexit__(None, None, None)
        finally:
            self._reset()
        return bool(suppress)

    def _reset(self) -> None:
        self.connection = None
        self._transaction = None
        self._lease = None


Transaction = UnitOfWork
