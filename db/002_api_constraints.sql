-- Applied automatically on a fresh volume, after 001_schema.sql.
-- Existing volumes: run this file once with psql --single-transaction.
-- Invalid legacy data aborts the migration; no existing events are deleted.
ALTER TABLE events
    ADD CONSTRAINT events_event_id_key UNIQUE (event_id),
    ADD CONSTRAINT events_event_id_length CHECK (char_length(event_id) BETWEEN 1 AND 200),
    ADD CONSTRAINT events_type_length CHECK (char_length(type) BETWEEN 1 AND 200),
    ADD CONSTRAINT events_payload_object CHECK (jsonb_typeof(payload) = 'object'),
    ADD CONSTRAINT events_status_valid CHECK (status IN ('pending', 'processing', 'delivered', 'failed')),
    ADD CONSTRAINT events_attempts_valid CHECK (attempts BETWEEN 0 AND 3);

ALTER TABLE delivery_attempts
    ADD CONSTRAINT delivery_attempts_event_attempt_key UNIQUE (event_pk, attempt),
    ADD CONSTRAINT delivery_attempts_attempt_valid CHECK (attempt BETWEEN 1 AND 3),
    ADD CONSTRAINT delivery_attempts_status_code_valid CHECK (status_code BETWEEN 100 AND 599);
