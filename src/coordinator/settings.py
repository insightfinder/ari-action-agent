from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class CoordinatorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    coordinator_agents_file: str = ""
    coordinator_name: str = "ARI"
    coordinator_description: str = (
        "Sends each request to the ARI agent that fits it, such as the on-call code agent or the "
        "Kubernetes agent, and relays the answer, including any change that waits for approval."
    )
    coordinator_public_url: str = "http://localhost:8000"
    coordinator_card_refresh_seconds: float = 300.0
    coordinator_card_timeout_seconds: float = 10.0
    coordinator_request_timeout_seconds: float = 60.0
    coordinator_poll_seconds: float = 5.0
    coordinator_follow_budget_seconds: float = 7200.0
    coordinator_agent_outage_seconds: float = 600.0
    coordinator_route_turns: int = 6
    coordinator_context_turns: int = 3


@lru_cache
def get_coordinator_settings() -> CoordinatorSettings:
    return CoordinatorSettings()
