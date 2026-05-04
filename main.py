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
from models import ClientConfig
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
        await db.update_stage(user_id, "stage_7_rescheduling")
        logger.info(
            "call_booked=False + stage_7_rescheduling per user %d (cancellazione Calendly)",
            user_id,
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

# ---------------------------------------------------------------------------
# Per-client state
# ---------------------------------------------------------------------------
_clients: dict[str, TelegramClient] = {}  # client_id -> TelegramClient
_queues: dict[str, dict[int, asyncio.Queue]] = {}  # client_id -> {user_id -> Queue}
_workers: dict[str, dict[int, asyncio.Task]] = {}  # client_id -> {user_id -> Task}
_seen_msg_ids: set[int] = set()  # global dedup


async def _user_worker(
    client: TelegramClient,
    user_id: int,
    queue: asyncio.Queue,
    client_id: str,
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
                    client,
                    user_id,
                    first_name,
                    username,
                    combined,
                    client_id=client_id,
                )
        except asyncio.TimeoutError:
            if buffer:
                logger.debug(
                    "user %d buffer timeout, flushing %d msgs", user_id, len(buffer)
                )
                combined = "\n".join(buffer)
                buffer.clear()
                await process_conversation(
                    client,
                    user_id,
                    first_name,
                    username,
                    combined,
                    client_id=client_id,
                )
        except FloodWaitError as e:
            logger.warning(
                "FloodWaitError for user %d: sleeping %ds", user_id, e.seconds
            )
            await asyncio.sleep(e.seconds)
        except Exception:
            logger.exception("Unhandled error in worker for user %d", user_id)


def _make_handler(tg_client: TelegramClient, client_id: str):
    """Returns a NewMessage handler closure bound to a specific client."""

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
                file_bytes = await tg_client.download_media(media, bytes)
                transcribed = await llm.transcribe_audio(file_bytes)
                text = f"[Vocale]: {transcribed}"
            except Exception:
                logger.exception("Errore trascrizione audio per user %d", sender_id)
                return
        elif event.message.photo:
            try:
                file_bytes = await tg_client.download_media(event.message.photo, bytes)
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

        client_queues = _queues[client_id]
        client_workers = _workers[client_id]

        if sender_id not in client_queues:
            q: asyncio.Queue = asyncio.Queue()
            client_queues[sender_id] = q
            task = asyncio.create_task(
                _user_worker(tg_client, sender_id, q, client_id),
                name=f"worker-{client_id[:8]}-{sender_id}",
            )
            client_workers[sender_id] = task
            logger.debug(
                "Spawned worker for user %d (client %s)", sender_id, client_id[:8]
            )

        await client_queues[sender_id].put((text, first_name, username))
        logger.info(
            "Queued message from user %d (%d in buffer) [client %s]",
            sender_id,
            client_queues[sender_id].qsize(),
            client_id[:8],
        )

    return handle_new_message


async def _start_telegram_client(cfg: ClientConfig) -> None:
    """Start a TelegramClient for a single client config."""
    if not cfg.session_string:
        logger.warning("Skipping client_id=%s — no session_string", cfg.client_id)
        return
    client = TelegramClient(
        StringSession(cfg.session_string),
        config.API_ID,
        config.API_HASH,
        flood_sleep_threshold=0,
    )
    _queues[cfg.client_id] = {}
    _workers[cfg.client_id] = {}
    handler = _make_handler(client, cfg.client_id)
    client.add_event_handler(handler, events.NewMessage(incoming=True))
    await client.start()
    _clients[cfg.client_id] = client
    logger.info("Started TelegramClient for client_id=%s", cfg.client_id)


async def _outbox_worker() -> None:
    """Polling outbox Supabase ogni 10s e invia messaggi via Telethon."""
    import store as _store

    while True:
        try:
            for client_id, tg_client in _clients.items():
                pending = await _store.get_pending_outbox(client_id, limit=20)
                for record in pending:
                    try:
                        await tg_client.send_message(
                            int(record["user_id"]), record["message"]
                        )
                        await _store.mark_outbox_sent(record["id"])
                        logger.info(
                            "Outbox sent: user=%s msg_id=%s",
                            record["user_id"],
                            record["id"],
                        )
                    except Exception as e:
                        await _store.mark_outbox_failed(record["id"], str(e))
                        logger.warning("Outbox failed: %s — %s", record["id"], e)
        except Exception as e:
            logger.warning("Outbox worker error: %s", e)
        await asyncio.sleep(10)


async def _poll_new_clients() -> None:
    """Poll Supabase every 60s for new active clients and start their TelegramClients."""
    while True:
        await asyncio.sleep(60)
        try:
            all_cfgs = await db.get_all_active_clients()
            for cfg in all_cfgs:
                if cfg.client_id not in _clients:
                    logger.info("New client detected: %s — starting", cfg.client_id)
                    await _start_telegram_client(cfg)
        except Exception:
            logger.exception("Error in _poll_new_clients")


async def main() -> None:
    await init_db()
    logger.info("Database initialised")

    # Start webhook server
    webhook_runner = await _start_webhook_server()

    # Load and start all active clients
    all_cfgs = await db.get_all_active_clients()
    if not all_cfgs:
        logger.warning(
            "No active clients with session_string found — starting with DEFAULT_CLIENT_ID fallback"
        )
        fallback_cfg = ClientConfig(
            client_id=config.DEFAULT_CLIENT_ID,
            system_prompt_base="",
            stage_instructions={},
            session_string=config.SESSION_STRING,
        )
        all_cfgs = [fallback_cfg]

    for cfg in all_cfgs:
        await _start_telegram_client(cfg)

    if not _clients:
        logger.error(
            "No TelegramClients started — check session_string in client_config or SESSION_STRING env var"
        )
        await webhook_runner.cleanup()
        return

    logger.info("Started %d TelegramClient(s)", len(_clients))

    # Run polling + outbox worker + all clients
    poll_task = asyncio.create_task(_poll_new_clients(), name="poll-new-clients")
    outbox_task = asyncio.create_task(_outbox_worker(), name="outbox-worker")

    try:
        await asyncio.gather(*[c.run_until_disconnected() for c in _clients.values()])
    finally:
        poll_task.cancel()
        outbox_task.cancel()
        await webhook_runner.cleanup()
        # Cancel all workers
        for client_workers in _workers.values():
            for task in client_workers.values():
                task.cancel()
        all_worker_tasks = [t for cw in _workers.values() for t in cw.values()]
        if all_worker_tasks:
            await asyncio.gather(*all_worker_tasks, return_exceptions=True)
        # Disconnect all clients
        for client in _clients.values():
            await client.disconnect()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
