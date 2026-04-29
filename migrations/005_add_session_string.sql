-- Migration 005: Add session_string column to client_config
-- Purpose: Store Telethon StringSession per client for multi-tenant bot runtime.
-- Rollback: ALTER TABLE client_config DROP COLUMN IF EXISTS session_string;

ALTER TABLE client_config ADD COLUMN IF NOT EXISTS session_string TEXT;
