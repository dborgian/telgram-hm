"""
cache.py — Upstash Redis
Gestisce: history chat per utente, cache profilo utente (TTL 1h)

Struttura Redis:
  history:{user_id}   → List di JSON {"role":..., "content":...}, max MAX_HISTORY_TURNS*2 elementi
  profile:{user_id}   → JSON del profilo cliente, TTL PROFILE_CACHE_TTL secondi
"""

import asyncio
import json
import logging

import redis.asyncio as aioredis
from redis.exceptions import RedisError

import config

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None
_REDIS_BACKOFF = [0.5, 1.0, 2.0]


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


async def _with_redis_retry(op) -> object:
    """Execute a Redis operation, reinitialising the client on connection errors.
    Initial attempt + up to 3 retries with backoff [0.5, 1.0, 2.0] seconds.
    """
    global _redis
    last_exc: Exception | None = None
    for i in range(len(_REDIS_BACKOFF) + 1):
        try:
            return await op(get_redis())
        except (OSError, RedisError) as exc:
            logger.warning(
                "Redis error (attempt %d/%d): %s", i + 1, len(_REDIS_BACKOFF) + 1, exc
            )
            _redis = None
            last_exc = exc
            if i < len(_REDIS_BACKOFF):
                await asyncio.sleep(_REDIS_BACKOFF[i])
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# History chat
# ---------------------------------------------------------------------------


def _history_key(user_id: int) -> str:
    return f"history:{user_id}"


async def get_history(user_id: int) -> list[dict]:
    """Ritorna gli ultimi MAX_HISTORY_TURNS*2 messaggi in ordine cronologico."""
    key = _history_key(user_id)
    raw: list[str] = await _with_redis_retry(lambda r: r.lrange(key, 0, -1))
    return [json.loads(m) for m in raw]


async def save_turn(user_id: int, user_msg: str, assistant_msg: str) -> None:
    """Appende user + assistant al log, mantiene sliding window."""
    key = _history_key(user_id)
    user_turn = json.dumps({"role": "user", "content": user_msg})
    asst_turn = json.dumps({"role": "assistant", "content": assistant_msg})
    trim_start = -(config.MAX_HISTORY_TURNS * 2)

    async def _pipe(r: aioredis.Redis) -> None:
        pipe = r.pipeline()
        pipe.rpush(key, user_turn)
        pipe.rpush(key, asst_turn)
        pipe.ltrim(key, trim_start, -1)
        await pipe.execute()

    await _with_redis_retry(_pipe)
    logger.debug("Saved turn to Redis history for user %d", user_id)


# ---------------------------------------------------------------------------
# Cache profilo utente
# ---------------------------------------------------------------------------


def _profile_key(user_id: int) -> str:
    return f"profile:{user_id}"


async def get_cached_profile(user_id: int) -> dict | None:
    """Ritorna il profilo dalla cache Redis, o None se assente/scaduto."""
    key = _profile_key(user_id)
    raw: str | None = await _with_redis_retry(lambda r: r.get(key))
    if raw is None:
        return None
    return json.loads(raw)


async def cache_profile(user_id: int, profile: dict) -> None:
    """Salva il profilo in Redis con TTL."""
    key = _profile_key(user_id)
    value = json.dumps(profile)
    ttl = config.PROFILE_CACHE_TTL
    await _with_redis_retry(lambda r: r.set(key, value, ex=ttl))


async def invalidate_profile(user_id: int) -> None:
    """Invalida la cache profilo (es. dopo update_stage o call_booked)."""
    key = _profile_key(user_id)
    await _with_redis_retry(lambda r: r.delete(key))


async def clear_history(user_id: int) -> None:
    """Cancella la history chat da Redis."""
    key = _history_key(user_id)
    await _with_redis_retry(lambda r: r.delete(key))
