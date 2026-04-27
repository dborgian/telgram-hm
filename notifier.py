"""
notifier.py — Notifiche interne su Telegram.

Invia un alert al chat interno (ALERT_CHAT_ID) quando:
- assistance_needed=True: il lead richiede intervento umano
- DISENGAGE: il lead è stato chiuso come Lost Lead
"""

import logging
import config

logger = logging.getLogger(__name__)


async def notify_alert(
    client,
    user_id: int,
    first_name: str | None,
    username: str | None,
    reason: str,
    stage: str = "",
    extra: str = "",
) -> None:
    """Invia notifica interna. No-op se ALERT_CHAT_ID non configurato."""
    if not config.ALERT_CHAT_ID:
        return

    name_str = first_name or "Sconosciuta"
    username_str = f"@{username}" if username else "nessun username"
    lines = [
        f"ALERT: {reason}",
        f"Lead: {name_str} ({username_str})",
        f"ID: {user_id}",
    ]
    if stage:
        lines.append(f"Stage: {stage}")
    if extra:
        lines.append(extra)

    text = "\n".join(lines)
    try:
        await client.send_message(config.ALERT_CHAT_ID, text)
        logger.info("notify_alert inviato per user %d: %s", user_id, reason)
    except Exception as e:
        logger.warning("notify_alert fallito per user %d: %s", user_id, e)
