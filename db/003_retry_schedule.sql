-- Additive migration: preserves event IDs, payloads, states, and attempt history.
-- Existing volumes: apply once with psql --single-transaction.
ALTER TABLE events ADD COLUMN next_attempt_at TIMESTAMPTZ DEFAULT now();

UPDATE events SET next_attempt_at = NULL WHERE status <> 'pending';

CREATE INDEX events_pending_due_idx ON events (next_attempt_at, id)
    WHERE status = 'pending';
