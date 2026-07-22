-- Migration 002: Create idempotency_records table
-- Ensures exactly-once processing with request hash conflict detection

CREATE TABLE idempotency_records (
    id BIGSERIAL PRIMARY KEY,
    principal_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,

    -- Request hash for conflict detection
    request_hash TEXT NOT NULL,

    -- Status tracking
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'success', 'failure')),

    -- Response storage
    response JSONB,

    -- Resource and correlation
    resource TEXT,
    correlation_id TEXT,

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- Unique constraint on principal + operation + key
    CONSTRAINT idempotency_uniq UNIQUE (principal_id, operation, idempotency_key)
);

-- Indexes for common queries (the unique constraint already indexes lookup).
CREATE INDEX idx_idempotency_correlation
    ON idempotency_records(correlation_id) WHERE correlation_id IS NOT NULL;

CREATE INDEX idx_idempotency_created
    ON idempotency_records(created_at DESC);

-- Index for cleanup of old records
CREATE INDEX idx_idempotency_updated
    ON idempotency_records(updated_at DESC);
