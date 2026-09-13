-- Migration 004: durable Auth0/gateway session registry (revoke + device list).
-- Same PostgreSQL owner as AgentGuard/commerce. No parallel Redis/file store.

CREATE TABLE auth_sessions (
    sid TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    aud TEXT,
    identity_provider TEXT,
    iat BIGINT NOT NULL,
    exp BIGINT,
    device_label TEXT NOT NULL DEFAULT 'session',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX auth_sessions_principal_exp_idx
    ON auth_sessions (principal_id, exp);

CREATE TABLE auth_revoked_sids (
    sid TEXT PRIMARY KEY,
    principal_id TEXT,
    revoked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE auth_revoked_principals (
    principal_id TEXT PRIMARY KEY,
    revoked_before BIGINT NOT NULL,
    revoked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
