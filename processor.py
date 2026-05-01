import asyncio
import logging
import random
import re as _re
from telethon.errors import FloodWaitError
import config
import db
import llm
import notifier

logger = logging.getLogger(__name__)

# Number of history turns passed to both classify_stage and generate_reply.
# Keeps context symmetric and bounded.
_HISTORY_CONTEXT = 10

# Pattern per rilevare segnali di close ad alta probabilità (da setter-bot)
_HOT_SIGNAL_PATTERNS = [
    r"(lascio|smetto|stanca|lasciare|smettere).*(lavoro|azienda|posto)",
    r"(già pagat[oa]|già investit[oa]|corso.*(non ha funzionat|inutile))",
    r"(rateizzare|pagare a rate|piano di pagamento|rate mensil)",
    r"(non vedo l'ora|pront[ao] subito|voglio iniziare subito|iniziare quanto prima)",
]

# Ordered stage progression — used for anti-regression guard.
# Supabase conversation_stage is the single source of truth; the LLM
# is never allowed to move a lead *backwards* except for the CB gate
# (Calendly cancellation forces stage_7_rescheduling when call_booked=False).
_STAGE_ORDER = [
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


def _split_reply(text: str) -> list[str]:
    """Spezza una risposta lunga in chunk naturali."""
    # Non splittare se il testo contiene un URL — preserva il link con il suo contesto
    if "https://" in text or "http://" in text:
        return [text]
    # Prima prova a splittare su paragrafi doppi
    chunks = [c.strip() for c in text.split("\n\n") if c.strip()]
    if len(chunks) == 1 and len(text) > 250:
        # Spezza su frasi se il testo è lungo e non ha paragrafi
        import re

        sentences = re.split(r"(?<=[.!?])\s+", text)
        chunks = []
        current = ""
        for s in sentences:
            if len(current) + len(s) < 200:
                current = (current + " " + s).strip()
            else:
                if current:
                    chunks.append(current)
                current = s
        if current:
            chunks.append(current)
    return chunks if chunks else [text]


SUMMARY_STAGES = {
    "stage_3_post_video",
    "stage_4_answering_questions",
    "stage_5_booking",
    "stage_6_verifying",
    "stage_7_rescheduling",
    "stage_8_postbooking",
    "stage_9_uninterested",
    "stage_10_budget_questioning",
}


async def _generate_and_save_summary(
    user_id: int, history: list, last_user_msg: str, last_reply: str, client_id: str
) -> None:
    """Genera un riassunto di 1-2 righe del profilo utente con OpenAI."""
    try:
        import openai as _openai

        recent = history[-6:] if len(history) >= 6 else history
        messages_text = "\n".join(
            f"{t['role'].upper()}: {t['content']}" for t in recent
        )
        client_oai = _openai.AsyncOpenAI()
        resp = await client_oai.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.3,
            max_tokens=80,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Sei un assistente che riassume brevemente il profilo di un lead. "
                        "Scrivi 1-2 frasi in italiano che descrivono chi è, cosa vuole e il suo stato emotivo/interesse. "
                        "Sii concreto e diretto. Nessun bullet point."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Riassumi questo lead basandoti sulla conversazione:\n{messages_text}\nUltimo messaggio utente: {last_user_msg}",
                },
            ],
        )
        summary = resp.choices[0].message.content.strip()
        if summary:
            await db.save_user_summary(user_id, summary, client_id=client_id)
            logger.info(
                "user_summary aggiornato per user %d: %s", user_id, summary[:60]
            )
    except Exception as e:
        logger.warning("_generate_and_save_summary fallita per user %d: %s", user_id, e)


async def process_conversation(
    client,
    user_id: int,
    first_name: str | None,
    username: str | None,
    combined_text: str,
    client_id: str = "",
) -> None:
    reply: str | None = None
    try:
        effective_client_id = client_id or config.DEFAULT_CLIENT_ID
        cfg = await db.get_client_config(effective_client_id)
        await db.upsert_customer(
            user_id, first_name, username, client_id=effective_client_id
        )
        # Reset awaiting_reply: l'utente ha risposto
        _f = asyncio.ensure_future(
            db.set_awaiting_reply(user_id, False, client_id=effective_client_id)
        )
        _f.add_done_callback(
            lambda f: (
                logger.warning("set_awaiting_reply(False) error: %s", f.exception())
                if f.exception()
                else None
            )
        )
        history = await db.get_history(user_id, client_id=effective_client_id)
        customer = await db.get_customer(user_id, client_id=effective_client_id)
        ctx_history = history[-_HISTORY_CONTEXT:]
        # Step 1: classifica stage (temp=0.0, veloce)
        stage, assistance_needed = await llm.classify_stage(
            customer, ctx_history, combined_text, cfg=cfg
        )

        # Anti-regression guard: Supabase è source of truth per lo stage.
        # Il modello non può retrocedere un lead (es. Redis vuota → stage_1).
        # Unica eccezione: CB gate con call_booked=False forza stage_7 (cancellazione Calendly).
        current_stage = (customer or {}).get("conversation_stage", "stage_1_greet")
        if current_stage in _STAGE_ORDER and stage in _STAGE_ORDER:
            call_booked = bool((customer or {}).get("call_booked", False))
            is_cb_regression = not call_booked and stage == "stage_7_rescheduling"
            if (
                _STAGE_ORDER.index(stage) < _STAGE_ORDER.index(current_stage)
                and not is_cb_regression
            ):
                logger.warning(
                    "Stage regression bloccato: %s → %s (mantengo %s)",
                    current_stage,
                    stage,
                    current_stage,
                )
                stage = current_stage

        await db.update_stage(user_id, stage, client_id=effective_client_id)

        # Hot signal detection: rileva segnali di close ad alta probabilità
        if not assistance_needed and any(
            _re.search(p, combined_text, _re.IGNORECASE) for p in _HOT_SIGNAL_PATTERNS
        ):
            await db.set_hot_lead(user_id, client_id=effective_client_id)
            await notifier.notify_alert(
                client,
                user_id,
                first_name,
                username,
                reason="🔥 HOT LEAD — segnale di close rilevato",
                stage=stage,
                extra=f"Messaggio: {combined_text[:200]}",
            )

        # Se serve assistenza umana: logga, notifica e non rispondere
        if assistance_needed:
            logger.warning(
                "assistance_needed=True per user %d — nessuna risposta AI inviata",
                user_id,
            )
            await db.save_turn(
                user_id,
                combined_text,
                "[ASSISTANCE_NEEDED]",
                client_id=effective_client_id,
            )
            await notifier.notify_alert(
                client,
                user_id,
                first_name,
                username,
                reason="ASSISTENZA UMANA RICHIESTA",
                stage=stage,
                extra=f"Messaggio: {combined_text[:200]}",
            )
            return

        # Step 2: genera risposta (temp=0.8, qualità)
        reply = await llm.generate_reply(
            ctx_history, customer, combined_text, stage, user_id, cfg=cfg
        )

        # DISENGAGE: il modello segnala di chiudere la conversazione — non inviare nulla
        if reply.strip().upper() == "DISENGAGE":
            logger.info("DISENGAGE per user %d — nessuna risposta inviata", user_id)
            await db.save_turn(
                user_id, combined_text, "[DISENGAGE]", client_id=effective_client_id
            )
            await db.set_lead_status(user_id, "LL")
            await notifier.notify_alert(
                client,
                user_id,
                first_name,
                username,
                reason="LEAD CHIUSA (Lost Lead)",
                stage=stage,
            )
            return

        await db.save_turn(user_id, combined_text, reply, client_id=effective_client_id)

        # Genera/aggiorna user_summary ogni 3 turni, dallo stage_3 in poi
        turn_count = (customer or {}).get("turn_count", 0)
        if stage in SUMMARY_STAGES and (turn_count + 1) % 3 == 0:
            asyncio.ensure_future(
                _generate_and_save_summary(
                    user_id, ctx_history, combined_text, reply, effective_client_id
                )
            )

        # stage_9: attendi 45s prima di inviare (replica n8n Wait node)
        if stage == "stage_9_uninterested":
            logger.info("stage_9: attesa 45s prima di inviare a user %d", user_id)
            await asyncio.sleep(45)

        chunks = _split_reply(reply)
        for chunk in chunks:
            delay = max(1.5, min(len(chunk) / 40.0, 4.0)) + random.uniform(-0.3, 0.5)
            async with client.action(user_id, "typing"):
                await asyncio.sleep(delay)
            await client.send_message(user_id, chunk)
        # Traccia che il bot ha inviato e sta aspettando risposta
        _g = asyncio.ensure_future(
            db.set_awaiting_reply(user_id, True, client_id=effective_client_id)
        )
        _g.add_done_callback(
            lambda f: (
                logger.warning("set_awaiting_reply(True) error: %s", f.exception())
                if f.exception()
                else None
            )
        )

    except FloodWaitError as e:
        logger.warning(
            "FloodWaitError: devo aspettare %ds prima di rispondere a %d",
            e.seconds,
            user_id,
        )
        await asyncio.sleep(e.seconds)
        if reply:
            try:
                await client.send_message(user_id, reply)
            except FloodWaitError as e2:
                logger.error(
                    "FloodWait ripetuto per user %d (%ds) — messaggio perso",
                    user_id,
                    e2.seconds,
                )
            except Exception as e2:
                logger.error(
                    "Errore invio dopo FloodWait per user %d: %s",
                    user_id,
                    e2,
                    exc_info=True,
                )
    except Exception as e:
        logger.error(
            "Errore in process_conversation per user %d: %s", user_id, e, exc_info=True
        )
