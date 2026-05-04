"""FastAPI dashboard backend for Telegram sales bot analytics."""

import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request
from supabase import AsyncClient, acreate_client
from telethon import TelegramClient as _TelegramClient
from telethon.errors import (
    SessionPasswordNeededError as _SessionPasswordNeededError,
    FloodWaitError as _FloodWaitError,
)
from telethon.sessions import StringSession as _StringSession

try:
    from dashboard.prompt_generator import generate_client_config
except ImportError:
    from prompt_generator import generate_client_config

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SUPABASE_URL: str = os.environ["SUPABASE_URL"]
SUPABASE_KEY: str = os.environ["SUPABASE_KEY"]
DASHBOARD_USER: str = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD: str = os.environ["DASHBOARD_PASSWORD"]

HOT_STAGES = {
    "stage_5_booking",
    "stage_6_verifying",
    "stage_7_rescheduling",
    "stage_8_postbooking",
}

security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    ok_user = secrets.compare_digest(
        credentials.username.encode(), DASHBOARD_USER.encode()
    )
    ok_pass = secrets.compare_digest(
        credentials.password.encode(), DASHBOARD_PASSWORD.encode()
    )
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Credenziali non valide",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# slug -> (TelegramClient, phone_code_hash, phone)
_pending_sessions: dict[str, tuple] = {}

_client: AsyncClient | None = None


async def get_client() -> AsyncClient:
    global _client
    if _client is None:
        _client = await acreate_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_client()
    yield


app = FastAPI(title="HM Dashboard", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:8050",
        "http://127.0.0.1:3000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class NotesUpdate(BaseModel):
    notes: str


class StatusUpdate(BaseModel):
    status: str


class ClientCreate(BaseModel):
    name: str
    slug: str = ""
    coach_name: str = ""
    specialization: str = ""
    target_description: str = ""
    tone: str = "warm"
    offer_description: str = ""
    price_display: str = ""
    budget_min_euros: int = 0
    guardrails: str = ""
    vsl_url: str = ""
    calendly_url: str = ""


class ClientPreview(ClientCreate):
    pass


class ClientUpdate(BaseModel):
    name: str | None = None
    vsl_url: str | None = None
    calendly_url: str | None = None
    system_prompt_base: str | None = None
    stage_instructions: dict | None = None
    brand_voice: dict | None = None
    icp_rules: dict | None = None
    is_active: bool | None = None
    session_string: str | None = None


class GenSessionStartRequest(BaseModel):
    phone: str  # es. "+39 333 123 456"


class GenSessionConfirmRequest(BaseModel):
    code: str  # es. "12345"
    password: str = ""  # 2FA opzionale


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, _: str = Depends(require_auth)) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="index.html")


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@app.get("/api/customers")
async def list_customers(_: str = Depends(require_auth)):
    # NOTE: la colonna `user_summary` deve essere aggiunta alla tabella `customers` in Supabase:
    # ALTER TABLE customers ADD COLUMN IF NOT EXISTS user_summary text;
    try:
        sb = await get_client()
        res = (
            await sb.table("customers")
            .select("*")
            .order("last_seen", desc=True)
            .execute()
        )
        rows = res.data or []

        # Separate query for conversation_state — avoids PostgREST FK join issues
        if rows:
            cs_res = await (
                sb.table("conversation_state")
                .select(
                    "client_id, user_id, conversation_stage, turn_count, last_reply_at"
                )
                .execute()
            )
            cs_map = {(r["client_id"], r["user_id"]): r for r in (cs_res.data or [])}
        else:
            cs_map = {}

        out = []
        for row in rows:
            r = dict(row)
            cs = cs_map.get((r.get("client_id"), r.get("user_id")), {})
            r["conversation_stage"] = cs.get("conversation_stage") or r.get("stage")
            r["turn_count"] = cs.get("turn_count", 0)
            r["last_reply_at"] = cs.get("last_reply_at")
            r["hot_lead"] = (
                bool(r.get("hot_lead"))
                or bool(r.get("call_booked"))
                or (r.get("conversation_stage") in HOT_STAGES)
            )
            r["user_summary"] = r.get("user_summary") or ""
            out.append(r)
        return out
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@app.get("/api/customers/{user_id}")
async def get_customer(user_id: int, _: str = Depends(require_auth)):
    try:
        sb = await get_client()
        res = await (
            sb.table("customers").select("*").eq("user_id", user_id).limit(1).execute()
        )
        if not res.data:
            raise HTTPException(status_code=404, detail="Customer not found")
        customer = dict(res.data[0])
        cs_res = await (
            sb.table("conversation_state")
            .select("conversation_stage, turn_count, last_reply_at")
            .eq("user_id", user_id)
            .eq("client_id", customer.get("client_id", ""))
            .limit(1)
            .execute()
        )
        cs = cs_res.data[0] if cs_res.data else {}
        customer["conversation_stage"] = cs.get("conversation_stage") or customer.get(
            "stage"
        )
        customer["turn_count"] = cs.get("turn_count", 0)
        customer["last_reply_at"] = cs.get("last_reply_at")
        customer["hot_lead"] = (
            bool(customer.get("hot_lead"))
            or bool(customer.get("call_booked"))
            or (customer.get("conversation_stage") in HOT_STAGES)
        )
        customer["user_summary"] = customer.get("user_summary") or ""

        try:
            transitions = await (
                sb.table("stage_transitions_log")
                .select("*")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(20)
                .execute()
            )
            transitions_data = transitions.data
        except Exception:
            transitions_data = []
        return {"customer": customer, "transitions": transitions_data}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@app.get("/api/funnel")
async def funnel(_: str = Depends(require_auth)):
    try:
        sb = await get_client()
        cs_res = (
            await sb.table("conversation_state").select("conversation_stage").execute()
        )
        stage_counts: dict[str, int] = {}
        for row in cs_res.data:
            stage = row.get("conversation_stage") or "unknown"
            stage_counts[stage] = stage_counts.get(stage, 0) + 1

        cust_res = await sb.table("customers").select("call_booked, status").execute()
        call_booked_count = sum(1 for r in cust_res.data if r.get("call_booked"))
        status_counts: dict[str, int] = {}
        for r in cust_res.data:
            s = r.get("status") or "unknown"
            status_counts[s] = status_counts.get(s, 0) + 1

        return {
            "stage_counts": stage_counts,
            "call_booked_count": call_booked_count,
            "status_counts": status_counts,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@app.get("/api/analytics/conversations")
async def analytics_conversations(_: str = Depends(require_auth)):
    """Conversations active per day (last 30 days) based on customers.last_seen."""
    try:
        sb = await get_client()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        res = await (
            sb.table("customers")
            .select("last_seen, user_summary")
            .gte("last_seen", cutoff)
            .execute()
        )
        counts: dict[str, int] = {}
        qualified_counts: dict[str, int] = {}
        for row in res.data:
            fs = row.get("last_seen")
            if not fs:
                continue
            day = fs[:10]  # "YYYY-MM-DD"
            counts[day] = counts.get(day, 0) + 1
            if row.get("user_summary"):
                qualified_counts[day] = qualified_counts.get(day, 0) + 1
        # Fill missing days with 0 for last 30 days
        today = date.today()
        result = []
        for i in range(30, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            result.append(
                {
                    "date": d,
                    "count": counts.get(d, 0),
                    "qualified_count": qualified_counts.get(d, 0),
                }
            )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@app.get("/api/alerts")
async def alerts(_: str = Depends(require_auth)):
    try:
        sb = await get_client()
        assist_data: list = []
        ll_data: list = []
        try:
            assist_res = await (
                sb.table("stage_transitions_log")
                .select("*")
                .eq("assistance_needed", True)
                .order("created_at", desc=True)
                .limit(50)
                .execute()
            )
            assist_data = assist_res.data or []
        except Exception:
            pass
        try:
            ll_res = await (
                sb.table("customers")
                .select("user_id, first_name, username, last_seen")
                .eq("status", "LL")
                .execute()
            )
            ll_data = ll_res.data or []
        except Exception:
            pass
        return {"assistance_needed": assist_data, "lost_leads": ll_data}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@app.get("/api/metrics")
async def metrics(_: str = Depends(require_auth)):
    try:
        sb = await get_client()
        try:
            res = await (
                sb.table("stage_metrics")
                .select("*, stages(stage_key)")
                .order("period_start", desc=True)
                .execute()
            )
            out = []
            for row in res.data or []:
                r = dict(row)
                stages_rel = r.pop("stages", None) or {}
                r["stage_key"] = stages_rel.get("stage_key")
                out.append(r)
            return out
        except Exception:
            return []
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@app.get("/api/stage-suggestions")
async def stage_suggestions(_: str = Depends(require_auth)):
    try:
        sb = await get_client()
        try:
            res = await (
                sb.table("stage_suggestions")
                .select("*")
                .order("created_at", desc=True)
                .limit(20)
                .execute()
            )
            return res.data or []
        except Exception:
            return []
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@app.get("/api/customers/{user_id}/messages")
async def get_messages(user_id: int, _: str = Depends(require_auth)):
    try:
        sb = await get_client()
        res = await (
            sb.table("messages")
            .select("id, role, content, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=False)
            .limit(200)
            .execute()
        )
        return res.data
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@app.patch("/api/customers/{user_id}/notes")
async def update_notes(user_id: int, body: NotesUpdate, _: str = Depends(require_auth)):
    try:
        sb = await get_client()
        await (
            sb.table("customers")
            .update({"notes": body.notes})
            .eq("user_id", user_id)
            .execute()
        )
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error") from exc


class SendMessageRequest(BaseModel):
    message: str
    client_id: str = ""


@app.post("/api/customers/{user_id}/send_message")
async def send_message_to_user(
    user_id: int, body: SendMessageRequest, _: str = Depends(require_auth)
):
    """Invia un messaggio manuale a un utente Telegram via outbox Supabase."""
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Messaggio vuoto")
    if len(body.message) > 4096:
        raise HTTPException(
            status_code=400, detail="Messaggio troppo lungo (max 4096 char)"
        )
    try:
        sb = await get_client()

        # Resolve client_id from customers table if not provided
        client_id = body.client_id
        if not client_id:
            res = await (
                sb.table("customers")
                .select("client_id")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            client_id = (
                res.data[0].get("client_id") if res.data else None
            ) or os.getenv("DEFAULT_CLIENT_ID", "00000000-0000-0000-0000-000000000001")

        # Insert directly via Supabase — avoids importing store.py (not available in dashboard container)
        record_id = str(uuid.uuid4())
        await sb.table("outbox").insert(
            {
                "id": record_id,
                "user_id": user_id,
                "client_id": client_id,
                "message": body.message.strip(),
            }
        ).execute()
        return {"ok": True, "record_id": record_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Errore invio messaggio: {str(exc)[:300]}"
        ) from exc


@app.patch("/api/customers/{user_id}/status")
async def update_status(
    user_id: int, body: StatusUpdate, _: str = Depends(require_auth)
):
    try:
        sb = await get_client()
        await (
            sb.table("customers")
            .update({"status": body.status})
            .eq("user_id", user_id)
            .execute()
        )
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error") from exc


# ---------------------------------------------------------------------------
# OTP Wizard — genera SESSION_STRING Telegram dalla dashboard
# ---------------------------------------------------------------------------


@app.post("/api/clients/{slug}/gen-session/start")
async def gen_session_start(
    slug: str, body: GenSessionStartRequest, _: str = Depends(require_auth)
):
    """Avvia il flusso OTP: invia il codice al numero di telefono."""
    sb = await get_client()
    res = await (
        sb.table("client_config")
        .select("client_id")
        .eq("slug", slug)
        .maybe_single()
        .execute()
    )
    if res.data is None:
        raise HTTPException(status_code=404, detail="Client not found")

    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]

    client = _TelegramClient(_StringSession(), api_id, api_hash)
    try:
        await client.connect()
        result = await client.send_code_request(body.phone)
        # 4-tuple: (client, phone_code_hash, phone, awaiting_2fa)
        _pending_sessions[slug] = (client, result.phone_code_hash, body.phone, False)
        return {"ok": True}
    except _FloodWaitError as e:
        await client.disconnect()
        raise HTTPException(
            status_code=429, detail=f"Troppi tentativi, riprova tra {e.seconds}s"
        ) from e
    except Exception as e:
        await client.disconnect()
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/clients/{slug}/gen-session/confirm")
async def gen_session_confirm(
    slug: str, body: GenSessionConfirmRequest, _: str = Depends(require_auth)
):
    """Completa il flusso OTP: verifica il codice e salva la session_string."""
    if slug not in _pending_sessions:
        raise HTTPException(status_code=400, detail="Nessuna sessione in attesa")

    client, phone_code_hash, phone, awaiting_2fa = _pending_sessions[slug]
    try:
        if awaiting_2fa:
            # OTP già accettato da Telegram, serve solo la password 2FA
            if not body.password:
                raise HTTPException(status_code=400, detail="Password 2FA richiesta")
            await client.sign_in(password=body.password)
        else:
            await client.sign_in(phone, body.code, phone_code_hash=phone_code_hash)
    except _SessionPasswordNeededError:
        if not body.password:
            # OTP corretto, 2FA richiesta — mantieni sessione in attesa, segnala al frontend
            _pending_sessions[slug] = (client, phone_code_hash, phone, True)
            return {"ok": False, "needs_2fa": True}
        try:
            await client.sign_in(password=body.password)
        except Exception as e:
            await client.disconnect()
            del _pending_sessions[slug]
            raise HTTPException(status_code=400, detail=str(e)) from e
    except _FloodWaitError as e:
        await client.disconnect()
        del _pending_sessions[slug]
        raise HTTPException(
            status_code=429, detail=f"Troppi tentativi, riprova tra {e.seconds}s"
        ) from e
    except Exception as e:
        await client.disconnect()
        del _pending_sessions[slug]
        raise HTTPException(status_code=400, detail=str(e)) from e

    session_string = client.session.save()
    await client.disconnect()
    del _pending_sessions[slug]

    sb = await get_client()
    await (
        sb.table("client_config")
        .update({"session_string": session_string})
        .eq("slug", slug)
        .execute()
    )
    return {
        "ok": True,
        "message": "Sessione creata. Il bot sarà attivo entro 60 secondi.",
    }


@app.post("/api/clients/{slug}/gen-session/cancel")
async def gen_session_cancel(slug: str, _: str = Depends(require_auth)):
    """Annulla il flusso OTP in corso."""
    if slug in _pending_sessions:
        client, _, __, ___ = _pending_sessions.pop(slug)
        try:
            await client.disconnect()
        except Exception:
            pass
    return {"ok": True}


@app.post("/api/clients/{slug}/gen-session/disconnect")
async def gen_session_disconnect(slug: str, _: str = Depends(require_auth)):
    """Rimuove la session_string dal DB — il bot si disconnette entro 60s."""
    sb = await get_client()
    res = await (
        sb.table("client_config")
        .select("client_id")
        .eq("slug", slug)
        .maybe_single()
        .execute()
    )
    if res.data is None:
        raise HTTPException(status_code=404, detail="Client not found")
    client_id = res.data["client_id"]
    await (
        sb.table("client_config")
        .update({"session_string": None})
        .eq("slug", slug)
        .execute()
    )
    # Invalida cache Redis config:{client_id}
    _redis_url = os.getenv("UPSTASH_REDIS_URL", "")
    if _redis_url:
        try:
            from upstash_redis.asyncio import Redis as _Redis

            _r = _Redis.from_url(_redis_url)
            await _r.delete(f"config:{client_id}")
        except Exception:
            pass
    return {"ok": True}


# ---------------------------------------------------------------------------
# Fase 2 — Prompt Generator / Client Management
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", "_", text)
    return text[:50]


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request, _: str = Depends(require_auth)):
    return templates.TemplateResponse(request=request, name="onboarding.html")


@app.post("/api/clients/preview")
async def preview_client(body: ClientPreview, _: str = Depends(require_auth)):
    try:
        form_data = body.model_dump()
        result = await generate_client_config(form_data)
        return {
            "system_prompt_base": result.get("system_prompt_base", ""),
            "stage_instructions": result.get("stage_instructions", {}),
            "ok": True,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@app.get("/api/clients")
async def list_clients(_: str = Depends(require_auth)):
    try:
        sb = await get_client()
        res = await (
            sb.table("client_config")
            .select("client_id, name, slug, is_active, created_at, session_string")
            .order("created_at", desc=True)
            .execute()
        )
        return [
            {
                **{k: v for k, v in row.items() if k != "session_string"},
                "has_session": bool(row.get("session_string")),
            }
            for row in res.data
        ]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@app.post("/api/clients", status_code=201)
async def create_client(body: ClientCreate, _: str = Depends(require_auth)):
    try:
        slug = body.slug or _slugify(body.name)

        form_data = body.model_dump()
        generated = await generate_client_config(form_data)

        client_id = str(uuid.uuid4())
        row = {
            "client_id": client_id,
            "name": body.name,
            "slug": slug,
            "vsl_url": body.vsl_url,
            "calendly_url": body.calendly_url,
            "guardrails": {"text": body.guardrails} if body.guardrails else {},
            "system_prompt_base": generated.get("system_prompt_base", ""),
            "stage_instructions": generated.get("stage_instructions", {}),
            "brand_voice": generated.get("brand_voice", {}),
            "icp_rules": {
                "coach_name": body.coach_name,
                "specialization": body.specialization,
                "target_description": body.target_description,
                "tone": body.tone,
                "offer_description": body.offer_description,
                "price_display": body.price_display,
                "budget_min_euros": body.budget_min_euros,
            },
            "is_active": True,
        }

        sb = await get_client()
        try:
            await sb.table("client_config").insert(row).execute()
        except Exception as e:
            if "duplicate" in str(e).lower() or "unique" in str(e).lower():
                raise HTTPException(status_code=409, detail="slug già esistente") from e
            raise

        return {"client_id": client_id, "slug": slug, "ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@app.patch("/api/clients/{slug}")
async def update_client(slug: str, body: ClientUpdate, _: str = Depends(require_auth)):
    try:
        updates = body.model_dump(exclude_unset=True)
        if not updates:
            return {"ok": True}
        sb = await get_client()
        res = await sb.table("client_config").update(updates).eq("slug", slug).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Client not found")
        # Invalida cache Redis config:{client_id} così il bot usa subito il nuovo prompt
        try:
            from upstash_redis.asyncio import Redis as _Redis

            _redis_url = os.getenv("UPSTASH_REDIS_URL", "")
            if _redis_url:
                _r = _Redis.from_url(_redis_url)
                client_id = res.data[0].get("client_id", "")
                if client_id:
                    await _r.delete(f"config:{client_id}")
        except Exception:
            pass  # cache invalidation best-effort
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error") from exc


# ---------------------------------------------------------------------------
# Fase 3 — AI Prompt Editor
# ---------------------------------------------------------------------------


class AISuggestRequest(BaseModel):
    field: str
    instruction: str
    current_value: str


@app.get("/clients/{slug}/prompt-editor", response_class=HTMLResponse)
async def prompt_editor_page(
    request: Request, slug: str, _: str = Depends(require_auth)
):
    return templates.TemplateResponse(
        request=request, name="prompt_editor.html", context={"slug": slug}
    )


@app.get("/api/clients/{slug}/config")
async def get_client_config_detail(slug: str, _: str = Depends(require_auth)):
    """Restituisce system_prompt_base, stage_instructions e brand_voice per l'editor."""
    try:
        sb = await get_client()
        res = await (
            sb.table("client_config")
            .select(
                "client_id, name, slug, system_prompt_base, stage_instructions, brand_voice, icp_rules"
            )
            .eq("slug", slug)
            .maybe_single()
            .execute()
        )
        if res.data is None:
            raise HTTPException(status_code=404, detail="Client not found")
        return res.data
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@app.post("/api/clients/{slug}/ai-suggest")
async def ai_suggest(slug: str, body: AISuggestRequest, _: str = Depends(require_auth)):
    """Usa Claude per migliorare un campo del prompt secondo l'istruzione dell'utente."""
    import anthropic as _anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY non configurata")
    if not body.instruction.strip():
        raise HTTPException(status_code=400, detail="Istruzione vuota")
    if len(body.current_value) > 20000:
        raise HTTPException(status_code=400, detail="Testo troppo lungo")

    try:
        # Load brand_voice for context so Claude knows the tone-of-voice
        brand_voice_ctx = ""
        try:
            sb = await get_client()
            cfg_res = await (
                sb.table("client_config")
                .select("brand_voice")
                .eq("slug", slug)
                .maybe_single()
                .execute()
            )
            bv = (cfg_res.data or {}).get("brand_voice") or {}
            if bv:
                tone = bv.get("tone", "")
                phrases_use = ", ".join(bv.get("phrases_use") or [])
                phrases_avoid = ", ".join(bv.get("phrases_avoid") or [])
                parts = []
                if tone:
                    parts.append(f"Tone: {tone}")
                if phrases_use:
                    parts.append(f"Frasi da usare: {phrases_use}")
                if phrases_avoid:
                    parts.append(f"Frasi da evitare: {phrases_avoid}")
                if parts:
                    brand_voice_ctx = (
                        "BRAND VOICE DEL CLIENTE:\n" + "\n".join(parts) + "\n\n"
                    )
        except Exception:
            pass  # brand_voice context is best-effort

        aclient = _anthropic.AsyncAnthropic(api_key=api_key)
        field_label = body.field.replace("stage_instructions.", "Stage: ").replace(
            "system_prompt_base", "System Prompt"
        )

        message = await aclient.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Sei un esperto di copywriting per sales bot AI in italiano.\n\n"
                        f"{brand_voice_ctx}"
                        f"Campo da modificare: {field_label}\n\n"
                        f"TESTO ATTUALE:\n{body.current_value}\n\n"
                        f"ISTRUZIONE DI MODIFICA: {body.instruction}\n\n"
                        f"Riscrivi il testo applicando l'istruzione. "
                        f"Mantieni la stessa struttura e lunghezza approssimativa. "
                        f"Rispondi SOLO con il testo modificato, senza spiegazioni o prefissi."
                    ),
                }
            ],
        )
        suggested = message.content[0].text.strip()
        return {"suggested": suggested, "ok": True}
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Errore AI: {str(exc)[:200]}"
        ) from exc
