"""Checked-in, numbered PostgreSQL migration discovery and execution."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from .connection import ConnectionPool

_MIGRATION_NAME = re.compile(r"^(?P<number>\d{3})_(?P<name>[a-z0-9_]+)\.sql$")
_MIGRATION_LOCK = 4_105_201  # repository-specific PostgreSQL advisory lock
MIGRATION_RANGES = {
    "core": (1, 9),
    "agentguard": (10, 19),
    "commerce_payment_ledger": (20, 29),
    "ondc": (30, 39),
}


@dataclass(frozen=True, order=True)
class Migration:
    number: int
    name: str
    path: Path
    checksum: str


def discover_migrations(migrations_dir: Path) -> list[Migration]:
    """Return valid migration files in numeric order and reject duplicates."""
    migrations: list[Migration] = []
    seen: set[int] = set()
    if not migrations_dir.exists():
        return migrations
    for path in migrations_dir.iterdir():
        match = _MIGRATION_NAME.fullmatch(path.name)
        if not path.is_file() or match is None:
            continue
        number = int(match.group("number"))
        if number in seen:
            raise ValueError(f"duplicate migration number: {number:03d}")
        seen.add(number)
        body = path.read_bytes()
        migrations.append(
            Migration(number, match.group("name"), path, sha256(body).hexdigest())
        )
    return sorted(migrations)


class MigrationRunner:
    """Apply each migration once, detecting changes to already-applied SQL."""

    def __init__(self, pool: ConnectionPool, migrations_dir: Path) -> None:
        self.pool = pool
        self.migrations_dir = migrations_dir

    def discover_migrations(self) -> list[Migration]:
        return discover_migrations(self.migrations_dir)

    async def apply(self) -> list[int]:
        migrations = self.discover_migrations()
        if not migrations or migrations[0].number != 1:
            raise RuntimeError("migration 001 must bootstrap schema_migrations")

        applied_now: list[int] = []
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
                table_query = await connection.execute(
                    """
                    SELECT to_regclass(
                        format('%I.%I', current_schema(), 'schema_migrations')
                    )
                    """
                )
                schema_migrations_exists = (await table_query.fetchone())[0] is not None

                if not schema_migrations_exists:
                    bootstrap = migrations[0]
                    await connection.execute(
                        bootstrap.path.read_text(encoding="utf-8")
                    )
                    await connection.execute(
                        """
                        INSERT INTO schema_migrations
                            (migration_number, migration_name, checksum)
                        VALUES (%s, %s, %s)
                        """,
                        (bootstrap.number, bootstrap.name, bootstrap.checksum),
                    )
                    applied_now.append(bootstrap.number)

                rows = await connection.execute(
                    """
                    SELECT migration_number, migration_name, checksum
                    FROM schema_migrations
                    """
                )
                applied = {
                    number: (name, checksum)
                    for number, name, checksum in await rows.fetchall()
                }

                if 1 not in applied:
                    raise RuntimeError(
                        "schema_migrations exists without migration 001 history"
                    )

                for migration in migrations:
                    prior = applied.get(migration.number)
                    if prior is not None:
                        prior_name, prior_checksum = prior
                        if (
                            prior_name != migration.name
                            or prior_checksum != migration.checksum
                        ):
                            raise RuntimeError(
                                f"applied migration changed: {migration.path.name}"
                            )
                        continue
                    await connection.execute(migration.path.read_text(encoding="utf-8"))
                    await connection.execute(
                        """
                        INSERT INTO schema_migrations
                            (migration_number, migration_name, checksum)
                        VALUES (%s, %s, %s)
                        """,
                        (migration.number, migration.name, migration.checksum),
                    )
                    applied_now.append(migration.number)
        return applied_now
