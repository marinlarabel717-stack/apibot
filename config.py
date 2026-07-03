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
    restock_channel: str
    customer_service_contact: str
    okpay_shop_id: str
    okpay_shop_token: str
    okpay_name: str
    okpay_callback_url: str
    okpay_api_url: str
    okpay_callback_host: str
    okpay_callback_port: int
    okpay_request_timeout: int
    okpay_poll_seconds: int
    okpay_poll_limit: int
    okpay_poll_concurrency: int
    trongrid_api_base: str
    trongrid_api_key: str
    trongrid_api_keys: str
    trongrid_request_timeout: int
    trongrid_poll_seconds: int
    trongrid_page_limit: int
    trongrid_max_pages: int
    trongrid_lookback_minutes: int
    trc20_usdt_contract: str
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
    order_poll_first_seconds: int
    order_poll_limit: int
    order_poll_concurrency: int
    order_fast_probe_seconds: int
    delivery_retry_cooldown_seconds: int
    telegram_media_write_timeout_seconds: int
    telegram_media_read_timeout_seconds: int


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
        restock_channel=os.getenv("RESTOCK_CHANNEL", "@xxx").strip() or "@xxx",
        customer_service_contact=os.getenv("CUSTOMER_SERVICE_CONTACT", "@id2uu").strip() or "@id2uu",
        okpay_shop_id=os.getenv("OKPAY_SHOP_ID", "").strip(),
        okpay_shop_token=os.getenv("OKPAY_SHOP_TOKEN", "").strip(),
        okpay_name=os.getenv("OKPAY_NAME", "号铺").strip() or "号铺",
        okpay_callback_url=os.getenv("OKPAY_CALLBACK_URL", "").strip(),
        okpay_api_url=os.getenv("OKPAY_API_URL", "https://api.okaypay.me/shop").strip().rstrip("/"),
        okpay_callback_host=os.getenv("OKPAY_CALLBACK_HOST", "0.0.0.0").strip() or "0.0.0.0",
        okpay_callback_port=int(os.getenv("OKPAY_CALLBACK_PORT", "8088")),
        okpay_request_timeout=max(5, int(os.getenv("OKPAY_REQUEST_TIMEOUT", "12"))),
        okpay_poll_seconds=max(3, int(os.getenv("OKPAY_POLL_SECONDS", "4"))),
        okpay_poll_limit=max(10, int(os.getenv("OKPAY_POLL_LIMIT", "50"))),
        okpay_poll_concurrency=max(1, int(os.getenv("OKPAY_POLL_CONCURRENCY", "4"))),
        trongrid_api_base=os.getenv("TRONGRID_API_BASE", "https://api.trongrid.io/v1").strip().rstrip("/"),
        trongrid_api_key=os.getenv("TRONGRID_API_KEY", "").strip(),
        trongrid_api_keys=os.getenv("TRONGRID_API_KEYS", "").strip(),
        trongrid_request_timeout=max(5, int(os.getenv("TRONGRID_REQUEST_TIMEOUT", "20"))),
        trongrid_poll_seconds=max(3, int(os.getenv("TRONGRID_POLL_SECONDS", "6"))),
        trongrid_page_limit=max(1, min(int(os.getenv("TRONGRID_PAGE_LIMIT", "100")), 200)),
        trongrid_max_pages=max(1, int(os.getenv("TRONGRID_MAX_PAGES", "20"))),
        trongrid_lookback_minutes=max(1, int(os.getenv("TRONGRID_LOOKBACK_MINUTES", "30"))),
        trc20_usdt_contract=os.getenv("TRC20_USDT_CONTRACT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t").strip(),
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
        order_poll_seconds=max(3, int(os.getenv("ORDER_POLL_SECONDS", "5"))),
        order_poll_first_seconds=max(1, int(os.getenv("ORDER_POLL_FIRST_SECONDS", "2"))),
        order_poll_limit=max(10, int(os.getenv("ORDER_POLL_LIMIT", "100"))),
        order_poll_concurrency=max(1, int(os.getenv("ORDER_POLL_CONCURRENCY", "8"))),
        order_fast_probe_seconds=max(1, int(os.getenv("ORDER_FAST_PROBE_SECONDS", "2"))),
        delivery_retry_cooldown_seconds=max(5, int(os.getenv("DELIVERY_RETRY_COOLDOWN_SECONDS", "60"))),
        telegram_media_write_timeout_seconds=max(20, int(os.getenv("TELEGRAM_MEDIA_WRITE_TIMEOUT_SECONDS", "180"))),
        telegram_media_read_timeout_seconds=max(20, int(os.getenv("TELEGRAM_MEDIA_READ_TIMEOUT_SECONDS", "120"))),
    )
