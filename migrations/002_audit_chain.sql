-- Tamper-evident audit chain.
--
-- Each audit event is hash-linked to the one before it and, when an audit signer is
-- configured, signed. Rows written before this migration have NULL hashes and are reported
-- as an unchained prefix rather than being presented as verified.
ALTER TABLE audit_events ADD COLUMN prev_hash TEXT;
ALTER TABLE audit_events ADD COLUMN event_hash TEXT;
ALTER TABLE audit_events ADD COLUMN signature TEXT;
ALTER TABLE audit_events ADD COLUMN signer TEXT;
