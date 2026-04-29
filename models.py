from dataclasses import dataclass
from typing import TypedDict


class HistoryTurn(TypedDict):
    role: str
    content: str


class CustomerProfile(TypedDict, total=False):
    user_id: int
    first_name: str | None
    username: str | None
    call_booked: bool
    notes: str | None
    stage: str
    conversation_stage: str
    turn_count: int
    status: str
    last_seen: str
    first_seen: str


@dataclass
class ClientConfig:
    client_id: str
    system_prompt_base: str
    stage_instructions: dict[str, str]
    llm_model: str = "gpt-4o-mini"
    vsl_base_url: str = ""
    calendly_base_url: str = ""
    vsl_domain: str = "go.onlineperdonne.com"
    cal_domain: str = "calendly.com/chat-manager"
    default_stage: str = "stage_1_greet"
    session_string: str = ""
