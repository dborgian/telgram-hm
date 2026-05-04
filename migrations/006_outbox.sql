-- Tabella outbox per messaggi manuali dalla dashboard al bot Telegram
CREATE TABLE IF NOT EXISTS outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id bigint NOT NULL,
    client_id uuid NOT NULL,
    message text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz
);

CREATE INDEX IF NOT EXISTS outbox_status_created ON outbox(status, created_at)
    WHERE status = 'pending';
