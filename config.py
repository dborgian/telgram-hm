from dotenv import load_dotenv
import os

load_dotenv()

API_ID: int = int(os.environ["API_ID"])
API_HASH: str = os.environ["API_HASH"]
SESSION_STRING: str = os.environ["SESSION_STRING"]
OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")  # opzionale, non usato

TEST_MODE_ENABLED: bool = os.getenv("TEST_MODE_ENABLED", "false").lower() == "true"
TEST_USERS: list[int] = [
    int(x) for x in os.getenv("TEST_USERS", "").split(",") if x.strip()
]

BUFFER_DELAY: int = int(os.getenv("BUFFER_DELAY", "90"))
MAX_MESSAGES: int = int(os.getenv("MAX_MESSAGES", "7"))
MAX_HISTORY_TURNS: int = int(os.getenv("MAX_HISTORY_TURNS", "20"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# Supabase — profilo utente persistente
SUPABASE_URL: str = os.environ["SUPABASE_URL"]
SUPABASE_KEY: str = os.environ["SUPABASE_KEY"]  # service_role key

# Upstash Redis — history chat + cache profilo
UPSTASH_REDIS_URL: str = os.environ[
    "UPSTASH_REDIS_URL"
]  # rediss://default:xxx@xxx.upstash.io:6379

# TTL cache profilo in Redis (secondi)
PROFILE_CACHE_TTL: int = int(os.getenv("PROFILE_CACHE_TTL", "3600"))

# Link VSL e Calendly (utm_source viene aggiunto dinamicamente con user_id)
VSL_BASE_URL: str = "https://go.onlineperdonne.com/vsl-513194"
CALENDLY_BASE_URL: str = "https://calendly.com/chat-manager/onlineconmary"

# Notifiche interne — Telegram chat ID dove inviare alert (assistance_needed, DISENGAGE)
# Lascia vuoto per disabilitare le notifiche
ALERT_CHAT_ID: int | None = (
    int(os.environ["ALERT_CHAT_ID"]) if os.getenv("ALERT_CHAT_ID") else None
)

# Webhook Calendly — server HTTP interno
# Railway inietta PORT automaticamente — usala se disponibile, altrimenti WEBHOOK_PORT
WEBHOOK_PORT: int = int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", "8080")))
# Secret opzionale per validare le richieste Calendly (Calendly-Webhook-Signature header)
CALENDLY_WEBHOOK_SECRET: str = os.getenv("CALENDLY_WEBHOOK_SECRET", "")
