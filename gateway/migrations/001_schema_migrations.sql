-- Migration 001: Create schema_migrations table
-- Tracks applied migrations for version control

CREATE TABLE schema_migrations (
    migration_number INTEGER PRIMARY KEY,
    migration_name TEXT NOT NULL,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    checksum TEXT
);

-- Index for faster lookups
CREATE INDEX idx_schema_migrations_applied_at
    ON schema_migrations(applied_at DESC);
