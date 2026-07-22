-- Migration 010: durable, principal-scoped AgentGuard state.

CREATE TABLE agentguard_agents (
    agent_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'revoked')),
    current_mandate_id TEXT,
    current_mandate_version INTEGER,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT agentguard_agents_owner_uniq UNIQUE (principal_id, agent_id),
    CONSTRAINT agentguard_agents_current_mandate_pair CHECK (
        (current_mandate_id IS NULL) = (current_mandate_version IS NULL)
    )
);

CREATE INDEX agentguard_agents_owner_status_idx
    ON agentguard_agents(principal_id, status, role);

CREATE TABLE agentguard_mandate_versions (
    mandate_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    principal_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('draft', 'active', 'expired', 'revoked')),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (mandate_id, version),
    CONSTRAINT agentguard_mandates_owner_version_uniq
        UNIQUE (principal_id, mandate_id, version),
    CONSTRAINT agentguard_mandates_agent_fk
        FOREIGN KEY (principal_id, agent_id)
        REFERENCES agentguard_agents(principal_id, agent_id)
);

CREATE UNIQUE INDEX agentguard_mandates_agent_version_idx
    ON agentguard_mandate_versions(principal_id, agent_id, version);

CREATE INDEX agentguard_mandates_owner_status_idx
    ON agentguard_mandate_versions(principal_id, status, agent_id, version DESC);

ALTER TABLE agentguard_agents
    ADD CONSTRAINT agentguard_agents_current_mandate_fk
    FOREIGN KEY (principal_id, current_mandate_id, current_mandate_version)
    REFERENCES agentguard_mandate_versions(principal_id, mandate_id, version)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE agentguard_decisions (
    decision_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    mandate_id TEXT NOT NULL,
    mandate_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    policy JSONB NOT NULL,
    risk JSONB NOT NULL,
    required_action TEXT NOT NULL,
    expiry TIMESTAMPTZ,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT agentguard_decisions_owner_uniq UNIQUE (principal_id, decision_id),
    CONSTRAINT agentguard_decisions_agent_fk
        FOREIGN KEY (principal_id, agent_id)
        REFERENCES agentguard_agents(principal_id, agent_id),
    CONSTRAINT agentguard_decisions_mandate_fk
        FOREIGN KEY (principal_id, mandate_id, mandate_version)
        REFERENCES agentguard_mandate_versions(principal_id, mandate_id, version)
);

CREATE INDEX agentguard_decisions_owner_status_idx
    ON agentguard_decisions(principal_id, status, created_at DESC);

CREATE INDEX agentguard_decisions_mandate_idx
    ON agentguard_decisions(principal_id, mandate_id, mandate_version);

CREATE TABLE agentguard_approvals (
    approval_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    mandate_id TEXT NOT NULL,
    mandate_version INTEGER NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'issued'
        CHECK (status IN ('issued', 'consumed', 'expired', 'revoked')),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT agentguard_approvals_owner_uniq UNIQUE (principal_id, approval_id),
    CONSTRAINT agentguard_approvals_decision_fk
        FOREIGN KEY (principal_id, decision_id)
        REFERENCES agentguard_decisions(principal_id, decision_id),
    CONSTRAINT agentguard_approvals_agent_fk
        FOREIGN KEY (principal_id, agent_id)
        REFERENCES agentguard_agents(principal_id, agent_id),
    CONSTRAINT agentguard_approvals_mandate_fk
        FOREIGN KEY (principal_id, mandate_id, mandate_version)
        REFERENCES agentguard_mandate_versions(principal_id, mandate_id, version),
    CONSTRAINT agentguard_approvals_consumed_pair CHECK (
        (status = 'consumed' AND consumed_at IS NOT NULL)
        OR (status <> 'consumed' AND consumed_at IS NULL)
    )
);

CREATE INDEX agentguard_approvals_owner_status_idx
    ON agentguard_approvals(principal_id, status, expires_at);

CREATE TABLE agentguard_execution_intents (
    intent_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    decision_id TEXT,
    approval_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'executing', 'succeeded', 'failed')),
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT agentguard_execution_intents_idempotency_uniq
        UNIQUE (principal_id, operation, idempotency_key),
    CONSTRAINT agentguard_execution_intents_owner_uniq
        UNIQUE (principal_id, intent_id),
    CONSTRAINT agentguard_execution_intents_decision_fk
        FOREIGN KEY (principal_id, decision_id)
        REFERENCES agentguard_decisions(principal_id, decision_id),
    CONSTRAINT agentguard_execution_intents_approval_fk
        FOREIGN KEY (principal_id, approval_id)
        REFERENCES agentguard_approvals(principal_id, approval_id)
);

CREATE INDEX agentguard_execution_intents_owner_status_idx
    ON agentguard_execution_intents(principal_id, status, created_at DESC);

CREATE TABLE agentguard_receipts (
    receipt_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    mandate_id TEXT NOT NULL,
    mandate_version INTEGER NOT NULL,
    decision_id TEXT,
    approval_id TEXT,
    intent_id TEXT,
    status TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT agentguard_receipts_owner_uniq UNIQUE (principal_id, receipt_id),
    CONSTRAINT agentguard_receipts_agent_fk
        FOREIGN KEY (principal_id, agent_id)
        REFERENCES agentguard_agents(principal_id, agent_id),
    CONSTRAINT agentguard_receipts_mandate_fk
        FOREIGN KEY (principal_id, mandate_id, mandate_version)
        REFERENCES agentguard_mandate_versions(principal_id, mandate_id, version),
    CONSTRAINT agentguard_receipts_decision_fk
        FOREIGN KEY (principal_id, decision_id)
        REFERENCES agentguard_decisions(principal_id, decision_id),
    CONSTRAINT agentguard_receipts_approval_fk
        FOREIGN KEY (principal_id, approval_id)
        REFERENCES agentguard_approvals(principal_id, approval_id),
    CONSTRAINT agentguard_receipts_intent_fk
        FOREIGN KEY (principal_id, intent_id)
        REFERENCES agentguard_execution_intents(principal_id, intent_id)
);

CREATE INDEX agentguard_receipts_owner_status_idx
    ON agentguard_receipts(principal_id, status, created_at DESC);

CREATE FUNCTION reject_agentguard_immutable_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER agentguard_mandates_immutable
BEFORE UPDATE OR DELETE ON agentguard_mandate_versions
FOR EACH ROW EXECUTE FUNCTION reject_agentguard_immutable_mutation();

CREATE TRIGGER agentguard_decisions_immutable
BEFORE UPDATE OR DELETE ON agentguard_decisions
FOR EACH ROW EXECUTE FUNCTION reject_agentguard_immutable_mutation();

CREATE TRIGGER agentguard_receipts_immutable
BEFORE UPDATE OR DELETE ON agentguard_receipts
FOR EACH ROW EXECUTE FUNCTION reject_agentguard_immutable_mutation();
