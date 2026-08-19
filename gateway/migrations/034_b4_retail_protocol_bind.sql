-- B4 retail protocol binding: one CommerceV1 order per RET10 transaction.

CREATE UNIQUE INDEX IF NOT EXISTS commerce_orders_retail_transaction_idx
    ON commerce_orders ((fulfilment->'retail'->>'transaction_id'))
    WHERE fulfilment->'retail'->>'transaction_id' IS NOT NULL
      AND fulfilment->'retail'->>'transaction_id' <> '';

CREATE INDEX IF NOT EXISTS commerce_orders_retail_protocol_order_idx
    ON commerce_orders ((fulfilment->'retail'->>'protocol_order_id'))
    WHERE fulfilment->'retail'->>'protocol_order_id' IS NOT NULL
      AND fulfilment->'retail'->>'protocol_order_id' <> '';
