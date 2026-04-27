"""
store.py — Supabase (Postgres)
Gestisce: profilo utente persistente, stage conversazione, call_booked

Tabelle attese su Supabase (già esistenti in produzione):
  customers         — user_id, first_name, username, stage, notes, call_booked, first_seen, last_seen
  conversation_state — user_id, conversation_stage, turn_count, last_reply_at
"""

import logging
from datetime import datetime, timezone

from supabase import AsyncClient, acreate_client

import config

logger = logging.getLogger(__name__)

_client: AsyncClient | None = None


async def get_client() -> AsyncClient:
    global _client
    if _client is None:
        _client = await acreate_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    return _client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Profilo cliente
# ---------------------------------------------------------------------------


async def upsert_customer(
    user_id: int,
    first_name: str | None,
    username: str | None,
) -> None:
    """Crea il profilo se non esiste, aggiorna last_seen."""
    sb = await get_client()
    await (
        sb.table("customers")
        .upsert(
            {
                "user_id": user_id,
                "first_name": first_name,
                "username": username,
                "last_seen": _now_iso(),
            },
            on_conflict="user_id",
            ignore_duplicates=False,
        )
        .execute()
    )
    logger.debug("upserted customer %d in Supabase", user_id)


async def get_customer(user_id: int) -> dict | None:
    """Legge il profilo completo da Supabase (source of truth)."""
    sb = await get_client()
    res = (
        await sb.table("customers")
        .select("*, conversation_state(conversation_stage, turn_count)")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if res.data is None:
        return None

    profile = dict(res.data)
    # Flatten conversation_state nested object
    cs = profile.pop("conversation_state", None) or {}
    profile["conversation_stage"] = cs.get("conversation_stage", "stage_1_greet")
    profile["turn_count"] = cs.get("turn_count", 0)
    return profile


async def update_stage(user_id: int, stage: str) -> None:
    """Aggiorna lo stage della conversazione."""
    sb = await get_client()
    await (
        sb.table("conversation_state")
        .upsert(
            {"user_id": user_id, "conversation_stage": stage},
            on_conflict="user_id",
        )
        .execute()
    )
    logger.debug("updated stage for user %d → %s", user_id, stage)


async def increment_turn_count(user_id: int) -> None:
    """Incrementa il contatore turni in modo atomico tramite RPC PostgreSQL.

    Richiede questa funzione su Supabase (SQL editor, una tantum):

        CREATE OR REPLACE FUNCTION increment_turn_count(p_user_id bigint)
        RETURNS void LANGUAGE sql AS $$
          INSERT INTO conversation_state (user_id, turn_count, last_reply_at)
          VALUES (p_user_id, 1, now())
          ON CONFLICT (user_id)
          DO UPDATE SET
            turn_count = conversation_state.turn_count + 1,
            last_reply_at = now();
        $$;
    """
    sb = await get_client()
    await sb.rpc("increment_turn_count", {"p_user_id": user_id}).execute()


async def set_call_booked(user_id: int, booked: bool = True) -> None:
    """Imposta call_booked. Chiamato dal webhook Calendly o dai test."""
    sb = await get_client()
    await (
        sb.table("customers")
        .update({"call_booked": booked})
        .eq("user_id", user_id)
        .execute()
    )
    logger.info("call_booked=%s per user %d", booked, user_id)


async def set_lead_status(user_id: int, status: str) -> None:
    """Aggiorna il campo status del cliente su Supabase."""
    sb = await get_client()
    await (
        sb.table("customers")
        .update({"status": status})
        .eq("user_id", user_id)
        .execute()
    )
    logger.info("status=%s per user %d", status, user_id)
