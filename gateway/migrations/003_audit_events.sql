-- Migration 003: append-only audit events

CREATE TABLE audit_events (
    event_id TEXT PRIMARY KEY,
    event TEXT NOT NULL,
    principal_id TEXT,
    actor TEXT,
    resource TEXT,
    correlation_id TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX audit_events_principal_idx
    ON audit_events(principal_id, occurred_at DESC)
    WHERE principal_id IS NOT NULL;

CREATE INDEX audit_events_correlation_idx
    ON audit_events(correlation_id, occurred_at ASC)
    WHERE correlation_id IS NOT NULL;

CREATE FUNCTION reject_audit_event_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END;
$$;

CREATE TRIGGER audit_events_append_only
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation();
