-- First CF2/CF3 lifecycle slice on the frozen CF0 state contracts.

ALTER TABLE commerce_orders
    ADD COLUMN IF NOT EXISTS fulfilment JSONB NOT NULL DEFAULT '{"history":[]}'::JSONB;

CREATE UNIQUE INDEX IF NOT EXISTS commerce_returns_one_per_order_idx
    ON commerce_returns (order_id);

CREATE INDEX IF NOT EXISTS commerce_issues_order_idx
    ON commerce_issues (order_id, created_at DESC);
