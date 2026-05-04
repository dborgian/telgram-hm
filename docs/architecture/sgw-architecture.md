# SGW Architecture — Self-Generating Workflow

## 1. Stage Validation Gate

### Current State
`llm.py:259-266` implements a binary CB (call_booked) gate:
- `call_booked=False` + stage in {7, 8} → force `stage_6_verifying`
- `call_booked=True` + stage NOT in {7, 8} → force `stage_8_postbooking`

### Valid Transition Matrix

```
FROM \ TO           | 1  | 2  | 3  | 4  | 5  | 6  | 7  | 8  | 9  | 10 |
--------------------|----|----|----|----|----|----|----|----|----|----|
stage_1_greet       | -  | OK | .  | .  | .  | .  | .  | .  | OK | .  |
stage_2_video       | .  | -  | OK | .  | .  | .  | .  | .  | OK | .  |
stage_3_post_video  | .  | .  | -  | OK | OK | .  | .  | .  | OK | OK |
stage_4_answering   | .  | .  | .  | -  | OK | .  | .  | .  | OK | OK |
stage_5_booking     | .  | .  | .  | OK | -  | OK | .  | .  | OK | OK |
stage_6_verifying   | .  | .  | .  | .  | OK | -  | CB | CB | OK | .  |
stage_7_rescheduling| .  | .  | .  | .  | .  | .  | -  | OK | OK | .  |
stage_8_postbooking | .  | .  | .  | .  | .  | .  | OK | -  | OK | .  |
stage_9_uninterested| .  | .  | .  | .  | .  | .  | .  | .  | -  | .  |
stage_10_budget     | .  | .  | .  | OK | OK | .  | .  | .  | OK | -  |

Legend: OK = allowed, CB = requires call_booked=True, . = blocked, - = self
```

### Rules (programmatic, not LLM-dependent)

1. **No backward jumps** — stage number can only increase (except stage_9 which is reachable from any stage >= 3)
2. **CB gate** — stage_7/8 require `call_booked=True`
3. **Sequential funnel** — stages 1→2→3 are strictly sequential
4. **Budget loop** — stage_10 can return to stage_4 or stage_5 only
5. **Terminal stage** — stage_9 (DISENGAGE) has no outgoing transitions
6. **stage_6 is the gateway** — post-booking stages (7, 8) are only reachable through 6

### Implementation in classify_stage()

```python
# After LLM classification, before returning

VALID_TRANSITIONS: dict[str, set[str]] = {
    "stage_1_greet":        {"stage_2_video", "stage_9_uninterested"},
    "stage_2_video":        {"stage_3_post_video", "stage_9_uninterested"},
    "stage_3_post_video":   {"stage_4_answering_questions", "stage_5_booking", "stage_9_uninterested", "stage_10_budget"},
    "stage_4_answering_questions": {"stage_5_booking", "stage_9_uninterested", "stage_10_budget"},
    "stage_5_booking":      {"stage_4_answering_questions", "stage_6_verifying", "stage_9_uninterested", "stage_10_budget"},
    "stage_6_verifying":    {"stage_5_booking", "stage_7_rescheduling", "stage_8_postbooking", "stage_9_uninterested"},
    "stage_7_rescheduling": {"stage_8_postbooking", "stage_9_uninterested"},
    "stage_8_postbooking":  {"stage_7_rescheduling", "stage_9_uninterested"},
    "stage_9_uninterested": set(),  # terminal
    "stage_10_budget":      {"stage_4_answering_questions", "stage_5_booking", "stage_9_uninterested"},
}

def validate_transition(
    current_stage: str,
    proposed_stage: str,
    call_booked: bool,
) -> tuple[str, str]:
    """Returns (final_stage, action_taken).
    action_taken: 'valid' | 'forced_current' | 'forced_cb_gate' | 'forced_default'
    """
    # Same stage = stay = always valid
    if proposed_stage == current_stage:
        return proposed_stage, "valid"

    # CB gate (existing logic, takes priority)
    cb_stages = {"stage_7_rescheduling", "stage_8_postbooking"}
    if not call_booked and proposed_stage in cb_stages:
        return "stage_6_verifying", "forced_cb_gate"
    if call_booked and proposed_stage not in cb_stages and proposed_stage not in {"stage_9_uninterested"}:
        return "stage_8_postbooking", "forced_cb_gate"

    # Transition validity
    allowed = VALID_TRANSITIONS.get(current_stage, set())
    if proposed_stage in allowed:
        return proposed_stage, "valid"

    # Invalid transition: stay on current stage, log warning
    return current_stage, "forced_current"
```

### On invalid transition
| Scenario | Action | Rationale |
|----------|--------|-----------|
| Invalid transition | Stay on current stage | Prevents impossible jumps. LLM gets another chance next message. |
| CB gate violation | Force to 6 or 8 | Existing behavior, proven in production. |
| Unknown stage from LLM | Default to stage_1_greet | Existing behavior. |

All overrides are logged to `stage_transitions_log` (see section 2) with `was_overridden=true` and `override_reason`.


## 2. SQL Schema (Supabase/PostgreSQL)

```sql
-- ============================================================
-- STAGES: versionable instruction storage
-- ============================================================
CREATE TABLE stages (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    stage_key       TEXT NOT NULL,                     -- e.g. "stage_1_greet"
    instructions    TEXT NOT NULL,                     -- the actual prompt text
    is_active       BOOLEAN NOT NULL DEFAULT false,    -- only 1 active per stage_key
    version         INTEGER NOT NULL DEFAULT 1,
    parent_id       UUID REFERENCES stages(id),        -- previous version
    ab_weight       REAL NOT NULL DEFAULT 1.0,         -- 0.0-1.0, for A/B testing
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      TEXT NOT NULL DEFAULT 'system',    -- 'system' | 'sgw_engine' | 'human'
    notes           TEXT,                              -- why this version was created

    CONSTRAINT uq_stage_version UNIQUE (stage_key, version),
    CONSTRAINT chk_ab_weight CHECK (ab_weight >= 0.0 AND ab_weight <= 1.0)
);

-- Partial unique index: exactly 1 active version per stage_key
CREATE UNIQUE INDEX idx_stages_active
    ON stages (stage_key) WHERE is_active = true;

CREATE INDEX idx_stages_key ON stages (stage_key);
CREATE INDEX idx_stages_parent ON stages (parent_id);

-- ============================================================
-- STAGE METRICS: per-stage, per-version performance tracking
-- ============================================================
CREATE TABLE stage_metrics (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    stage_id        UUID NOT NULL REFERENCES stages(id),
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,

    -- Counters
    impressions     INTEGER NOT NULL DEFAULT 0,    -- times this stage was active for a user
    advances        INTEGER NOT NULL DEFAULT 0,    -- successful transitions to next stage
    retreats        INTEGER NOT NULL DEFAULT 0,    -- backward transitions
    disengages      INTEGER NOT NULL DEFAULT 0,    -- transitions to stage_9
    assistance_needed INTEGER NOT NULL DEFAULT 0,  -- escalations to human

    -- Rates (computed, stored for fast queries)
    advance_rate    REAL GENERATED ALWAYS AS (
        CASE WHEN impressions > 0 THEN advances::REAL / impressions ELSE 0 END
    ) STORED,
    disengage_rate  REAL GENERATED ALWAYS AS (
        CASE WHEN impressions > 0 THEN disengages::REAL / impressions ELSE 0 END
    ) STORED,

    -- Response quality
    avg_reply_length    REAL,
    avg_response_time_s REAL,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_stage_period UNIQUE (stage_id, period_start)
);

CREATE INDEX idx_metrics_stage ON stage_metrics (stage_id);
CREATE INDEX idx_metrics_period ON stage_metrics (period_start, period_end);
CREATE INDEX idx_metrics_advance_rate ON stage_metrics (advance_rate);

-- ============================================================
-- STAGE SUGGESTIONS: SGW engine proposals
-- ============================================================
CREATE TYPE suggestion_status AS ENUM (
    'pending',       -- generated, awaiting review
    'approved',      -- human approved, ready to activate
    'rejected',      -- human rejected
    'active',        -- currently live
    'rolled_back',   -- was active, rolled back
    'expired'        -- superseded by newer suggestion
);

CREATE TABLE stage_suggestions (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    stage_key       TEXT NOT NULL,
    instructions    TEXT NOT NULL,
    rationale       TEXT NOT NULL,              -- why the engine proposed this
    source_metrics  JSONB,                     -- metrics snapshot that triggered suggestion
    validation_result JSONB,                   -- output of instruction validator
    status          suggestion_status NOT NULL DEFAULT 'pending',
    reviewed_by     TEXT,                      -- null = not yet reviewed
    reviewed_at     TIMESTAMPTZ,
    stage_id        UUID REFERENCES stages(id), -- linked stage (set on approval)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_suggestions_status ON stage_suggestions (status);
CREATE INDEX idx_suggestions_stage_key ON stage_suggestions (stage_key);

-- ============================================================
-- STAGE TRANSITIONS LOG: every classify_stage() decision
-- ============================================================
CREATE TABLE stage_transitions_log (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    stage_from      TEXT NOT NULL,
    stage_to        TEXT NOT NULL,
    stage_proposed  TEXT NOT NULL,              -- what LLM originally proposed
    was_overridden  BOOLEAN NOT NULL DEFAULT false,
    override_reason TEXT,                      -- 'cb_gate' | 'invalid_transition' | 'unknown_stage'
    call_booked     BOOLEAN NOT NULL,
    assistance_needed BOOLEAN NOT NULL DEFAULT false,
    stage_version   INTEGER,                   -- which instruction version was active
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_transitions_user ON stage_transitions_log (user_id, created_at DESC);
CREATE INDEX idx_transitions_stages ON stage_transitions_log (stage_from, stage_to);
CREATE INDEX idx_transitions_overridden ON stage_transitions_log (was_overridden) WHERE was_overridden = true;
CREATE INDEX idx_transitions_time ON stage_transitions_log (created_at);

-- Partition by month for production scale (optional, recommended after 1M rows)
-- CREATE TABLE stage_transitions_log (...) PARTITION BY RANGE (created_at);

-- ============================================================
-- RPC: Atomic rollback function
-- ============================================================
CREATE OR REPLACE FUNCTION rollback_stage_version(
    p_stage_key TEXT,
    p_target_version INTEGER DEFAULT NULL  -- NULL = previous version
)
RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    v_current_id UUID;
    v_target_id UUID;
BEGIN
    -- Find current active version
    SELECT id INTO v_current_id
    FROM stages
    WHERE stage_key = p_stage_key AND is_active = true;

    IF v_current_id IS NULL THEN
        RAISE EXCEPTION 'No active version for stage %', p_stage_key;
    END IF;

    -- Find target version
    IF p_target_version IS NOT NULL THEN
        SELECT id INTO v_target_id
        FROM stages
        WHERE stage_key = p_stage_key AND version = p_target_version;
    ELSE
        -- Previous version = parent_id of current
        SELECT parent_id INTO v_target_id
        FROM stages
        WHERE id = v_current_id;
    END IF;

    IF v_target_id IS NULL THEN
        RAISE EXCEPTION 'Target version not found for stage %', p_stage_key;
    END IF;

    -- Atomic swap
    UPDATE stages SET is_active = false WHERE id = v_current_id;
    UPDATE stages SET is_active = true WHERE id = v_target_id;

    RETURN v_target_id;
END;
$$;
```


## 3. Instruction Schema Validator

```python
"""
instruction_validator.py — Validates stage instructions before activation.
"""
import re
from dataclasses import dataclass, field

# Variables required per stage (stage_key -> set of required vars)
REQUIRED_VARIABLES: dict[str, set[str]] = {
    "stage_1_greet":            set(),
    "stage_2_video":            {"{vsl_link}"},
    "stage_3_post_video":       set(),
    "stage_4_answering_questions": set(),
    "stage_5_booking":          {"{calendly_link}", "{today}", "{tomorrow}", "{vsllinksent2}"},
    "stage_6_verifying":        {"{vsllinksent2}"},
    "stage_7_rescheduling":     {"{today}"},
    "stage_8_postbooking":      set(),
    "stage_9_uninterested":     set(),
    "stage_10_budget":          set(),
}

# Variables allowed (superset — anything not here is flagged)
ALL_VALID_VARIABLES = {"{vsl_link}", "{calendly_link}", "{today}", "{tomorrow}", "{vsllinksent2}"}

# Injection / safety blocklist
FORBIDDEN_PATTERNS: list[re.Pattern] = [
    re.compile(r"ignore\s+(previous|all|above)\s+instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now", re.IGNORECASE),
    re.compile(r"system\s*:\s*", re.IGNORECASE),
    re.compile(r"forget\s+(everything|all)", re.IGNORECASE),
    re.compile(r"disregard", re.IGNORECASE),
    re.compile(r"pretend\s+you", re.IGNORECASE),
    re.compile(r"act\s+as\s+if", re.IGNORECASE),
    re.compile(r"<\s*script", re.IGNORECASE),
    re.compile(r"javascript:", re.IGNORECASE),
]

MAX_INSTRUCTION_LENGTH = 2000


@dataclass
class ValidationResult:
    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.valid = False
        self.errors.append(msg)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def validate_instruction(stage_key: str, instructions: str) -> ValidationResult:
    """Validate stage instructions before activation.

    Returns ValidationResult with errors (blocking) and warnings (informational).
    """
    result = ValidationResult()

    # 1. Stage key exists
    if stage_key not in REQUIRED_VARIABLES:
        result.add_error(f"Unknown stage_key: {stage_key}")
        return result

    # 2. Length check
    if len(instructions) > MAX_INSTRUCTION_LENGTH:
        result.add_error(
            f"Instructions too long: {len(instructions)} chars (max {MAX_INSTRUCTION_LENGTH})"
        )

    if len(instructions) < 20:
        result.add_error(f"Instructions too short: {len(instructions)} chars (min 20)")

    # 3. Required variables present
    required = REQUIRED_VARIABLES[stage_key]
    found_vars = set(re.findall(r"\{[a-z_]+\}", instructions))

    missing = required - found_vars
    if missing:
        result.add_error(f"Missing required variables: {missing}")

    # 4. No unknown variables
    unknown = found_vars - ALL_VALID_VARIABLES
    if unknown:
        result.add_warning(f"Unknown variables (will not be substituted): {unknown}")

    # 5. Forbidden patterns (prompt injection defense)
    for pattern in FORBIDDEN_PATTERNS:
        match = pattern.search(instructions)
        if match:
            result.add_error(
                f"Forbidden pattern detected: '{match.group()}' "
                f"(possible prompt injection)"
            )

    # 6. Must contain at least one actionable directive
    action_indicators = ["obiettivo", "azione", "script", "se ", "quando"]
    has_action = any(ind in instructions.lower() for ind in action_indicators)
    if not has_action:
        result.add_warning(
            "No actionable directive found (expected: Obiettivo, Azione, Script, etc.)"
        )

    return result
```


## 4. Rollback Mechanism

### Target: <30 seconds from decision to live

#### Option A: Database-first rollback (recommended)

```
1. Call Supabase RPC:
     SELECT rollback_stage_version('stage_5_booking');
   This atomically:
     - Sets current active version is_active=false
     - Sets previous version is_active=true
   Time: ~200ms

2. Invalidate in-memory cache (if any):
     Redis DEL stage_instructions:{stage_key}
   Time: ~50ms

3. Next classify_stage() call loads new instructions automatically.
   No process restart needed.
   Time: 0ms (lazy load)

Total: ~250ms
```

#### Implementation

```python
async def rollback_stage(
    stage_key: str,
    target_version: int | None = None,
) -> dict:
    """Rollback a stage to its previous (or specific) version.

    Returns {"rolled_back_to": version, "stage_id": uuid}.
    Raises ValueError if no previous version exists.
    """
    sb = await store.get_client()

    # Call the atomic RPC function
    result = await sb.rpc(
        "rollback_stage_version",
        {
            "p_stage_key": stage_key,
            "p_target_version": target_version,
        },
    ).execute()

    new_active_id = result.data

    # Fetch the version info for confirmation
    version_info = await sb.table("stages") \
        .select("version, instructions") \
        .eq("id", new_active_id) \
        .single() \
        .execute()

    # Update suggestion status if applicable
    await sb.table("stage_suggestions") \
        .update({"status": "rolled_back"}) \
        .eq("stage_key", stage_key) \
        .eq("status", "active") \
        .execute()

    logger.info(
        "Rolled back %s to version %d (id=%s)",
        stage_key, version_info.data["version"], new_active_id,
    )

    return {
        "stage_key": stage_key,
        "rolled_back_to": version_info.data["version"],
        "stage_id": new_active_id,
    }
```

#### Architecture for instruction loading

Currently `STAGE_INSTRUCTIONS` is a hardcoded dict in `llm.py:51-113`. The SGW requires dynamic loading:

```
                    +------------------+
                    |   Supabase       |
                    |   stages table   |
                    +--------+---------+
                             |
                     (lazy load, cache 60s)
                             |
                    +--------v---------+
                    |  instruction     |
                    |  cache (Redis)   |
                    |  TTL=60s         |
                    +--------+---------+
                             |
              +--------------+--------------+
              |                             |
     +--------v---------+         +--------v---------+
     | classify_stage() |         | generate_reply() |
     | reads stage_key  |         | reads full instr |
     +------------------+         +------------------+
```

**Loading strategy:**
1. On first call per stage_key: load from Supabase `stages WHERE is_active=true AND stage_key=X`
2. Cache in Redis with `stage_instructions:{stage_key}` TTL=60s
3. On rollback: delete the Redis cache key → next call fetches fresh
4. Fallback: if DB unreachable, use hardcoded `STAGE_INSTRUCTIONS` dict as last resort

This means rollback propagation is at most 60s (cache TTL), but immediate if Redis key is explicitly deleted during rollback.


## Data Flow Summary

```
User message
    │
    ▼
main.py: buffer + dedup
    │
    ▼
processor.py: process_conversation()
    │
    ├──► classify_stage()
    │       │
    │       ├── LLM proposes stage
    │       ├── validate_transition() ◄── VALID_TRANSITIONS matrix
    │       ├── CB gate check
    │       └── Log to stage_transitions_log
    │
    ├──► Load instructions (DB → Redis cache → hardcoded fallback)
    │       │
    │       └── instruction_validator.validate_instruction() ◄── on activation only
    │
    ├──► generate_reply()
    │
    └──► Update stage_metrics (async, non-blocking)
```

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| DB down during rollback | Can't swap versions | Hardcoded fallback dict stays active |
| Cache stale after rollback | Up to 60s serving old instructions | Explicit Redis DEL on rollback → 0s |
| LLM hallucinating stage keys | Invalid transition | validate_transition() catches it |
| Prompt injection in SGW-generated instructions | Corrupted persona | instruction_validator blocks activation |
| A/B test bias | Wrong conclusions | ab_weight + minimum impression threshold before evaluation |
| Transition matrix too restrictive | LLM stuck in wrong stage | Override with `stage_from=null` (first message) bypasses matrix |
