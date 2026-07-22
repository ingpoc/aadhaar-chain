CREATE TABLE commerce_inventory (
    seller_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    title TEXT NOT NULL,
    unit_price_paise BIGINT NOT NULL CHECK (unit_price_paise >= 0),
    available_quantity INTEGER NOT NULL CHECK (available_quantity >= 0),
    reserved_quantity INTEGER NOT NULL DEFAULT 0
        CHECK (reserved_quantity >= 0 AND reserved_quantity <= available_quantity),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (seller_id, sku)
);

CREATE TABLE commerce_carts (
    cart_id UUID PRIMARY KEY,
    principal_id TEXT NOT NULL,
    seller_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'checked_out')),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_commerce_carts_principal ON commerce_carts(principal_id, created_at DESC);

CREATE TABLE commerce_cart_lines (
    cart_id UUID NOT NULL REFERENCES commerce_carts(cart_id) ON DELETE CASCADE,
    sku TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (cart_id, sku)
);

CREATE TABLE commerce_quotes (
    quote_id UUID PRIMARY KEY,
    cart_id UUID NOT NULL REFERENCES commerce_carts(cart_id),
    principal_id TEXT NOT NULL,
    seller_id TEXT NOT NULL,
    cart_version BIGINT NOT NULL,
    subtotal_paise BIGINT NOT NULL CHECK (subtotal_paise >= 0),
    landed_total_paise BIGINT NOT NULL CHECK (landed_total_paise >= subtotal_paise),
    line_snapshot JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'consumed', 'expired', 'released')),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_at TIMESTAMPTZ
);

CREATE INDEX idx_commerce_quotes_expiry ON commerce_quotes(status, expires_at);

CREATE TABLE commerce_orders (
    order_id UUID PRIMARY KEY,
    principal_id TEXT NOT NULL,
    seller_id TEXT NOT NULL,
    cart_id UUID NOT NULL REFERENCES commerce_carts(cart_id),
    quote_id UUID NOT NULL UNIQUE REFERENCES commerce_quotes(quote_id),
    landed_total_paise BIGINT NOT NULL CHECK (landed_total_paise >= 0),
    status TEXT NOT NULL CHECK (
        status IN ('prepared', 'payment_pending', 'paid', 'payment_failed', 'payment_unknown', 'cancelled')
    ),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE commerce_inventory_reservations (
    reservation_id UUID PRIMARY KEY,
    quote_id UUID NOT NULL REFERENCES commerce_quotes(quote_id),
    order_id UUID REFERENCES commerce_orders(order_id),
    seller_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    status TEXT NOT NULL DEFAULT 'held' CHECK (status IN ('held', 'consumed', 'released')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    released_at TIMESTAMPTZ,
    UNIQUE (quote_id, sku),
    FOREIGN KEY (seller_id, sku) REFERENCES commerce_inventory(seller_id, sku)
);

CREATE TABLE commerce_payment_attempts (
    payment_attempt_id UUID PRIMARY KEY,
    order_id UUID NOT NULL REFERENCES commerce_orders(order_id),
    principal_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'simulated',
    provider_reference TEXT,
    amount_paise BIGINT NOT NULL CHECK (amount_paise >= 0),
    status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed', 'unknown', 'reconciled')),
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_commerce_payment_one_active_attempt
    ON commerce_payment_attempts(order_id);

CREATE TABLE commerce_ledger_transactions (
    ledger_transaction_id UUID PRIMARY KEY,
    order_id UUID NOT NULL REFERENCES commerce_orders(order_id),
    payment_attempt_id UUID NOT NULL REFERENCES commerce_payment_attempts(payment_attempt_id),
    posting_type TEXT NOT NULL CHECK (posting_type IN ('payment', 'reconciliation')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (payment_attempt_id, posting_type)
);

CREATE TABLE commerce_ledger_entries (
    ledger_entry_id UUID PRIMARY KEY,
    ledger_transaction_id UUID NOT NULL REFERENCES commerce_ledger_transactions(ledger_transaction_id),
    account TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('debit', 'credit')),
    amount_paise BIGINT NOT NULL CHECK (amount_paise > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_commerce_ledger_entries_transaction
    ON commerce_ledger_entries(ledger_transaction_id);

CREATE OR REPLACE FUNCTION reject_commerce_ledger_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'commerce ledger is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER commerce_ledger_transactions_append_only
    BEFORE UPDATE OR DELETE ON commerce_ledger_transactions
    FOR EACH ROW EXECUTE FUNCTION reject_commerce_ledger_mutation();

CREATE TRIGGER commerce_ledger_entries_append_only
    BEFORE UPDATE OR DELETE ON commerce_ledger_entries
    FOR EACH ROW EXECUTE FUNCTION reject_commerce_ledger_mutation();

CREATE OR REPLACE FUNCTION enforce_balanced_commerce_ledger() RETURNS TRIGGER AS $$
DECLARE
    target_id UUID;
    debits BIGINT;
    credits BIGINT;
BEGIN
    target_id := COALESCE(NEW.ledger_transaction_id, OLD.ledger_transaction_id);
    SELECT
        COALESCE(SUM(amount_paise) FILTER (WHERE side = 'debit'), 0),
        COALESCE(SUM(amount_paise) FILTER (WHERE side = 'credit'), 0)
    INTO debits, credits
    FROM commerce_ledger_entries
    WHERE ledger_transaction_id = target_id;
    IF debits <> credits OR debits = 0 THEN
        RAISE EXCEPTION 'unbalanced commerce ledger transaction %', target_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER commerce_ledger_balanced
    AFTER INSERT ON commerce_ledger_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_balanced_commerce_ledger();

CREATE CONSTRAINT TRIGGER commerce_ledger_transaction_balanced
    AFTER INSERT ON commerce_ledger_transactions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_balanced_commerce_ledger();
