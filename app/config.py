"""
app/config.py — centralized environment config via pydantic-settings.

Fix applied: optional fields (gemini_api_key, api_key, failure_injection_seed)
use field_validator(mode="before") to convert an empty string ("") coming
from a .env file into None before Pydantic tries to type-check it. Without
this, a line like `FAILURE_INJECTION_SEED=` (present but blank, which is a
completely normal thing to leave blank in a .env file) crashes on startup
with "Input should be a valid integer, unable to parse string as an
integer" - Pydantic sees the empty string as a value to validate, not as
"unset." Only truly *absent* env vars were previously treated as None.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.0-flash"
    llm_timeout_seconds: float = 8.0

    database_path: str = "data/events.db"

    api_key: str | None = None  # optional single API key check for non-UI endpoints

    log_level: str = "INFO"

    failure_injection_seed: int | None = None  # set for reproducible demo runs

    @field_validator("gemini_api_key", "api_key", "failure_injection_seed", mode="before")
    @classmethod
    def _blank_string_means_unset(cls, value):
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


settings = Settings()