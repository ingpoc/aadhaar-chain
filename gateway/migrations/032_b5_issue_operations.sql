-- B5 local issue operations: one persisted owner, SLA targets, and audit timeline.

ALTER TABLE commerce_issues
    ADD COLUMN IF NOT EXISTS owner_id TEXT,
    ADD COLUMN IF NOT EXISTS response_due_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS escalation_due_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS history JSONB NOT NULL DEFAULT '[]'::JSONB;

UPDATE commerce_issues
SET history = jsonb_build_array(
    jsonb_build_object(
        'status', status,
        'actor_id', principal_id,
        'note', 'Existing issue imported into operational history',
        'at', created_at
    )
)
WHERE history = '[]'::JSONB;
