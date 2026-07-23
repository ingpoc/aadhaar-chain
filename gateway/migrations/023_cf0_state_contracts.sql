-- CF0 lifecycle contract v1: normalize issue states and establish durable returns.

UPDATE commerce_issues
SET status = CASE
    WHEN status = 'investigating' THEN 'acknowledged'
    WHEN status = 'resolved' THEN 'resolution_proposed'
    ELSE status
END;

ALTER TABLE commerce_issues
    DROP CONSTRAINT IF EXISTS commerce_issues_status_check;

ALTER TABLE commerce_issues
    ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1
        CHECK (version > 0),
    ADD CONSTRAINT commerce_issues_status_check CHECK (
        status IN (
            'open', 'acknowledged', 'resolution_proposed', 'escalated',
            'accepted', 'rejected', 'closed'
        )
    );

CREATE TABLE commerce_returns (
    return_id UUID PRIMARY KEY,
    order_id UUID NOT NULL REFERENCES commerce_orders(order_id),
    principal_id TEXT NOT NULL,
    seller_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested' CHECK (
        status IN (
            'requested', 'approved', 'rejected', 'cancelled', 'in_transit',
            'received', 'refund_pending', 'replacement_pending', 'completed',
            'failed'
        )
    ),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    reason TEXT NOT NULL,
    resolution JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX commerce_returns_principal_idx
    ON commerce_returns (principal_id, created_at DESC);
CREATE INDEX commerce_returns_seller_idx
    ON commerce_returns (seller_id, created_at DESC);

DROP TRIGGER IF EXISTS commerce_refunds_append_only ON commerce_refunds;
ALTER TABLE commerce_refunds
    DROP CONSTRAINT IF EXISTS commerce_refunds_status_check;
ALTER TABLE commerce_refunds
    ADD CONSTRAINT commerce_refunds_status_check CHECK (
        status IN ('pending', 'succeeded', 'failed', 'unknown', 'reconciled')
    );

CREATE OR REPLACE FUNCTION guard_commerce_refund_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'commerce refund records are append-only';
    END IF;
    IF OLD.refund_id IS DISTINCT FROM NEW.refund_id
       OR OLD.order_id IS DISTINCT FROM NEW.order_id
       OR OLD.payment_attempt_id IS DISTINCT FROM NEW.payment_attempt_id
       OR OLD.seller_id IS DISTINCT FROM NEW.seller_id
       OR OLD.principal_id IS DISTINCT FROM NEW.principal_id
       OR OLD.amount_paise IS DISTINCT FROM NEW.amount_paise
       OR OLD.idempotency_key IS DISTINCT FROM NEW.idempotency_key
       OR OLD.correlation_id IS DISTINCT FROM NEW.correlation_id
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'commerce refund evidence is immutable';
    END IF;
    IF OLD.status = 'pending'
       AND NEW.status IN ('succeeded', 'failed', 'unknown') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'unknown' AND NEW.status IN ('reconciled', 'failed') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal commerce refund mutation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER commerce_refunds_state_guard
    BEFORE UPDATE OR DELETE ON commerce_refunds
    FOR EACH ROW EXECUTE FUNCTION guard_commerce_refund_mutation();
