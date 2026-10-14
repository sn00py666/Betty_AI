from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.bonuses import BonusOffer

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class RankingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    valve_rankings_enabled: bool = True
    dota_opendota_enabled: bool = True
    dota_cyberscore_enabled: bool = True
    hltv_enabled: bool = True


class AISettings(RankingSettings):
    ai_api_key: SecretStr | None = None
    ai_base_url: str = "https://openrouter.ai/api/v1"
    ai_model: str = "google/gemini-2.5-flash-lite"
    ai_temperature: float = Field(default=0.35, ge=0, le=2)
    team_matching_enabled: bool = True


class Settings(AISettings):
    pandascore_api_key: SecretStr | None = None
    pandascore_daily_limit: int = Field(default=100, ge=0)

    admin_user_ids: list[int] = Field(default_factory=list)
    bonus_offers: list[BonusOffer] = Field(default_factory=list, max_length=6)
    custom_emoji_enabled: bool = True

    telegram_bot_token: SecretStr
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
