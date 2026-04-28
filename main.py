import asyncio
import hashlib
import hmac
import json
import logging
from aiohttp import web

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

import config
import db
import llm
from db import init_db
from processor import process_conversation

# ---------------------------------------------------------------------------
# Calendly webhook server
# ---------------------------------------------------------------------------


async def _handle_calendly_webhook(request: web.Request) -> web.Response:
    """POST /webhook/calendly — aggiorna call_booked quando Calendly conferma una prenotazione."""
    raw_body = await request.read()

    # --- HMAC-SHA256 validation (FIX C-1) ---
    if config.CALENDLY_WEBHOOK_SECRET:
        sig_header = request.headers.get("Calendly-Webhook-Signature", "")
        try:
            parts = dict(part.split("=", 1) for part in sig_header.split(","))
            timestamp = parts["t"]
            received_sig = parts["v1"]
        except (KeyError, ValueError):
            logger.warning("Calendly webhook: header firma mancante o malformato")
            return web.Response(status=401, text="firma non valida")
        signing_payload = f"{timestamp}.".encode() + raw_body
        expected_sig = hmac.new(
            config.CALENDLY_WEBHOOK_SECRET.encode(),
            signing_payload,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_sig, received_sig):
            logger.warning("Calendly webhook: firma HMAC non valida")
            return web.Response(status=401, text="firma non valida")
    else:
        logger.warning(
            "CALENDLY_WEBHOOK_SECRET non configurato — validazione firma saltata"
        )

    try:
        payload = json.loads(raw_body)
    except Exception:
        return web.Response(status=400, text="invalid json")

    # Estrai user_id da utm_source (come da Call booking.json di n8n)
    try:
        utm_source = payload["payload"]["tracking"]["utm_source"]
        user_id = int(utm_source)
    except (KeyError, TypeError, ValueError):
        event_type = (
            payload.get("event", "unknown") if isinstance(payload, dict) else "unknown"
        )
        logger.warning(
            "Calendly webhook: utm_source mancante o non valido (event=%s)", event_type
        )
        return web.Response(status=400, text="utm_source mancante")

    event_type = payload.get("event", "")
    logger.info("Calendly webhook: event=%s user_id=%d", event_type, user_id)

    if event_type == "invitee.created":
        await db.set_call_booked(user_id, True)
        logger.info(
            "call_booked=True impostato per user %d via Calendly webhook", user_id
        )
    elif event_type == "invitee.canceled":
        await db.set_call_booked(user_id, False)
        logger.info(
            "call_booked=False impostato per user %d (cancellazione Calendly)", user_id
        )

    return web.Response(status=200, text="ok")


async def _start_webhook_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_post("/webhook/calendly", _handle_calendly_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.WEBHOOK_PORT)
    await site.start()
    logger.info("Webhook server avviato su porta %d", config.WEBHOOK_PORT)
    return runner


logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# user_id -> asyncio.Queue of (text, first_name, username)
_queues: dict[int, asyncio.Queue] = {}
# user_id -> running worker Task
_workers: dict[int, asyncio.Task] = {}
# dedup: set of message IDs already enqueued (max 500 entries)
_seen_msg_ids: set[int] = set()


async def _user_worker(
    client: TelegramClient,
    user_id: int,
    queue: asyncio.Queue,
) -> None:
    """Buffer messages per user, flush on timeout or MAX_MESSAGES."""
    buffer: list[str] = []
    first_name: str | None = None
    username: str | None = None

    while True:
        try:
            text, first_name, username = await asyncio.wait_for(
                queue.get(), timeout=config.BUFFER_DELAY
            )
            buffer.append(text)
            if len(buffer) >= config.MAX_MESSAGES:
                logger.debug("user %d hit MAX_MESSAGES, flushing", user_id)
                combined = "\n".join(buffer)
                buffer.clear()
                await process_conversation(
                    client, user_id, first_name, username, combined
                )
        except asyncio.TimeoutError:
            if buffer:
                logger.debug(
                    "user %d buffer timeout, flushing %d msgs", user_id, len(buffer)
                )
                combined = "\n".join(buffer)
                buffer.clear()
                await process_conversation(
                    client, user_id, first_name, username, combined
                )
        except FloodWaitError as e:
            logger.warning(
                "FloodWaitError for user %d: sleeping %ds", user_id, e.seconds
            )
            await asyncio.sleep(e.seconds)
        except Exception:
            logger.exception("Unhandled error in worker for user %d", user_id)


async def main() -> None:
    await init_db()
    logger.info("Database initialised")

    client = TelegramClient(
        StringSession(config.SESSION_STRING),
        config.API_ID,
        config.API_HASH,
        flood_sleep_threshold=0,
    )

    @client.on(events.NewMessage(incoming=True))
    async def handle_new_message(event: events.NewMessage.Event) -> None:
        if not event.is_private:
            return

        sender_id: int = event.sender_id
        text: str | None = None

        if event.message.text:
            text = event.message.text
        elif event.message.voice or event.message.audio:
            media = event.message.voice or event.message.audio
            try:
                file_bytes = await client.download_media(media, bytes)
                transcribed = await llm.transcribe_audio(file_bytes)
                text = f"[Vocale]: {transcribed}"
            except Exception:
                logger.exception("Errore trascrizione audio per user %d", sender_id)
                return
        elif event.message.photo:
            try:
                file_bytes = await client.download_media(event.message.photo, bytes)
                caption = event.message.text or ""
                description = await llm.describe_image(file_bytes, caption)
                text = f"[Immagine]: {description}"
            except Exception:
                logger.exception("Errore descrizione immagine per user %d", sender_id)
                return

        if not text or not text.strip():
            return

        if config.TEST_MODE_ENABLED and sender_id not in config.TEST_USERS:
            return

        msg_id: int = event.message.id
        if msg_id in _seen_msg_ids:
            logger.debug(
                "Duplicate event for msg_id=%d user=%d — skipped", msg_id, sender_id
            )
            return
        _seen_msg_ids.add(msg_id)
        if len(_seen_msg_ids) > 500:
            _seen_msg_ids.clear()

        sender = await event.get_sender()
        first_name: str | None = getattr(sender, "first_name", None)
        username: str | None = getattr(sender, "username", None)

        if sender_id not in _queues:
            q: asyncio.Queue = asyncio.Queue()
            _queues[sender_id] = q
            task = asyncio.create_task(
                _user_worker(client, sender_id, q),
                name=f"worker-{sender_id}",
            )
            _workers[sender_id] = task
            logger.debug("Spawned worker for user %d", sender_id)

        await _queues[sender_id].put((text, first_name, username))
        logger.info(
            "Queued message from user %d (%d in buffer)",
            sender_id,
            _queues[sender_id].qsize(),
        )

    webhook_runner = await _start_webhook_server()

    await client.start()
    logger.info("Userbot connected and listening")
    try:
        await client.run_until_disconnected()
    finally:
        await webhook_runner.cleanup()
        logger.info("Webhook server fermato")
        logger.info("Shutting down — cancelling %d worker task(s)", len(_workers))
        for task in _workers.values():
            task.cancel()
        if _workers:
            await asyncio.gather(*_workers.values(), return_exceptions=True)
        logger.info("All worker tasks cancelled")


if __name__ == "__main__":
    asyncio.run(main())
