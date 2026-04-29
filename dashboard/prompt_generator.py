"""Prompt generator for new client onboarding.

Uses OpenAI to generate system_prompt_base and stage_instructions
from a client profile form, with Python template fallback.
"""

from __future__ import annotations

import json
import logging
import os
import re

import openai

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TONE_DESCRIPTIONS: dict[str, str] = {
    "warm": "caldo, empatico, incoraggiante",
    "professional": "professionale, diretto, autorevole",
    "urgent": "urgente, orientato all'azione, con senso di scarsità",
    "empathetic": "empatico, paziente, comprensivo",
}

STAGE_KEYS: list[str] = [
    "stage_1_greet",
    "stage_2_video",
    "stage_3_post_video",
    "stage_4_answering_questions",
    "stage_5_booking",
    "stage_6_verifying",
    "stage_7_rescheduling",
    "stage_8_postbooking",
    "stage_9_uninterested",
    "stage_10_budget_questioning",
]

_META_PROMPT = (
    "Sei un esperto di bot di vendita conversazionale in italiano. "
    "Dato questo profilo cliente, genera un JSON con queste chiavi esatte: "
    "system_prompt_base (string, prompt di sistema completo in italiano), "
    "stage_instructions (object con esattamente queste 10 chiavi: {stages}), "
    "brand_voice (object con tone, phrases_use, phrases_avoid). "
    "Il bot deve qualificare lead e prenotare chiamate. "
    "Output: solo JSON puro, nessun testo fuori."
).format(stages=", ".join(STAGE_KEYS))


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------


async def generate_client_config(form_data: dict) -> dict:
    """Generate system_prompt_base and stage_instructions for a new client.

    Uses OpenAI gpt-4o-mini with fallback to Python template.
    Always returns a valid dict with keys:
    system_prompt_base, stage_instructions, brand_voice.
    """
    try:
        return await _generate_via_openai(form_data)
    except Exception:
        logger.exception("OpenAI generation failed, using template fallback")
        try:
            return _template_fallback(form_data)
        except Exception:
            logger.exception("Template fallback also failed")
            return {
                "system_prompt_base": "",
                "stage_instructions": {k: "" for k in STAGE_KEYS},
                "brand_voice": {"tone": "", "phrases_use": [], "phrases_avoid": []},
            }


async def _generate_via_openai(form_data: dict) -> dict:
    """Call OpenAI and parse/validate the JSON response."""
    client = openai.AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.2,
        max_tokens=3000,
        messages=[
            {"role": "system", "content": _META_PROMPT},
            {"role": "user", "content": json.dumps(form_data, ensure_ascii=False)},
        ],
    )

    content = response.choices[0].message.content or ""
    # Strip markdown code fences if present
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\n?", "", content).rstrip("`").strip()
    result = json.loads(content)

    # Validate all stage keys present
    instructions = result.get("stage_instructions", {})
    missing = [k for k in STAGE_KEYS if k not in instructions]
    if missing:
        logger.warning("Missing stage keys from OpenAI: %s — falling back", missing)
        return _template_fallback(form_data)

    return result


# ---------------------------------------------------------------------------
# Template fallback
# ---------------------------------------------------------------------------


def _template_fallback(form_data: dict) -> dict:
    """Generate config from form data — no external imports, always safe to call."""
    coach_name = form_data.get("coach_name") or "il coach"
    tone_key = form_data.get("tone") or "warm"
    tone_desc = TONE_DESCRIPTIONS.get(tone_key, TONE_DESCRIPTIONS["warm"])
    offer = form_data.get("offer_description") or ""
    target = form_data.get("target_description") or ""
    guardrails = form_data.get("guardrails") or ""
    vsl_url = form_data.get("vsl_url") or "{vsl_link}"
    calendly_url = form_data.get("calendly_url") or "{calendly_link}"
    budget_min = form_data.get("budget_min_euros") or 150

    system_prompt = f"""# RUOLO & PERSONA
Sei {coach_name}, assistente esperta. La tua personalità è {tone_desc}.

# OBIETTIVO PRINCIPALE
Qualificare emotivamente i prospect per una chiamata di consulenza gratuita e senza impegno.

# TARGET
{target or "Persone interessate all'offerta."}

# OFFERTA
{offer or "Programma di affiancamento personalizzato."}

# REGOLE FONDAMENTALI
- Comunica SEMPRE in italiano, tono {tone_desc}.
- Mai messaggi con più di 500 caratteri.
- LINK: includi URL come testo grezzo, mai markdown.
- Budget minimo: €{budget_min}. Sotto questa soglia, rimanda a futuro.
{f"- GUARDRAIL: {guardrails}" if guardrails else ""}"""

    stage_instructions = {
        "stage_1_greet": "Rispondi al messaggio iniziale. Chiedi se conosce già il ruolo o l'offerta.",
        "stage_2_video": f"Invia il link al video: {vsl_url} — aspetta conferma prima di procedere.",
        "stage_3_post_video": "Ri-coinvolgi dopo il video. Proponi la consulenza gratuita.",
        "stage_4_answering_questions": f"Rispondi a domande e obiezioni. Chiudi sempre verso la prenotazione. Budget gate: €{budget_min}.",
        "stage_5_booking": f"Invia il link Calendly: {calendly_url} — chiedi conferma prenotazione. Data odierna: {{today}}, prossimo slot: {{tomorrow}}.",
        "stage_6_verifying": "Verifica se la prenotazione è confermata. Se non ancora: re-invia il link.",
        "stage_7_rescheduling": f"Gestisci rescheduling. Data odierna: {{today}}. Re-invia: {calendly_url}",
        "stage_8_postbooking": "Post-prenotazione: rassicura, rispondi a domande, mantieni entusiasmo.",
        "stage_9_uninterested": "Lead non interessato. Ringrazia e concludi con DISENGAGE.",
        "stage_10_budget_questioning": f"Budget insufficiente dichiarato. Sonda con domande dirette. Gate: €{budget_min}.",
    }

    return {
        "system_prompt_base": system_prompt,
        "stage_instructions": stage_instructions,
        "brand_voice": {
            "tone": tone_key,
            "phrases_use": [],
            "phrases_avoid": [],
        },
    }
