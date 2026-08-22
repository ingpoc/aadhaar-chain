-- Buyer SPA cart/billing session keyed by ondc-session-id.
-- PATCH /api/cart/buyer/{session_id} upserts; GET never 404s a missing profile.

CREATE TABLE IF NOT EXISTS commerce_buyer_sessions (
    session_id TEXT PRIMARY KEY,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
