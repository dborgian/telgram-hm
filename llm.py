import io
import logging
from openai import AsyncOpenAI
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)
from openai import RateLimitError, APIError
import config

logger = logging.getLogger(__name__)
_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)

# ---------------------------------------------------------------------------
# Persona & stage definitions
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_BASE = """# RUOLO
Sei un assistente che risponde ai messaggi privati su Telegram per conto di un'azienda.
Il tuo obiettivo è qualificare i contatti e portarli a prenotare una chiamata gratuita con un coach.

REGOLE:
- Parla SEMPRE in italiano. Mai usare parole in altre lingue.
- Tono caldo, rassicurante, professionale ma non formale.
- MAI superare i 500 caratteri per messaggio.
- Emoji con moderazione (circa 10% dei messaggi).
- Non rivelare mai di essere un'AI.
- Non presentarti con il tuo nome.
- Riferisciti ai responsabili interni come "coach"."""

# Ogni stage riceve {vsl_link} e {calendly_link} già formattati con utm_source
STAGE_INSTRUCTIONS: dict[str, str] = {
    "stage_1_greet": """OBIETTIVO: Rompere il ghiaccio, capire se il contatto conosce già l'opportunità.
AZIONE: Saluta calorosamente e fai UNA domanda per capire il loro punto di partenza.
SCRIPT: "Ciao, piacere di conoscerti! Conosci già la nostra opportunità o è la prima volta che ne senti parlare? 💕" """,
    "stage_2_video": """OBIETTIVO: Educare il contatto inviando il video informativo.
AZIONE: Invia il link al video. Aspetta che confermino di averlo visto prima di andare avanti.
Inducili a guardarlo senza dirlo esplicitamente. Non rispondere a domande prima del video.
SCRIPT: "Nessun problema! Ti invio una breve video-lezione dove ti spiego tutto nel dettaglio: {vsl_link}" """,
    "stage_3_post_video": """OBIETTIVO: Ri-coinvolgere dopo il video e proporre la consulenza gratuita.
AZIONE: Riconosci la loro risposta e proponi subito la chiamata con il coach.
SCRIPT: "Perfetto! L'ultimo step sarebbe fissare una Consulenza Gratuita con il mio coach, in videochiamata, per valutare la tua situazione. Che te ne pare? 🤔" """,
    "stage_4_answering_questions": """OBIETTIVO: Rispondere a domande e obiezioni, tornando sempre verso la prenotazione.
COMPORTAMENTO:
- Rispondi in modo conciso, poi chiudi con la proposta della chiamata
- Prezzi: non sono fissi, dipendono dalla situazione specifica — se ne parla in chiamata
- Rateizzazione: possibile, si discute in chiamata
- Se non trova orari: prenoti uno slot qualsiasi, si sposta dopo
- PIVOT: "Il modo migliore per capire tutto è parlarne con un coach 📞" """,
    "stage_5_booking": """OBIETTIVO: Inviare il link di prenotazione.
AZIONE: Invia il link Calendly e chiedi conferma dopo la prenotazione. Non si prenota per lo stesso giorno.
SCRIPT: "Prenota pure da questo link quando ti fa comodo e confermami appena fatto! ✍️ {calendly_link}" """,
    "stage_6_verifying": """OBIETTIVO: Verificare se la chiamata è stata prenotata.
- Se registrata: "Ti confermo che abbiamo ricevuto la tua prenotazione. Ti contatterà un coach su WhatsApp."
- Se NON registrata: "Non ci risulta la tua prenotazione, hai provato a compilare il modulo?" """,
    "stage_7_rescheduling": """OBIETTIVO: Aiutare chi vuole spostare l'appuntamento già prenotato.
AZIONE: Non usare il link di prenotazione. Chiedi data e orario desiderati. Solo ore intere (16:00, 17:00 — NON 16:30). Non riprenota per lo stesso giorno.
SCRIPT: "Dimmi una data e orario che preferisci e provvedo io a spostare la chiamata." """,
    "stage_8_postbooking": """OBIETTIVO: Rispondere a domande dopo la prenotazione.
AZIONE: Rispondi alle domande. Le chiamate sono su Google Meet. Conferma via email e WhatsApp da un coach.
Se non hai info sufficienti: rimanda alla chiamata. """,
    "stage_9_uninterested": """OBIETTIVO: Capire perché non è interessata, tentare di recuperarla o chiudere con grazia.
AZIONE 1: Chiedi gentilmente il motivo e gestisci l'obiezione.
AZIONE 2 (se insiste): "Capisco perfettamente, grazie per il tuo tempo. Se cambiassi idea siamo qui. In bocca al lupo! ☺️"
AZIONE 3: Il prossimo messaggio deve essere esattamente "DISENGAGE". """,
    "stage_10_budget": """OBIETTIVO: Capire il budget disponibile.
AZIONE: Chiedi il budget. Se insufficiente (sotto la soglia minima): non prenotare, di' che il programma richiede un investimento maggiore.
ESEMPI: "Quale sarebbe il tuo budget esatto? 💕" / "Hai un budget specifico da investire?" """,
}

_VALID_STAGES = set(STAGE_INSTRUCTIONS.keys())
_DEFAULT_STAGE = "stage_1_greet"


def _build_vsl_link(user_id: int) -> str:
    return f"{config.VSL_BASE_URL}?utm_source={user_id}"


def _build_calendly_link(user_id: int) -> str:
    return f"{config.CALENDLY_BASE_URL}?utm_source={user_id}"


# ---------------------------------------------------------------------------
# Pre-Agent: detect_stage
# ---------------------------------------------------------------------------


async def detect_stage(
    customer: dict | None,
    history: list[dict],
    new_message: str,
) -> str:
    """Classifica lo stage attuale tra i 10 stage del workflow."""
    profile_summary = ""
    if customer:
        parts = []
        if customer.get("first_name"):
            parts.append(f"Nome: {customer['first_name']}")
        if customer.get("conversation_stage"):
            parts.append(f"Stage precedente: {customer['conversation_stage']}")
        if customer.get("call_booked"):
            parts.append(f"Chiamata prenotata: {customer['call_booked']}")
        if parts:
            profile_summary = "\n".join(parts) + "\n"

    history_text = (
        "\n".join(f"{m['role'].upper()}: {m['content']}" for m in history)
        or "(nessuna storia)"
    )

    call_booked = bool((customer or {}).get("call_booked", False))

    prompt = (
        f"{profile_summary}"
        f"Ultimi messaggi:\n{history_text}\n"
        f"Nuovo messaggio: {new_message}\n\n"
        f"Chiamata prenotata: {call_booked}\n\n"
        "Classifica lo stage attuale scegliendo UNA sola stringa tra:\n"
        "- stage_1_greet: primo contatto, non sa ancora di cosa si tratta\n"
        "- stage_2_video: ha mostrato interesse, deve vedere il video\n"
        "- stage_3_post_video: ha visto il video, si propone la chiamata\n"
        "- stage_4_answering_questions: ha domande o obiezioni\n"
        "- stage_5_booking: vuole prenotare, si invia il link Calendly\n"
        "- stage_6_verifying: ha detto di aver prenotato, si verifica\n"
        "- stage_7_rescheduling: ha prenotato ma vuole spostare (solo se call_booked=true)\n"
        "- stage_8_postbooking: ha prenotato e ha domande (solo se call_booked=true)\n"
        "- stage_9_uninterested: ha espresso disinteresse esplicito\n"
        "- stage_10_budget: ha tirato fuori preoccupazioni di budget\n\n"
        "REGOLE: stage_7 e stage_8 solo se call_booked=true. "
        "Lo stage può solo avanzare, mai retrocedere (salvo stage_1).\n"
        "Rispondi con UNA sola stringa esatta."
    )

    response = await _client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=20,
    )
    stage = response.choices[0].message.content.strip().lower()
    if stage not in _VALID_STAGES:
        logger.warning(
            "detect_stage returned unknown stage %r, defaulting to %s",
            stage,
            _DEFAULT_STAGE,
        )
        stage = _DEFAULT_STAGE
    logger.info("detect_stage -> %s", stage)
    return stage


# ---------------------------------------------------------------------------
# Main Agent: generate_reply
# ---------------------------------------------------------------------------


@retry(
    retry=retry_if_exception_type((RateLimitError, APIError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def generate_reply(
    history: list[dict],
    customer: dict | None,
    new_message: str,
    stage: str = _DEFAULT_STAGE,
    user_id: int = 0,
) -> str:
    """Genera la risposta con le istruzioni specifiche per lo stage."""
    vsl_link = _build_vsl_link(user_id)
    calendly_link = _build_calendly_link(user_id)

    stage_instr = STAGE_INSTRUCTIONS.get(stage, STAGE_INSTRUCTIONS[_DEFAULT_STAGE])
    stage_instr = stage_instr.format(vsl_link=vsl_link, calendly_link=calendly_link)

    system_content = SYSTEM_PROMPT_BASE
    system_content += f"\n\n# STAGE ATTUALE: {stage}\n{stage_instr}"

    if customer:
        profile_parts = []
        if customer.get("first_name"):
            profile_parts.append(f"Nome: {customer['first_name']}")
        if customer.get("username"):
            profile_parts.append(f"Username: @{customer['username']}")
        if customer.get("call_booked"):
            profile_parts.append(f"Chiamata prenotata: {customer['call_booked']}")
        if customer.get("notes"):
            profile_parts.append(f"Note: {customer['notes']}")
        if profile_parts:
            system_content += "\n\n# PROFILO CONTATTO\n" + "\n".join(profile_parts)

    recent_history = history[-(config.MAX_HISTORY_TURNS * 2) :] if history else []

    messages = [{"role": "system", "content": system_content}]
    messages.extend(recent_history)
    messages.append({"role": "user", "content": new_message})

    response = await _client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.8,
        max_tokens=500,
    )

    usage = response.usage
    logger.info(
        "LLM tokens: prompt=%d completion=%d",
        usage.prompt_tokens,
        usage.completion_tokens,
    )

    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# Audio & Vision
# ---------------------------------------------------------------------------


async def transcribe_audio(file_bytes: bytes) -> str:
    """Trascrive un messaggio vocale/audio con OpenAI Whisper."""
    response = await _client.audio.transcriptions.create(
        model="whisper-1",
        file=("audio.ogg", io.BytesIO(file_bytes), "audio/ogg"),
    )
    return response.text.strip()


async def describe_image(file_bytes: bytes, caption: str = "") -> str:
    """Descrive un'immagine con GPT-4o Vision."""
    import base64

    b64 = base64.b64encode(file_bytes).decode()
    content: list[dict] = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]
    if caption:
        content.append({"type": "text", "text": f"Didascalia: {caption}"})
    content.append(
        {
            "type": "text",
            "text": "Descrivi brevemente il contenuto di questa immagine in italiano.",
        }
    )

    response = await _client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": content}],
        max_tokens=200,
    )
    return response.choices[0].message.content.strip()
