-- Migration 022: one durable simulated refund effect per paid order.

ALTER TABLE commerce_ledger_transactions
    DROP CONSTRAINT commerce_ledger_transactions_posting_type_check;
ALTER TABLE commerce_ledger_transactions
    ADD CONSTRAINT commerce_ledger_transactions_posting_type_check
    CHECK (posting_type IN ('payment', 'reconciliation', 'refund'));

CREATE TABLE commerce_refunds (
    refund_id UUID PRIMARY KEY,
    order_id UUID NOT NULL UNIQUE REFERENCES commerce_orders(order_id),
    payment_attempt_id UUID NOT NULL REFERENCES commerce_payment_attempts(payment_attempt_id),
    seller_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    amount_paise BIGINT NOT NULL CHECK (amount_paise > 0),
    status TEXT NOT NULL CHECK (status = 'succeeded'),
    idempotency_key TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (seller_id, idempotency_key)
);

CREATE TRIGGER commerce_refunds_append_only
    BEFORE UPDATE OR DELETE ON commerce_refunds
    FOR EACH ROW EXECUTE FUNCTION reject_commerce_ledger_mutation();
