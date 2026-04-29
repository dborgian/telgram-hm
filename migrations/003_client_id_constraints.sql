-- Migration 003: Enforce NOT NULL + unique constraints + indexes + updated RPC
-- Purpose: Complete multi-tenant isolation.
-- Prerequisites: Migration 002 applied AND Python app deployed (all rows have client_id set).
-- Rollback:
--   ALTER TABLE customers ALTER COLUMN client_id DROP NOT NULL;
--   ALTER TABLE customers DROP CONSTRAINT IF EXISTS customers_client_user_unique;
--   ALTER TABLE conversation_state ALTER COLUMN client_id DROP NOT NULL;
--   ALTER TABLE conversation_state DROP CONSTRAINT IF EXISTS conversation_state_client_user_unique;
--   DROP INDEX CONCURRENTLY IF EXISTS idx_customers_client_id;
--   DROP INDEX CONCURRENTLY IF EXISTS idx_conversation_state_client_id;
--   DROP INDEX CONCURRENTLY IF EXISTS idx_messages_client_user;
--   DROP INDEX CONCURRENTLY IF EXISTS idx_messages_client_id;
--   -- Restore original RPC (single-param, no client_id):
--   CREATE OR REPLACE FUNCTION increment_turn_count(p_user_id bigint)
--   RETURNS void LANGUAGE sql AS $$
--     INSERT INTO conversation_state (user_id, turn_count, last_reply_at)
--     VALUES (p_user_id, 1, now())
--     ON CONFLICT (user_id)
--     DO UPDATE SET
--       turn_count = conversation_state.turn_count + 1,
--       last_reply_at = now();
--   $$;

-- ── Step 1: Enforce NOT NULL ───────────────────────────────────────────────

ALTER TABLE customers
    ALTER COLUMN client_id SET NOT NULL;

ALTER TABLE conversation_state
    ALTER COLUMN client_id SET NOT NULL;

-- messages may not exist yet — guard with existence check
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'messages'
    ) THEN
        ALTER TABLE messages ALTER COLUMN client_id SET NOT NULL;
    END IF;
END $$;

-- ── Step 2: Unique constraints (client_id, user_id) ───────────────────────
-- Allows future multi-client use where the same Telegram user_id
-- can exist under different clients without collision.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'customers_client_user_unique'
    ) THEN
        ALTER TABLE customers
            ADD CONSTRAINT customers_client_user_unique UNIQUE (client_id, user_id);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'conversation_state_client_user_unique'
    ) THEN
        ALTER TABLE conversation_state
            ADD CONSTRAINT conversation_state_client_user_unique UNIQUE (client_id, user_id);
    END IF;
END $$;

-- ── Step 3: Indexes ────────────────────────────────────────────────────────
-- NOTE: These use plain CREATE INDEX (not CONCURRENTLY) — takes a brief table lock.
-- Safe at current table sizes. For large prod tables run outside a transaction with CONCURRENTLY.

CREATE INDEX IF NOT EXISTS idx_customers_client_id
    ON customers(client_id);

CREATE INDEX IF NOT EXISTS idx_conversation_state_client_id
    ON conversation_state(client_id);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'messages'
    ) THEN
        -- Covers dashboard queries: all messages per client ordered by time
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_messages_client_user
                     ON messages(client_id, user_id, created_at DESC)';
        -- Covers aggregate queries filtering by client only
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_messages_client_id
                     ON messages(client_id)';
    END IF;
END $$;

-- ── Step 4: Updated increment_turn_count RPC ──────────────────────────────
-- Adds p_client_id parameter (defaults to HeyMyra UUID for backward compat).
-- ON CONFLICT target remains user_id (still unique in conversation_state).
-- TODO (before multi-client go-live): change ON CONFLICT to (client_id, user_id)
--   and drop the old single-column unique constraint on user_id.

CREATE OR REPLACE FUNCTION increment_turn_count(
    p_user_id   bigint,
    p_client_id uuid DEFAULT '00000000-0000-0000-0000-000000000001'
)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO conversation_state (user_id, client_id, turn_count, last_reply_at)
    VALUES (p_user_id, p_client_id, 1, now())
    ON CONFLICT (user_id)
    DO UPDATE SET
        turn_count     = conversation_state.turn_count + 1,
        last_reply_at  = now(),
        client_id      = EXCLUDED.client_id;
$$;
