from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = ""
    backend_mode: Literal["mock", "http", "team"] = "mock"
    backend_base_url: str = "http://localhost:8000"
    backend_api_key: str = ""
    team_backend_state_file: str = "data/team_backend_state.json"
    llm_provider: Literal["none", "ollama", "openai_compatible"] = "none"
    llm_model: str = ""
    ollama_base_url: str = "http://localhost:11434"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    default_timezone: str = "Asia/Singapore"
    log_level: str = "INFO"
    event_poll_seconds: float = 5
    fsm_storage: Literal["memory"] = "memory"
