-- Migration 002: Add client_id to existing tables (nullable — zero downtime)
-- Purpose: Multi-tenant data isolation. Nullable now, NOT NULL after Python deploy (migration 003).
-- Rollback: ALTER TABLE customers DROP COLUMN IF EXISTS client_id;
--           ALTER TABLE messages DROP COLUMN IF EXISTS client_id;
--           ALTER TABLE conversation_state DROP COLUMN IF EXISTS client_id;

ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS client_id UUID REFERENCES client_config(client_id);

-- NOTE: conversation_state exists in production (confirmed in store.py).
ALTER TABLE conversation_state
    ADD COLUMN IF NOT EXISTS client_id UUID REFERENCES client_config(client_id);

-- NOTE: messages table may not exist yet — store.py marks it as "Migration richiesta (una tantum)".
-- The DO block below is a no-op if the table does not exist yet; it will be created and
-- populated with client_id when the messages table migration runs separately.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'messages'
    ) THEN
        ALTER TABLE messages
            ADD COLUMN IF NOT EXISTS client_id UUID REFERENCES client_config(client_id);
    END IF;
END $$;

-- Backfill all existing rows to HeyMyra (the only client that exists)
BEGIN;
UPDATE customers
    SET client_id = '00000000-0000-0000-0000-000000000001'
    WHERE client_id IS NULL;

UPDATE conversation_state
    SET client_id = '00000000-0000-0000-0000-000000000001'
    WHERE client_id IS NULL;

-- Backfill messages only if the table exists
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'messages'
    ) THEN
        UPDATE messages
            SET client_id = '00000000-0000-0000-0000-000000000001'
            WHERE client_id IS NULL;
    END IF;
END $$;

COMMIT;
