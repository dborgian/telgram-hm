"""
cache.py — Upstash Redis
Gestisce: history chat per utente, cache profilo utente (TTL 1h)

Struttura Redis:
  history:{user_id}   → List di JSON {"role":..., "content":...}, max MAX_HISTORY_TURNS*2 elementi
  profile:{user_id}   → JSON del profilo cliente, TTL PROFILE_CACHE_TTL secondi
"""

import json
import logging

import redis.asyncio as aioredis

import config

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            config.UPSTASH_REDIS_URL,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
    return _redis


# ---------------------------------------------------------------------------
# History chat
# ---------------------------------------------------------------------------


def _history_key(user_id: int) -> str:
    return f"history:{user_id}"


async def get_history(user_id: int) -> list[dict]:
    """Ritorna gli ultimi MAX_HISTORY_TURNS*2 messaggi in ordine cronologico."""
    r = get_redis()
    # LRANGE 0 -1 ritorna dal più vecchio al più recente (RPUSH preserva ordine)
    raw: list[str] = await r.lrange(_history_key(user_id), 0, -1)
    return [json.loads(m) for m in raw]


async def save_turn(user_id: int, user_msg: str, assistant_msg: str) -> None:
    """Appende user + assistant al log, mantiene sliding window."""
    r = get_redis()
    key = _history_key(user_id)
    pipe = r.pipeline()
    pipe.rpush(key, json.dumps({"role": "user", "content": user_msg}))
    pipe.rpush(key, json.dumps({"role": "assistant", "content": assistant_msg}))
    # Taglia a MAX_HISTORY_TURNS * 2 elementi (sliding window)
    pipe.ltrim(key, -(config.MAX_HISTORY_TURNS * 2), -1)
    await pipe.execute()
    logger.debug("Saved turn to Redis history for user %d", user_id)


# ---------------------------------------------------------------------------
# Cache profilo utente
# ---------------------------------------------------------------------------


def _profile_key(user_id: int) -> str:
    return f"profile:{user_id}"


async def get_cached_profile(user_id: int) -> dict | None:
    """Ritorna il profilo dalla cache Redis, o None se assente/scaduto."""
    r = get_redis()
    raw = await r.get(_profile_key(user_id))
    if raw is None:
        return None
    return json.loads(raw)


async def cache_profile(user_id: int, profile: dict) -> None:
    """Salva il profilo in Redis con TTL."""
    r = get_redis()
    await r.set(
        _profile_key(user_id),
        json.dumps(profile),
        ex=config.PROFILE_CACHE_TTL,
    )


async def invalidate_profile(user_id: int) -> None:
    """Invalida la cache profilo (es. dopo update_stage o call_booked)."""
    r = get_redis()
    await r.delete(_profile_key(user_id))


async def clear_history(user_id: int) -> None:
    """Cancella la history chat da Redis."""
    r = get_redis()
    await r.delete(_history_key(user_id))
