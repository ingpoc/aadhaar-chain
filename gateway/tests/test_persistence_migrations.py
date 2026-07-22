from pathlib import Path

import pytest

from app.persistence.migrations import (
    MIGRATION_RANGES,
    discover_migrations,
)


def _migration(directory: Path, name: str, sql: str = "SELECT 1;\n") -> None:
    (directory / name).write_text(sql, encoding="utf-8")


def test_discovery_is_numeric_and_deterministic(tmp_path: Path) -> None:
    _migration(tmp_path, "020_ledger.sql")
    _migration(tmp_path, "001_schema_migrations.sql")
    _migration(tmp_path, "010_agentguard.sql")
    _migration(tmp_path, "003_audit_events.sql")
    _migration(tmp_path, "README.sql")

    first = discover_migrations(tmp_path)
    second = discover_migrations(tmp_path)

    assert [migration.number for migration in first] == [1, 3, 10, 20]
    assert first == second
    assert [migration.checksum for migration in first] == [
        migration.checksum for migration in second
    ]


def test_discovery_rejects_duplicate_numbers(tmp_path: Path) -> None:
    _migration(tmp_path, "002_idempotency_records.sql")
    _migration(tmp_path, "002_other.sql")

    with pytest.raises(ValueError, match="duplicate migration number: 002"):
        discover_migrations(tmp_path)


def test_discovery_checksum_tracks_exact_file_content(tmp_path: Path) -> None:
    path = tmp_path / "001_schema_migrations.sql"
    _migration(tmp_path, path.name, "SELECT 1;\n")
    before = discover_migrations(tmp_path)[0].checksum

    path.write_text("SELECT 2;\n", encoding="utf-8")

    assert discover_migrations(tmp_path)[0].checksum != before


def test_migration_number_ranges_are_reserved_by_owner() -> None:
    assert MIGRATION_RANGES == {
        "core": (1, 9),
        "agentguard": (10, 19),
        "commerce_payment_ledger": (20, 29),
        "ondc": (30, 39),
    }
