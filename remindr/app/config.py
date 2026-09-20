from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str | None = None
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "care_companion"
    linq_api_base_url: str = "https://api.linqapp.com/api/partner/v3"
    linq_api_token: str | None = None
    linq_webhook_secret: str | None = None
    linq_send_max_attempts: int = 3
    default_timezone: str = "America/New_York"
    scheduler_poll_seconds: int = 30
    default_patient_chat_id: str | None = "c742f2a9-0e8e-4ed9-a6cb-bcc39fdc1001"
    default_caregiver_chat_id: str | None = "576d31b7-7599-42b0-9868-66c7124efad0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
