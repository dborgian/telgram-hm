"""
db.py — Facade unificata per processor.py
Delega a:
  cache.py  → Upstash Redis  (history chat, cache profilo)
  store.py  → Supabase       (profilo persistente, stage, call_booked)

processor.py non cambia — chiama sempre db.xxx().
"""

import cache
import store


async def init_db() -> None:
    """Inizializza le connessioni Redis e Supabase al boot."""
    # Redis: connessione lazy (primo get_redis()), verifica ping
    r = cache.get_redis()
    await r.ping()

    # Supabase: connessione lazy (primo get_client())
    await store.get_client()


async def get_history(user_id: int) -> list[dict]:
    """History da Redis — veloce, usata ad ogni turno."""
    return await cache.get_history(user_id)


async def save_turn(user_id: int, user_msg: str, assistant_msg: str) -> None:
    """Salva il turno su Redis (history) e incrementa contatore su Supabase."""
    await cache.save_turn(user_id, user_msg, assistant_msg)
    await store.increment_turn_count(user_id)


async def upsert_customer(
    user_id: int,
    first_name: str | None,
    username: str | None,
) -> None:
    """Crea/aggiorna profilo su Supabase e invalida cache Redis."""
    await store.upsert_customer(user_id, first_name, username)
    await cache.invalidate_profile(user_id)


async def get_customer(user_id: int) -> dict | None:
    """
    Legge profilo con cache-aside:
      1. Redis (veloce, TTL 1h)
      2. Se miss → Supabase → salva in Redis
    """
    profile = await cache.get_cached_profile(user_id)
    if profile is not None:
        return profile

    profile = await store.get_customer(user_id)
    if profile is not None:
        await cache.cache_profile(user_id, profile)
    return profile


async def update_stage(user_id: int, stage: str) -> None:
    """Aggiorna stage su Supabase e invalida cache Redis."""
    await store.update_stage(user_id, stage)
    await cache.invalidate_profile(user_id)


# ---------------------------------------------------------------------------
# Test utilities
# ---------------------------------------------------------------------------


async def reset_user(user_id: int) -> None:
    """Resetta history Redis e stage Supabase — utile per test."""
    await cache.clear_history(user_id)
    await cache.invalidate_profile(user_id)
    await store.update_stage(user_id, "stage_1_greet")


async def set_call_booked(user_id: int, booked: bool = True) -> None:
    """Simula una prenotazione Calendly senza toccare Calendly reale."""
    await store.set_call_booked(user_id, booked)
    await cache.invalidate_profile(user_id)


async def set_lead_status(user_id: int, status: str) -> None:
    """Aggiorna lo status del lead su Supabase (es. 'LL' = Lost Lead)."""
    await store.set_lead_status(user_id, status)
