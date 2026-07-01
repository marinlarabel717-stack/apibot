from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def _parse_json_map(raw: str, field_name: str) -> dict[str, str]:
    raw = (raw or "").strip() or "{}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return {str(k): str(v) for k, v in value.items()}


def _parse_float(raw: str, field_name: str, default: float) -> float:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid number: {raw}") from exc


def _parse_price_rules(raw: str, field_name: str) -> list[dict[str, Any]]:
    raw = (raw or "").strip() or "{}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object")

    rules: list[dict[str, Any]] = []
    for keyword, rule_value in value.items():
        entry: dict[str, Any] = {
            "keyword": str(keyword).strip(),
            "add": None,
            "multiplier": None,
        }
        if not entry["keyword"]:
            continue
        if isinstance(rule_value, (int, float)):
            entry["add"] = float(rule_value)
        elif isinstance(rule_value, dict):
            if "multiplier" in rule_value and rule_value["multiplier"] is not None:
                entry["multiplier"] = float(rule_value["multiplier"])
            if "add" in rule_value and rule_value["add"] is not None:
                entry["add"] = float(rule_value["add"])
        else:
            raise ValueError(f"{field_name} rule for {keyword} must be a number or object")
        rules.append(entry)
    return rules


@dataclass(slots=True)
class Settings:
    bot_token: str
    admin_user_ids: set[int]
    shop_title: str
    restock_channel: str
    customer_service_contact: str
    recharge_text: str
    sell_price_add: float
    sell_price_rules: list[dict[str, Any]]
    inline_button_custom_emoji_enabled: bool
    button_custom_emoji_ids: dict[str, str]
    api_base_url: str
    api_timeout_seconds: int
    api_auth_header_name: str
    api_auth_header_value: str
    api_auth_try_bearer_variants: bool
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
        raise ValueError("BOT_TOKEN not configured")

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
        shop_title=os.getenv("SHOP_TITLE", "TG-Matrix 账号商城").strip() or "TG-Matrix 账号商城",
        restock_channel=os.getenv("RESTOCK_CHANNEL", "@xxx").strip() or "@xxx",
        customer_service_contact=os.getenv("CUSTOMER_SERVICE_CONTACT", "@id2uu").strip() or "@id2uu",
        recharge_text=os.getenv(
            "RECHARGE_TEXT",
            "请联系管理员充值，或者让管理员使用 /add 给你调整余额。",
        ).strip(),
        sell_price_add=_parse_float(os.getenv("SELL_PRICE_ADD", "0"), "SELL_PRICE_ADD", 0.0),
        sell_price_rules=_parse_price_rules(os.getenv("SELL_PRICE_RULES_JSON", "{}"), "SELL_PRICE_RULES_JSON"),
        inline_button_custom_emoji_enabled=os.getenv("INLINE_BUTTON_CUSTOM_EMOJI_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        button_custom_emoji_ids=_parse_json_map(os.getenv("BUTTON_CUSTOM_EMOJI_IDS_JSON", "{}"), "BUTTON_CUSTOM_EMOJI_IDS_JSON"),
        api_base_url=os.getenv("API_BASE_URL", "https://onlinestore-fx-api.add4533.com").rstrip("/"),
        api_timeout_seconds=int(os.getenv("API_TIMEOUT_SECONDS", "20")),
        api_auth_header_name=os.getenv("API_AUTH_HEADER_NAME", "").strip(),
        api_auth_header_value=os.getenv("API_AUTH_HEADER_VALUE", "").strip(),
        api_auth_try_bearer_variants=os.getenv("API_AUTH_TRY_BEARER_VARIANTS", "true").strip().lower() not in {"0", "false", "no", "off"},
        api_auth_query_name=os.getenv("API_AUTH_QUERY_NAME", "").strip(),
        api_auth_query_value=os.getenv("API_AUTH_QUERY_VALUE", "").strip(),
        api_extra_headers=_parse_json_map(os.getenv("API_EXTRA_HEADERS_JSON", "{}"), "API_EXTRA_HEADERS_JSON"),
        api_extra_query=_parse_json_map(os.getenv("API_EXTRA_QUERY_JSON", "{}"), "API_EXTRA_QUERY_JSON"),
        database_path=database_path,
        order_poll_seconds=max(10, int(os.getenv("ORDER_POLL_SECONDS", "20"))),
    )
