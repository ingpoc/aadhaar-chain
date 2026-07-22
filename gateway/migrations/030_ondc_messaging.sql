-- Migration 030: durable ONDC callback inbox and delivery outbox

CREATE TABLE ondc_inbox (
    inbox_id BIGSERIAL PRIMARY KEY,
    event_commitment CHAR(64) NOT NULL UNIQUE,
    subscriber_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    action TEXT NOT NULL,
    correlation_id TEXT,
    raw_envelope_commitment CHAR(64) NOT NULL,
    redacted_payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'processing', 'delivered', 'dead_letter')),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_token UUID,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ondc_inbox_correlation_uniq
        UNIQUE (subscriber_id, transaction_id, message_id, action),
    CONSTRAINT ondc_inbox_event_commitment_sha256
        CHECK (event_commitment ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ondc_inbox_raw_commitment_sha256
        CHECK (raw_envelope_commitment ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ondc_inbox_lease_shape CHECK (
        (state = 'processing' AND lease_token IS NOT NULL
            AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR state <> 'processing'
    )
);

CREATE INDEX ondc_inbox_claim_idx
    ON ondc_inbox (next_attempt_at, created_at)
    WHERE state IN ('pending', 'processing');
CREATE INDEX ondc_inbox_transaction_idx
    ON ondc_inbox (subscriber_id, transaction_id, created_at);

CREATE TABLE ondc_outbox (
    outbox_id BIGSERIAL PRIMARY KEY,
    event_commitment CHAR(64) NOT NULL UNIQUE,
    subscriber_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    action TEXT NOT NULL,
    correlation_id TEXT,
    destination TEXT NOT NULL,
    raw_envelope_commitment CHAR(64) NOT NULL,
    redacted_payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'processing', 'delivered', 'dead_letter')),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_token UUID,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ondc_outbox_event_commitment_sha256
        CHECK (event_commitment ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ondc_outbox_raw_commitment_sha256
        CHECK (raw_envelope_commitment ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ondc_outbox_lease_shape CHECK (
        (state = 'processing' AND lease_token IS NOT NULL
            AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR state <> 'processing'
    )
);

CREATE INDEX ondc_outbox_claim_idx
    ON ondc_outbox (next_attempt_at, created_at)
    WHERE state IN ('pending', 'processing');
CREATE INDEX ondc_outbox_transaction_idx
    ON ondc_outbox (subscriber_id, transaction_id, created_at);

CREATE FUNCTION reject_ondc_message_commitment_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.event_commitment <> OLD.event_commitment
        OR NEW.subscriber_id <> OLD.subscriber_id
        OR NEW.transaction_id <> OLD.transaction_id
        OR NEW.message_id <> OLD.message_id
        OR NEW.action <> OLD.action
        OR NEW.raw_envelope_commitment <> OLD.raw_envelope_commitment
        OR NEW.redacted_payload <> OLD.redacted_payload
        OR NEW.correlation_id IS DISTINCT FROM OLD.correlation_id
        OR (TG_TABLE_NAME = 'ondc_outbox'
            AND to_jsonb(NEW)->>'destination'
                IS DISTINCT FROM to_jsonb(OLD)->>'destination')
    THEN
        RAISE EXCEPTION 'ONDC message commitments are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER ondc_inbox_commitments_immutable
BEFORE UPDATE ON ondc_inbox
FOR EACH ROW EXECUTE FUNCTION reject_ondc_message_commitment_mutation();

CREATE TRIGGER ondc_outbox_commitments_immutable
BEFORE UPDATE ON ondc_outbox
FOR EACH ROW EXECUTE FUNCTION reject_ondc_message_commitment_mutation();
