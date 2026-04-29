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

try:
    from dashboard.prompt_generator import generate_client_config
except ImportError:
    from prompt_generator import generate_client_config

load_dotenv(Path(__file__).parent.parent / ".env")

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
    coach_name: str | None = None
    specialization: str | None = None
    target_description: str | None = None
    tone: str | None = None
    offer_description: str | None = None
    price_display: str | None = None
    budget_min_euros: int | None = None
    guardrails: str | None = None
    vsl_url: str | None = None
    calendly_url: str | None = None
    is_active: bool | None = None


class ClientUpdate(BaseModel):
    name: str | None = None
    coach_name: str | None = None
    specialization: str | None = None
    target_description: str | None = None
    tone: str | None = None
    offer_description: str | None = None
    price_display: str | None = None
    budget_min_euros: int | None = None
    guardrails: str | None = None
    vsl_url: str | None = None
    calendly_url: str | None = None
    system_prompt_base: str | None = None
    stage_instructions: dict | None = None
    brand_voice: dict | None = None
    is_active: bool | None = None


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
        res = await (
            sb.table("customers")
            .select(
                "*, conversation_state(conversation_stage, turn_count, last_reply_at)"
            )
            .order("last_seen", desc=True)
            .execute()
        )
        out = []
        for row in res.data:
            r = dict(row)
            cs = r.pop("conversation_state", None) or {}
            r["conversation_stage"] = cs.get("conversation_stage")
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
            sb.table("customers")
            .select(
                "*, conversation_state(conversation_stage, turn_count, last_reply_at)"
            )
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        if res.data is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        customer = dict(res.data)
        cs = customer.pop("conversation_state", None) or {}
        customer["conversation_stage"] = cs.get("conversation_stage")
        customer["turn_count"] = cs.get("turn_count", 0)
        customer["last_reply_at"] = cs.get("last_reply_at")
        customer["hot_lead"] = (
            bool(customer.get("hot_lead"))
            or bool(customer.get("call_booked"))
            or (customer.get("conversation_stage") in HOT_STAGES)
        )

        transitions = await (
            sb.table("stage_transitions_log")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        return {"customer": customer, "transitions": transitions.data}
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
# Fase 2 — Prompt Generator / Client Management
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:50]


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request, _: str = Depends(require_auth)):
    return templates.TemplateResponse(request=request, name="onboarding.html")


@app.post("/api/clients/preview")
async def preview_client(body: ClientPreview, _: str = Depends(require_auth)):
    try:
        form_data = body.model_dump()
        result = await generate_client_config(form_data)
        return {"system_prompt_base": result.get("system_prompt_base", ""), "ok": True}
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
            .select("client_id, name, slug, is_active, created_at")
            .order("created_at", desc=True)
            .execute()
        )
        return res.data
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
            "coach_name": body.coach_name,
            "specialization": body.specialization,
            "target_description": body.target_description,
            "tone": body.tone,
            "offer_description": body.offer_description,
            "price_display": body.price_display,
            "budget_min_euros": body.budget_min_euros,
            "guardrails": body.guardrails,
            "vsl_base_url": body.vsl_url,
            "calendly_base_url": body.calendly_url,
            "system_prompt_base": generated.get("system_prompt_base", ""),
            "stage_instructions": generated.get("stage_instructions", {}),
            "brand_voice": generated.get("brand_voice", {}),
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
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error") from exc
