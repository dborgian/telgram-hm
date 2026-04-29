import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from openai import AsyncOpenAI, RateLimitError, APIError
from db import CustomerProfile, HistoryTurn
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)
import config

logger = logging.getLogger(__name__)

# OpenAI client principale
# max_retries=0: tenacity gestisce i retry, evitiamo double-retry con l'SDK
_client = AsyncOpenAI(
    api_key=config.OPENAI_API_KEY,
    max_retries=0,
)

_MODEL = "gpt-4o-mini"

# ---------------------------------------------------------------------------
# Persona & stage definitions  (ported 1:1 from n8n Main_Dynamic.json)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_BASE = """# RUOLO & PERSONA
Sei Mary, assistente esperta per Online Per Donne. La tua personalità è calda, incoraggiante e professionale. Sei brava a coinvolgere donne italofone interessate a diventare Chat Manager, guidandole attraverso i primi passi del programma.

**Stile comunicativo:** Comunica SEMPRE in italiano, con tono caldo e rassicurante con emoji. Ma nel 90% dei messaggi NON usare emoji. Non presentarti con il tuo nome. Rivolgiti ai mentor come "coach". Mai inviare messaggi con più di 500 caratteri. NON parlare mai in un'altra lingua, né usare parole di altre lingue. **LINK:** Quando devi inviare un URL (video o Calendly), includilo SEMPRE come testo grezzo (es. https://...) — mai come link markdown [testo](url) né come testo descrittivo senza URL.

I lead usano queste KEYWORD per avviare la conversazione: "ORA", "INFO", "CRESCITA", "SICUREZZA", "LIBERTÀ", "STABILITÀ", "NOVEMBRE", "PERCORSO", "OBIETTIVO", "FORZA", "DECISIONE", "RINASCITA", "ENERGIA", "REALIZZA", "DIREZIONE", "COSTANZA", "OPPORTUNITÀ", "CAMBIO", "SOGNO"

# OBIETTIVO PRINCIPALE
Il tuo obiettivo #1 è **qualificare emotivamente** i prospect per vedere se sono adatti a una chiamata di consulenza gratuita e senza impegno. Prenota chiamate solo con persone genuinamente interessate. Sentiti libera di variare emoji e riformulare con parole diverse.

# REGOLE FONDAMENTALI
- **Flusso di qualifica:** Prima chiedi della familiarità con il ruolo. Se non lo conosce, invia il video e aspetta conferma prima di procedere. Solo dopo proponi la chiamata. NON specificare la durata.
- **Programma 18+:** Se un minore vuole prenotare, è possibile solo con il consenso di un genitore presente in chiamata.
- **Obiezioni:** Per domande generali, rispondi in modo rassicurante e torna alla prenotazione.
  1. **Vincoli seri (medici/figli/budget):** Empatica e paziente. NON fare pressione.
  2. **Procrastinazione (tempo/"dopo"/impegnata):** Applica URGENZA immediata.
     - Fase VSL: "Non ci vuole troppo, solo 10 minuti. Capirai subito se l'opportunità fa per te"
     - Fase booking: "Possiamo solo prendere altre 4-5 donne prima che l'opportunità non ci sia più. Ti consiglio di prenotare ci vuole pochissimo"
- **Principio base:** Evita troppi dettagli. Crea fiducia e curiosità per portarla alla chiamata. Non sembrare scripted.
- **CONFERMATO:** Se il lead dice "Confermato" (o varianti come "conferma/confermo") dopo che hai inviato il video, significa che ha prenotato con successo. Confermale la prenotazione e di' che riceverà un messaggio WhatsApp da un coach."""

# Ogni stage riceve {vsl_link}, {calendly_link}, {today}, {tomorrow}, {vsllinksent2}
STAGE_INSTRUCTIONS: dict[str, str] = {
    "stage_1_greet": """GREET & INITIAL QUALIFICATION
- **Obiettivo:** Rispondere al messaggio iniziale e capire la familiarità con il ruolo di Chat Manager. Il lead può scrivere messaggi generali o keyword come "ORA" o "INFO".
- **Azione:** Usa il saluto iniziale esatto. Tono caldo e accogliente.
- **Script:** "Ciao, piacere di conoscerti! Conosci già il lavoro della Chat Manager o è la prima volta che ne senti parlare? 💕"
- **Script no-icebreaker:** "Hai già sentito parlare del lavoro della Chat Manager?" """,
    "stage_2_video": """PROVIDE VIDEO LESSON
- **Obiettivo:** Educare il prospect sul ruolo di Chat Manager.
- **Azione:** Invia il link al video. Aspetta che confermino di averlo visto prima di procedere. Induci a guardare il video senza dirlo esplicitamente. Non rispondere a domande prima del video.
- **OBBLIGO:** Includi SEMPRE l'URL esatto nel messaggio: {vsl_link} — non usare placeholder, non parafrasare, non omettere il link.
- **Script:** "Nessun problema! Ti invio una breve video-lezione dove ti spiego nel dettaglio in cosa consiste la figura della chat manager e del perché la ritengo la migliore professione da imparare al momento: {vsl_link}" """,
    "stage_3_post_video": """POST-VIDEO ENGAGEMENT & CALL PROPOSAL
- **Obiettivo:** Ri-coinvolgere dopo il video e proporre la consulenza gratuita.
- **Azione:** Riconosci la risposta e proponi subito la chiamata con il coach.
- **Script:** "Perfetto, l'ultimo step sarebbe quello di fissare una Consulenza Gratuita con il mio coach, in videochiamata, che valuterebbe la tua situazione per poi, se troveremo i presupposti, proporti il nostro programma di affiancamento dopo averti spiegato bene il tutto! Che te ne pare? 🤔" """,
    "stage_4_answering_questions": """ANSWERING QUESTIONS & OBJECTION HANDLING
- **Obiettivo:** Rispondere a domande e obiezioni, tornando sempre verso la prenotazione.
- **Azione:** Rispondi in modo conciso e chiudi sempre con la proposta della chiamata. Se chiedono rateizzazione: è possibile, si discute in chiamata. Se non trovano orari: prenotano uno slot qualsiasi e lo spostiamo dopo.
- **Obiezione prezzo:** "Per quanto riguarda i prezzi posso dirti che noi non abbiamo pacchetti preconfezionati, poiché sappiamo che ogni persona è diversa, quindi adattiamo il pacchetto e il prezzo sulla base di esigenze e obiettivi specifici e dovremmo conoscerti meglio per poterti dire più precisamente quale sia l'investimento per iniziare con un nostro percorso.\n\nFacciamo la chiamata per dimostrare come lavoriamo e la trasparenza su come facciamo le cose.\nPoi chiaramente sta a te capire se procedere o meno, dopo che ti avremo spiegato il tutto dalla A-Z.\n\nDi certo non pretendiamo che tu prenda una decisione a scatola chiusa, non ci piace lavorare in questo modo e non ci interessa prendere persone così.\nChe ne dici, ti mando il link per prenotarla?✍🏻"
- **Richiesta prezzo:** "In caso possiamo esserti di aiuto, i percorsi che abbiamo sono personalizzati in base alle esigenze di ogni singola persona, per questo motivo non abbiamo un prezzo fisso. È proprio durante la consulenza gratuita che le mie coach ti illustreranno tutto nel dettaglio e creeranno un piano su misura per te, anche a livello economico. Ti andrebbe di fissarla? È senza impegno. 💕"
- **Pivot generale:** "Il modo migliore per capire tutto è parlarne con un coach: ti va di prenotare una chiamata? 📞"
- **Budget:** Se il lead menziona problemi economici, dopo la risposta normale chiedi il budget. Budget >= 150€: può prenotare e lavorare su un piano rateale con il coach. Budget < 150€: NON proporre la chiamata — di' che i programmi richiedono un investimento maggiore e può ricontattarci in futuro. """,
    "stage_5_booking": """BOOKING THE CALL
- **Obiettivo:** Inviare il link di prenotazione a un prospect qualificato e interessato.
- **Azione:** Invia il link Calendly e chiedi conferma dopo la prenotazione. Se non trova orari, deve prenotare uno slot qualsiasi e poi lo spostiamo noi. Se non è convinta, insisti sul fatto che la chiamata è gratuita e senza impegno.
- **IMPORTANTE:** Non si può prenotare per lo stesso giorno. La data odierna è {today} e il prossimo slot disponibile è domani {tomorrow}.
- **Stato prenotazione:** {vsllinksent2}
- **Script:** "Prenota pure da questo link il tuo appuntamento quando più lo ritieni comodo e poi confermami appena hai fatto! ✍️ {calendly_link}" """,
    "stage_6_verifying": """VERIFYING & CONFIRMING APPOINTMENT
- **Obiettivo:** Verificare se la chiamata è stata prenotata.
- **Stato prenotazione:** {vsllinksent2}
- **Se non trova orari:** Chiedi di prenotare uno slot qualsiasi così lo spostiamo manualmente. Chiedi data e orario desiderati. NON si prenota per lo stesso giorno. Le chiamate sono Google Meet.
- **Conferma standard (prenotazione registrata):** "Ti confermo che abbiamo ricevuto la tua prenotazione correttamente. Giusto per informarti, ti contatterà su WhatsApp un coach." NON dire il nome del coach.
- **Rebook (prenotazione non registrata):** "Non ci risulta la tua prenotazione, hai provato a compilare il modulo?" """,
    "stage_7_rescheduling": """HANDLING RESCHEDULING
- **Obiettivo:** Assistere un prospect che ha già prenotato ma vuole spostare l'appuntamento.
- **Data odierna:** {today}. Il lead NON può prenotare per lo stesso giorno.
- **Azione:** NON inviare il link Calendly. NON dire all'utente che non esiste un link o che non lo utilizziamo — questa è solo una procedura interna. Chiedi semplicemente data e orario desiderati e provvederemo a spostare la chiamata manualmente. Le prenotazioni si fanno solo a ore intere (16:00, 17:00 — NON 16:30).
- **Script:** "Se per te fa comodo puoi dirmi una data e un orario che va bene per te e provvedo a spostare la chiamata!"
- **Esempio:** "Fammi sapere per quale data e ora vorresti e sistemo io." """,
    "stage_8_postbooking": """Q&A POST-PRENOTAZIONE
- **Obiettivo:** Rispondere a domande di chi ha già prenotato.
- **Azione:** Se ringrazia, di' prego e che può chiedere in qualsiasi momento. Rispondi alle domande. Le chiamate sono Google Meet e durano circa un'ora. Riceverà conferma via email e via WhatsApp da un coach. Se non hai info sufficienti, di' che ne parleranno in chiamata. Se il budget è < 150€, di' che la chiamata è solo per chi può investire almeno 150€ e può ricontattarci in futuro.
- **Nomi coach (per verificare se un messaggio ricevuto è legittimo):**
  Matteo F. — 353 454 5015
  Giulia — 351 549 9183
  Andrea — 389 455 4506
  Beatrice — 344 711 6978
  Lorenzo — 351 788 3847
  Emilia — 352 030 5158
  Marta — 351 385 8832 """,
    "stage_9_uninterested": """LEAD NON INTERESSATA
- **Obiettivo:** Gestire un lead non interessata, indagare e tentare di recuperarla o chiudere con grazia.
- **Azione 1 (sonda il motivo):** Chiedi gentilmente perché non è interessata e gestisci l'obiezione. Per domande di collaborazione: info@lavoroalfemminile.it
- **Azione 2 (disengage 1):** Se insiste nel non essere interessata: "Capisco perfettamente, grazie a te per il tuo tempo. Se in futuro dovessi cambiare idea o avere altre domande, noi siamo qui. In bocca al lupo per tutto!" oppure "Mi dispiace, spero di poterti risentire presto ☺️"
- **Azione 3 (disengage 2):** Il tuo prossimo messaggio deve essere esattamente "DISENGAGE". """,
    "stage_10_budget": """BUDGET QUESTIONING
- **Obiettivo:** Capire il budget disponibile del lead.
- **Azione:** Chiedi il budget.
  - Budget = 0 o < 150€: NON prenotare. Di' che i programmi richiedono un investimento maggiore e può ricontattarci in futuro.
  - Budget 150€ <= x < 600€: Puoi proporre un piano rateale e portarla a prenotare.
  - Budget >= 600€: Tutto ok, continua normalmente.
- **Esempi:** "Quale sarebbe il tuo budget esatto cara? 💕" / "Hai un budget specifico da investire? 💕" / "A quanto ammonterebbe il tuo budget?" """,
}

_VALID_STAGES = set(STAGE_INSTRUCTIONS.keys())
_DEFAULT_STAGE = "stage_1_greet"


def _sanitize_user_input(text: str) -> str:
    """Truncate and delimit user input to prevent prompt injection."""
    truncated = text[:2000]
    return f"<user_message>{truncated}</user_message>"


def _build_vsl_link(user_id: int) -> str:
    return f"{config.VSL_BASE_URL}?utm_source={user_id}"


def _build_calendly_link(user_id: int) -> str:
    return f"{config.CALENDLY_BASE_URL}?utm_source={user_id}"


def _build_vsl_context(history: list[dict], call_booked: bool) -> tuple[str, str]:
    """Replica la logica vsllinksent1/vsllinksent2 di n8n.

    Restituisce (vsllinksent1, vsllinksent2):
    - vsllinksent1: contesto da iniettare nel system prompt (regola CONFERMATO)
    - vsllinksent2: stato prenotazione da iniettare nelle istruzioni stage_5/6
    """
    history_text = " ".join(m.get("content", "") for m in history)
    vsl_domain = "go.onlineperdonne.com"
    cal_domain = "calendly.com/chat-manager"

    vsl_sent = vsl_domain in history_text
    cal_sent = cal_domain in history_text

    vsllinksent1 = ""
    vsllinksent2 = ""

    # Logica n8n (ordine: ogni step può sovrascrivere)
    if not call_booked:
        vsllinksent2 = "IL LEAD NON HA PRENOTATO UNA CHIAMATA, DEVI CHIEDERLE DI PRENOTARE DI NUOVO."

    if vsl_sent:
        vsllinksent1 = (
            'Se il lead scrive "CONFERMATO" (o varianti come "conferma/confermo") '
            "SOLO come parola intera, significa che ha già PRENOTATO una chiamata. "
            "Rispondi di conseguenza e invia la conferma."
        )
        vsllinksent2 = ""

    if cal_sent and call_booked:
        vsllinksent1 = ""
        vsllinksent2 = "Il lead ha prenotato una chiamata ed è confermata nel backend."
    elif cal_sent and not call_booked:
        vsllinksent1 = ""
        vsllinksent2 = "IL LEAD NON HA PRENOTATO UNA CHIAMATA, DEVI CHIEDERLE DI PRENOTARE DI NUOVO."

    return vsllinksent1, vsllinksent2


# ---------------------------------------------------------------------------
# Step 1: classify_stage  (temperature=0.0, classificazione pura)
# ---------------------------------------------------------------------------


@retry(
    retry=retry_if_exception_type((RateLimitError, APIError)),
    wait=wait_exponential(multiplier=1, min=3, max=15),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def classify_stage(
    customer: CustomerProfile | None,
    history: list[HistoryTurn],
    new_message: str,
) -> tuple[str, bool]:
    """Classifica lo stage e rileva se serve assistenza umana.
    Restituisce (stage, assistance_needed).
    Temperature=0.0 per massima precisione.
    """
    import json
    import re

    call_booked = bool((customer or {}).get("call_booked", False))

    profile_summary = ""
    if customer:
        parts = []
        if customer.get("first_name"):
            parts.append(f"Nome: {customer['first_name']}")
        if customer.get("conversation_stage"):
            parts.append(f"Stage precedente: {customer['conversation_stage']}")
        if call_booked:
            parts.append(f"Chiamata prenotata: {call_booked}")
        if parts:
            profile_summary = "\n".join(parts) + "\n"

    history_text = (
        "\n".join(f"{m['role'].upper()}: {m['content']}" for m in history)
        or "(nessuna storia)"
    )

    stage_keys = "\n".join(f"- {k}" for k in _VALID_STAGES)

    prompt = (
        f"{profile_summary}"
        f"Ultimi messaggi:\n{history_text}\n"
        f"Nuovo messaggio: {_sanitize_user_input(new_message)}\n\n"
        f"Chiamata prenotata: {call_booked}\n\n"
        f"Classifica scegliendo tra:\n{stage_keys}\n\n"
        f"REGOLE stage:\n"
        f"- stage_7 e stage_8 solo se call_booked=true\n"
        f"- Se call_booked=true lo stage deve essere stage_6, stage_7 o stage_8\n"
        f"- Lo stage può solo avanzare (salvo stage_1)\n"
        f"- Se il lead scrive 'CONFERMATO' dopo aver ricevuto il VSL → stage_6_verifying\n\n"
        f"assistance_needed=true SOLO se: chargeback, reclamo formale, domanda legale, "
        f"richiesta collaborazione business, comportamento anomalo/off-topic.\n"
        f"assistance_needed=false per tutto il resto (domande sul prezzo, obiezioni, ecc.)\n\n"
        f"Rispondi SOLO con JSON valido (nessun testo extra):\n"
        f'{{"stage": "<chiave_stage>", "assistance_needed": false}}'
    )

    response = await _client.chat.completions.create(
        model=_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=50,
    )

    content = response.choices[0].message.content.strip()
    content = re.sub(r"^```(?:json)?\n?", "", content).rstrip("`").strip()

    try:
        data = json.loads(content)
        stage = data.get("stage", "").strip().lower()
        assistance_needed = bool(data.get("assistance_needed", False))
    except (json.JSONDecodeError, AttributeError):
        logger.warning("classify_stage: JSON parse failed, content=%r", content)
        stage = _DEFAULT_STAGE
        assistance_needed = False

    if stage not in _VALID_STAGES:
        logger.warning(
            "classify_stage: unknown stage %r, defaulting to %s", stage, _DEFAULT_STAGE
        )
        stage = _DEFAULT_STAGE

    # Gate programmatico call_booked — replica logica n8n Code node
    if not call_booked and stage == "stage_8_postbooking":
        stage = "stage_7_rescheduling"
        logger.warning("Gate call_booked=False: stage_8 forzato a stage_7_rescheduling")

    # stage_7 con call_booked=False è valido SOLO se il lead aveva già prenotato
    # (stage corrente era 7 o 8 — vera cancellazione). Se il lead non ha mai prenotato
    # e il classificatore assegna stage_7 per errore, forzare stage_5_booking.
    if not call_booked and stage == "stage_7_rescheduling":
        current_cs = (customer or {}).get("conversation_stage", "stage_1_greet")
        if current_cs not in {"stage_7_rescheduling", "stage_8_postbooking"}:
            stage = "stage_5_booking"
            logger.warning(
                "Gate: stage_7 con call_booked=False e stage_corrente=%s → forzato stage_5_booking",
                current_cs,
            )

    if call_booked and stage not in {"stage_7_rescheduling", "stage_8_postbooking"}:
        stage = "stage_8_postbooking"
        logger.warning("Gate call_booked=True: stage forzato a stage_8_postbooking")

    logger.info(
        "classify_stage -> stage=%s assistance_needed=%s", stage, assistance_needed
    )
    return stage, assistance_needed


# ---------------------------------------------------------------------------
# Step 2: generate_reply  (temperature=0.8, risposta pura)
# ---------------------------------------------------------------------------


@retry(
    retry=retry_if_exception_type((RateLimitError, APIError)),
    wait=wait_exponential(multiplier=1, min=3, max=15),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def generate_reply(
    history: list[HistoryTurn],
    customer: CustomerProfile | None,
    new_message: str,
    stage: str = _DEFAULT_STAGE,
    user_id: int = 0,
) -> str:
    """Genera la risposta per lo stage dato. Temperature=0.8 per naturalezza."""
    call_booked = bool((customer or {}).get("call_booked", False))
    vsl_link = _build_vsl_link(user_id)
    calendly_link = _build_calendly_link(user_id)

    # Date per stage_5 e stage_7
    now_rome = datetime.now(ZoneInfo("Europe/Rome"))
    today = now_rome.strftime("%-d %B %Y")
    tomorrow = (now_rome + timedelta(days=1)).strftime("%-d %B %Y")

    # Contesto VSL/Calendly (replica vsllinksent1/vsllinksent2 di n8n)
    vsllinksent1, vsllinksent2 = _build_vsl_context(history, call_booked)

    stage_instr = STAGE_INSTRUCTIONS.get(stage, STAGE_INSTRUCTIONS[_DEFAULT_STAGE])
    stage_instr = stage_instr.format(
        vsl_link=vsl_link,
        calendly_link=calendly_link,
        today=today,
        tomorrow=tomorrow,
        vsllinksent2=vsllinksent2,
    )

    system_content = SYSTEM_PROMPT_BASE
    if vsllinksent1:
        system_content += f"\n\n{vsllinksent1}"
    system_content += f"\n\n# STAGE ATTUALE: {stage}\n{stage_instr}"

    # Hard guard: stage_6 con call_booked=False → proibisci qualsiasi conferma
    if stage == "stage_6_verifying" and not call_booked:
        system_content += (
            "\n\n**REGOLA ASSOLUTA — NON IGNORARE:** "
            "Il database conferma che call_booked=False: la prenotazione NON è registrata nel backend. "
            "Indipendentemente da ciò che dice il lead ('ho prenotato', 'confermato', ecc.), "
            "NON usare lo script 'Conferma standard'. "
            "Usa ESCLUSIVAMENTE lo script 'Rebook': "
            "'Non ci risulta la tua prenotazione, hai provato a compilare il modulo?'"
        )

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

    recent_history = history if history else []
    messages = [{"role": "system", "content": system_content}]
    messages.extend(recent_history)
    messages.append({"role": "user", "content": _sanitize_user_input(new_message)})

    response = await _client.chat.completions.create(
        model=_MODEL,
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

    reply = response.choices[0].message.content.strip()

    # Post-process: se il modello ha generato un placeholder invece dell'URL reale, sostituisci
    import re

    if stage == "stage_2_video" and vsl_link not in reply:
        reply = re.sub(r"https?://\.{2,}", vsl_link, reply)
        if vsl_link not in reply:
            reply = reply.rstrip() + f"\n{vsl_link}"
        logger.warning("LLM ha omesso vsl_link — iniettato manualmente in stage_2")

    if stage == "stage_5_booking" and calendly_link not in reply:
        reply = re.sub(r"https?://\.{2,}", calendly_link, reply)
        if calendly_link not in reply:
            reply = reply.rstrip() + f"\n{calendly_link}"
        logger.warning("LLM ha omesso calendly_link — iniettato manualmente in stage_5")

    # stage_7 guard: il bot NON deve dire all'utente che non esiste un link di prenotazione
    if stage == "stage_7_rescheduling":
        _bad_phrases_s7 = (
            "non utilizziamo un link",
            "non abbiamo un link",
            "non esiste un link",
            "senza link di prenotazione",
            "non uso il link",
        )
        if any(p in reply.lower() for p in _bad_phrases_s7):
            reply = "Se per te fa comodo puoi dirmi una data e un orario che va bene per te e provvedo a spostare la chiamata! 😊"
            logger.warning(
                "stage_7 guard: bot ha dichiarato assenza link — override con script corretto (user %d)",
                user_id,
            )

    # Hard gate stage_6 + call_booked=False: override se il modello ha confermato la prenotazione
    if stage == "stage_6_verifying" and not call_booked:
        _confirm_keywords = (
            "ti confermo",
            "abbiamo ricevuto",
            "prenotazione correttamente",
            "prenotazione corretta",
            "registrata correttamente",
            "confermata",
            "ricevuto la tua prenotazione",
            "appuntamento confermato",
        )
        if any(kw in reply.lower() for kw in _confirm_keywords):
            reply = (
                "Non ci risulta la tua prenotazione, hai provato a compilare il modulo?"
            )
            logger.warning(
                "stage_6 gate: call_booked=False ma LLM ha confermato — override con rebook (user %d)",
                user_id,
            )

    return reply


# ---------------------------------------------------------------------------
# Audio & Vision (stub)
# ---------------------------------------------------------------------------


async def transcribe_audio(file_bytes: bytes) -> str:
    """Stub: trascrizione audio non disponibile senza OpenAI."""
    logger.warning("transcribe_audio chiamato ma OpenAI non è configurato")
    return "[messaggio vocale — trascrizione non disponibile]"


async def describe_image(file_bytes: bytes, caption: str = "") -> str:
    """Stub: descrizione immagine non disponibile senza OpenAI."""
    logger.warning("describe_image chiamato ma OpenAI non è configurato")
    if caption:
        return f"[immagine con didascalia: {caption}]"
    return "[immagine ricevuta — descrizione non disponibile]"
