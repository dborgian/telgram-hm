import asyncio
import logging
import random
from telethon.errors import FloodWaitError
import db
import llm

logger = logging.getLogger(__name__)


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
        stage = await llm.detect_stage(customer, history[-6:], combined_text)
        await db.update_stage(user_id, stage)
        reply = await llm.generate_reply(
            history, customer, combined_text, stage, user_id
        )
        await db.save_turn(user_id, combined_text, reply)

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
            await client.send_message(user_id, reply)
    except Exception as e:
        logger.error(
            "Errore in process_conversation per user %d: %s", user_id, e, exc_info=True
        )
