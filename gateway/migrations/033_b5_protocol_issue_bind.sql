-- Protocol-bound IGM issues: a Workbench/BAP issue may exist before a local
-- commerce order UUID. Bind to the ONDC confirm order_id instead.

ALTER TABLE commerce_issues
    ALTER COLUMN order_id DROP NOT NULL;

ALTER TABLE commerce_issues
    ADD COLUMN IF NOT EXISTS protocol_order_id TEXT,
    ADD COLUMN IF NOT EXISTS protocol_transaction_id TEXT;

CREATE INDEX IF NOT EXISTS commerce_issues_protocol_order_idx
    ON commerce_issues (protocol_order_id, created_at DESC)
    WHERE protocol_order_id IS NOT NULL;
