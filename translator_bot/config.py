from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from croniter import croniter
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pymongo.uri_parser import parse_uri


ENV_PATTERN = re.compile(r"\$\{([A-ZA-Z0-9_]+)(?::-(.*?))?\}")


class TelegramSettings(BaseModel):
    api_id: int
    api_hash: str
    session_name: str = "translator_session"
    send_translations_to: int | str = "me"
    send_summaries_to_chat_id: int | str
    control_chat_id: int | str | None = None

    @field_validator("send_translations_to", "send_summaries_to_chat_id", "control_chat_id", mode="before")
    @classmethod
    def parse_numeric_destination(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
        return value


class OpenAISettings(BaseModel):
    api_key: str
    translation_model: str = "gpt-4.1-mini"
    summary_model: str = "gpt-4.1-mini"
    alert_model: str = "gpt-4.1-mini"


class MongoSettings(BaseModel):
    uri: str

    @property
    def database_name(self) -> str:
        parsed = parse_uri(self.uri)
        database = parsed.get("database")
        if not database:
            raise ValueError("MongoDB URI must include a database name")
        return database


class FeatureSettings(BaseModel):
    instant_translation: bool = True
    batch_translation: bool = True
    hourly_summary: bool = True
    daily_summary: bool = True
    original_plus_translation: bool = True
    translation_only_view: bool = False
    important_points: bool = True
    action_items: bool = True
    meeting_detection: bool = True
    unread_catchup: bool = True


class AlertSettings(BaseModel):
    name_mentions_enabled: bool = True
    names: list[str] = Field(default_factory=list)
    question_request_alert: bool = True
    urgency_alert: bool = True


class ChatSettings(BaseModel):
    id: int
    name: str | None = None
    enabled: bool = True
    instant_translation: bool = True
    summaries: bool = True
    important_only: bool = False
    muted: bool = False


class IgnoreSettings(BaseModel):
    users: set[int] = Field(default_factory=set)
    chats: set[int] = Field(default_factory=set)


class SummarySettings(BaseModel):
    hourly_cron: str = "0 * * * *"
    daily_cron: str = "0 21 * * *"
    timezone: str = "UTC"
    combined_summary: bool = True
    per_chat_summary: bool = True

    @field_validator("hourly_cron", "daily_cron")
    @classmethod
    def validate_cron(cls, value: str) -> str:
        if not croniter.is_valid(value):
            raise ValueError(f"Invalid cron expression: {value}")
        return value


class StorageSettings(BaseModel):
    auto_delete_after_days: int = 30


class RuntimeSettings(BaseModel):
    config_reload_enabled: bool = True
    command_prefix: str = "/"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telegram: TelegramSettings
    openai: OpenAISettings
    mongodb: MongoSettings
    features: FeatureSettings = Field(default_factory=FeatureSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    chats: list[ChatSettings] = Field(default_factory=list)
    ignore: IgnoreSettings = Field(default_factory=IgnoreSettings)
    summary: SummarySettings = Field(default_factory=SummarySettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)

    @model_validator(mode="after")
    def ensure_mongodb_database(self) -> Settings:
        self.mongodb.database_name
        return self

    @property
    def enabled_chat_ids(self) -> set[int]:
        return {chat.id for chat in self.chats if chat.enabled}

    @property
    def chat_map(self) -> dict[int, ChatSettings]:
        return {chat.id: chat for chat in self.chats}

    def chat_for(self, chat_id: int) -> ChatSettings | None:
        return self.chat_map.get(chat_id)


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            fallback = match.group(2)
            env_value = os.getenv(name)
            if env_value is not None:
                return env_value
            if fallback is not None:
                return fallback
            raise ValueError(f"Missing required environment variable: {name}")

        return ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def load_settings(config_path: str | Path = "config.yaml") -> Settings:
    load_dotenv()
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    raw = yaml.safe_load(path.read_text()) or {}
    expanded = _expand_env(raw)
    try:
        return Settings.model_validate(expanded)
    except ValidationError as exc:
        raise ValueError(f"Invalid config file {path}:\n{exc}") from exc
