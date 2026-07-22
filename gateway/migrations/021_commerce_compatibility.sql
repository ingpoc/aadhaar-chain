-- Migration 021: CommerceV1 metadata required by the legacy Buyer/Seller API

ALTER TABLE commerce_inventory
    ADD COLUMN description TEXT NOT NULL DEFAULT '',
    ADD COLUMN status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'published', 'archived')),
    ADD COLUMN seller_name TEXT,
    ADD COLUMN category_id TEXT,
    ADD COLUMN delivery_estimate TEXT,
    ADD COLUMN return_policy TEXT,
    ADD COLUMN image_url TEXT,
    ADD COLUMN image_caption TEXT,
    ADD COLUMN delivery_areas JSONB NOT NULL DEFAULT '[]'::JSONB,
    ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE commerce_orders DROP CONSTRAINT commerce_orders_status_check;
ALTER TABLE commerce_orders ADD CONSTRAINT commerce_orders_status_check CHECK (
    status IN (
        'prepared', 'payment_pending', 'paid', 'payment_failed', 'payment_unknown',
        'confirmed', 'preparing', 'shipped', 'delivered', 'cancelled'
    )
);

CREATE TABLE commerce_issues (
    issue_id UUID PRIMARY KEY,
    order_id UUID NOT NULL REFERENCES commerce_orders(order_id),
    principal_id TEXT NOT NULL,
    seller_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'investigating', 'resolved')),
    reason TEXT NOT NULL,
    description TEXT NOT NULL,
    response TEXT,
    remedy JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX commerce_issues_principal_idx
    ON commerce_issues (principal_id, created_at DESC);
CREATE INDEX commerce_issues_seller_idx
    ON commerce_issues (seller_id, created_at DESC);
