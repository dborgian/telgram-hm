import asyncio
import logging

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

import config
import llm
from db import init_db
from processor import process_conversation

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# user_id -> asyncio.Queue of (text, first_name, username)
_queues: dict[int, asyncio.Queue] = {}
# user_id -> running worker Task
_workers: dict[int, asyncio.Task] = {}


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

    await client.start()
    logger.info("Userbot connected and listening")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
