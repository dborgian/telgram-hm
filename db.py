"""
db.py — Facade unificata per processor.py
Delega a:
  cache.py  → Upstash Redis  (history chat, cache profilo)
  store.py  → Supabase       (profilo persistente, stage, call_booked)

processor.py non cambia — chiama sempre db.xxx().
"""

import json
import logging
from typing import TypedDict

import cache
import config
import store

logger = logging.getLogger(__name__)


class HistoryTurn(TypedDict):
    role: str  # "user" | "assistant"
    content: str


class CustomerProfile(TypedDict):
    user_id: int
    first_name: str | None
    username: str | None
    stage: str | None
    call_booked: bool
    status: str | None
    notes: str | None
    conversation_stage: str
    turn_count: int


async def init_db() -> None:
    """Inizializza le connessioni Redis e Supabase al boot."""
    # Redis: connessione lazy (primo get_redis()), verifica ping
    r = cache.get_redis()
    await r.ping()

    # Supabase: connessione lazy (primo get_client())
    await store.get_client()


async def get_history(user_id: int, client_id: str = "") -> list[dict]:
    """History da Redis — veloce, usata ad ogni turno."""
    cid = client_id or config.DEFAULT_CLIENT_ID
    return await cache.get_history(cid, user_id)


async def save_turn(
    user_id: int, user_msg: str, assistant_msg: str, client_id: str = ""
) -> None:
    """Salva il turno su Redis (history), incrementa contatore e persiste su Supabase."""
    import asyncio

    cid = client_id or config.DEFAULT_CLIENT_ID
    await cache.save_turn(cid, user_id, user_msg, assistant_msg)
    await asyncio.gather(
        store.increment_turn_count(user_id),
        store.save_message(user_id, "user", user_msg),
        store.save_message(user_id, "assistant", assistant_msg),
        return_exceptions=True,  # messages table potrebbe non esistere ancora
    )


async def upsert_customer(
    user_id: int,
    first_name: str | None,
    username: str | None,
    client_id: str = "",
) -> None:
    """Crea/aggiorna profilo su Supabase e invalida cache Redis."""
    cid = client_id or config.DEFAULT_CLIENT_ID
    await store.upsert_customer(user_id, first_name, username)
    await cache.invalidate_profile(cid, user_id)


async def get_customer(user_id: int, client_id: str = "") -> dict | None:
    """
    Legge profilo con cache-aside:
      1. Redis (veloce, TTL 1h)
      2. Se miss → Supabase → salva in Redis
    """
    cid = client_id or config.DEFAULT_CLIENT_ID
    profile = await cache.get_cached_profile(cid, user_id)
    if profile is not None:
        return profile

    profile = await store.get_customer(user_id)
    if profile is not None:
        await cache.cache_profile(cid, user_id, profile)
    return profile


async def update_stage(user_id: int, stage: str, client_id: str = "") -> None:
    """Aggiorna stage su Supabase e invalida cache Redis."""
    cid = client_id or config.DEFAULT_CLIENT_ID
    await store.update_stage(user_id, stage)
    await cache.invalidate_profile(cid, user_id)


# ---------------------------------------------------------------------------
# Test utilities
# ---------------------------------------------------------------------------


async def reset_user(user_id: int, client_id: str = "") -> None:
    """Resetta history Redis e stage Supabase — utile per test."""
    cid = client_id or config.DEFAULT_CLIENT_ID
    await cache.clear_history(cid, user_id)
    await cache.invalidate_profile(cid, user_id)
    await store.update_stage(user_id, "stage_1_greet")


async def set_call_booked(
    user_id: int, booked: bool = True, client_id: str = ""
) -> None:
    """Simula una prenotazione Calendly senza toccare Calendly reale."""
    cid = client_id or config.DEFAULT_CLIENT_ID
    await store.set_call_booked(user_id, booked)
    await cache.invalidate_profile(cid, user_id)


async def set_lead_status(user_id: int, status: str) -> None:
    """Aggiorna lo status del lead su Supabase (es. 'LL' = Lost Lead)."""
    await store.set_lead_status(user_id, status)


async def set_awaiting_reply(user_id: int, value: bool, client_id: str = "") -> None:
    """Aggiorna awaiting_reply su Supabase e invalida cache Redis."""
    cid = client_id or config.DEFAULT_CLIENT_ID
    await store.set_awaiting_reply(user_id, value)
    await cache.invalidate_profile(cid, user_id)


async def set_hot_lead(user_id: int, client_id: str = "") -> None:
    """Marca il lead come hot su Supabase e invalida cache Redis."""
    cid = client_id or config.DEFAULT_CLIENT_ID
    await store.set_hot_lead(user_id)
    await cache.invalidate_profile(cid, user_id)


# ---------------------------------------------------------------------------
# Client config (multi-tenant)
# ---------------------------------------------------------------------------


def _dict_to_client_config(client_id: str, data: dict) -> "ClientConfig":
    from models import ClientConfig

    import llm as _llm

    return ClientConfig(
        client_id=client_id,
        system_prompt_base=data.get("system_prompt_base") or _llm.SYSTEM_PROMPT_BASE,
        stage_instructions=data.get("stage_instructions")
        or dict(_llm.STAGE_INSTRUCTIONS),
        llm_model=data.get("llm_model") or "gpt-4o-mini",
        vsl_base_url=data.get("vsl_base_url") or "",
        calendly_base_url=data.get("calendly_base_url") or "",
        vsl_domain=data.get("vsl_domain") or "go.onlineperdonne.com",
        cal_domain=data.get("cal_domain") or "calendly.com/chat-manager",
        default_stage=data.get("default_stage") or "stage_1_greet",
        session_string=data.get("session_string") or "",
    )


async def get_client_config(client_id: str) -> "ClientConfig":
    from models import ClientConfig

    # 1. Redis cache
    cache_key = f"config:{client_id}"
    try:
        raw: str | None = await cache._with_redis_retry(lambda r: r.get(cache_key))
        if raw is not None:
            return _dict_to_client_config(client_id, json.loads(raw))
    except Exception:
        logger.warning("Redis cache miss/error for config:%s", client_id)

    # 2. Supabase
    try:
        res = await store._with_supabase_retry(
            lambda sb: sb.table("client_config")
            .select("*")
            .eq("client_id", client_id)
            .maybe_single()
            .execute()
        )
        if res.data is not None:
            try:
                await cache._with_redis_retry(
                    lambda r: r.set(
                        cache_key,
                        json.dumps(res.data),
                        ex=config.CLIENT_CONFIG_CACHE_TTL,
                    )
                )
            except Exception:
                pass
            return _dict_to_client_config(client_id, res.data)
    except Exception:
        logger.warning("Supabase miss/error for client_config %s", client_id)

    # 3. Fallback — hardcoded from current llm.py
    import llm as _llm

    return ClientConfig(
        client_id=client_id,
        system_prompt_base=_llm.SYSTEM_PROMPT_BASE,
        stage_instructions=dict(_llm.STAGE_INSTRUCTIONS),
    )


async def get_all_active_clients() -> list["ClientConfig"]:
    """Return all active ClientConfig entries that have a session_string set.

    Used by main.py to start one TelegramClient per client at boot.
    Never raises — returns empty list on error.
    """
    from models import ClientConfig

    try:
        res = await store._with_supabase_retry(
            lambda sb: sb.table("client_config")
            .select("*")
            .eq("is_active", True)
            .not_.is_("session_string", "null")
            .neq("session_string", "")
            .execute()
        )
        return [
            _dict_to_client_config(row["client_id"], row) for row in (res.data or [])
        ]
    except Exception:
        logger.exception("get_all_active_clients failed")
        return []


async def invalidate_client_config(client_id: str) -> None:
    cache_key = f"config:{client_id}"
    try:
        await cache._with_redis_retry(lambda r: r.delete(cache_key))
    except Exception:
        logger.warning("Failed to invalidate config cache for %s", client_id)
