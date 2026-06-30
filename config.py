from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _parse_json_map(raw: str, field_name: str) -> dict[str, str]:
    raw = (raw or "").strip() or "{}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} 不是合法 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} 必须是 JSON object")
    return {str(k): str(v) for k, v in value.items()}


@dataclass(slots=True)
class Settings:
    bot_token: str
    admin_user_ids: set[int]
    api_base_url: str
    api_timeout_seconds: int
    api_auth_header_name: str
    api_auth_header_value: str
    api_auth_query_name: str
    api_auth_query_value: str
    api_extra_headers: dict[str, str]
    api_extra_query: dict[str, str]
    database_path: Path
    order_poll_seconds: int


def load_settings() -> Settings:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("BOT_TOKEN 未配置")

    admin_raw = os.getenv("ADMIN_USER_IDS", "").strip()
    admin_user_ids = {
        int(part.strip())
        for part in admin_raw.split(",")
        if part.strip()
    }

    database_path = Path(os.getenv("DATABASE_PATH", "data/apibot.db")).resolve()

    return Settings(
        bot_token=bot_token,
        admin_user_ids=admin_user_ids,
        api_base_url=os.getenv("API_BASE_URL", "https://onlinestore-fx-api.add4533.com").rstrip("/"),
        api_timeout_seconds=int(os.getenv("API_TIMEOUT_SECONDS", "20")),
        api_auth_header_name=os.getenv("API_AUTH_HEADER_NAME", "").strip(),
        api_auth_header_value=os.getenv("API_AUTH_HEADER_VALUE", "").strip(),
        api_auth_query_name=os.getenv("API_AUTH_QUERY_NAME", "").strip(),
        api_auth_query_value=os.getenv("API_AUTH_QUERY_VALUE", "").strip(),
        api_extra_headers=_parse_json_map(os.getenv("API_EXTRA_HEADERS_JSON", "{}"), "API_EXTRA_HEADERS_JSON"),
        api_extra_query=_parse_json_map(os.getenv("API_EXTRA_QUERY_JSON", "{}"), "API_EXTRA_QUERY_JSON"),
        database_path=database_path,
        order_poll_seconds=max(10, int(os.getenv("ORDER_POLL_SECONDS", "20"))),
    )

