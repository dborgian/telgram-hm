"""
store.py — Supabase (Postgres)
Gestisce: profilo utente persistente, stage conversazione, call_booked

Tabelle attese su Supabase (già esistenti in produzione):
  customers         — user_id, first_name, username, stage, notes, call_booked, first_seen, last_seen
  conversation_state — user_id, conversation_stage, turn_count, last_reply_at
"""

import asyncio
import logging
from datetime import datetime, timezone

from supabase import AsyncClient, acreate_client

import config

logger = logging.getLogger(__name__)

_client: AsyncClient | None = None
_SUPABASE_BACKOFF = [0.5, 1.0, 2.0]


async def get_client() -> AsyncClient:
    global _client
    if _client is None:
        _client = await acreate_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    return _client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_retryable(exc: Exception) -> bool:
    """True for connection-type errors worth retrying."""
    if isinstance(exc, (OSError, TimeoutError)):
        return True
    cls_name = type(exc).__name__.lower()
    return any(
        s in cls_name for s in ("connect", "timeout", "network", "socket", "read")
    )


async def _with_supabase_retry(op) -> object:
    """Execute a Supabase operation, reinitialising the client on connection errors.
    Initial attempt + up to 3 retries with backoff [0.5, 1.0, 2.0] seconds.
    """
    global _client
    last_exc: Exception | None = None
    for i in range(len(_SUPABASE_BACKOFF) + 1):
        try:
            sb = await get_client()
            return await op(sb)
        except Exception as exc:
            if not _is_retryable(exc):
                raise
            logger.warning(
                "Supabase connection error (attempt %d/%d): %s",
                i + 1,
                len(_SUPABASE_BACKOFF) + 1,
                exc,
            )
            _client = None
            last_exc = exc
            if i < len(_SUPABASE_BACKOFF):
                await asyncio.sleep(_SUPABASE_BACKOFF[i])
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Profilo cliente
# ---------------------------------------------------------------------------


async def upsert_customer(
    user_id: int,
    first_name: str | None,
    username: str | None,
    client_id: str = "",
) -> None:
    """Crea il profilo se non esiste, aggiorna last_seen."""
    payload = {
        "user_id": user_id,
        "client_id": client_id or config.DEFAULT_CLIENT_ID,
        "first_name": first_name,
        "username": username,
        "last_seen": _now_iso(),
    }
    await _with_supabase_retry(
        lambda sb: sb.table("customers")
        .upsert(payload, on_conflict="user_id", ignore_duplicates=False)
        .execute()
    )
    logger.debug("upserted customer %d in Supabase", user_id)


async def get_customer(user_id: int, client_id: str = "") -> dict | None:
    """Legge il profilo completo da Supabase (source of truth)."""

    async def _query(sb):
        q = (
            sb.table("customers")
            .select("*, conversation_state(conversation_stage, turn_count)")
            .eq("user_id", user_id)
        )
        if client_id:
            q = q.eq("client_id", client_id)
        return await q.maybe_single().execute()

    res = await _with_supabase_retry(_query)
    if res.data is None:
        return None

    profile = dict(res.data)
    # Flatten conversation_state nested object
    cs = profile.pop("conversation_state", None) or {}
    profile["conversation_stage"] = cs.get("conversation_stage", "stage_1_greet")
    profile["turn_count"] = cs.get("turn_count", 0)
    return profile


async def update_stage(user_id: int, stage: str, client_id: str = "") -> None:
    """Aggiorna lo stage della conversazione."""
    _cid = client_id or config.DEFAULT_CLIENT_ID
    await _with_supabase_retry(
        lambda sb: sb.table("conversation_state")
        .upsert(
            {"user_id": user_id, "client_id": _cid, "conversation_stage": stage},
            on_conflict="user_id",
        )
        .execute()
    )

    async def _update_customers(sb):
        q = sb.table("customers").update({"stage": stage}).eq("user_id", user_id)
        if _cid:
            q = q.eq("client_id", _cid)
        return await q.execute()

    await _with_supabase_retry(_update_customers)
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
    await _with_supabase_retry(
        lambda sb: sb.rpc("increment_turn_count", {"p_user_id": user_id}).execute()
    )


async def set_call_booked(
    user_id: int, booked: bool = True, client_id: str = ""
) -> None:
    """Imposta call_booked. Chiamato dal webhook Calendly o dai test."""

    async def _op(sb):
        q = sb.table("customers").update({"call_booked": booked}).eq("user_id", user_id)
        if client_id:
            q = q.eq("client_id", client_id)
        return await q.execute()

    await _with_supabase_retry(_op)
    logger.info("call_booked=%s per user %d", booked, user_id)


async def set_lead_status(user_id: int, status: str, client_id: str = "") -> None:
    """Aggiorna il campo status del cliente su Supabase."""

    async def _op(sb):
        q = sb.table("customers").update({"status": status}).eq("user_id", user_id)
        if client_id:
            q = q.eq("client_id", client_id)
        return await q.execute()

    await _with_supabase_retry(_op)
    logger.info("status=%s per user %d", status, user_id)


async def save_message(
    user_id: int, role: str, content: str, client_id: str = ""
) -> None:
    """Persiste un messaggio su Supabase per la dashboard conversazioni.

    Migration richiesta (una tantum su Supabase):
        CREATE TABLE IF NOT EXISTS messages (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS messages_user_id_idx
            ON messages(user_id, created_at DESC);
    """
    await _with_supabase_retry(
        lambda sb: sb.table("messages")
        .insert(
            {
                "user_id": user_id,
                "client_id": client_id or config.DEFAULT_CLIENT_ID,
                "role": role,
                "content": content,
            }
        )
        .execute()
    )
    logger.debug("saved message role=%s for user %d", role, user_id)


async def set_awaiting_reply(user_id: int, value: bool, client_id: str = "") -> None:
    """Traccia se il bot sta aspettando una risposta dall'utente.

    Migration richiesta (una tantum su Supabase):
        ALTER TABLE customers ADD COLUMN IF NOT EXISTS awaiting_reply BOOLEAN DEFAULT FALSE;
    """

    async def _op(sb):
        q = (
            sb.table("customers")
            .update({"awaiting_reply": value})
            .eq("user_id", user_id)
        )
        if client_id:
            q = q.eq("client_id", client_id)
        return await q.execute()

    await _with_supabase_retry(_op)
    logger.debug("awaiting_reply=%s per user %d", value, user_id)


async def save_user_summary(user_id: int, summary: str, client_id: str = "") -> None:
    """Salva il riassunto AI del profilo utente su Supabase."""

    async def _op(sb):
        q = (
            sb.table("customers")
            .update({"user_summary": summary})
            .eq("user_id", user_id)
        )
        if client_id:
            q = q.eq("client_id", client_id)
        return await q.execute()

    await _with_supabase_retry(_op)
    logger.debug("user_summary aggiornato per user %d", user_id)


async def insert_outbox_message(user_id: int, client_id: str, message: str) -> str:
    """Inserisce un messaggio in outbox. Ritorna l'id del record."""
    import uuid as _uuid

    record_id = str(_uuid.uuid4())
    await _with_supabase_retry(
        lambda sb: sb.table("outbox")
        .insert(
            {
                "id": record_id,
                "user_id": user_id,
                "client_id": client_id,
                "message": message,
            }
        )
        .execute()
    )
    return record_id


async def get_pending_outbox(client_id: str, limit: int = 20) -> list[dict]:
    """Legge messaggi pending per un client."""
    res = await _with_supabase_retry(
        lambda sb: sb.table("outbox")
        .select("*")
        .eq("client_id", client_id)
        .eq("status", "pending")
        .order("created_at")
        .limit(limit)
        .execute()
    )
    return res.data or []


async def mark_outbox_sent(record_id: str) -> None:
    """Marca un messaggio outbox come inviato (solo se ancora pending — previene doppio-send)."""
    await _with_supabase_retry(
        lambda sb: sb.table("outbox")
        .update({"status": "sent", "sent_at": _now_iso()})
        .eq("id", record_id)
        .eq("status", "pending")
        .execute()
    )


async def mark_outbox_failed(record_id: str, error: str) -> None:
    """Marca un messaggio outbox come fallito."""
    await _with_supabase_retry(
        lambda sb: sb.table("outbox")
        .update({"status": "failed", "error_message": error[:500]})
        .eq("id", record_id)
        .execute()
    )


async def set_hot_lead(user_id: int, client_id: str = "") -> None:
    """Marca il lead come hot nel DB (segnale di close rilevato).

    Migration richiesta (una tantum su Supabase):
        ALTER TABLE customers ADD COLUMN IF NOT EXISTS hot_lead BOOLEAN DEFAULT FALSE;
    """

    async def _op(sb):
        q = sb.table("customers").update({"hot_lead": True}).eq("user_id", user_id)
        if client_id:
            q = q.eq("client_id", client_id)
        return await q.execute()

    await _with_supabase_retry(_op)
    logger.info("hot_lead=True impostato per user %d", user_id)
