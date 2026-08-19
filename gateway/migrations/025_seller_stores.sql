-- Migration 025: seller store profile used by /business setup and listing copy.

CREATE TABLE commerce_seller_stores (
    seller_id TEXT PRIMARY KEY,
    store_name TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    pin TEXT NOT NULL DEFAULT '',
    serviceability_tokens JSONB NOT NULL DEFAULT '[]'::JSONB,
    fulfilment_sla_hours INTEGER
        CHECK (
            fulfilment_sla_hours IS NULL
            OR (fulfilment_sla_hours >= 1 AND fulfilment_sla_hours <= 72)
        ),
    return_window_days INTEGER
        CHECK (
            return_window_days IS NULL
            OR (return_window_days >= 0 AND return_window_days <= 30)
        ),
    support_hours TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'ready')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
