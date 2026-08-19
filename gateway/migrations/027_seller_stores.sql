-- Seller store profile for /business setup.
-- Live already applied 025_cf3_seller_store and 026_cf3_seller_staff; do not reuse those numbers.

CREATE TABLE IF NOT EXISTS commerce_seller_stores (
    seller_id TEXT PRIMARY KEY,
    store_name TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    pin TEXT NOT NULL DEFAULT '',
    serviceability_tokens TEXT[] NOT NULL DEFAULT '{}',
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
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE commerce_seller_stores
    ADD COLUMN IF NOT EXISTS store_name TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS city TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS pin TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS serviceability_tokens TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS fulfilment_sla_hours INTEGER,
    ADD COLUMN IF NOT EXISTS return_window_days INTEGER,
    ADD COLUMN IF NOT EXISTS support_hours TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'draft',
    ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
