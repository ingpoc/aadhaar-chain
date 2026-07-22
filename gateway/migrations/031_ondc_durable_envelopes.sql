-- Migration 031: retain ONDC envelopes required for restart-safe processing

ALTER TABLE ondc_inbox
    ADD COLUMN envelope JSONB NOT NULL DEFAULT '{}'::JSONB;

ALTER TABLE ondc_outbox
    ADD COLUMN envelope JSONB NOT NULL DEFAULT '{}'::JSONB;

CREATE OR REPLACE FUNCTION reject_ondc_message_commitment_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.event_commitment <> OLD.event_commitment
        OR NEW.subscriber_id <> OLD.subscriber_id
        OR NEW.transaction_id <> OLD.transaction_id
        OR NEW.message_id <> OLD.message_id
        OR NEW.action <> OLD.action
        OR NEW.raw_envelope_commitment <> OLD.raw_envelope_commitment
        OR NEW.envelope <> OLD.envelope
        OR NEW.redacted_payload <> OLD.redacted_payload
        OR NEW.correlation_id IS DISTINCT FROM OLD.correlation_id
        OR (TG_TABLE_NAME = 'ondc_outbox'
            AND to_jsonb(NEW)->>'destination'
                IS DISTINCT FROM to_jsonb(OLD)->>'destination')
    THEN
        RAISE EXCEPTION 'ONDC message commitments are immutable';
    END IF;
    RETURN NEW;
END;
$$;
