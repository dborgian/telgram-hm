import asyncio
import logging
import random
from telethon.errors import FloodWaitError
import db
import llm
import notifier

logger = logging.getLogger(__name__)

# Number of history turns passed to both classify_stage and generate_reply.
# Keeps context symmetric and bounded.
_HISTORY_CONTEXT = 10


def _split_reply(text: str) -> list[str]:
    """Spezza una risposta lunga in chunk naturali."""
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


async def process_conversation(
    client,
    user_id: int,
    first_name: str | None,
    username: str | None,
    combined_text: str,
) -> None:
    reply: str | None = None
    try:
        await db.upsert_customer(user_id, first_name, username)
        history = await db.get_history(user_id)
        customer = await db.get_customer(user_id)
        ctx_history = history[-_HISTORY_CONTEXT:]
        # Step 1: classifica stage (temp=0.0, veloce)
        stage, assistance_needed = await llm.classify_stage(
            customer, ctx_history, combined_text
        )
        await db.update_stage(user_id, stage)

        # Se serve assistenza umana: logga, notifica e non rispondere
        if assistance_needed:
            logger.warning(
                "assistance_needed=True per user %d — nessuna risposta AI inviata",
                user_id,
            )
            await db.save_turn(user_id, combined_text, "[ASSISTANCE_NEEDED]")
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
            ctx_history, customer, combined_text, stage, user_id
        )

        # DISENGAGE: il modello segnala di chiudere la conversazione — non inviare nulla
        if reply.strip().upper() == "DISENGAGE":
            logger.info("DISENGAGE per user %d — nessuna risposta inviata", user_id)
            await db.save_turn(user_id, combined_text, "[DISENGAGE]")
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

        await db.save_turn(user_id, combined_text, reply)

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
