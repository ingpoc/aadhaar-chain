-- Seller staff membership for operated-merchant order/catalog visibility.
-- Live already applied 026_cf3_seller_staff; keep this additive and idempotent.

CREATE TABLE IF NOT EXISTS commerce_seller_staff (
    staff_id TEXT PRIMARY KEY,
    seller_id TEXT NOT NULL,
    member_principal_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'viewer'
        CHECK (role IN ('owner', 'manager', 'fulfilment', 'support', 'viewer')),
    status TEXT NOT NULL DEFAULT 'invited'
        CHECK (status IN ('active', 'invited', 'revoked')),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE commerce_seller_staff
    ADD COLUMN IF NOT EXISTS display_name TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS email TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'viewer',
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'invited',
    ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS commerce_seller_staff_seller_member_unique
    ON commerce_seller_staff (seller_id, member_principal_id);

CREATE INDEX IF NOT EXISTS idx_commerce_seller_staff_member
    ON commerce_seller_staff (member_principal_id, status);

CREATE INDEX IF NOT EXISTS idx_commerce_seller_staff_seller
    ON commerce_seller_staff (seller_id, status, updated_at DESC);
