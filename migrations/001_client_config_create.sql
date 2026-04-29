-- Migration 001: Create client_config table
-- Purpose: Multi-tenant support — each client (coach) has their own config
-- Rollback: DROP TABLE IF EXISTS client_config CASCADE;
--           (safe only before Step 2 migration runs)

CREATE TABLE IF NOT EXISTS client_config (
    client_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                  TEXT NOT NULL,
    slug                  TEXT NOT NULL UNIQUE,
    system_prompt_base    TEXT NOT NULL DEFAULT '',
    stage_instructions    JSONB NOT NULL DEFAULT '{}',
    brand_voice           JSONB NOT NULL DEFAULT '{}',
    icp_rules             JSONB NOT NULL DEFAULT '{}',
    calendly_url          TEXT,
    vsl_url               TEXT,
    guardrails            JSONB NOT NULL DEFAULT '{}',
    few_shot_examples     TEXT[] NOT NULL DEFAULT '{}',
    is_active             BOOLEAN NOT NULL DEFAULT true,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed HeyMyra with fixed known UUID so code can reference it without DB lookup
INSERT INTO client_config (client_id, name, slug, is_active)
VALUES ('00000000-0000-0000-0000-000000000001', 'reavermarketing', 'reavermarketing', true)
ON CONFLICT (client_id) DO NOTHING;
