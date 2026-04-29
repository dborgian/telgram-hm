-- Migration 004: Add user_summary column to customers
-- Purpose: Store a concise AI-generated summary of the user profile/conversation
--          for quick context injection without reading full history.
-- Rollback: ALTER TABLE customers DROP COLUMN IF EXISTS user_summary;

ALTER TABLE customers ADD COLUMN IF NOT EXISTS user_summary TEXT;
