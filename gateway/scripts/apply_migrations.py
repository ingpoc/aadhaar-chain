#!/usr/bin/env python3
"""Apply checked-in gateway PostgreSQL migrations via MigrationRunner."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.persistence import ConnectionPool, MigrationRunner

MIGRATIONS = ROOT / "migrations"


async def main() -> None:
    pool = ConnectionPool()
    await pool.open()
    try:
        applied = await MigrationRunner(pool, MIGRATIONS).apply()
        print("applied", applied)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
