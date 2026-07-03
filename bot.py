from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import logging
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
try:
    import qrcode
except ModuleNotFoundError:
    qrcode = None

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MessageEntity,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import KeyboardButtonStyle
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Settings, load_settings
from store import Store
from supplier_client import SupplierApiError, SupplierClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("apibot")
OKPAY_HTTP_LOCAL = threading.local()


PRODUCTS_PER_PAGE = 8
SEARCH_RESULTS_LIMIT = 8
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
PURCHASE_CONFIRM_IMAGE_PATH = ASSETS_DIR / "purchase-confirm.png"
DELIVERY_READY_IMAGE_PATH = ASSETS_DIR / "delivery-ready.png"
START_MENU_IMAGE_PATH = ASSETS_DIR / "start-menu.png"
LEGACY_START_MENU_IMAGE_PATH = PURCHASE_CONFIRM_IMAGE_PATH
DELIVERY_FILES_DIR = Path(__file__).resolve().parent / "data" / "deliveries"
BUTTON_ICON_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("vip", "会员"), "💎", "vip"),
    (("spam",), "⚠️", "spam"),
    (("靓号",), "✨", "liang"),
    (("亚洲", "东南亚", "亚区", "日本", "韩国", "香港", "台湾", "菲律宾", "印尼", "越南", "泰国", "马来西亚", "新加坡", "印度"), "🌏", "asia"),
    (("欧美", "欧洲", "美洲", "美国", "英国", "德国", "法国", "加拿大", "澳洲"), "🌎", "west"),
    (("非洲", "南非", "尼日利亚", "埃及", "摩洛哥", "肯尼亚"), "🦁", "africa"),
    (("2-5", "2~5", "2-5天", "2~5天", "2至5天"), "🌱", "age_2_5"),
    (("6-12", "6~12", "6-12天", "6~12天", "6至12天"), "⭐", "age_6_12"),
    (("1-2年", "1-2 年", "1~2年", "1~2 年", "13-24月", "12-24月"), "💠", "age_1_2y"),
    (("3-4年", "3-4 年", "3~4年", "3~4 年", "36-48月"), "🔮", "age_3_4y"),
    (("5年以上", "5年", "5+年", "60月"), "👑", "age_5y"),
    (("7年以上", "7年", "7+年", "84月"), "🏆", "age_7y"),
]
BUTTON_PRODUCTS = "商品列表"
BUTTON_MAIN_MENU = "主菜单"
BUTTON_PROFILE = "个人中心"
BUTTON_RECHARGE = "我要充值"
BUTTON_ACCOUNT_LIST = "账号列表"
BUTTON_RECHARGE_BALANCE = "充值余额"
BUTTON_PURCHASE_NOTICE = "购买须知"
BUTTON_ORDER_HISTORY = "购买记录"
BUTTON_SWITCH_LANGUAGE = "切换语言"
BOTTOM_BUTTON_MAIN_MENU = "主菜单"
BOTTOM_BUTTON_CUSTOMER_SERVICE = "联系客服"
BOTTOM_BUTTON_RECHARGE_BALANCE = "充值余额"
LEGACY_BOTTOM_BUTTON_MAIN_MENU = "🏠主菜单"
LEGACY_BOTTOM_BUTTON_CUSTOMER_SERVICE = "☎️ 联系客服"
LEGACY_BOTTOM_BUTTON_RECHARGE_BALANCE = "💰充值余额"
BOTTOM_BUTTON_HOME_EMOJI_ID = "6334492495723890409"
BOTTOM_BUTTON_CUSTOMER_SERVICE_EMOJI_ID = "6334344946417404152"
BOTTOM_BUTTON_RECHARGE_EMOJI_ID = "6334575946938451719"
MENU_BUTTON_TEXTS = {
    BOTTOM_BUTTON_MAIN_MENU,
    BOTTOM_BUTTON_CUSTOMER_SERVICE,
    BOTTOM_BUTTON_RECHARGE_BALANCE,
    LEGACY_BOTTOM_BUTTON_MAIN_MENU,
    LEGACY_BOTTOM_BUTTON_CUSTOMER_SERVICE,
    LEGACY_BOTTOM_BUTTON_RECHARGE_BALANCE,
}
LEGACY_MENU_BUTTON_TEXTS = {
    BUTTON_ACCOUNT_LIST,
    BUTTON_RECHARGE_BALANCE,
    BUTTON_PURCHASE_NOTICE,
    BUTTON_ORDER_HISTORY,
    BUTTON_SWITCH_LANGUAGE,
}
NON_SEARCH_BUTTON_TEXTS = MENU_BUTTON_TEXTS | LEGACY_MENU_BUTTON_TEXTS | {
    BUTTON_PRODUCTS,
    BUTTON_MAIN_MENU,
    BUTTON_PROFILE,
    BUTTON_RECHARGE,
}
SEARCH_COUNTRY_KEYWORDS = {
    "中国", "香港", "澳门", "台湾",
    "日本", "韩国", "朝鲜", "蒙古",
    "越南", "泰国", "老挝", "柬埔寨", "缅甸",
    "马来西亚", "新加坡", "印尼", "印度尼西亚", "菲律宾", "文莱", "东帝汶",
    "印度", "巴基斯坦", "孟加拉", "尼泊尔", "斯里兰卡", "不丹", "马尔代夫",
    "哈萨克斯坦", "乌兹别克斯坦", "土库曼斯坦", "吉尔吉斯斯坦", "塔吉克斯坦",
    "阿联酋", "迪拜", "沙特", "沙特阿拉伯", "卡塔尔", "科威特", "阿曼", "巴林", "也门",
    "伊朗", "伊拉克", "叙利亚", "约旦", "黎巴嫩", "以色列", "巴勒斯坦", "土耳其",
    "埃及", "阿尔及利亚", "摩洛哥", "突尼斯", "利比亚", "苏丹",
    "尼日利亚", "加纳", "肯尼亚", "乌干达", "坦桑尼亚", "埃塞俄比亚", "卢旺达",
    "南非", "赞比亚", "津巴布韦", "安哥拉", "喀麦隆", "科特迪瓦", "塞内加尔",
    "美国", "加拿大", "墨西哥", "巴西", "阿根廷", "智利", "哥伦比亚", "秘鲁",
    "委内瑞拉", "玻利维亚", "巴拉圭", "乌拉圭", "厄瓜多尔", "巴拿马", "哥斯达黎加",
    "英国", "爱尔兰", "法国", "德国", "意大利", "西班牙", "葡萄牙", "荷兰", "比利时",
    "瑞士", "奥地利", "波兰", "捷克", "匈牙利", "罗马尼亚", "希腊", "瑞典", "挪威",
    "芬兰", "丹麦", "冰岛", "乌克兰", "俄罗斯", "白俄罗斯",
    "澳洲", "澳大利亚", "新西兰",
}

MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        [
            KeyboardButton(
                BOTTOM_BUTTON_MAIN_MENU,
                icon_custom_emoji_id=BOTTOM_BUTTON_HOME_EMOJI_ID,
                style=KeyboardButtonStyle.PRIMARY,
            ),
            KeyboardButton(
                BOTTOM_BUTTON_CUSTOMER_SERVICE,
                icon_custom_emoji_id=BOTTOM_BUTTON_CUSTOMER_SERVICE_EMOJI_ID,
                style=KeyboardButtonStyle.DANGER,
            ),
            KeyboardButton(
                BOTTOM_BUTTON_RECHARGE_BALANCE,
                icon_custom_emoji_id=BOTTOM_BUTTON_RECHARGE_EMOJI_ID,
                style=KeyboardButtonStyle.SUCCESS,
            ),
        ],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

PENDING_PURCHASE_KEY = "pending_purchase_quantity"
PENDING_RECHARGE_KEY = "pending_recharge_amount"
PENDING_ADMIN_KEY = "pending_admin_action"
ADMIN_USERS_PAGE_SIZE = 8
ADMIN_SEND_SCOPE_SINGLE = "single"
ADMIN_SEND_SCOPE_ALL = "all"
RUNTIME_KEY_RECHARGE_ADDRESS = "recharge_address"
RUNTIME_KEY_OKPAY_CONFIG = "okpay_config"
RUNTIME_KEY_OKPAY_SHOP_ID = "okpay_shop_id"
RUNTIME_KEY_OKPAY_SHOP_TOKEN = "okpay_shop_token"
RUNTIME_KEY_OKPAY_NAME = "okpay_name"
RUNTIME_KEY_OKPAY_CALLBACK_URL = "okpay_callback_url"
RUNTIME_KEY_OKPAY_API_URL = "okpay_api_url"
RUNTIME_KEY_CUSTOMER_SERVICE = "customer_service_contact"
RUNTIME_KEY_RESTOCK_CHANNEL = "restock_channel"
RUNTIME_KEY_BUSINESS_STATUS = "business_status"
START_MENU_EMOJI_USDT_ID = "6334575946938451719"
START_MENU_EMOJI_SPENT_ID = "6334456344984159861"
START_MENU_EMOJI_QUANTITY_ID = "6334602442591700514"
START_MENU_EMOJI_RESTOCK_ID = "6334740096293537039"
START_MENU_EMOJI_SUPPORT_ID = "6334344946417404152"
MAIN_MENU_EMOJI_ACCOUNT_LIST_ID = "5875462364110787088"
MAIN_MENU_EMOJI_RECHARGE_BALANCE_ID = "5987880246865565644"
MAIN_MENU_EMOJI_PURCHASE_NOTICE_ID = "5258328383183396223"
MAIN_MENU_EMOJI_ORDER_HISTORY_ID = "5258134813302332906"
MAIN_MENU_EMOJI_SWITCH_LANGUAGE_ID = "5879585266426973039"
CATEGORY_LIST_EMOJI_ID = "6334677956706698772"
ALERT_EMOJI_ID = "5775887550262546277"
HOME_EMOJI_ID = "5967822972931542886"
BUYING_EMOJI_ID = "5776375003280838798"
PRICE_EMOJI_ID = "5897958754267174109"
STOCK_EMOJI_ID = "5875291072225087249"
BUY_BUTTON_EMOJI_ID = "5985596818912712352"
BACK_EMOJI_ID = "5875082500023258804"
PRODUCT_EMOJI_ID = "6334767047213319650"
UNIT_PRICE_EMOJI_ID = "6334793031765460638"
ITEM_COUNT_EMOJI_ID = "5278330174729907327"
TOTAL_DUE_EMOJI_ID = "5204242830687494041"
PACKED_DONE_EMOJI_ID = "6323524880121726602"
PRODUCT_LIST_EMOJI_ID = "6334767047213319650"
PRODUCT_LIST_ALERT_EMOJI_ID = "6323546926188857158"
SEARCH_RESULTS_EMOJI_ID = "6332075107741075109"
CLOSE_EMOJI_ID = "6323186419518932861"
RECENT_ORDERS_EMOJI_ID = "5278660453419996132"
ORDER_CREATED_EMOJI_ID = "6323523703300688017"
CUSTOMER_SERVICE_EMOJI_ID = "6334344946417404152"
BALANCE_NOTICE_TITLE_EMOJI_ID = "6321283126236552928"
BALANCE_NOTICE_INCREASE_EMOJI_ID = "6320894118163651482"
BALANCE_NOTICE_REFUND_EMOJI_ID = "6321175945327680365"
BALANCE_NOTICE_CURRENT_EMOJI_ID = "6323372022235667141"
ADMIN_ADD_BALANCE_TITLE_EMOJI_ID = "6321041414067068140"
ADMIN_ADD_BALANCE_USER_EMOJI_ID = "6273676592036191055"
ADMIN_ADD_BALANCE_INCREASE_EMOJI_ID = "6320823470246600333"
SYSTEM_ERROR_EMOJI_ID = "6321241559543062538"
ADMIN_NEW_ORDER_TITLE_EMOJI_ID = "5994502837327892086"
ADMIN_NEW_ORDER_USER_EMOJI_ID = "5886412370347036129"
ADMIN_NEW_ORDER_USER_ID_EMOJI_ID = "5771887475421090729"
ADMIN_NEW_ORDER_PRODUCT_EMOJI_ID = "5985472565508838112"
ADMIN_NEW_ORDER_QUANTITY_EMOJI_ID = "5877485980901971030"
ADMIN_NEW_ORDER_AMOUNT_EMOJI_ID = "5931546553868095844"
ADMIN_NEW_ORDER_BALANCE_EMOJI_ID = "5992430854909989581"
CATEGORY_BUTTON_EMOJI_IDS: dict[str, str] = {
    "asia": "6334321852378252986",
    "west": "6334717028024190508",
    "africa": "6334806079876106286",
    "age_2_5": "6323503680163153903",
    "age_6_12": "6323427942709856876",
    "age_1_2y": "6321332501180581681",
    "age_3_4y": "6323443194138723748",
    "age_5y": "6323526692597925524",
    "age_7y": "6334710044407368265",
    "vip": "6334875048460944921",
    "liang": "6334508275433735767",
    "spam": "6323249027257206448",
}


def format_money(value: float) -> str:
    return f"{value:.2f}"


def is_admin(settings: Settings, user_id: int) -> bool:
    return int(user_id) in settings.admin_user_ids


def shorten(text: str, limit: int) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 1]}…"


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_search_keyword(value: str) -> str:
    return " ".join(str(value or "").split())


def should_trigger_product_search(keyword: str) -> bool:
    normalized = normalize_search_keyword(keyword)
    if not normalized or normalized in NON_SEARCH_BUTTON_TEXTS:
        return False

    compact = normalized.replace(" ", "")
    if re.fullmatch(r"\+\d{1,4}(?:[^\d].*)?", compact):
        return True

    return any(compact.startswith(country) for country in SEARCH_COUNTRY_KEYWORDS)


def get_pending_admin_action(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    pending = context.user_data.get(PENDING_ADMIN_KEY)
    return pending if isinstance(pending, dict) else None


def set_pending_admin_action(context: ContextTypes.DEFAULT_TYPE, pending: dict[str, Any]) -> None:
    context.user_data[PENDING_ADMIN_KEY] = pending


def clear_pending_admin_action(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(PENDING_ADMIN_KEY, None)


def get_or_create_admin_broadcast_draft(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    pending = get_pending_admin_action(context)
    if not isinstance(pending, dict) or str(pending.get("scope") or "") != ADMIN_SEND_SCOPE_ALL:
        pending = {"kind": "broadcast_idle", "scope": ADMIN_SEND_SCOPE_ALL, "payload": {}}
    payload = pending.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    pending["payload"] = {
        "content_type": str(payload.get("content_type") or "text"),
        "photo_file_id": str(payload.get("photo_file_id") or ""),
        "text": str(payload.get("text") or ""),
        "button_text": str(payload.get("button_text") or ""),
        "button_url": str(payload.get("button_url") or ""),
    }
    set_pending_admin_action(context, pending)
    return pending


def get_runtime_config(context: ContextTypes.DEFAULT_TYPE) -> dict[str, str]:
    return context.application.bot_data.setdefault("runtime_config", {})


def runtime_value(context: ContextTypes.DEFAULT_TYPE, key: str, default: str = "") -> str:
    return str(get_runtime_config(context).get(key) or default or "")


def effective_customer_service_contact(context: ContextTypes.DEFAULT_TYPE, settings: Settings) -> str:
    return runtime_value(context, RUNTIME_KEY_CUSTOMER_SERVICE, settings.customer_service_contact)


def effective_restock_channel(context: ContextTypes.DEFAULT_TYPE, settings: Settings) -> str:
    return runtime_value(context, RUNTIME_KEY_RESTOCK_CHANNEL, settings.restock_channel)


def effective_recharge_address(context: ContextTypes.DEFAULT_TYPE) -> str:
    return runtime_value(context, RUNTIME_KEY_RECHARGE_ADDRESS, "")


def business_status_is_open(raw_value: str) -> bool:
    return str(raw_value or "1").strip().lower() not in {"0", "false", "off", "closed", "stop"}


def effective_business_open(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return business_status_is_open(runtime_value(context, RUNTIME_KEY_BUSINESS_STATUS, "1"))


def business_status_label(context: ContextTypes.DEFAULT_TYPE) -> str:
    return "营业中" if effective_business_open(context) else "已停止"


def effective_okpay_config(context: ContextTypes.DEFAULT_TYPE) -> str:
    return runtime_value(context, RUNTIME_KEY_OKPAY_CONFIG, "")


def parse_okpay_config_text(raw: str) -> dict[str, str]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        result: dict[str, str] = {}
        for key in ("shop_id", "shop_token", "name", "callback_url", "api_url"):
            value = payload.get(key)
            if value is not None and str(value).strip():
                result[key] = str(value).strip()
        return result

    result: dict[str, str] = {}
    alias_map = {
        "shop_id": "shop_id",
        "shopid": "shop_id",
        "app id": "shop_id",
        "appid": "shop_id",
        "id": "shop_id",
        "merchant_id": "shop_id",
        "merchantid": "shop_id",
        "okpay_shop_id": "shop_id",
        "商户id": "shop_id",
        "商户号": "shop_id",
        "shop_token": "shop_token",
        "shoptoken": "shop_token",
        "token": "shop_token",
        "key": "shop_token",
        "secret": "shop_token",
        "app secret": "shop_token",
        "okpay_token": "shop_token",
        "okpay_shop_token": "shop_token",
        "商户token": "shop_token",
        "token值": "shop_token",
        "密钥": "shop_token",
        "name": "name",
        "okpay_name": "name",
        "名称": "name",
        "callback_url": "callback_url",
        "callback": "callback_url",
        "okpay_callback_url": "callback_url",
        "回调地址": "callback_url",
        "api_url": "api_url",
        "okpay_api_url": "api_url",
        "接口地址": "api_url",
    }

    def clean_cell(value: str) -> str:
        cleaned = str(value or "").strip()
        cleaned = cleaned.replace("**", "").replace("__", "")
        cleaned = cleaned.strip("`").strip()
        return cleaned

    pending_key: str | None = None
    in_code_block = False
    for raw_line in text.splitlines():
        if str(raw_line).strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        line = clean_cell(raw_line)
        if not line:
            continue
        if pending_key and in_code_block:
            result[pending_key] = line
            pending_key = None
            continue
        if in_code_block and "shop_token" not in result and re.fullmatch(r"[A-Za-z0-9_-]{16,}", line):
            result["shop_token"] = line
            continue
        if pending_key and line:
            result[pending_key] = line
            pending_key = None
            continue

        delimiter = None
        if "=" in line:
            delimiter = "="
        elif "：" in line:
            delimiter = "："
        elif ":" in line:
            delimiter = ":"
        if delimiter is None:
            continue

        key, value = line.split(delimiter, 1)
        normalized = alias_map.get(clean_cell(key).lower())
        if not normalized:
            continue
        normalized_value = clean_cell(value)
        if not normalized_value or normalized_value.lower() in {"未设置", "none", "null", "-"}:
            if normalized == "shop_token":
                pending_key = normalized
            continue
        result[normalized] = normalized_value
    return result

def summarize_okpay_config(config: dict[str, str]) -> str:
    shop_id = config.get("shop_id", "")
    token = config.get("shop_token", "")
    callback_url = config.get("callback_url", "")
    api_url = config.get("api_url", "")
    name = config.get("name", "")
    enabled = "已开启" if shop_id and token else "未开启"
    masked_token = token[:6] + "******" + token[-4:] if len(token) >= 12 else ("已配置" if token else "未配置")
    return (
        f"商户ID：{shop_id or '未配置'}\n"
        f"Token：{masked_token}\n"
        f"名称：{name or '未配置'}\n"
        f"回调地址：{callback_url or '未配置'}\n"
        f"API地址：{api_url or '未配置'}\n"
        f"充值入口：{enabled}"
    )


def build_okpay_config_text(config: dict[str, str]) -> str:
    return (
        "OKPAY 配置\n\n"
        f"{summarize_okpay_config(config)}\n\n"
        "后台改这里就行，不需要每次再去改 env。"
    )


def build_okpay_config_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("设置商户ID", callback_data="adm:set:okid"),
                InlineKeyboardButton("设置Token", callback_data="adm:set:oktoken"),
            ],
            [
                InlineKeyboardButton("设置名称", callback_data="adm:set:okname"),
                InlineKeyboardButton("设置回调地址", callback_data="adm:set:okcallback"),
            ],
            [
                InlineKeyboardButton("设置API地址", callback_data="adm:set:okapi"),
                InlineKeyboardButton("整段配置", callback_data="adm:set:okpay"),
            ],
            [InlineKeyboardButton("返回后台", callback_data="adm:home")],
        ]
    )


def dump_okpay_config_text(config: dict[str, str]) -> str:
    lines: list[str] = []
    for key in ("shop_id", "shop_token", "name", "callback_url", "api_url"):
        value = str(config.get(key) or "").strip()
        if value:
            lines.append(f"{key}={value}")
    return "\n".join(lines)


def update_okpay_runtime_config(existing_raw: str, settings: Settings, field: str, value: str) -> str:
    config = resolve_okpay_settings({RUNTIME_KEY_OKPAY_CONFIG: existing_raw}, settings)
    updated = {
        "shop_id": str(config.get("shop_id") or "").strip(),
        "shop_token": str(config.get("shop_token") or "").strip(),
        "name": str(config.get("name") or "").strip(),
        "callback_url": str(config.get("callback_url") or "").strip(),
        "api_url": str(config.get("api_url") or "").strip(),
    }
    updated[field] = str(value or "").strip()
    return dump_okpay_config_text(updated)


def resolve_okpay_settings(runtime_config: dict[str, str], settings: Settings) -> dict[str, str]:
    parsed = parse_okpay_config_text(str(runtime_config.get(RUNTIME_KEY_OKPAY_CONFIG) or ""))
    legacy_shop_id = str(runtime_config.get(RUNTIME_KEY_OKPAY_SHOP_ID) or "").strip()
    legacy_shop_token = str(runtime_config.get(RUNTIME_KEY_OKPAY_SHOP_TOKEN) or "").strip()
    legacy_name = str(runtime_config.get(RUNTIME_KEY_OKPAY_NAME) or "").strip()
    legacy_callback_url = str(runtime_config.get(RUNTIME_KEY_OKPAY_CALLBACK_URL) or "").strip()

    # Prefer the unified OKPAY config block when present so stale legacy fields
    # do not silently override newly saved shop credentials.
    if parsed:
        shop_id = parsed.get("shop_id", "") or settings.okpay_shop_id
        shop_token = parsed.get("shop_token", "") or settings.okpay_shop_token
        name = parsed.get("name", "") or settings.okpay_name
        callback_url = parsed.get("callback_url", "") or settings.okpay_callback_url
    else:
        shop_id = legacy_shop_id or settings.okpay_shop_id
        shop_token = legacy_shop_token or settings.okpay_shop_token
        name = legacy_name or settings.okpay_name
        callback_url = legacy_callback_url or settings.okpay_callback_url
    api_url = parsed.get("api_url", "") or settings.okpay_api_url
    return {
        "shop_id": str(shop_id or "").strip(),
        "shop_token": str(shop_token or "").strip(),
        "name": str(name or "").strip(),
        "callback_url": str(callback_url or "").strip(),
        "api_url": str(api_url or "").strip().rstrip("/"),
        "callback_host": str(settings.okpay_callback_host or "").strip(),
        "callback_port": str(settings.okpay_callback_port),
        "request_timeout": str(settings.okpay_request_timeout),
        "create_timeout": str(settings.okpay_create_timeout),
    }


def effective_okpay_settings(context: ContextTypes.DEFAULT_TYPE, settings: Settings) -> dict[str, str]:
    return resolve_okpay_settings(get_runtime_config(context), settings)


def okpay_enabled(config: dict[str, str]) -> bool:
    return bool(config.get("shop_id") and config.get("shop_token"))


def build_trongrid_headers(api_key: str = "") -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["TRON-PRO-API-KEY"] = api_key
    return headers


def fetch_trongrid_transactions(
    settings: Settings,
    recharge_address: str,
    min_timestamp: int,
    api_keys: list[str],
) -> list[dict[str, Any]]:
    url = f"{settings.trongrid_api_base}/accounts/{recharge_address}/transactions/trc20"
    params = {
        "only_confirmed": "true",
        "only_to": "true",
        "limit": str(settings.trongrid_page_limit),
        "order_by": "block_timestamp,desc",
        "min_timestamp": str(max(0, int(min_timestamp))),
    }
    if settings.trc20_usdt_contract:
        params["contract_address"] = settings.trc20_usdt_contract

    candidates = api_keys or [""]
    for api_key in candidates:
        items: list[dict[str, Any]] = []
        fingerprint = ""
        page_count = 0
        seen_fingerprints: set[str] = set()
        while True:
            page_count += 1
            if page_count > settings.trongrid_max_pages:
                break
            request_params = dict(params)
            if fingerprint:
                if fingerprint in seen_fingerprints:
                    break
                seen_fingerprints.add(fingerprint)
                request_params["fingerprint"] = fingerprint
            response = requests.get(
                url,
                params=request_params,
                headers=build_trongrid_headers(api_key),
                timeout=settings.trongrid_request_timeout,
            )
            if response.status_code in {403, 429}:
                items = []
                break
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") or []
            if not isinstance(data, list):
                data = []
            items.extend(item for item in data if isinstance(item, dict))
            fingerprint = str(((payload.get("meta") or {}).get("fingerprint")) or "").strip()
            if not fingerprint or not data:
                return items
        if items:
            return items
    return []


def normalize_trc20_transfer(item: dict[str, Any], recharge_address: str, contract_address: str) -> dict[str, Any] | None:
    txid = str(item.get("transaction_id") or item.get("transactionId") or item.get("id") or "").strip()
    if not txid or item.get("confirmed") is False:
        return None
    event_type = str(item.get("type") or item.get("event_type") or "").strip().lower()
    if any(keyword in event_type for keyword in ("approve", "approval", "authorize", "authorization")):
        return None
    if event_type and "transfer" not in event_type:
        return None
    result = str(item.get("result") or item.get("transaction_result") or "").strip().upper()
    if result and result not in {"SUCCESS", "SUCESS"}:
        return None
    token_info = item.get("token_info") if isinstance(item.get("token_info"), dict) else {}
    to_address = str(item.get("to") or item.get("to_address") or "").strip()
    from_address = str(item.get("from") or item.get("from_address") or "").strip()
    if from_address and from_address == to_address:
        return None
    token_address = str(token_info.get("address") or item.get("contract_address") or "").strip()
    token_symbol = str(token_info.get("symbol") or item.get("tokenName") or "USDT").strip().upper()
    if contract_address and token_address and token_address != contract_address:
        return None
    if token_symbol and token_symbol != "USDT":
        return None
    if to_address != recharge_address or not from_address:
        return None
    decimals = safe_int(token_info.get("decimals"), 6)
    raw_value = Decimal(str(item.get("value") or "0"))
    amount = raw_value / (Decimal(10) ** max(0, decimals))
    amount = amount.quantize(Decimal("0.0001"))
    if amount <= 0:
        return None
    return {
        "txid": txid,
        "to_address": to_address,
        "from_address": from_address,
        "amount": float(amount),
        "amount_text": format_trc20_amount(float(amount)),
        "currency": token_symbol or "USDT",
        "block_timestamp": safe_int(item.get("block_timestamp") or item.get("block_ts")),
        "event_type": event_type or "transfer",
        "payload": item,
    }


def user_label(row: dict[str, Any]) -> str:
    display_name = " ".join(str(row.get("display_name") or "").split()).strip()
    username = str(row.get("username") or "").strip()
    if display_name:
        return display_name
    if username:
        return f"@{username}"
    return str(row.get("user_id") or "unknown")


def format_user_created_at(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "-"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw


def format_topup_timestamp(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "-"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return raw


def build_topup_detail_text(
    *,
    title: str,
    amount_label: str,
    amount_value: str,
    address: str = "",
    pay_hint: str = "",
    created_at: str = "",
    expire_at: str = "",
    extra_tips: list[str] | None = None,
) -> str:
    lines = [title, "", f"{amount_label}：{amount_value}"]
    if address:
        lines.extend(["", "收款地址（TRC20）", f"`{address}`"])
    if pay_hint:
        lines.extend(["", pay_hint])
    lines.extend(["", "⚠️ 重要提示"])
    tips = extra_tips or []
    for tip in tips:
        lines.append(f"• {tip}")
    lines.extend(
        [
            "",
            f"创建时间：{format_topup_timestamp(created_at)}",
            f"过期时间：{format_topup_timestamp(expire_at)}",
        ]
    )
    return "\n".join(lines)


def admin_send_button_markup(payload: dict[str, Any]) -> InlineKeyboardMarkup | None:
    button_text = str(payload.get("button_text") or "").strip()
    button_url = str(payload.get("button_url") or "").strip()
    if not button_text or not button_url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(button_text, url=button_url)]])


def is_delivery_failure(exc: Exception) -> bool:
    if isinstance(exc, Forbidden):
        return True
    text = str(exc).lower()
    return any(token in text for token in ("chat not found", "bot was blocked", "user is deactivated", "forbidden"))


async def call_blocking(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def get_okpay_http_session() -> requests.Session:
    session = getattr(OKPAY_HTTP_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        OKPAY_HTTP_LOCAL.session = session
    return session


def tg_custom_emoji(emoji_id: str, fallback: str) -> str:
    del fallback
    return f'<tg-emoji emoji-id="{emoji_id}"></tg-emoji>'


def premium_text_prefix(emoji_id: str, fallback: str, label: str) -> str:
    return f"{tg_custom_emoji(emoji_id, fallback)} {html.escape(label)}"


def format_order_date(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return f"{dt.year}/{dt.month}-{dt.day}"
    except ValueError:
        match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", raw)
        if match:
            year, month, day = match.groups()
            return f"{int(year)}/{int(month)}-{int(day)}"
    return raw


def build_orders_text(rows: list[dict[str, Any]]) -> str:
    title = premium_text_prefix(RECENT_ORDERS_EMOJI_ID, "🛍", "最近订单")
    if not rows:
        return f"{title}\n\n暂无订单"
    text_lines = [title, ""]
    for row in rows:
        order_date = format_order_date(row.get("created_at")) or "-"
        product_name = " ".join(str(row.get("product_name") or "").split()) or "商品"
        quantity = safe_int(row.get("quantity"), 1)
        spent = max(0.0, safe_float(row.get("total_price")) - safe_float(row.get("refund_amount")))
        text_lines.append(
            f"{html.escape(order_date)} | {html.escape(product_name)} |{quantity} | {format_money(spent)} $"
        )
    return "\n".join(text_lines)


def get_pending_purchase(context: ContextTypes.DEFAULT_TYPE) -> dict[str, int] | None:
    pending = context.user_data.get(PENDING_PURCHASE_KEY)
    return pending if isinstance(pending, dict) else None


def set_pending_purchase(
    context: ContextTypes.DEFAULT_TYPE,
    product_id: int,
    category_id: int,
    page: int,
) -> None:
    context.user_data[PENDING_PURCHASE_KEY] = {
        "product_id": product_id,
        "category_id": category_id,
        "page": page,
    }


def clear_pending_purchase(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(PENDING_PURCHASE_KEY, None)


def get_pending_recharge(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    pending = context.user_data.get(PENDING_RECHARGE_KEY)
    return pending if isinstance(pending, dict) else None


def set_pending_recharge(context: ContextTypes.DEFAULT_TYPE, channel: str = "okpay") -> None:
    context.user_data[PENDING_RECHARGE_KEY] = {"channel": str(channel or "okpay")}


def clear_pending_recharge(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(PENDING_RECHARGE_KEY, None)


def is_valid_trc20_address(address: str) -> bool:
    address = str(address or "").strip()
    if len(address) != 34 or not address.startswith("T"):
        return False
    allowed = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return all(ch in allowed for ch in address)


def trc20_enabled(recharge_address: str) -> bool:
    return is_valid_trc20_address(recharge_address)


def format_trc20_amount(value: float) -> str:
    text = f"{float(value):.4f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def quantize_recharge_amount(value: float) -> float:
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return float(amount)


def allocate_trc20_amount(store: Store, recharge_address: str, requested_amount: float, user_id: int) -> tuple[float, str]:
    base = Decimal(str(quantize_recharge_amount(requested_amount)))
    if base <= 0:
        raise ValueError("充值金额必须大于 0")
    used = {
        format_trc20_amount(amount)
        for amount in store.list_pending_topup_amounts("trc20", recharge_address)
    }
    start = abs(int(user_id)) % 100
    for offset in range(1, 100):
        step = ((start + offset - 1) % 99) + 1
        candidate = (base + (Decimal(step) / Decimal("10000"))).quantize(Decimal("0.0001"))
        text = format_trc20_amount(float(candidate))
        if text not in used:
            return float(candidate), text
    raise RuntimeError("当前 TRC20 待支付订单较多，请稍后再试")


def parse_trongrid_api_keys(settings: Settings) -> list[str]:
    keys: list[str] = []
    for raw in (settings.trongrid_api_keys, settings.trongrid_api_key):
        if not raw:
            continue
        for item in str(raw).replace("\n", ",").split(","):
            key = item.strip()
            if key and key not in keys:
                keys.append(key)
    return keys


def build_price_match_text(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in ("productName", "categoryName", "productId", "categoryId")
    ).lower()


def resolve_sell_price(settings: Settings, row: dict[str, Any]) -> float:
    base_price = safe_float(row.get("price"))
    add = settings.sell_price_add
    multiplier = 1.0
    match_text = build_price_match_text(row)
    for rule in settings.sell_price_rules:
        keyword = str(rule.get("keyword") or "").strip().lower()
        if keyword and keyword in match_text:
            if rule.get("multiplier") is not None:
                multiplier = safe_float(rule.get("multiplier"), multiplier)
            if rule.get("add") is not None:
                add = safe_float(rule.get("add"), add)
            break
    return round(max(0.0, base_price * multiplier + add), 4)


def resolve_button_icon(settings: Settings, name: str) -> tuple[str, str | None]:
    match_text = str(name or "").lower()
    for keywords, fallback_icon, icon_key in BUTTON_ICON_RULES:
        if any(keyword.lower() in match_text for keyword in keywords):
            custom_id = CATEGORY_BUTTON_EMOJI_IDS.get(icon_key)
            if custom_id is None and settings.inline_button_custom_emoji_enabled:
                custom_id = (
                    settings.button_custom_emoji_ids.get(icon_key)
                    or next((settings.button_custom_emoji_ids.get(keyword) for keyword in keywords if settings.button_custom_emoji_ids.get(keyword)), None)
                )
            return fallback_icon, custom_id
    return "📦", None


def catalog_button(settings: Settings, label: str, callback_data: str) -> InlineKeyboardButton:
    fallback_icon, custom_id = resolve_button_icon(settings, label)
    button_text = label if custom_id else f"{fallback_icon} {label}"
    kwargs: dict[str, Any] = {
        "text": button_text,
        "callback_data": callback_data,
    }
    if custom_id:
        kwargs["icon_custom_emoji_id"] = custom_id
    return InlineKeyboardButton(**kwargs)


def plain_catalog_button(label: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=callback_data)


def premium_inline_button(label: str, callback_data: str, custom_emoji_id: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=label,
        callback_data=callback_data,
        icon_custom_emoji_id=custom_emoji_id,
    )


def build_text_with_custom_emoji(parts: list[tuple[str, str | None]], code_spans: list[tuple[int, int]] | None = None) -> tuple[str, tuple[MessageEntity, ...]]:
    text_parts: list[str] = []
    entities: list[MessageEntity] = []
    offset = 0
    for text, custom_emoji_id in parts:
        text_parts.append(text)
        length = len(text)
        if custom_emoji_id:
            entities.append(
                MessageEntity(
                    type=MessageEntity.CUSTOM_EMOJI,
                    offset=offset,
                    length=length,
                    custom_emoji_id=custom_emoji_id,
                )
            )
        offset += length
    for span_offset, span_length in code_spans or []:
        entities.append(
            MessageEntity(
                type=MessageEntity.CODE,
                offset=span_offset,
                length=span_length,
            )
        )
    text = "".join(text_parts)
    utf16_entities = MessageEntity.adjust_message_entities_to_utf_16(text, entities)
    return text, tuple(utf16_entities)


def build_balance_change_notice_text(action_label: str, amount: float, balance: float) -> tuple[str, tuple[MessageEntity, ...]]:
    return build_text_with_custom_emoji(
        [
            ("😄", BALANCE_NOTICE_TITLE_EMOJI_ID),
            (" 余额变动提醒\n\n", None),
            ("😃", BALANCE_NOTICE_INCREASE_EMOJI_ID),
            (f" {action_label}: {format_money(amount)} USDT\n\n", None),
            ("😃", BALANCE_NOTICE_CURRENT_EMOJI_ID),
            (f" 当前余额: {format_money(balance)} USDT", None),
        ]
    )


def build_admin_add_balance_text(target_user_id: int, amount: float, balance: float) -> tuple[str, tuple[MessageEntity, ...]]:
    return build_text_with_custom_emoji(
        [
            ("👤", ADMIN_ADD_BALANCE_TITLE_EMOJI_ID),
            (" 管理员添加余额\n\n", None),
            ("😀", ADMIN_ADD_BALANCE_USER_EMOJI_ID),
            (f" 用户 {target_user_id}\n\n", None),
            ("➕", ADMIN_ADD_BALANCE_INCREASE_EMOJI_ID),
            (f" 已增加 {format_money(amount)} USDT\n\n", None),
            ("😃", BALANCE_NOTICE_CURRENT_EMOJI_ID),
            (f" 当前余额: {format_money(balance)} USDT", None),
        ]
    )


def build_purchase_refund_error_text(refund_amount: float, balance: float) -> tuple[str, tuple[MessageEntity, ...]]:
    return build_text_with_custom_emoji(
        [
            ("⚠️", SYSTEM_ERROR_EMOJI_ID),
            ("系统错误 ：请咨询客服\n\n", None),
            ("😃", BALANCE_NOTICE_REFUND_EMOJI_ID),
            (f"已退款 {format_money(refund_amount)} USDT\n", None),
            ("😃", BALANCE_NOTICE_CURRENT_EMOJI_ID),
            (f"当前余额: {format_money(balance)} USDT", None),
        ]
    )


def build_admin_new_order_text(
    buyer_user_id: int,
    buyer_username: str,
    buyer_display_name: str,
    product_label: str,
    quantity: int,
    total_price: float,
    remain_balance: float,
) -> tuple[str, tuple[MessageEntity, ...]]:
    parts: list[tuple[str, str | None]] = []
    code_spans: list[tuple[int, int]] = []
    offset = 0

    def add_text(value: str, custom_emoji_id: str | None = None, code: bool = False) -> None:
        nonlocal offset
        parts.append((value, custom_emoji_id))
        length = len(value)
        if code:
            code_spans.append((offset, length))
        offset += length

    user_line = buyer_display_name.strip() or f"用户 {buyer_user_id}"
    username = buyer_username.strip().lstrip("@")
    if username:
        user_line = f"{user_line} @{username}"

    add_text("🎉", ADMIN_NEW_ORDER_TITLE_EMOJI_ID)
    add_text(" 您有新的购买订单\n\n")
    add_text("👤", ADMIN_NEW_ORDER_USER_EMOJI_ID)
    add_text(f" 用户: {user_line}\n")
    add_text("👤", ADMIN_NEW_ORDER_USER_ID_EMOJI_ID)
    add_text(" 用户ID: ")
    add_text(str(buyer_user_id), code=True)
    add_text("\n")
    add_text("🎁", ADMIN_NEW_ORDER_PRODUCT_EMOJI_ID)
    add_text(f" 购买商品: {product_label}\n")
    add_text("📊", ADMIN_NEW_ORDER_QUANTITY_EMOJI_ID)
    add_text(f" 购买数量：{quantity}\n")
    add_text("🔨", ADMIN_NEW_ORDER_AMOUNT_EMOJI_ID)
    add_text(f" 扣除金额：{format_money(total_price)}\n")
    add_text("🪙", ADMIN_NEW_ORDER_BALANCE_EMOJI_ID)
    add_text(f" 剩余余额：{format_money(remain_balance)}")
    return build_text_with_custom_emoji(parts, code_spans)


async def notify_admin_new_purchase(
    context: ContextTypes.DEFAULT_TYPE,
    admin_user_ids: set[int],
    buyer_user_id: int,
    buyer_username: str,
    buyer_display_name: str,
    product_label: str,
    quantity: int,
    total_price: float,
    remain_balance: float,
) -> None:
    if not admin_user_ids:
        return
    text, entities = build_admin_new_order_text(
        buyer_user_id,
        buyer_username,
        buyer_display_name,
        product_label,
        quantity,
        total_price,
        remain_balance,
    )
    for admin_user_id in admin_user_ids:
        try:
            await context.bot.send_message(
                chat_id=admin_user_id,
                text=text,
                entities=entities,
            )
        except Exception:
            logger.exception("发送管理员新订单提醒失败: admin=%s buyer=%s", admin_user_id, buyer_user_id)


def build_start_menu_text(
    settings: Settings,
    user: Any,
    balance: float,
    total_spent: float,
    total_quantity: int,
    restock_channel: str,
    customer_service_contact: str,
) -> tuple[str, tuple[MessageEntity, ...]]:
    parts: list[tuple[str, str | None]] = []
    code_spans: list[tuple[int, int]] = []
    offset = 0

    def add_text(value: str, custom_emoji_id: str | None = None, code: bool = False) -> None:
        nonlocal offset
        parts.append((value, custom_emoji_id))
        length = len(value)
        if code:
            code_spans.append((offset, length))
        offset += length

    add_text("ID: ")
    add_text(str(user.id), code=True)
    add_text("\n\n")

    add_text("💰", custom_emoji_id=START_MENU_EMOJI_USDT_ID)
    add_text(" USDT : ")
    add_text(format_money(balance), code=True)
    add_text("\n")

    add_text("📊", custom_emoji_id=START_MENU_EMOJI_SPENT_ID)
    add_text(" 消费金额 : ")
    add_text(format_money(total_spent), code=True)
    add_text("\n")

    add_text("📦", custom_emoji_id=START_MENU_EMOJI_QUANTITY_ID)
    add_text(" 购买数量 : ")
    add_text(str(total_quantity), code=True)
    add_text("\n\n")

    add_text("🟢", custom_emoji_id=START_MENU_EMOJI_RESTOCK_ID)
    add_text(f" 补货频道：{restock_channel}\n")

    add_text("☎️", custom_emoji_id=START_MENU_EMOJI_SUPPORT_ID)
    add_text(f" 联系客服：{customer_service_contact}")

    return build_text_with_custom_emoji(parts, code_spans)


def build_categories_intro_text() -> tuple[str, tuple[MessageEntity, ...]]:
    parts: list[tuple[str, str | None]] = [
        ("🛍", PRODUCT_LIST_EMOJI_ID),
        (" 这是商品分类列表，请选择你需要的分类：", None),
        ("\n\n", None),
        ("❗️", PRODUCT_LIST_ALERT_EMOJI_ID),
        (" 首次购买建议先少量测试，确认符合需求再放量。", None),
        ("\n", None),
        ("❗️", PRODUCT_LIST_ALERT_EMOJI_ID),
        (" 虚拟商品一经发货通常不支持无理由处理，请先看清分类与说明。", None),
    ]
    return build_text_with_custom_emoji(parts)


def build_products_intro_text(category_name: str) -> tuple[str, tuple[MessageEntity, ...]]:
    parts: list[tuple[str, str | None]] = [
        ("🛍", PRODUCT_LIST_EMOJI_ID),
        (" 这是商品列表，当前分类：", None),
        (category_name, None),
        ("\n\n", None),
        ("❗️", PRODUCT_LIST_ALERT_EMOJI_ID),
        (" 没用过的本店商品，请先少量购买测试，以免造成不必要的争议。", None),
        ("\n", None),
        ("❗️", PRODUCT_LIST_ALERT_EMOJI_ID),
        (" 账号放久难免会死，有差异请联系客服处理。", None),
    ]
    return build_text_with_custom_emoji(parts)


def build_search_results_text(keyword: str, rows: list[dict[str, Any]], price_resolver) -> tuple[str, tuple[MessageEntity, ...]]:
    parts: list[tuple[str, str | None]] = [
        ("🔎", SEARCH_RESULTS_EMOJI_ID),
        (" 搜索结果：", None),
        (keyword, None),
        ("\n", None),
        ("点击下面商品按钮查看详情：", None),
        ("\n\n", None),
    ]
    for row in rows[:SEARCH_RESULTS_LIMIT]:
        sell_price = price_resolver(row)
        parts.extend(
            [
                ("- ", None),
                (str(row.get("productName") or "商品"), None),
                (" | 库存 ", None),
                (str(safe_int(row.get("totalStock"))), None),
                (" | $", None),
                (f"{sell_price:.2f}", None),
                ("\n", None),
            ]
        )
    if parts[-1][0] == "\n":
        parts.pop()
    return build_text_with_custom_emoji(parts)


def detail_notice() -> str:
    return premium_text_prefix(ALERT_EMOJI_ID, "❗️", "未使用过的本店商品，请先少量购买测试，以免造成不必要的争议。")


def build_product_detail_text(
    product_name: str,
    price: float,
    stock: int,
) -> tuple[str, tuple[MessageEntity, ...]]:
    parts: list[tuple[str, str | None]] = [
        ("✅", BUYING_EMOJI_ID),
        (" 您正在购买：", None),
        (product_name, None),
        ("\n\n", None),
        ("💰", PRICE_EMOJI_ID),
        (" 价格：", None),
        (f"{format_money(price)} USDT", None),
        ("\n\n", None),
        ("📊", STOCK_EMOJI_ID),
        (" 库存：", None),
        (str(stock), None),
        ("\n\n", None),
        ("❗️", ALERT_EMOJI_ID),
        (" 未使用过的本店商品，请先少量购买测试，以免造成不必要的争议", None),
    ]
    return build_text_with_custom_emoji(parts)


def build_purchase_confirm_text(product_name: str, unit_price: float, quantity: int) -> tuple[str, tuple[MessageEntity, ...]]:
    total_price = unit_price * quantity
    parts: list[tuple[str, str | None]] = [
        ("🛍", PRODUCT_EMOJI_ID),
        (" 商品：", None),
        (product_name, None),
        ("\n", None),
        ("🪙", UNIT_PRICE_EMOJI_ID),
        (" 单价：", None),
        (f"{format_money(unit_price)} USDT", None),
        ("\n", None),
        ("📦", ITEM_COUNT_EMOJI_ID),
        (" 数量：", None),
        (str(quantity), None),
        ("\n\n", None),
        ("🧾", TOTAL_DUE_EMOJI_ID),
        (" 应付金额：", None),
        (f"{format_money(total_price)} USDT", None),
    ]
    return build_text_with_custom_emoji(parts)


def build_purchase_confirm_keyboard(
    product_id: int,
    quantity: int,
    category_id: int,
    page: int,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = [
        [premium_inline_button("确认购买", f"cbuy:{product_id}:{quantity}", BUY_BUTTON_EMOJI_ID)],
    ]
    if category_id > 0:
        buttons.append(
            [
                premium_inline_button(BUTTON_MAIN_MENU, "nav:menu", HOME_EMOJI_ID),
                premium_inline_button("返回商品", f"prd:{product_id}:{category_id}:{page}", BACK_EMOJI_ID),
            ]
        )
    else:
        buttons.append([premium_inline_button(BUTTON_MAIN_MENU, "nav:menu", HOME_EMOJI_ID)])
    return InlineKeyboardMarkup(buttons)


def build_delivery_ready_text(
    product_name: str,
    quantity: int,
    quantity_success: int,
    refund_amount: float,
) -> tuple[str, tuple[MessageEntity, ...]]:
    parts: list[tuple[str, str | None]] = [
        ("🛍", PRODUCT_EMOJI_ID),
        (" 商品：", None),
        (product_name, None),
        ("\n", None),
        ("📦", ITEM_COUNT_EMOJI_ID),
        (" 数量：", None),
        (str(quantity), None),
        ("\n", None),
        ("✅", PACKED_DONE_EMOJI_ID),
        (" 打包完成：存活账号 ", None),
        (str(quantity_success), None),
    ]
    if refund_amount > 0:
        parts.extend(
            [
                ("\n", None),
                ("💸", None),
                (" 已退款：", None),
                (f"{format_money(refund_amount)} USDT", None),
            ]
        )
    return build_text_with_custom_emoji(parts)


def order_created_caption() -> str:
    return premium_text_prefix(PACKED_DONE_EMOJI_ID, "✅", "订单已创建，正在检查账号存活并打包，请稍后...")


def delivery_storage_filename(task_id: str, file_url: str) -> str:
    parsed = urlparse(file_url)
    candidate = Path(unquote(parsed.path)).name.strip()
    suffix = Path(candidate).suffix.lower()
    if not suffix:
        suffix = ".zip"
    return f"{task_id}{suffix}"


def sanitize_delivery_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\\\|?*]+', " ", str(value or ""))
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned or "商品"


def delivery_display_filename(product_name: str, quantity: int, file_url: str) -> str:
    parsed = urlparse(file_url)
    candidate = Path(unquote(parsed.path)).name.strip()
    suffix = Path(candidate).suffix.lower()
    if not suffix:
        suffix = ".zip"
    return f"{sanitize_delivery_name(product_name)}-{max(int(quantity), 0)}{suffix}"


def download_delivery_file(supplier: SupplierClient, task_id: str, file_url: str) -> Path:
    DELIVERY_FILES_DIR.mkdir(parents=True, exist_ok=True)
    target_path = DELIVERY_FILES_DIR / delivery_storage_filename(task_id, file_url)
    if target_path.exists() and target_path.stat().st_size > 0:
        return target_path

    temp_path = target_path.with_suffix(target_path.suffix + ".part")
    with supplier.session.get(file_url, timeout=supplier.settings.api_timeout_seconds, stream=True) as response:
        response.raise_for_status()
        with temp_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    handle.write(chunk)
    temp_path.replace(target_path)
    return target_path


def is_delivery_file_url_expired(file_url: str, now: datetime | None = None) -> bool:
    parsed = urlparse(str(file_url or "").strip())
    query = parse_qs(parsed.query)
    amz_date = (query.get("X-Amz-Date") or [""])[0].strip()
    amz_expires = (query.get("X-Amz-Expires") or [""])[0].strip()
    if not amz_date or not amz_expires:
        return False
    try:
        issued_at = datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        expires_after = max(0, int(amz_expires))
    except (ValueError, TypeError):
        return False
    current = now or datetime.now(timezone.utc)
    return current >= issued_at + timedelta(seconds=max(0, expires_after - 30))


async def refresh_delivery_file_url(
    context: ContextTypes.DEFAULT_TYPE,
    supplier: SupplierClient,
    task_id: str,
    previous_file_url: str = "",
) -> dict[str, Any] | None:
    _, store, _ = get_services(context)
    payload = await call_blocking(supplier.query_order, task_id)
    data = payload.get("data") or {}
    status = safe_int(data.get("taskStatus"))
    file_url = str(data.get("fileUrl") or "").strip()
    if status != 1 or not file_url:
        logger.warning(
            "刷新订单 zip 下载链接失败: task_id=%s status=%s has_file_url=%s",
            task_id,
            status,
            bool(file_url),
        )
        return None
    logger.info(
        "刷新订单 zip 下载链接: task_id=%s changed=%s expired=%s",
        task_id,
        file_url != str(previous_file_url or "").strip(),
        is_delivery_file_url_expired(file_url),
    )
    return await call_blocking(store.update_order_delivery_file, task_id, file_url, payload)


async def download_delivery_file_with_refresh(
    context: ContextTypes.DEFAULT_TYPE,
    supplier: SupplierClient,
    order_row: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    task_id = str(order_row.get("task_id") or "").strip()
    file_url = str(order_row.get("file_url") or "").strip()
    current_order = order_row

    if is_delivery_file_url_expired(file_url):
        refreshed_row = await refresh_delivery_file_url(context, supplier, task_id, file_url)
        if refreshed_row:
            current_order = refreshed_row
            file_url = str(current_order.get("file_url") or "").strip()

    try:
        zip_path = await call_blocking(download_delivery_file, supplier, task_id, file_url)
        return zip_path, current_order
    except requests.HTTPError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code != 403:
            raise
        logger.warning("订单 zip 下载链接返回 403，尝试刷新后重试: %s", task_id)
        refreshed_row = await refresh_delivery_file_url(context, supplier, task_id, file_url)
        if not refreshed_row:
            raise
        current_order = refreshed_row
        file_url = str(current_order.get("file_url") or "").strip()
        zip_path = await call_blocking(download_delivery_file, supplier, task_id, file_url)
        return zip_path, current_order


async def deliver_order_file(
    context: ContextTypes.DEFAULT_TYPE,
    order_row: dict[str, Any],
    supplier: SupplierClient,
    include_ready_photo: bool = True,
    notify_failure: bool = True,
) -> bool:
    settings, store, _ = get_services(context)
    task_id = str(order_row.get("task_id") or "").strip()
    file_url = str(order_row.get("file_url") or "").strip()
    if not task_id or not file_url:
        return False
    if str(order_row.get("delivery_sent_at") or "").strip():
        return True

    quantity = safe_int(order_row.get("quantity"))
    quantity_success = safe_int(order_row.get("quantity_success"))
    refund_amount = safe_float(order_row.get("refund_amount"))
    user_id = safe_int(order_row.get("user_id"))

    media_write_timeout = max(20, int(settings.telegram_media_write_timeout_seconds))
    media_read_timeout = max(20, int(settings.telegram_media_read_timeout_seconds))
    ready_photo_sent = str(order_row.get("delivery_ready_sent_at") or "").strip()
    product_name = str(order_row.get("product_name") or f"商品 {order_row.get('product_id')}")

    try:
        zip_path, order_row = await download_delivery_file_with_refresh(context, supplier, order_row)
        file_url = str(order_row.get("file_url") or "").strip()
        ready_photo_sent = str(order_row.get("delivery_ready_sent_at") or "").strip()
        if include_ready_photo and not ready_photo_sent and DELIVERY_READY_IMAGE_PATH.exists():
            with DELIVERY_READY_IMAGE_PATH.open("rb") as photo_fp:
                delivery_text, delivery_entities = build_delivery_ready_text(
                    product_name,
                    quantity,
                    quantity_success,
                    refund_amount,
                )
                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=photo_fp,
                    caption=delivery_text,
                    caption_entities=delivery_entities,
                    write_timeout=media_write_timeout,
                    read_timeout=media_read_timeout,
                    connect_timeout=min(media_read_timeout, 30),
                )
            order_row = await call_blocking(store.mark_order_delivery_ready_sent, task_id) or order_row
        with zip_path.open("rb") as document_fp:
            logger.info(
                "å‡†å¤‡å‘é€è®¢å• zip: task_id=%s user_id=%s size=%s timeout=%ss",
                task_id,
                user_id,
                zip_path.stat().st_size,
                media_write_timeout,
            )
            await context.bot.send_document(
                chat_id=user_id,
                document=document_fp,
                filename=delivery_display_filename(product_name, quantity, file_url),
                reply_markup=MENU_KEYBOARD,
                write_timeout=media_write_timeout,
                read_timeout=media_read_timeout,
                connect_timeout=min(media_read_timeout, 30),
            )
    except Exception as exc:
        await call_blocking(store.mark_order_delivery_failed, task_id, repr(exc))
        logger.exception("发送订单 zip 文件失败: %s", task_id)
        if notify_failure:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"zip 文件发送失败，请稍后用 /order {task_id} 重试。",
                reply_markup=MENU_KEYBOARD,
            )
        return False

    await call_blocking(store.mark_order_delivery_sent, task_id)
    return True


async def reply_inline(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
    entities: tuple[MessageEntity, ...] | None = None,
) -> None:
    if update.callback_query is not None:
        query = update.callback_query
        await query.answer()
        message = query.message
        if message is not None and (
            message.photo
            or message.video
            or message.animation
            or message.document
        ):
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except BadRequest:
                pass
            await message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode, entities=entities)
            return
        try:
            await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode, entities=entities)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
    elif update.message is not None:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode, entities=entities)


async def notify_inline(update: Update, text: str, *, show_alert: bool = False) -> None:
    if update.callback_query is not None:
        try:
            await update.callback_query.answer(text=text, show_alert=show_alert)
        except BadRequest:
            await update.callback_query.answer()
        return
    await reply_inline(update, text)


async def send_progress_reply(update: Update, text: str) -> Any | None:
    if update.callback_query is not None:
        query = update.callback_query
        await query.answer()
        message = query.message
        if message is not None and not (
            message.photo
            or message.video
            or message.animation
            or message.document
        ):
            try:
                await message.edit_text(text=text)
                return message
            except BadRequest:
                pass
        if message is not None:
            return await message.reply_text(text)
        return None
    if update.message is not None:
        return await update.message.reply_text(text)
    return None


async def update_progress_reply(
    progress_message: Any | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
    entities: tuple[MessageEntity, ...] | None = None,
) -> bool:
    if progress_message is None:
        return False
    try:
        await progress_message.edit_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            entities=entities,
        )
        return True
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return True
        return False


async def send_menu_message(update: Update, text: str) -> None:
    if update.message is not None:
        await update.message.reply_text(text, reply_markup=MENU_KEYBOARD)
    elif update.callback_query is not None:
        await update.callback_query.message.reply_text(text, reply_markup=MENU_KEYBOARD)


def build_topup_order_id(channel: str, user_id: int) -> str:
    return f"{str(channel or 'TOPUP').upper()}{int(datetime.now(timezone.utc).timestamp() * 1000)}{int(user_id)}"


def build_topup_expire_at(minutes: int = 10) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=max(1, int(minutes)))).replace(microsecond=0).isoformat()


def okpay_sign_payload(config: dict[str, str], data: dict[str, Any]) -> dict[str, Any]:
    shop_id = str(config.get("shop_id") or "").strip()
    shop_token = str(config.get("shop_token") or "").strip()
    if not shop_id or not shop_token:
        raise RuntimeError("OKPay 未配置完整，请先设置商户ID和Token。")
    payload = dict(data)
    payload["id"] = shop_id
    payload = {key: value for key, value in payload.items() if value not in (None, "")}
    ordered_items = sorted((str(key), value) for key, value in payload.items())
    query = urllib.parse.urlencode(ordered_items, quote_via=urllib.parse.quote)
    query = urllib.parse.unquote(query)
    payload["sign"] = hashlib.md5((query + "&token=" + shop_token).encode()).hexdigest().upper()
    return payload


def okpay_post(config: dict[str, str], api_name: str, data: dict[str, Any]) -> dict[str, Any]:
    url = str(config.get("api_url") or "https://api.okaypay.me/shop").rstrip("/") + "/" + str(api_name).lstrip("/")
    timeout = max(5, safe_int(config.get("request_timeout"), 12))
    response = get_okpay_http_session().post(url, data=okpay_sign_payload(config, data), timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"OKPay 返回了异常响应：{payload!r}")
    return payload


def okpay_pay_link(config: dict[str, str], order_id: str, amount: float, bot_username: str = "", include_callback: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "unique_id": str(order_id),
        "name": f"{config.get('name') or 'OKPay'}充值",
        "amount": format_money(amount),
        "return_url": f"https://t.me/{bot_username}" if bot_username else "https://t.me/",
        "coin": "USDT",
    }
    if include_callback and config.get("callback_url"):
        data["callback_url"] = str(config.get("callback_url") or "")
    request_config = dict(config)
    request_config["request_timeout"] = safe_int(
        config.get("create_timeout"),
        safe_int(config.get("request_timeout"), 12),
    )
    return okpay_post(request_config, "payLink", data)


def okpay_check_deposit(config: dict[str, str], order_id: str) -> dict[str, Any]:
    return okpay_post(config, "checkDeposit", {"unique_id": str(order_id)})


def okpay_callback_fallback_active(application: Application) -> bool:
    until_ts = float(application.bot_data.get("okpay_skip_callback_url_until") or 0.0)
    return until_ts > datetime.now(timezone.utc).timestamp()


def mark_okpay_callback_fallback(application: Application, cooldown_seconds: int = 900) -> None:
    application.bot_data["okpay_skip_callback_url_until"] = (
        datetime.now(timezone.utc).timestamp() + max(60, int(cooldown_seconds))
    )


def okpay_build_query(data: dict[str, Any]) -> str:
    pairs: list[str] = []
    for key in sorted(data.keys()):
        value = data[key]
        if value in (None, ""):
            continue
        encoded_key = urllib.parse.quote(str(key), safe="[]")
        encoded_value = urllib.parse.quote(str(value), safe="+-")
        pairs.append(f"{encoded_key}={encoded_value}")
    return "&".join(pairs)


def okpay_build_nested_callback_query(data: dict[str, Any]) -> str:
    normal: dict[str, Any] = {}
    nested_data: dict[str, Any] = {}
    for key, value in data.items():
        matched = re.fullmatch(r"data\[([^\]]+)\]", str(key))
        if matched:
            nested_data[matched.group(1)] = value
        else:
            normal[str(key)] = value

    parts: list[str] = []
    for key in sorted(normal.keys()):
        if key == "data":
            continue
        parts.append(f"{urllib.parse.quote(key, safe='[]')}={urllib.parse.quote(str(normal[key]), safe='+-')}")
        if key == "code" and nested_data:
            primary_keys = ["order_id", "unique_id", "pay_user_id", "amount", "coin", "status", "type"]
            for nested_key in primary_keys:
                if nested_key in nested_data and nested_data[nested_key] not in (None, ""):
                    parts.append(f"data[{nested_key}]={urllib.parse.quote(str(nested_data[nested_key]), safe='+-')}")
            for nested_key in sorted(k for k in nested_data if k not in primary_keys):
                if nested_data[nested_key] not in (None, ""):
                    parts.append(f"data[{nested_key}]={urllib.parse.quote(str(nested_data[nested_key]), safe='+-')}")
    if nested_data and "code" not in normal:
        for nested_key in ["order_id", "unique_id", "pay_user_id", "amount", "coin", "status", "type"]:
            if nested_key in nested_data and nested_data[nested_key] not in (None, ""):
                parts.append(f"data[{nested_key}]={urllib.parse.quote(str(nested_data[nested_key]), safe='+-')}")
    return "&".join(parts)


def okpay_verify_callback(config: dict[str, str], payload: dict[str, Any]) -> bool:
    raw_sign = str(payload.get("sign") or "").strip()
    token = str(config.get("shop_token") or "").strip()
    if not raw_sign or not token:
        return False
    data = {str(key): value for key, value in payload.items() if str(key) != "sign" and value not in (None, "")}
    for query in (okpay_build_query(data), okpay_build_nested_callback_query(data)):
        sign = hashlib.md5((query + "&token=" + token).encode()).hexdigest().upper()
        if sign == raw_sign:
            return True
    return False


def okpay_normalize_check_result(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else result
    return {
        "unique_id": data.get("unique_id") or result.get("unique_id"),
        "order_id": data.get("order_id") or result.get("order_id"),
        "amount": data.get("amount") or result.get("amount"),
        "status": str(data.get("status") or result.get("status") or ""),
        "coin": data.get("coin") or result.get("coin") or "USDT",
        "type": data.get("type") or result.get("type") or "deposit",
        "pay_user_id": data.get("pay_user_id") or result.get("pay_user_id") or "",
    }


async def send_okpay_topup_notifications(
    application: Application,
    order: dict[str, Any],
    paid_amount: float,
    paid_coin: str,
) -> None:
    settings = application.bot_data["settings"]
    store = application.bot_data["store"]
    user_id = safe_int(order.get("user_id"))
    user_row = await call_blocking(store.get_user, user_id) or {}
    balance = safe_float(user_row.get("balance"))
    username = str(user_row.get("username") or "").strip()
    user_text = (
        f"✅ OKPay充值到账\n\n"
        f"金额：{format_money(paid_amount)} {paid_coin}\n"
        f"当前余额：{format_money(balance)} USDT"
    )
    try:
        await application.bot.send_message(chat_id=user_id, text=user_text, reply_markup=MENU_KEYBOARD)
    except Exception:
        logger.exception("发送 OKPay 到账通知失败: %s", user_id)

    admin_lines = ["用户充值到账", "", f"用户ID：{user_id}"]
    if username:
        admin_lines.append(f"用户名：@{username}")
    admin_text = "\n".join(admin_lines)
    admin_text += (
        f"\n订单号：{order.get('order_id')}\n"
        f"金额：{format_money(paid_amount)} {paid_coin}\n"
        f"余额：{format_money(balance)} USDT"
    )
    for admin_user_id in sorted(settings.admin_user_ids):
        try:
            await application.bot.send_message(chat_id=int(admin_user_id), text=admin_text)
        except Exception:
            logger.exception("发送管理员到账通知失败: %s", admin_user_id)


async def send_trc20_topup_notifications(
    application: Application,
    order: dict[str, Any],
    paid_amount: float,
    txid: str,
    from_address: str,
) -> None:
    settings = application.bot_data["settings"]
    store = application.bot_data["store"]
    user_id = safe_int(order.get("user_id"))
    user_row = await call_blocking(store.get_user, user_id) or {}
    balance = safe_float(user_row.get("balance"))
    username = str(user_row.get("username") or "").strip()
    user_text = (
        "✅ TRC20 充值到账\n\n"
        f"金额：{format_trc20_amount(paid_amount)} USDT\n"
        f"交易哈希：`{txid}`\n"
        f"当前余额：{format_money(balance)} USDT"
    )
    try:
        await application.bot.send_message(chat_id=user_id, text=user_text, reply_markup=MENU_KEYBOARD, parse_mode="Markdown")
    except Exception:
        logger.exception("发送 TRC20 到账通知失败: %s", user_id)

    admin_lines = ["用户 TRC20 充值到账", "", f"用户ID：{user_id}"]
    if username:
        admin_lines.append(f"用户名：@{username}")
    admin_lines.append(f"订单号：{order.get('order_id')}")
    admin_lines.append(f"金额：{format_trc20_amount(paid_amount)} USDT")
    admin_lines.append(f"来源地址：{from_address}")
    admin_lines.append(f"TxID：{txid}")
    admin_lines.append(f"余额：{format_money(balance)} USDT")
    admin_text = "\n".join(admin_lines)
    for admin_user_id in sorted(settings.admin_user_ids):
        try:
            await application.bot.send_message(chat_id=int(admin_user_id), text=admin_text)
        except Exception:
            logger.exception("发送管理员 TRC20 到账通知失败: %s", admin_user_id)


def process_okpay_topup(application: Application, payload: dict[str, Any], source: str = "callback") -> tuple[bool, str, dict[str, Any] | None]:
    settings = application.bot_data["settings"]
    store = application.bot_data["store"]
    runtime_config = application.bot_data.get("runtime_config", {})
    config = resolve_okpay_settings(runtime_config, settings)
    unique_id = str(payload.get("data[unique_id]") or payload.get("unique_id") or "").strip()
    pay_type = str(payload.get("data[type]") or payload.get("type") or "deposit").strip().lower()
    pay_status = str(payload.get("data[status]") or payload.get("status") or "").strip()
    paid_amount = safe_float(payload.get("data[amount]") or payload.get("amount"))
    paid_coin = str(payload.get("data[coin]") or payload.get("coin") or "USDT").strip().upper()
    upstream_order_id = str(payload.get("data[order_id]") or payload.get("order_id") or "").strip()
    pay_user_id = str(payload.get("data[pay_user_id]") or payload.get("pay_user_id") or "").strip()
    if not unique_id or pay_type != "deposit" or pay_status != "1":
        return False, "not_paid", None
    status, order = store.complete_topup_order(
        unique_id,
        paid_amount=paid_amount,
        currency=paid_coin,
        upstream_order_id=upstream_order_id,
        pay_user_id=pay_user_id,
        callback_payload=payload,
        note=f"{source}:{paid_coin}",
    )
    if status == "paid" and order is not None:
        loop = application.bot_data.get("main_loop")
        if loop is not None:
            asyncio.run_coroutine_threadsafe(
                send_okpay_topup_notifications(application, order, paid_amount, paid_coin),
                loop,
            )
        return True, status, order
    return status == "already_paid", status, order


async def poll_okpay_topups_once(application: Application) -> None:
    settings = application.bot_data["settings"]
    store = application.bot_data["store"]
    runtime_config = application.bot_data.get("runtime_config", {})
    config = resolve_okpay_settings(runtime_config, settings)
    if not okpay_enabled(config):
        return

    await call_blocking(store.expire_topup_orders, "okpay")
    pending_orders = await call_blocking(
        store.list_pending_topup_orders,
        None,
        "okpay",
        settings.okpay_poll_limit,
    )
    if not pending_orders:
        return

    semaphore = asyncio.Semaphore(settings.okpay_poll_concurrency)

    async def run_order(order: dict[str, Any]) -> None:
        order_id = str(order.get("order_id") or "").strip()
        if not order_id:
            return
        async with semaphore:
            try:
                result = await call_blocking(okpay_check_deposit, config, order_id)
            except Exception:
                logger.exception("OKPay poll check failed: %s", order_id)
                return
            payload = okpay_normalize_check_result(result)
            process_okpay_topup(application, payload, source="poll_check")

    await asyncio.gather(*(run_order(order) for order in pending_orders))


async def poll_okpay_topups(context: ContextTypes.DEFAULT_TYPE) -> None:
    application = getattr(context, "application", None)
    if application is None:
        return
    poll_lock = application.bot_data.setdefault("okpay_poll_lock", asyncio.Lock())
    if poll_lock.locked():
        logger.info("OKPay poll is still running, skip this round")
        return
    async with poll_lock:
        await poll_okpay_topups_once(application)


async def poll_trc20_topups_once(application: Application) -> None:
    settings = application.bot_data["settings"]
    store = application.bot_data["store"]
    runtime_config = application.bot_data.get("runtime_config", {})
    recharge_address = str(runtime_config.get(RUNTIME_KEY_RECHARGE_ADDRESS) or "").strip()
    if not trc20_enabled(recharge_address):
        return

    api_keys = parse_trongrid_api_keys(settings)
    state = application.bot_data.setdefault("trc20_listener_state", {})
    last_ts = safe_int(state.get(recharge_address))
    lookback_ms = settings.trongrid_lookback_minutes * 60 * 1000
    min_timestamp = max(0, last_ts - 60 * 1000) if last_ts > 0 else int(datetime.now(timezone.utc).timestamp() * 1000) - lookback_ms
    await call_blocking(store.expire_topup_orders, "trc20")
    try:
        items = await call_blocking(fetch_trongrid_transactions, settings, recharge_address, min_timestamp, api_keys)
    except Exception:
        logger.exception("拉取 TronGrid 转账失败")
        return

    max_ts = last_ts
    for item in sorted(items, key=lambda row: safe_int(row.get("block_timestamp") or row.get("block_ts"))):
        normalized = normalize_trc20_transfer(item, recharge_address, settings.trc20_usdt_contract)
        if normalized is None:
            continue
        max_ts = max(max_ts, safe_int(normalized.get("block_timestamp")))
        status, order = await call_blocking(
            store.complete_trc20_topup,
            txid=str(normalized["txid"]),
            to_address=str(normalized["to_address"]),
            from_address=str(normalized["from_address"]),
            paid_amount=safe_float(normalized["amount"]),
            currency=str(normalized["currency"]),
            block_timestamp=safe_int(normalized["block_timestamp"]),
            payload={
                "txid": normalized["txid"],
                "to_address": normalized["to_address"],
                "from_address": normalized["from_address"],
                "amount": normalized["amount_text"],
                "currency": normalized["currency"],
                "block_timestamp": normalized["block_timestamp"],
                "event_type": normalized["event_type"],
            },
        )
        if status == "paid" and order is not None:
            await send_trc20_topup_notifications(
                application,
                order,
                safe_float(normalized["amount"]),
                str(normalized["txid"]),
                str(normalized["from_address"]),
            )
    if max_ts > 0:
        state[recharge_address] = max_ts


async def poll_trc20_topups(context: ContextTypes.DEFAULT_TYPE) -> None:
    application = getattr(context, "application", None)
    if application is None:
        return
    poll_lock = application.bot_data.setdefault("trc20_poll_lock", asyncio.Lock())
    if poll_lock.locked():
        return
    async with poll_lock:
        await poll_trc20_topups_once(application)


class OkpayCallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OKPay callback server is running")

    def do_POST(self) -> None:
        application = getattr(self.server, "apibot_application", None)
        if application is None:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"application unavailable")
            return

        settings = application.bot_data["settings"]
        runtime_config = application.bot_data.get("runtime_config", {})
        config = resolve_okpay_settings(runtime_config, settings)
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="ignore")
        if "application/json" in str(self.headers.get("Content-Type") or "").lower():
            try:
                body = json.loads(raw or "{}")
            except json.JSONDecodeError:
                body = {}
            payload: dict[str, Any] = {}
            if isinstance(body, dict):
                for key, value in body.items():
                    if isinstance(value, dict):
                        for inner_key, inner_value in value.items():
                            payload[f"{key}[{inner_key}]"] = inner_value
                    else:
                        payload[str(key)] = value
        else:
            parsed = parse_qs(raw, keep_blank_values=True)
            payload = {str(key): values[-1] for key, values in parsed.items()}

        if not okpay_verify_callback(config, payload):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"bad sign")
            return

        ok, status, _ = process_okpay_topup(application, payload, source="callback")
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "success" if ok else "error", "message": status}).encode())


async def ensure_okpay_callback_server(application: Application) -> None:
    settings = application.bot_data["settings"]
    runtime_config = application.bot_data.get("runtime_config", {})
    config = resolve_okpay_settings(runtime_config, settings)
    if not okpay_enabled(config):
        return
    if not str(config.get("callback_url") or "").strip():
        return
    if application.bot_data.get("okpay_callback_server") is not None:
        return
    try:
        server = ThreadingHTTPServer((settings.okpay_callback_host, settings.okpay_callback_port), OkpayCallbackHandler)
        setattr(server, "apibot_application", application)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        application.bot_data["okpay_callback_server"] = server
        logger.info("OKPay callback server started at %s:%s", settings.okpay_callback_host, settings.okpay_callback_port)
    except Exception:
        logger.exception("启动 OKPay 回调服务失败")


async def reply_help(update: Update, context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
    if context is not None and await should_ignore_for_closed_business(update, context):
        return
    text = (
        "可用命令:\n"
        "/start - 启动说明\n"
        "/menu - 主菜单\n"
        "/me - 查看我的余额\n"
        "/categories - 浏览商品分类\n"
        "/products <category_id> - 查看某分类商品\n"
        "/product <product_id> - 查看商品详情\n"
        "/buy <product_id> <数量> - 购买商品\n"
        "/orders - 查看最近订单\n"
        "/order <task_id> - 查询订单状态\n"
        "/supplier_balance - 管理员查看上游余额\n"
        "/add <user_id> <+金额/-金额> - 管理员调整余额\n"
        "/credit <user_id> <金额> - 兼容旧命令\n\n"
        "底部也有常驻按钮：🏠主菜单 / ☎️ 联系客服 / 💰充值余额。"
    )
    await send_menu_message(update, text)


def get_services(context: ContextTypes.DEFAULT_TYPE) -> tuple[Settings, Store, SupplierClient]:
    settings: Settings = context.application.bot_data["settings"]
    store: Store = context.application.bot_data["store"]
    supplier: SupplierClient = context.application.bot_data["supplier"]
    return settings, store, supplier


async def should_ignore_for_closed_business(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings, _, _ = get_services(context)
    user = update.effective_user
    if user is None or is_admin(settings, user.id) or effective_business_open(context):
        return False
    if update.callback_query is not None:
        try:
            await update.callback_query.answer()
        except BadRequest:
            pass
    return True


async def handle_admin_business_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    settings, store, _ = get_services(context)
    user = update.effective_user
    normalized = normalize_search_keyword(text)
    if user is None or not is_admin(settings, user.id):
        return False
    if normalized not in {"开始营业", "停止营业"}:
        return False
    value = "1" if normalized == "开始营业" else "0"
    runtime_config = get_runtime_config(context)
    await call_blocking(store.set_runtime_setting, RUNTIME_KEY_BUSINESS_STATUS, value, user.id)
    runtime_config[RUNTIME_KEY_BUSINESS_STATUS] = value
    await call_blocking(store.log_admin_action, user.id, "business_status", value, normalized)
    clear_pending_admin_action(context)
    await send_menu_message(update, normalized)
    return True


def build_main_menu_button(
    settings: Settings,
    label: str,
    callback_data: str,
    custom_emoji_id: str,
    fallback_icon: str,
) -> InlineKeyboardButton:
    if settings.inline_button_custom_emoji_enabled:
        return premium_inline_button(label, callback_data, custom_emoji_id)
    return InlineKeyboardButton(text=f"{fallback_icon} {label}", callback_data=callback_data)


def build_main_menu_inline(settings: Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                build_main_menu_button(settings, BUTTON_ACCOUNT_LIST, "nav:cats", MAIN_MENU_EMOJI_ACCOUNT_LIST_ID, "📂"),
                build_main_menu_button(settings, BUTTON_RECHARGE_BALANCE, "nav:recharge", MAIN_MENU_EMOJI_RECHARGE_BALANCE_ID, "💰"),
            ],
            [
                build_main_menu_button(settings, BUTTON_PURCHASE_NOTICE, "nav:notice", MAIN_MENU_EMOJI_PURCHASE_NOTICE_ID, "📖"),
                build_main_menu_button(settings, BUTTON_ORDER_HISTORY, "nav:orders", MAIN_MENU_EMOJI_ORDER_HISTORY_ID, "📦"),
            ],
            [build_main_menu_button(settings, BUTTON_SWITCH_LANGUAGE, "nav:language", MAIN_MENU_EMOJI_SWITCH_LANGUAGE_ID, "🌐")],
        ]
    )


def build_category_keyboard(rows: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows:
        category_id = safe_int(row.get("categoryId"))
        stock = safe_int(row.get("totalStock"))
        name = shorten(str(row.get("categoryName") or f"分类 {category_id}"), 26)
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"📂 {name} [{stock}]",
                    callback_data=f"cat:{category_id}:0",
                )
            ]
        )
    buttons.append([premium_inline_button(BUTTON_MAIN_MENU, "nav:menu", HOME_EMOJI_ID)])
    buttons.append([premium_inline_button("关闭", "nav:close", CLOSE_EMOJI_ID)])
    return InlineKeyboardMarkup(buttons)


def category_name_from_rows(rows: list[dict[str, Any]], category_id: int) -> str:
    for row in rows:
        if safe_int(row.get("categoryId")) == category_id:
            return str(row.get("categoryName") or f"分类 {category_id}")
    return f"分类 {category_id}"


def build_product_keyboard(
    rows: list[dict[str, Any]],
    category_id: int,
    page: int,
) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(rows) + PRODUCTS_PER_PAGE - 1) // PRODUCTS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * PRODUCTS_PER_PAGE
    page_rows = rows[start : start + PRODUCTS_PER_PAGE]

    buttons: list[list[InlineKeyboardButton]] = []
    for row in page_rows:
        product_id = safe_int(row.get("productId"))
        product_name = shorten(str(row.get("productName") or f"商品 {product_id}"), 28)
        price = safe_float(row.get("price"))
        stock = safe_int(row.get("totalStock"))
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{product_name} ({stock}) - ${price:.2f}",
                    callback_data=f"prd:{product_id}:{category_id}:{page}",
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"cat:{category_id}:{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=f"cat:{category_id}:{page}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"cat:{category_id}:{page + 1}"))
    buttons.append(nav_row)
    buttons.append(
        [
            premium_inline_button(BUTTON_MAIN_MENU, "nav:menu", HOME_EMOJI_ID),
            premium_inline_button("返回分类", "nav:cats", BACK_EMOJI_ID),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def render_products_view(
    category_name: str,
    category_id: int,
    rows: list[dict[str, Any]],
    page: int,
) -> tuple[str, tuple[MessageEntity, ...], InlineKeyboardMarkup]:
    text, entities = build_products_intro_text(category_name)
    keyboard = build_product_keyboard(rows, category_id, page)
    return text, entities, keyboard


def build_product_detail_keyboard(product_id: int, category_id: int, page: int) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = [[premium_inline_button("购买", f"qbuy:{product_id}:1:{category_id}:{page}", BUY_BUTTON_EMOJI_ID)]]
    if category_id > 0:
        buttons.append(
            [
                premium_inline_button(BUTTON_MAIN_MENU, "nav:menu", HOME_EMOJI_ID),
                premium_inline_button("返回", f"cat:{category_id}:{page}", BACK_EMOJI_ID),
            ]
        )
    else:
        buttons.append([premium_inline_button(BUTTON_MAIN_MENU, "nav:menu", HOME_EMOJI_ID)])
    return InlineKeyboardMarkup(buttons)


def render_product_detail_view(
    row: dict[str, Any],
    category_id: int,
    page: int,
) -> tuple[str, tuple[MessageEntity, ...], InlineKeyboardMarkup]:
    product_id = safe_int(row.get("productId"))
    product_name = str(row.get("productName") or f"商品 {product_id}")
    text, entities = build_product_detail_text(
        product_name,
        safe_float(row.get("price")),
        safe_int(row.get("totalStock")),
    )
    return text, entities, build_product_detail_keyboard(product_id, category_id, page)


def build_category_keyboard_configured(settings: Settings, rows: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows:
        category_id = safe_int(row.get("categoryId"))
        stock = safe_int(row.get("totalStock"))
        name = shorten(str(row.get("categoryName") or f"分类 {category_id}"), 26)
        buttons.append([catalog_button(settings, f"{name} 库存 [{stock}]", f"cat:{category_id}:0")])
    buttons.append([premium_inline_button(BUTTON_MAIN_MENU, "nav:menu", HOME_EMOJI_ID)])
    buttons.append([premium_inline_button("关闭", "nav:close", CLOSE_EMOJI_ID)])
    return InlineKeyboardMarkup(buttons)


def build_product_keyboard_configured(
    settings: Settings,
    rows: list[dict[str, Any]],
    category_id: int,
    page: int,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows:
        product_id = safe_int(row.get("productId"))
        product_name = shorten(str(row.get("productName") or f"商品 {product_id}"), 28)
        stock = safe_int(row.get("totalStock"))
        price = resolve_sell_price(settings, row)
        buttons.append([plain_catalog_button(f"{product_name} 库存 [{stock}] - ${price:.2f}", f"prd:{product_id}:{category_id}:0")])

    buttons.append(
        [
            premium_inline_button(BUTTON_MAIN_MENU, "nav:menu", HOME_EMOJI_ID),
            premium_inline_button("返回分类", "nav:cats", BACK_EMOJI_ID),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def render_products_view_configured(
    settings: Settings,
    category_name: str,
    category_id: int,
    rows: list[dict[str, Any]],
    page: int,
) -> tuple[str, tuple[MessageEntity, ...], InlineKeyboardMarkup]:
    text, entities = build_products_intro_text(category_name)
    return text, entities, build_product_keyboard_configured(settings, rows, category_id, page)


def render_product_detail_view_configured(
    settings: Settings,
    row: dict[str, Any],
    category_id: int,
    page: int,
) -> tuple[str, tuple[MessageEntity, ...], InlineKeyboardMarkup]:
    product_id = safe_int(row.get("productId"))
    product_name = str(row.get("productName") or f"商品 {product_id}")
    sell_price = resolve_sell_price(settings, row)
    text, entities = build_product_detail_text(
        product_name,
        sell_price,
        safe_int(row.get("totalStock")),
    )
    return text, entities, build_product_detail_keyboard(product_id, category_id, page)


async def fetch_categories(supplier: SupplierClient) -> list[dict[str, Any]]:
    payload = await call_blocking(supplier.get_categories)
    return payload.get("data") or []


async def fetch_category_products(supplier: SupplierClient, category_id: int) -> list[dict[str, Any]]:
    payload = await call_blocking(supplier.get_products, category_id)
    return payload.get("data") or []


async def build_main_menu_message(
    context: ContextTypes.DEFAULT_TYPE,
    user: Any,
) -> tuple[str, tuple[MessageEntity, ...], InlineKeyboardMarkup]:
    settings, store, _ = get_services(context)
    await call_blocking(store.ensure_user, user.id, user.username or "", user.full_name or "")
    balance = await call_blocking(store.get_balance, user.id)
    summary = await call_blocking(store.get_user_summary, user.id)
    text, entities = build_start_menu_text(
        settings,
        user,
        balance,
        safe_float(summary.get("total_spent")),
        safe_int(summary.get("total_quantity")),
        effective_restock_channel(context, settings),
        effective_customer_service_contact(context, settings),
    )
    main_menu_inline = build_main_menu_inline(settings)
    return text, entities, main_menu_inline


async def refresh_bottom_menu_keyboard(update: Update) -> None:
    if update.message is not None:
        await update.message.reply_text("底部菜单已刷新。", reply_markup=MENU_KEYBOARD)


async def show_start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    text, text_entities, main_menu_inline = await build_main_menu_message(context, user)
    if update.callback_query is not None:
        await update.callback_query.answer()
    await refresh_bottom_menu_keyboard(update)
    start_menu_image_path = START_MENU_IMAGE_PATH if START_MENU_IMAGE_PATH.exists() else LEGACY_START_MENU_IMAGE_PATH
    if start_menu_image_path.exists():
        with start_menu_image_path.open("rb") as photo_fp:
            if update.message is not None:
                await update.message.reply_photo(
                    photo=photo_fp,
                    caption=text,
                    caption_entities=text_entities,
                    reply_markup=main_menu_inline,
                )
            elif update.callback_query is not None and update.callback_query.message is not None:
                await update.callback_query.message.reply_photo(
                    photo=photo_fp,
                    caption=text,
                    caption_entities=text_entities,
                    reply_markup=main_menu_inline,
                )
        return
    if update.message is not None:
        await update.message.reply_text(text, entities=text_entities, reply_markup=main_menu_inline)
    elif update.callback_query is not None and update.callback_query.message is not None:
        await update.callback_query.message.reply_text(text, entities=text_entities, reply_markup=main_menu_inline)


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    text, entities, main_menu_inline = await build_main_menu_message(context, user)
    await reply_inline(update, text, main_menu_inline, entities=entities)


async def show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, _, supplier = get_services(context)
    try:
        rows = await fetch_categories(supplier)
    except SupplierApiError as exc:
        await reply_inline(update, f"获取分类失败: {exc}")
        return
    if not rows:
        await reply_inline(update, "当前没有分类。")
        return
    text, entities = build_categories_intro_text()
    await reply_inline(update, text, build_category_keyboard_configured(settings, rows), entities=entities)


async def show_products(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    category_id: int,
    page: int = 0,
) -> None:
    settings, _, supplier = get_services(context)
    try:
        categories = await fetch_categories(supplier)
        rows = await fetch_category_products(supplier, category_id)
    except SupplierApiError as exc:
        await reply_inline(update, f"获取商品列表失败: {exc}")
        return
    if not rows:
        await reply_inline(update, "这个分类下没有商品。")
        return
    category_name = category_name_from_rows(categories, category_id)
    text, entities, keyboard = render_products_view_configured(settings, category_name, category_id, rows, page)
    await reply_inline(update, text, keyboard, entities=entities)


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, store, _ = get_services(context)
    user = update.effective_user
    if user is None:
        return
    await call_blocking(store.ensure_user, user.id, user.username or "", user.full_name or "")
    balance = await call_blocking(store.get_balance, user.id)
    rows = await call_blocking(store.list_user_orders, user.id, 5)
    lines = [
        "👤 个人中心",
        "",
        f"🆔 用户ID：{user.id}",
        f"👤 用户名：@{user.username}" if user.username else "👤 用户名：未设置",
        f"💰 当前余额：{format_money(balance)} USDT",
        "",
        "📦 最近订单：",
    ]
    if rows:
        for row in rows:
            lines.append(
                f"- {row['product_name']} | {row['state']} | "
                f"{row['quantity_success']}/{row['quantity']}"
            )
    else:
        lines.append("- 暂无订单")
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💰 我要充值", callback_data="nav:recharge")],
            [
                InlineKeyboardButton("🛒 商品列表", callback_data="nav:cats"),
                InlineKeyboardButton("📦 我的订单", callback_data="nav:orders"),
            ],
            [premium_inline_button(BUTTON_MAIN_MENU, "nav:menu", HOME_EMOJI_ID)],
        ]
    )
    await reply_inline(update, "\n".join(lines), keyboard)


def normalize_recharge_channel(value: str | None) -> str | None:
    channel = str(value or "").strip().lower()
    if channel in {"okpay", "trc20"}:
        return channel
    return None


def recharge_channel_label(channel: str) -> str:
    normalized = normalize_recharge_channel(channel)
    if normalized == "trc20":
        return "USDT 充值 | TRC20"
    if normalized == "okpay":
        return "OKPay充值 | 秒到账"
    return "充值"


RECHARGE_PRESET_AMOUNTS: tuple[tuple[int, ...], ...] = (
    (10, 30, 50),
    (100, 200, 500),
    (1000, 1500, 2000),
)


def build_recharge_keyboard(
    okpay_config: dict[str, str],
    recharge_address: str,
    selected_channel: str | None = None,
) -> InlineKeyboardMarkup:
    trc20_available = trc20_enabled(recharge_address)
    okpay_available = okpay_enabled(okpay_config)
    selected_channel = normalize_recharge_channel(selected_channel)
    rows: list[list[InlineKeyboardButton]] = []
    if selected_channel is None:
        if trc20_available:
            rows.append([InlineKeyboardButton(recharge_channel_label("trc20"), callback_data="rchg:select:trc20")])
        if okpay_available:
            rows.append([InlineKeyboardButton(recharge_channel_label("okpay"), callback_data="rchg:select:okpay")])
        rows.append([InlineKeyboardButton("取消充值", callback_data="rchg:close")])
        return InlineKeyboardMarkup(rows)

    if selected_channel == "trc20" and not trc20_available:
        selected_channel = None
    if selected_channel == "okpay" and not okpay_available:
        selected_channel = None
    if selected_channel is not None:
        for amount_row in RECHARGE_PRESET_AMOUNTS:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{amount}USDT",
                        callback_data=f"rchg:{selected_channel}:create:{amount}",
                    )
                    for amount in amount_row
                ]
            )
        rows.append([InlineKeyboardButton("自定义充值金额", callback_data=f"rchg:{selected_channel}:custom")])
        rows.append([InlineKeyboardButton("返回支付方式", callback_data="rchg:back")])
        rows.append([InlineKeyboardButton("取消充值", callback_data="rchg:close")])
    return InlineKeyboardMarkup(rows)


async def create_trc20_topup_order(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float) -> None:
    _, store, _ = get_services(context)
    user = update.effective_user
    if user is None:
        return
    recharge_address = effective_recharge_address(context)
    if not trc20_enabled(recharge_address):
        await reply_inline(update, "TRC20 充值地址还没有配置好，请先联系管理员。")
        return
    if amount <= 0:
        await reply_inline(update, "充值金额必须大于 0。")
        return

    requested_amount = quantize_recharge_amount(amount)
    if requested_amount <= 0:
        await reply_inline(update, "充值金额必须大于 0。")
        return

    await call_blocking(store.ensure_user, user.id, user.username or "", user.full_name or "")
    try:
        order = await call_blocking(
            store.create_trc20_topup_order,
            order_id=build_topup_order_id("TRC20", user.id),
            user_id=user.id,
            recharge_address=recharge_address,
            requested_amount=requested_amount,
            currency="USDT",
            note="trc20",
            expire_at=build_topup_expire_at(10),
        )
    except Exception as exc:
        await reply_inline(update, f"创建 TRC20 充值订单失败：{exc}")
        return

    order_id = str(order.get("order_id") or "")
    pay_amount = safe_float(order.get("amount"))
    pay_amount_text = format_trc20_amount(pay_amount)
    if not order_id or pay_amount <= 0:
        await reply_inline(update, "创建 TRC20 充值订单失败：订单数据异常")
        return

    created_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    deadline_time = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    caption = (
        "<b>充值详情</b>\n\n"
        f"唯一收款地址：<code>{html.escape(recharge_address)}</code>\n"
        "（推荐使用扫码转账更加安全 点击上方地址即可快速复制粘贴）\n\n"
        f"实际支付金额：<code>{html.escape(pay_amount_text)} USDT</code>\n"
        "（点击上方金额可快速复制粘贴）\n\n"
        f"充值订单创建时间：{created_time}\n"
        f"转账最后截止时间：{deadline_time}\n\n"
        "❗️请一定按照金额后面小数点转账，否则无法自动到账\n"
        "❗️付款前请再次核对地址与金额，避免转错"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("取消订单", callback_data=f"rchg:trc20:cancel:{order_id}")],
        ]
    )
    sent_message = None
    if qrcode is not None:
        qr_image = await call_blocking(qrcode.make, recharge_address)
        qr_buffer = io.BytesIO()
        await call_blocking(qr_image.save, qr_buffer, "PNG")
        qr_buffer.seek(0)
        qr_buffer.name = f"{order_id}.png"
        if update.callback_query is not None and update.callback_query.message is not None:
            sent_message = await update.callback_query.message.reply_photo(
                photo=qr_buffer,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        elif update.message is not None:
            sent_message = await update.message.reply_photo(
                photo=qr_buffer,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
    else:
        logger.warning("qrcode module not installed; sending TRC20 topup details without QR image")
        if update.callback_query is not None and update.callback_query.message is not None:
            sent_message = await update.callback_query.message.reply_text(
                caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        elif update.message is not None:
            sent_message = await update.message.reply_text(
                caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
    if sent_message is not None:
        await call_blocking(store.set_topup_order_message_id, order_id, sent_message.message_id)


async def create_okpay_topup_order(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float) -> None:
    settings, store, _ = get_services(context)
    user = update.effective_user
    if user is None:
        return
    okpay_config = effective_okpay_settings(context, settings)
    if not okpay_enabled(okpay_config):
        await reply_inline(update, "OKPay 还没有配置完成，请先联系管理员。")
        return
    if amount <= 0:
        await reply_inline(update, "充值金额必须大于 0。")
        return

    progress_message = await send_progress_reply(update, "正在创建 OKPay 充值订单，请稍等…")
    await call_blocking(store.ensure_user, user.id, user.username or "", user.full_name or "")
    await call_blocking(store.cancel_pending_topup_orders, user.id, "okpay", "recreated")
    order_id = build_topup_order_id("OKPAY", user.id)
    bot_username = str(getattr(context.bot, "username", "") or "").strip().lstrip("@")
    started_at = datetime.now(timezone.utc)
    try:
        result = await call_blocking(okpay_pay_link, okpay_config, order_id, amount, bot_username, False)
    except Exception as exc:
        elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        logger.warning("OKPay payLink failed after %sms for order %s: %s", elapsed_ms, order_id, exc)
        message = f"创建 OKPay 充值订单失败：{exc}"
        if not await update_progress_reply(progress_message, message):
            await reply_inline(update, message)
        return
    elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    logger.info("OKPay payLink created in %sms for order %s", elapsed_ms, order_id)

    if isinstance(result, dict) and str(result.get("status") or "").lower() == "error":
        message = f"创建 OKPay 充值订单失败：{result}"
        if not await update_progress_reply(progress_message, message):
            await reply_inline(update, message)
        return

    data = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else {}
    pay_url = str(data.get("pay_url") or result.get("pay_url") or "").strip()
    upstream_order_id = str(data.get("order_id") or result.get("order_id") or "").strip()
    if not pay_url:
        message = f"创建 OKPay 充值订单失败：{result}"
        if not await update_progress_reply(progress_message, message):
            await reply_inline(update, message)
        return

    expire_at = build_topup_expire_at(10)
    await call_blocking(
        store.create_topup_order,
        order_id,
        user.id,
        "okpay",
        amount,
        "USDT",
        pay_url=pay_url,
        upstream_order_id=upstream_order_id,
        note="okpay",
        expire_at=expire_at,
    )

    text = (
        "<b>OKPay充值订单已创建</b>\n\n"
        f"订单号：<code>{html.escape(order_id)}</code>\n"
        f"充值金额：<code>{html.escape(format_money(amount))} USDT</code>\n\n"
        "请点击下面按钮完成支付。\n"
        "支付完成后，请回到机器人点击“我已支付”手动核验真实到账；"
        "未点击前不会自动到账。"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 打开OKPay支付", url=pay_url)],
            [InlineKeyboardButton("✅ 我已支付", callback_data=f"rchg:okpay:paid:{order_id}")],
            [InlineKeyboardButton("取消订单", callback_data=f"rchg:okpay:cancel:{order_id}")],
        ]
    )
    sent_message = progress_message
    if not await update_progress_reply(sent_message, text, reply_markup=keyboard, parse_mode="HTML"):
        if update.callback_query is not None and update.callback_query.message is not None:
            sent_message = await update.callback_query.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")
        elif update.message is not None:
            sent_message = await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")
        else:
            sent_message = None
    if sent_message is not None:
        await call_blocking(store.set_topup_order_message_id, order_id, sent_message.message_id)


async def check_okpay_topup_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> None:
    settings, store, _ = get_services(context)
    user = update.effective_user
    if user is None:
        return
    order = await call_blocking(store.get_topup_order, order_id)
    if order is None or str(order.get("channel") or "") != "okpay":
        await reply_inline(update, "未找到对应的 OKPay 充值订单。")
        return
    if safe_int(order.get("user_id")) != user.id:
        await reply_inline(update, "这笔充值订单不属于你。")
        return
    if str(order.get("state") or "") == "paid":
        await reply_inline(update, "这笔订单已经到账，无需重复检查。")
        return
    if str(order.get("state") or "") != "pending":
        await reply_inline(update, "这笔订单已失效，请重新创建新的充值订单。")
        return

    okpay_config = effective_okpay_settings(context, settings)
    if not okpay_enabled(okpay_config):
        await notify_inline(update, "OKPay 还没有配置完成，请先联系管理员。", show_alert=True)
        return

    try:
        result = await call_blocking(okpay_check_deposit, okpay_config, order_id)
    except Exception as exc:
        await notify_inline(update, f"查询 OKPay 订单失败：{exc}", show_alert=True)
        return

    payload = okpay_normalize_check_result(result)
    ok, status, fresh_order = process_okpay_topup(context.application, payload, source="manual_check")
    if ok and fresh_order is not None:
        await reply_inline(update, "✅ OKPay 订单已确认支付，余额已经自动到账。")
        return
    if status == "already_paid":
        await notify_inline(update, "这笔订单已经到账，无需重复检查。")
        return
    if status in {"expired", "canceled"}:
        await reply_inline(update, "这笔订单已经失效，请重新创建新的充值订单。")
        return
    await notify_inline(update, "暂时还没有查到支付成功，请支付后再点一次“我已支付”。")


async def cancel_okpay_topup_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> None:
    _, store, _ = get_services(context)
    user = update.effective_user
    if user is None:
        return
    changed, order = await call_blocking(store.cancel_topup_order, order_id, user_id=user.id, reason="user_canceled")
    if order is None:
        await reply_inline(update, "未找到这笔充值订单。")
        return
    if safe_int(order.get("user_id")) != user.id:
        await reply_inline(update, "这笔充值订单不属于你。")
        return
    if changed:
        if update.callback_query is not None and update.callback_query.message is not None:
            try:
                await update.callback_query.message.delete()
                return
            except BadRequest:
                pass
        await reply_inline(update, "充值订单已取消。")
        return
    await reply_inline(update, "这笔订单当前不能取消。")


async def check_trc20_topup_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> None:
    _, store, _ = get_services(context)
    user = update.effective_user
    if user is None:
        return
    await call_blocking(store.expire_topup_orders, "trc20")
    order = await call_blocking(store.get_topup_order, order_id)
    if order is None or str(order.get("channel") or "") != "trc20":
        await reply_inline(update, "未找到对应的 TRC20 充值订单。")
        return
    if safe_int(order.get("user_id")) != user.id:
        await reply_inline(update, "这笔充值订单不属于你。")
        return
    if str(order.get("state") or "") == "paid":
        await reply_inline(update, "这笔订单已经到账，无需重复检查。")
        return
    if str(order.get("state") or "") != "pending":
        await reply_inline(update, "这笔订单已经失效，请重新创建新的充值订单。")
        return

    await poll_trc20_topups_once(context.application)
    fresh_order = await call_blocking(store.get_topup_order, order_id)
    if fresh_order is None:
        await reply_inline(update, "未找到对应的 TRC20 充值订单。")
        return
    if str(fresh_order.get("state") or "") == "paid":
        await reply_inline(update, "✅ TRC20 充值已确认到账，余额已经自动增加。")
        return
    if str(fresh_order.get("state") or "") != "pending":
        await reply_inline(update, "这笔订单已经失效，请重新创建新的充值订单。")
        return
    await reply_inline(update, "暂时还没有检测到这笔 TRC20 转账，请付款后稍等几秒再点一次。")


async def cancel_trc20_topup_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> None:
    _, store, _ = get_services(context)
    user = update.effective_user
    if user is None:
        return
    changed, order = await call_blocking(store.cancel_topup_order, order_id, user_id=user.id, reason="user_canceled")
    if order is None:
        await reply_inline(update, "未找到这笔 TRC20 充值订单。")
        return
    if safe_int(order.get("user_id")) != user.id:
        await reply_inline(update, "这笔充值订单不属于你。")
        return
    if changed:
        if update.callback_query is not None and update.callback_query.message is not None:
            try:
                await update.callback_query.message.delete()
                return
            except BadRequest:
                pass
        await reply_inline(update, "TRC20 充值订单已取消。")
        return
    await reply_inline(update, "这笔订单当前不能取消。")


async def show_recharge(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    selected_channel: str | None = None,
) -> None:
    settings, store, _ = get_services(context)
    user = update.effective_user
    if user is not None:
        await call_blocking(store.ensure_user, user.id, user.username or "", user.full_name or "")
    recharge_address = effective_recharge_address(context)
    okpay_config = effective_okpay_settings(context, settings)
    trc20_available = trc20_enabled(recharge_address)
    okpay_available = okpay_enabled(okpay_config)
    selected_channel = normalize_recharge_channel(selected_channel)
    if selected_channel == "trc20" and not trc20_available:
        selected_channel = None
    if selected_channel == "okpay" and not okpay_available:
        selected_channel = None
    if selected_channel is None:
        if not trc20_available and not okpay_available:
            await reply_inline(update, "当前未开启充值方式，请联系管理员")
            return
        text = "💳 请选择充值方式"
        parse_mode = None
    elif selected_channel == "trc20":
        text = "<b>请选择下面 USDT(TRC20) 充值金额</b>"
        parse_mode = "HTML"
    else:
        text = "<b>请选择下面 OKPay 充值金额</b>"
        parse_mode = "HTML"
    await reply_inline(
        update,
        text,
        build_recharge_keyboard(okpay_config, recharge_address, selected_channel),
        parse_mode=parse_mode,
    )


async def show_customer_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, _, _ = get_services(context)
    text = premium_text_prefix(
        CUSTOMER_SERVICE_EMOJI_ID,
        "☎️",
        f"联系客服：{effective_customer_service_contact(context, settings)}",
    )
    await reply_inline(update, text, parse_mode="HTML")


async def show_notice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await should_ignore_for_closed_business(update, context):
        return
    text = (
        "📖 购买须知\n\n"
        "1. 首次购买建议先少量测试。\n"
        "2. 虚拟商品请及时验货。\n"
        "3. 已发货商品默认不支持无理由退换。\n"
        "4. 如遇问题请尽快联系管理员处理。"
    )
    keyboard = InlineKeyboardMarkup([[premium_inline_button(BUTTON_MAIN_MENU, "nav:menu", HOME_EMOJI_ID)]])
    await reply_inline(update, text, keyboard)


async def show_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await should_ignore_for_closed_business(update, context):
        return
    await send_menu_message(update, "🌐 切换语言功能稍后补上，当前默认中文。")


def build_admin_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("用户列表", callback_data="adm:users:0"),
                InlineKeyboardButton("群发通知", callback_data="adm:bcast:open"),
            ],
            [
                InlineKeyboardButton("充值地址", callback_data="adm:cfg:recharge"),
                InlineKeyboardButton("OKPAY配置", callback_data="adm:cfg:okpay"),
            ],
            [
                InlineKeyboardButton("客服/补货", callback_data="adm:cfg:contact"),
                InlineKeyboardButton("取消当前操作", callback_data="adm:cancel"),
            ],
        ]
    )


def build_admin_broadcast_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("图文设置", callback_data="adm:bcast:setcontent"),
                InlineKeyboardButton("按钮设置", callback_data="adm:bcast:setbutton"),
            ],
            [
                InlineKeyboardButton("查看图文", callback_data="adm:bcast:preview"),
                InlineKeyboardButton("开始群发", callback_data="adm:bcast:start"),
            ],
            [InlineKeyboardButton("关闭", callback_data="adm:home")],
        ]
    )


async def show_admin_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, store, _ = get_services(context)
    user = update.effective_user
    if user is None:
        return
    if not is_admin(settings, user.id):
        await send_menu_message(update, "只有管理员可以使用 /admin。")
        return
    clear_pending_admin_action(context)
    total_users = await call_blocking(store.count_users, True)
    all_users = await call_blocking(store.count_users, False)
    inactive_users = max(0, all_users - total_users)
    text = (
        "管理员后台\n\n"
        f"活跃用户：{total_users}\n"
        f"失效用户：{inactive_users}\n"
        f"营业状态：{business_status_label(context)}\n"
        f"充值地址：{effective_recharge_address(context) or '未配置'}\n"
        f"客服：{effective_customer_service_contact(context, settings)}\n"
        f"补货频道：{effective_restock_channel(context, settings)}"
    )
    await reply_inline(update, text, build_admin_home_keyboard())


async def show_admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    settings, store, _ = get_services(context)
    user = update.effective_user
    if user is None or not is_admin(settings, user.id):
        await send_menu_message(update, "只有管理员可以查看用户列表。")
        return
    total = await call_blocking(store.count_users, True)
    total_pages = max(1, (total + ADMIN_USERS_PAGE_SIZE - 1) // ADMIN_USERS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    rows = await call_blocking(store.list_users, ADMIN_USERS_PAGE_SIZE, page * ADMIN_USERS_PAGE_SIZE, True)
    lines = [f"用户列表 {page + 1}/{total_pages}", "", "排序：有余额用户按余额从高到低，无余额用户按注册先后。", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    start_index = page * ADMIN_USERS_PAGE_SIZE
    for index, row in enumerate(rows, start=1):
        username_text = f"@{row.get('username')}" if row.get("username") else "未设置用户名"
        user_id = int(row.get("user_id") or 0)
        balance_text = format_money(safe_float(row.get("balance")))
        lines.append(f"{start_index + index}. {format_user_created_at(row.get('created_at'))} | {user_label(row)} | {username_text}")
        lines.append(f"ID: <code>{user_id}</code> | 余额: {balance_text} USDT")
        lines.append("")
    if not rows:
        lines.append("暂无活跃用户。")
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("上一页", callback_data=f"adm:users:{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=f"adm:users:{page}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("下一页", callback_data=f"adm:users:{page + 1}"))
    buttons.append(nav_row)
    buttons.append([InlineKeyboardButton("返回后台", callback_data="adm:home")])
    await reply_inline(update, "\n".join(lines).strip(), InlineKeyboardMarkup(buttons), parse_mode="HTML")


async def show_admin_user_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id: int, page: int = 0) -> None:
    settings, store, _ = get_services(context)
    user = update.effective_user
    if user is None or not is_admin(settings, user.id):
        await send_menu_message(update, "只有管理员可以查看用户详情。")
        return
    row = await call_blocking(store.get_user, target_user_id)
    if not row:
        await reply_inline(update, f"找不到用户 {target_user_id}。", InlineKeyboardMarkup([[InlineKeyboardButton("返回列表", callback_data=f"adm:users:{page}")]]))
        return
    username_text = f"@{row.get('username')}" if row.get("username") else "未设置"
    text = (
        "用户详情\n\n"
        f"ID：{row.get('user_id')}\n"
        f"名称：{user_label(row)}\n"
        f"用户名：{username_text}\n"
        f"注册时间：{format_user_created_at(row.get('created_at'))}\n"
        f"余额：{format_money(safe_float(row.get('balance')))} USDT\n"
        f"状态：{'活跃' if safe_int(row.get('is_active'), 1) == 1 else '失效'}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("返回列表", callback_data=f"adm:users:{page}")],
        ]
    )
    await reply_inline(update, text, keyboard)


def format_admin_broadcast_summary(payload: dict[str, Any]) -> str:
    has_content = bool(str(payload.get("text") or "").strip() or str(payload.get("photo_file_id") or "").strip())
    has_button = bool(str(payload.get("button_text") or "").strip() and str(payload.get("button_url") or "").strip())
    content_type = "图片" if str(payload.get("content_type") or "") == "photo" and str(payload.get("photo_file_id") or "").strip() else "文本"
    text_preview = shorten(str(payload.get("text") or "").strip() or "未设置", 60)
    button_preview = "未设置"
    if has_button:
        button_preview = f"{shorten(str(payload.get('button_text') or '').strip(), 20)} -> {shorten(str(payload.get('button_url') or '').strip(), 36)}"
    return (
        "群发通知\n\n"
        f"群发状态：{'已就绪' if has_content else '未设置'}\n"
        f"内容类型：{content_type if has_content else '未设置'}\n"
        f"文案预览：{text_preview}\n"
        f"按钮状态：{'已设置' if has_button else '未设置'}\n"
        f"按钮预览：{button_preview}\n\n"
        "操作说明：\n"
        "1. 点 图文设置 后发送文本，或直接发图片+文案\n"
        "2. 点 按钮设置 后发送：按钮文字 | https://example.com\n"
        "3. 点 查看图文 先预览，再点 开始群发 正式发送"
    )


async def show_admin_broadcast_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = get_or_create_admin_broadcast_draft(context)
    payload = pending.get("payload") or {}
    await reply_inline(update, format_admin_broadcast_summary(payload), build_admin_broadcast_keyboard())


async def show_admin_config_page(update: Update, context: ContextTypes.DEFAULT_TYPE, section: str) -> None:
    settings, _, _ = get_services(context)
    if section == "recharge":
        text = (
            "充值地址配置\n\n"
            f"当前充值地址：{effective_recharge_address(context) or '未配置'}"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("修改充值地址", callback_data="adm:set:raddr")],
                [InlineKeyboardButton("返回后台", callback_data="adm:home")],
            ]
        )
    elif section == "okpay":
        okpay_config = effective_okpay_settings(context, settings)
        text = build_okpay_config_text(okpay_config)
        keyboard = build_okpay_config_keyboard()
    else:
        text = (
            "客服 / 补货配置\n\n"
            f"客服：{effective_customer_service_contact(context, settings)}\n"
            f"补货频道：{effective_restock_channel(context, settings)}"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("修改客服", callback_data="adm:set:cs"),
                    InlineKeyboardButton("修改补货频道", callback_data="adm:set:restock"),
                ],
                [InlineKeyboardButton("返回后台", callback_data="adm:home")],
            ]
        )
    await reply_inline(update, text, keyboard)


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await should_ignore_for_closed_business(update, context):
        return
    await show_admin_home(update, context)


async def prompt_admin_broadcast_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = get_or_create_admin_broadcast_draft(context)
    pending["kind"] = "broadcast_wait_content"
    set_pending_admin_action(context, pending)
    await send_menu_message(update, "请发送群发文案，或者直接发送一张图片并带 caption。")


async def prompt_admin_broadcast_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = get_or_create_admin_broadcast_draft(context)
    pending["kind"] = "broadcast_wait_button"
    set_pending_admin_action(context, pending)
    await send_menu_message(update, "请发送按钮，格式：按钮文字 | https://example.com\n如果要清空按钮，直接发送：-")


async def prompt_admin_setting_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, title: str) -> None:
    set_pending_admin_action(context, {"kind": "setting_edit", "setting_key": key, "setting_title": title})
    if key == RUNTIME_KEY_OKPAY_CONFIG:
        await send_menu_message(
            update,
            "请发送新的 OKPAY 配置。\n"
            "支持 JSON，或者多行 key=value：\n"
            "shop_id=xxx\nshop_token=xxx\nname=号铺\ncallback_url=https://你的域名/okpay/callback\n"
            "如果要清空，直接发：-",
        )
        return
    if key == RUNTIME_KEY_OKPAY_SHOP_ID:
        await send_menu_message(update, "请输入 OKPay 商户ID。\n如果要清空，直接发：-")
        return
    if key == RUNTIME_KEY_OKPAY_SHOP_TOKEN:
        await send_menu_message(update, "请输入 OKPay Token。\n如果要清空，直接发：-")
        return
    if key == RUNTIME_KEY_OKPAY_NAME:
        await send_menu_message(update, "请输入 OKPay 名称，例如：号铺。\n如果要清空，直接发：-")
        return
    if key == RUNTIME_KEY_OKPAY_CALLBACK_URL:
        await send_menu_message(update, "请输入 OKPay 回调地址，例如：https://你的域名/okpay/callback\n如果要清空，直接发：-")
        return
    if key == RUNTIME_KEY_OKPAY_API_URL:
        await send_menu_message(update, "请输入 OKPay API 地址，例如：https://api.okaypay.me/shop\n如果要清空，直接发：-")
        return
    await send_menu_message(update, f"请发送新的 {title}。\n如果要清空，直接发：-")


async def send_admin_preview(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: dict[str, Any], title: str = "消息预览") -> None:
    reply_markup = admin_send_button_markup(payload)
    target_message = update.callback_query.message if update.callback_query is not None else update.message
    if target_message is None:
        return
    await target_message.reply_text(title)
    if payload.get("content_type") == "photo" and payload.get("photo_file_id"):
        await target_message.reply_photo(
            photo=payload["photo_file_id"],
            caption=str(payload.get("text") or "").strip() or None,
            reply_markup=reply_markup,
        )
    else:
        await target_message.reply_text(str(payload.get("text") or "（空文本）"), reply_markup=reply_markup)


async def deliver_admin_payload(context: ContextTypes.DEFAULT_TYPE, user_id: int, payload: dict[str, Any]) -> None:
    reply_markup = admin_send_button_markup(payload)
    if payload.get("content_type") == "photo" and payload.get("photo_file_id"):
        await context.bot.send_photo(
            chat_id=int(user_id),
            photo=payload["photo_file_id"],
            caption=str(payload.get("text") or "").strip() or None,
            reply_markup=reply_markup,
        )
        return
    await context.bot.send_message(
        chat_id=int(user_id),
        text=str(payload.get("text") or "").strip() or " ",
        reply_markup=reply_markup,
    )


async def execute_admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, store, _ = get_services(context)
    user = update.effective_user
    pending = get_or_create_admin_broadcast_draft(context)
    payload = pending.get("payload") or {}
    has_content = bool(str(payload.get("text") or "").strip() or str(payload.get("photo_file_id") or "").strip())
    if user is None or not is_admin(settings, user.id):
        return
    if not has_content:
        await send_menu_message(update, "还没有设置群发内容，先点 图文设置。")
        return

    users = await call_blocking(store.list_users, 100000, 0, True)
    total = len(users)
    sent = 0
    failed = 0
    cleared = 0
    progress_message = None
    if update.callback_query is not None and update.callback_query.message is not None:
        progress_message = await update.callback_query.message.reply_text(f"群发进度：0/{total}")
    elif update.message is not None:
        progress_message = await update.message.reply_text(f"群发进度：0/{total}")
    for index, row in enumerate(users, start=1):
        try:
            await deliver_admin_payload(context, safe_int(row.get("user_id")), payload)
            sent += 1
        except Exception as exc:
            failed += 1
            if is_delivery_failure(exc):
                await call_blocking(store.mark_user_inactive, safe_int(row.get("user_id")))
                cleared += 1
        if progress_message is not None and (index == total or index % 10 == 0):
            try:
                await progress_message.edit_text(f"群发进度：{index}/{total}\n成功：{sent}\n失败：{failed}\n已清理失效用户：{cleared}")
            except BadRequest:
                pass
    await call_blocking(store.log_admin_action, user.id, "admin_broadcast", str(total), f"sent={sent},failed={failed},cleared={cleared}")
    pending["kind"] = "broadcast_idle"
    set_pending_admin_action(context, pending)
    await send_menu_message(update, f"群发完成。\n总数：{total}\n成功：{sent}\n失败：{failed}\n已清理失效用户：{cleared}")


async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    settings, store, _ = get_services(context)
    user = update.effective_user
    pending = get_pending_admin_action(context)
    if user is None or pending is None or not is_admin(settings, user.id):
        return False
    kind = str(pending.get("kind") or "")
    if kind == "setting_edit":
        setting_key = str(pending.get("setting_key") or "")
        setting_title = str(pending.get("setting_title") or "配置")
        value = "" if text.strip() == "-" else text.strip()
        runtime_config = get_runtime_config(context)
        okpay_field_map = {
            RUNTIME_KEY_OKPAY_SHOP_ID: "shop_id",
            RUNTIME_KEY_OKPAY_SHOP_TOKEN: "shop_token",
            RUNTIME_KEY_OKPAY_NAME: "name",
            RUNTIME_KEY_OKPAY_CALLBACK_URL: "callback_url",
            RUNTIME_KEY_OKPAY_API_URL: "api_url",
        }
        if setting_key in okpay_field_map:
            merged_value = update_okpay_runtime_config(
                str(runtime_config.get(RUNTIME_KEY_OKPAY_CONFIG) or ""),
                settings,
                okpay_field_map[setting_key],
                value,
            )
            await call_blocking(store.set_runtime_setting, RUNTIME_KEY_OKPAY_CONFIG, merged_value, user.id)
            runtime_config[RUNTIME_KEY_OKPAY_CONFIG] = merged_value
            for legacy_key in (
                RUNTIME_KEY_OKPAY_SHOP_ID,
                RUNTIME_KEY_OKPAY_SHOP_TOKEN,
                RUNTIME_KEY_OKPAY_NAME,
                RUNTIME_KEY_OKPAY_CALLBACK_URL,
                RUNTIME_KEY_OKPAY_API_URL,
            ):
                await call_blocking(store.set_runtime_setting, legacy_key, "", user.id)
                runtime_config.pop(legacy_key, None)
        else:
            await call_blocking(store.set_runtime_setting, setting_key, value, user.id)
            runtime_config[setting_key] = value
            if setting_key == RUNTIME_KEY_OKPAY_CONFIG:
                for legacy_key in (
                    RUNTIME_KEY_OKPAY_SHOP_ID,
                    RUNTIME_KEY_OKPAY_SHOP_TOKEN,
                    RUNTIME_KEY_OKPAY_NAME,
                    RUNTIME_KEY_OKPAY_CALLBACK_URL,
                    RUNTIME_KEY_OKPAY_API_URL,
                ):
                    await call_blocking(store.set_runtime_setting, legacy_key, "", user.id)
                    runtime_config.pop(legacy_key, None)
        await call_blocking(store.log_admin_action, user.id, "admin_setting_update", setting_key, value)
        clear_pending_admin_action(context)
        if setting_key == RUNTIME_KEY_OKPAY_CONFIG or setting_key in okpay_field_map:
            await ensure_okpay_callback_server(context.application)
        await send_menu_message(update, f"{setting_title} 已更新。")
        return True
    if kind == "broadcast_wait_content":
        draft = get_or_create_admin_broadcast_draft(context)
        draft["payload"] = {
            "content_type": "text",
            "photo_file_id": "",
            "text": text,
            "button_text": str((draft.get("payload") or {}).get("button_text") or ""),
            "button_url": str((draft.get("payload") or {}).get("button_url") or ""),
        }
        draft["kind"] = "broadcast_idle"
        set_pending_admin_action(context, draft)
        await send_menu_message(update, "群发图文已保存。")
        await show_admin_broadcast_panel(update, context)
        return True
    if kind == "broadcast_wait_button":
        draft = get_or_create_admin_broadcast_draft(context)
        if text.strip() == "-":
            payload = draft.get("payload") or {}
            payload["button_text"] = ""
            payload["button_url"] = ""
            draft["payload"] = payload
            draft["kind"] = "broadcast_idle"
            set_pending_admin_action(context, draft)
            await send_menu_message(update, "群发按钮已清空。")
            await show_admin_broadcast_panel(update, context)
            return True
        pieces = [part.strip() for part in text.split("|", 1)]
        if len(pieces) != 2 or not pieces[0] or not pieces[1].startswith(("http://", "https://")):
            await update.message.reply_text("格式不对，请按这个发：按钮文字 | https://example.com", reply_markup=MENU_KEYBOARD)
            return True
        payload = draft.get("payload") or {}
        payload["button_text"] = pieces[0]
        payload["button_url"] = pieces[1]
        draft["payload"] = payload
        draft["kind"] = "broadcast_idle"
        set_pending_admin_action(context, draft)
        await send_menu_message(update, "群发按钮已保存。")
        await show_admin_broadcast_panel(update, context)
        return True
    if kind == "send_content":
        pending["payload"] = {
            "content_type": "text",
            "text": text,
            "button_text": "",
            "button_url": "",
        }
        pending["kind"] = "send_button_choice"
        set_pending_admin_action(context, pending)
        await update.message.reply_text(
            "消息内容已记录。要不要加按钮？",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("直接预览", callback_data="adm:sendopt:none"),
                        InlineKeyboardButton("添加按钮", callback_data="adm:sendopt:add"),
                    ],
                    [InlineKeyboardButton("取消", callback_data="adm:cancel")],
                ]
            ),
        )
        return True
    if kind == "send_button_choice":
        await update.message.reply_text("请直接点按钮选择“直接预览”或“添加按钮”。", reply_markup=MENU_KEYBOARD)
        return True
    if kind == "send_button":
        pieces = [part.strip() for part in text.split("|", 1)]
        if len(pieces) != 2 or not pieces[0] or not pieces[1].startswith(("http://", "https://")):
            await update.message.reply_text("格式不对，请按这个发：按钮文字 | https://example.com", reply_markup=MENU_KEYBOARD)
            return True
        payload = pending.get("payload") or {}
        payload["button_text"] = pieces[0]
        payload["button_url"] = pieces[1]
        pending["payload"] = payload
        await send_admin_preview(update, context, payload)
        return True
    if kind == "send_ready":
        await update.message.reply_text("预览已经生成了，直接点“确认发送”或“取消”就行。", reply_markup=MENU_KEYBOARD)
        return True
    return False


async def handle_admin_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, _, _ = get_services(context)
    user = update.effective_user
    pending = get_pending_admin_action(context)
    if user is None or update.message is None or pending is None or not is_admin(settings, user.id):
        return
    kind = str(pending.get("kind") or "")
    if kind not in {"send_content", "broadcast_wait_content"}:
        return
    photo = update.message.photo[-1] if update.message.photo else None
    if photo is None:
        return
    if kind == "broadcast_wait_content":
        draft = get_or_create_admin_broadcast_draft(context)
        payload = draft.get("payload") or {}
        payload["content_type"] = "photo"
        payload["photo_file_id"] = photo.file_id
        payload["text"] = str(update.message.caption or "").strip()
        draft["payload"] = payload
        draft["kind"] = "broadcast_idle"
        set_pending_admin_action(context, draft)
        await send_menu_message(update, "群发图文已保存。")
        await show_admin_broadcast_panel(update, context)
        return
    pending["payload"] = {
        "content_type": "photo",
        "photo_file_id": photo.file_id,
        "text": str(update.message.caption or "").strip(),
        "button_text": "",
        "button_url": "",
    }
    pending["kind"] = "send_button_choice"
    set_pending_admin_action(context, pending)
    await update.message.reply_text(
        "图片内容已记录。要不要加按钮？",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("直接预览", callback_data="adm:sendopt:none"),
                    InlineKeyboardButton("添加按钮", callback_data="adm:sendopt:add"),
                ],
                [InlineKeyboardButton("取消", callback_data="adm:cancel")],
            ]
        ),
    )


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    settings, _, _ = get_services(context)
    user = update.effective_user
    query = update.callback_query
    if query is None or user is None:
        return
    if not is_admin(settings, user.id):
        await query.answer("只有管理员可以操作", show_alert=True)
        return
    sub = parts[1] if len(parts) > 1 else ""
    if sub == "home":
        await show_admin_home(update, context)
        return
    if sub == "cancel":
        clear_pending_admin_action(context)
        await send_menu_message(update, "已取消当前管理员操作。")
        return
    if sub == "users":
        await show_admin_users(update, context, safe_int(parts[2], 0) if len(parts) > 2 else 0)
        return
    if sub == "user" and len(parts) > 3:
        await show_admin_user_detail(update, context, safe_int(parts[2], 0), safe_int(parts[3], 0))
        return
    if sub == "send" and len(parts) > 2:
        if parts[2] == "all":
            await show_admin_broadcast_panel(update, context)
            return
    if sub == "bcast" and len(parts) > 2:
        action = parts[2]
        if action == "open":
            await show_admin_broadcast_panel(update, context)
            return
        if action == "setcontent":
            await prompt_admin_broadcast_content(update, context)
            return
        if action == "setbutton":
            await prompt_admin_broadcast_button(update, context)
            return
        if action == "preview":
            pending = get_or_create_admin_broadcast_draft(context)
            payload = pending.get("payload") or {}
            has_content = bool(str(payload.get("text") or "").strip() or str(payload.get("photo_file_id") or "").strip())
            if not has_content:
                await send_menu_message(update, "还没有设置群发内容，先点 图文设置。")
                return
            await send_admin_preview(update, context, payload, "群发预览（仅管理员可见）")
            return
        if action == "start":
            await execute_admin_broadcast(update, context)
            return
    if sub == "sendu" and len(parts) > 2:
        await send_menu_message(update, "单独私信入口已经关闭，请直接使用 群发通知。")
        return
    if sub == "sendopt" and len(parts) > 2:
        pending = get_pending_admin_action(context)
        if pending is None:
            await query.answer("没有待发送内容", show_alert=True)
            return
        if parts[2] == "none":
            await send_admin_preview(update, context, pending.get("payload") or {})
            return
        pending["kind"] = "send_button"
        set_pending_admin_action(context, pending)
        await send_menu_message(update, "请发送按钮，格式：按钮文字 | https://example.com")
        return
    if sub == "sendgo":
        await execute_admin_broadcast(update, context)
        return
    if sub == "cfg" and len(parts) > 2:
        await show_admin_config_page(update, context, parts[2])
        return
    if sub == "set" and len(parts) > 2:
        mapping = {
            "raddr": (RUNTIME_KEY_RECHARGE_ADDRESS, "充值地址"),
            "okpay": (RUNTIME_KEY_OKPAY_CONFIG, "OKPAY 配置"),
            "okid": (RUNTIME_KEY_OKPAY_SHOP_ID, "OKPay 商户ID"),
            "oktoken": (RUNTIME_KEY_OKPAY_SHOP_TOKEN, "OKPay Token"),
            "okname": (RUNTIME_KEY_OKPAY_NAME, "OKPay 名称"),
            "okcallback": (RUNTIME_KEY_OKPAY_CALLBACK_URL, "OKPay 回调地址"),
            "okapi": (RUNTIME_KEY_OKPAY_API_URL, "OKPay API 地址"),
            "cs": (RUNTIME_KEY_CUSTOMER_SERVICE, "客服联系方式"),
            "restock": (RUNTIME_KEY_RESTOCK_CHANNEL, "补货频道"),
        }
        if parts[2] in mapping:
            key, title = mapping[parts[2]]
            await prompt_admin_setting_edit(update, context, key, title)
            return
    await query.answer("暂不支持这个后台按钮", show_alert=False)


async def show_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, store, _ = get_services(context)
    user = update.effective_user
    if user is None:
        return
    rows = await call_blocking(store.list_user_orders, user.id, 10)
    text = build_orders_text(rows)
    keyboard = InlineKeyboardMarkup([[premium_inline_button(BUTTON_MAIN_MENU, "nav:menu", HOME_EMOJI_ID)]])
    await reply_inline(update, text, keyboard, parse_mode="HTML")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await should_ignore_for_closed_business(update, context):
        return
    clear_pending_purchase(context)
    clear_pending_recharge(context)
    await show_start_menu(update, context)


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await should_ignore_for_closed_business(update, context):
        return
    clear_pending_purchase(context)
    clear_pending_recharge(context)
    await show_start_menu(update, context)


async def me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await should_ignore_for_closed_business(update, context):
        return
    await show_profile(update, context)


async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await should_ignore_for_closed_business(update, context):
        return
    await show_categories(update, context)


async def products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await should_ignore_for_closed_business(update, context):
        return
    if update.message is None:
        return
    if not context.args:
        await update.message.reply_text("用法: /products <category_id>")
        return
    try:
        category_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("category_id 必须是数字")
        return
    await show_products(update, context, category_id, page=0)


async def product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await should_ignore_for_closed_business(update, context):
        return
    settings, _, supplier = get_services(context)
    if update.message is None:
        return
    if not context.args:
        await update.message.reply_text("用法: /product <product_id>")
        return
    try:
        product_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("product_id 必须是数字")
        return
    try:
        payload = await call_blocking(supplier.get_product_detail, product_id)
    except SupplierApiError as exc:
        await update.message.reply_text(f"获取商品详情失败: {exc}")
        return
    row = payload.get("data") or {}
    text, entities, keyboard = render_product_detail_view_configured(settings, row, category_id=0, page=0)
    await update.message.reply_text(text, entities=entities, reply_markup=keyboard)


async def execute_purchase(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    username: str,
    display_name: str,
    product_id: int,
    quantity: int,
) -> tuple[str, tuple[MessageEntity, ...] | None] | None:
    settings, store, supplier = get_services(context)
    await call_blocking(store.ensure_user, user_id, username, display_name)

    detail_payload = await call_blocking(supplier.get_product_detail, product_id)
    row = detail_payload.get("data") or {}
    unit_price = resolve_sell_price(settings, row)
    total_stock = safe_int(row.get("totalStock"))
    product_name = str(row.get("productName") or f"商品 {product_id}")
    category_name = str(row.get("categoryName") or "").strip()
    product_label = f"{category_name} {product_name}".strip() if category_name else product_name
    total_price = unit_price * quantity

    if total_stock < quantity:
        return f"库存不足。当前库存 {total_stock}，你要买 {quantity}", None

    ok, remain = await call_blocking(
        store.debit_balance,
        user_id,
        total_price,
        "purchase",
        "",
        f"{product_name} x{quantity}",
    )
    if not ok:
        return (
            "余额不足。\n"
            f"当前余额: {format_money(remain)} USDT\n"
            f"本次需要: {format_money(total_price)} USDT"
        ), None

    try:
        buy_payload = await call_blocking(supplier.buy_product, product_id, quantity)
    except SupplierApiError as exc:
        refunded = await call_blocking(
            store.add_balance,
            user_id,
            total_price,
            "purchase_refund",
            "",
            f"下单失败退款: {product_name}",
        )
        logger.warning("上游下单失败，已退款 user_id=%s product_id=%s quantity=%s error=%s", user_id, product_id, quantity, exc)
        return build_purchase_refund_error_text(total_price, refunded)

    data = buy_payload.get("data") or {}
    task_id = str(data.get("taskId") or "").strip()
    if not task_id:
        reason_map = {
            "1": "上游余额不足",
            "2": "上游库存不足",
            "3": "上游创建订单失败",
        }
        upstream_reason = reason_map.get(str(data.get("type") or ""), "上游未返回 taskId")
        refunded = await call_blocking(
            store.add_balance,
            user_id,
            total_price,
            "purchase_refund",
            "",
            f"下单失败退款: {product_name}",
        )
        logger.warning("上游创建订单失败，已退款 user_id=%s product_id=%s quantity=%s reason=%s", user_id, product_id, quantity, upstream_reason)
        return build_purchase_refund_error_text(total_price, refunded)

    await call_blocking(
        store.record_order,
        task_id,
        user_id,
        username,
        product_id,
        product_name,
        quantity,
        unit_price,
        total_price,
        buy_payload,
    )
    await notify_admin_new_purchase(
        context,
        settings.admin_user_ids,
        user_id,
        username,
        display_name,
        product_label,
        quantity,
        total_price,
        remain,
    )
    schedule_fast_order_probe(context, task_id)
    return None


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await should_ignore_for_closed_business(update, context):
        return
    user = update.effective_user
    if user is None or update.message is None:
        return
    if len(context.args) < 2:
        await update.message.reply_text("用法: /buy <product_id> <数量>")
        return
    try:
        product_id = int(context.args[0])
        quantity = int(context.args[1])
    except ValueError:
        await update.message.reply_text("product_id 和 数量 都必须是数字")
        return
    if quantity <= 0:
        await update.message.reply_text("数量必须大于 0")
        return

    try:
        result = await execute_purchase(context, user.id, user.username or "", user.full_name or "", product_id, quantity)
    except SupplierApiError as exc:
        await update.message.reply_text(f"获取商品详情失败: {exc}")
        return
    if result:
        result_text, result_entities = result
        await update.message.reply_text(result_text, entities=result_entities, reply_markup=MENU_KEYBOARD)
    else:
        await update.message.reply_text(order_created_caption(), reply_markup=MENU_KEYBOARD, parse_mode="HTML")


async def finalize_remote_order(
    context: ContextTypes.DEFAULT_TYPE,
    task_id: str,
    notify_user: bool,
) -> tuple[str, str]:
    _, store, supplier = get_services(context)
    order = await call_blocking(store.get_order, task_id)
    if not order:
        return "missing", "本地没有这笔订单"

    try:
        payload = await call_blocking(supplier.query_order, task_id)
    except SupplierApiError as exc:
        return "error", f"查询上游订单失败: {exc}"

    data = payload.get("data") or {}
    status = safe_int(data.get("taskStatus"))
    quantity_success = safe_int(data.get("quantitySuccess"))
    file_url = str(data.get("fileUrl") or "").strip()
    quantity = safe_int(order["quantity"])
    unit_price = safe_float(order["unit_price"])
    total_price = safe_float(order["total_price"])

    if status == 2:
        return "processing", "订单仍在处理中"

    if status == 3:
        final_row, changed = await call_blocking(
            store.finalize_order,
            task_id,
            "failed",
            0,
            file_url,
            total_price,
            payload,
        )
        if changed and notify_user and final_row:
            await context.bot.send_message(
                chat_id=int(final_row["user_id"]),
                text=(
                    "订单失败，已自动退款。\n"
                    f"订单号: {task_id}\n"
                    f"退款: {format_money(total_price)} USDT"
                ),
                reply_markup=MENU_KEYBOARD,
            )
        return "failed", "订单失败，已退款"

    if status == 1:
        refund_amount = 0.0
        final_state = "completed"
        if 0 <= quantity_success < quantity:
            refund_amount = (quantity - quantity_success) * unit_price
            final_state = "partial"
        final_row, changed = await call_blocking(
            store.finalize_order,
            task_id,
            final_state,
            quantity_success,
            file_url,
            refund_amount,
            payload,
        )
        if changed and notify_user and final_row:
            lines = [
                f"订单号: {task_id}",
                f"成功数量: {quantity_success}/{quantity}",
            ]
            if refund_amount > 0:
                lines.append(f"已退款: {format_money(refund_amount)} USDT")
            await context.bot.send_message(
                chat_id=int(final_row["user_id"]),
                text="\n".join(lines),
                reply_markup=MENU_KEYBOARD,
            )
        if final_row and file_url and not str(final_row.get("delivery_sent_at") or "").strip():
            await deliver_order_file(
                context,
                final_row,
                supplier,
                include_ready_photo=notify_user,
                notify_failure=notify_user,
            )
        summary = f"订单完成，成功数量 {quantity_success}/{quantity}"
        if refund_amount > 0:
            summary += f"，已退款 {format_money(refund_amount)} USDT"
        return final_state, summary

    return "unknown", f"未知订单状态: {status}"


async def poll_single_processing_order(
    context: ContextTypes.DEFAULT_TYPE,
    task_id: str,
) -> None:
    try:
        await finalize_remote_order(context, task_id, notify_user=True)
    except Exception:
        logger.exception("轮询订单失败: %s", task_id)


async def poll_single_processing_order_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = getattr(context, "job", None)
    payload = getattr(job, "data", None) or {}
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        return
    await poll_single_processing_order(context, task_id)


def schedule_fast_order_probe(context: ContextTypes.DEFAULT_TYPE, task_id: str) -> None:
    settings, _, _ = get_services(context)
    application = getattr(context, "application", None)
    job_queue = getattr(application, "job_queue", None)
    if job_queue is None:
        return
    job_queue.run_once(
        poll_single_processing_order_job,
        when=settings.order_fast_probe_seconds,
        data={"task_id": str(task_id)},
    )


async def order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await should_ignore_for_closed_business(update, context):
        return
    _, store, supplier = get_services(context)
    if update.message is None:
        return
    if not context.args:
        await update.message.reply_text("用法: /order <task_id>")
        return
    task_id = context.args[0].strip()
    local_order = await call_blocking(store.get_order, task_id)
    if not local_order:
        await update.message.reply_text("本地没有这笔订单记录。")
        return
    _, summary = await finalize_remote_order(context, task_id, notify_user=False)
    local_order = await call_blocking(store.get_order, task_id) or local_order
    if local_order.get("file_url") and not str(local_order.get("delivery_sent_at") or "").strip():
        await deliver_order_file(
            context,
            local_order,
            supplier,
            include_ready_photo=True,
            notify_failure=True,
        )
        local_order = await call_blocking(store.get_order, task_id) or local_order
    lines = [
        f"订单号: {task_id}",
        f"商品: {local_order.get('product_name')}",
        f"状态: {local_order.get('state')}",
        f"数量: {local_order.get('quantity')}",
        f"成功数量: {local_order.get('quantity_success')}",
        f"退款: {format_money(safe_float(local_order.get('refund_amount')))} USDT",
        f"结果: {summary}",
    ]
    if local_order.get("file_url"):
        if str(local_order.get("delivery_sent_at") or "").strip():
            lines.append("发货文件: zip 已发送")
        else:
            lines.append("发货文件: zip 待发送，可用 /order 重试")
    await update.message.reply_text("\n".join(lines), reply_markup=MENU_KEYBOARD)


async def orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await should_ignore_for_closed_business(update, context):
        return
    await show_orders(update, context)


async def supplier_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await should_ignore_for_closed_business(update, context):
        return
    settings, _, supplier = get_services(context)
    user = update.effective_user
    if user is None or update.message is None:
        return
    if not is_admin(settings, user.id):
        await update.message.reply_text("只有管理员可以查看上游余额。")
        return
    try:
        payload = await call_blocking(supplier.query_balance)
    except SupplierApiError as exc:
        await update.message.reply_text(f"查询上游余额失败: {exc}")
        return
    data = payload.get("data") or {}
    await update.message.reply_text(
        "上游余额:\n"
        f"userId: {data.get('userId')}\n"
        f"userName: {data.get('userName')}\n"
        f"accountBalance: {data.get('accountBalance')}",
        reply_markup=MENU_KEYBOARD,
    )


async def credit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await should_ignore_for_closed_business(update, context):
        return
    settings, store, _ = get_services(context)
    user = update.effective_user
    if user is None or update.message is None:
        return
    if not is_admin(settings, user.id):
        await update.message.reply_text("只有管理员可以调整余额。")
        return
    if len(context.args) < 2:
        await update.message.reply_text("用法: /add <user_id> <+金额/-金额>\n示例: /add 123456 +20 或 /add 123456 -20")
        return
    try:
        target_user_id = int(context.args[0])
        amount = float(context.args[1])
    except ValueError:
        await update.message.reply_text("user_id 或 金额 格式不对")
        return
    if amount == 0:
        await update.message.reply_text("金额不能为 0")
        return
    if amount > 0:
        balance = await call_blocking(
            store.add_balance,
            target_user_id,
            amount,
            "admin_credit",
            "",
            f"by {user.id}",
        )
        text, text_entities = build_admin_add_balance_text(target_user_id, amount, balance)
        user_notice, user_notice_entities = build_balance_change_notice_text("已增加", amount, balance)
    else:
        debit_amount = abs(amount)
        ok, balance = await call_blocking(
            store.debit_balance,
            target_user_id,
            debit_amount,
            "admin_debit",
            "",
            f"by {user.id}",
        )
        if not ok:
            await update.message.reply_text(
                f"扣减失败，用户 {target_user_id} 余额不足。\n"
                f"当前余额: {format_money(balance)} USDT\n"
                f"尝试扣减: {format_money(debit_amount)} USDT",
                reply_markup=MENU_KEYBOARD,
            )
            return
        text = (
            f"已给用户 {target_user_id} 扣减 {format_money(debit_amount)} USDT\n"
            f"当前余额: {format_money(balance)} USDT"
        )
        text_entities = None
        user_notice, user_notice_entities = build_balance_change_notice_text("已扣减", debit_amount, balance)
    if target_user_id != user.id:
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=user_notice,
                entities=user_notice_entities,
                reply_markup=MENU_KEYBOARD,
            )
        except Exception:
            logger.exception("发送余额变动提醒失败: %s", target_user_id)
    await update.message.reply_text(text, entities=text_entities, reply_markup=MENU_KEYBOARD)


async def route_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await should_ignore_for_closed_business(update, context):
        return
    if update.message is None or not update.message.text:
        return
    text = update.message.text.strip()
    if text == BUTTON_PRODUCTS or text == BUTTON_ACCOUNT_LIST:
        clear_pending_purchase(context)
        clear_pending_recharge(context)
        await show_categories(update, context)
        return
    if text in {BUTTON_MAIN_MENU, BOTTOM_BUTTON_MAIN_MENU, LEGACY_BOTTOM_BUTTON_MAIN_MENU}:
        clear_pending_purchase(context)
        clear_pending_recharge(context)
        await show_start_menu(update, context)
        return
    if text in {BUTTON_PROFILE, BUTTON_RECHARGE_BALANCE, BOTTOM_BUTTON_RECHARGE_BALANCE, LEGACY_BOTTOM_BUTTON_RECHARGE_BALANCE}:
        clear_pending_purchase(context)
        clear_pending_recharge(context)
        await show_recharge(update, context)
        return
    if text in {BOTTOM_BUTTON_CUSTOMER_SERVICE, LEGACY_BOTTOM_BUTTON_CUSTOMER_SERVICE}:
        clear_pending_purchase(context)
        clear_pending_recharge(context)
        await show_customer_service(update, context)
        return
    if text == BUTTON_PURCHASE_NOTICE:
        clear_pending_purchase(context)
        clear_pending_recharge(context)
        await show_notice(update, context)
        return
    if text == BUTTON_ORDER_HISTORY:
        clear_pending_purchase(context)
        clear_pending_recharge(context)
        await show_orders(update, context)
        return
    if text == BUTTON_SWITCH_LANGUAGE:
        clear_pending_purchase(context)
        clear_pending_recharge(context)
        await show_language(update, context)


async def search_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, _, supplier = get_services(context)
    if update.message is None or not update.message.text:
        return
    keyword = normalize_search_keyword(update.message.text)
    if not should_trigger_product_search(keyword):
        return
    try:
        payload = await call_blocking(supplier.search_products, keyword)
    except SupplierApiError as exc:
        await update.message.reply_text(f"搜索失败: {exc}")
        return
    rows = payload.get("data") or []
    if not rows:
        await update.message.reply_text("没有搜到商品。", reply_markup=MENU_KEYBOARD)
        return

    text, entities = build_search_results_text(keyword, rows, lambda row: safe_float(row.get("price")))
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows[:SEARCH_RESULTS_LIMIT]:
        product_id = safe_int(row.get("productId"))
        category_id = safe_int(row.get("categoryId"))
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"查看 {shorten(str(row.get('productName')), 22)}",
                    callback_data=f"prd:{product_id}:{category_id}:0",
                )
            ]
        )
    buttons.append([InlineKeyboardButton("🛒 浏览全部分类", callback_data="nav:cats")])
    await update.message.reply_text(text, entities=entities, reply_markup=InlineKeyboardMarkup(buttons))


async def search_text_rich(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not update.message.text:
        return
    keyword = normalize_search_keyword(update.message.text)
    if await handle_admin_business_toggle(update, context, keyword):
        return
    if await should_ignore_for_closed_business(update, context):
        return
    settings, _, supplier = get_services(context)
    if await handle_admin_text_input(update, context, keyword):
        return
    pending_recharge = get_pending_recharge(context)
    if pending_recharge is not None:
        try:
            amount = quantize_recharge_amount(float(keyword))
        except ValueError:
            await update.message.reply_text("请输入数字", reply_markup=MENU_KEYBOARD)
            return
        if amount <= 0:
            await update.message.reply_text("充值金额必须大于 0。", reply_markup=MENU_KEYBOARD)
            return
        channel = str(pending_recharge.get("channel") or "okpay").strip().lower()
        clear_pending_recharge(context)
        if channel == "trc20":
            await create_trc20_topup_order(update, context, amount)
        else:
            await create_okpay_topup_order(update, context, amount)
        return
    pending_purchase = get_pending_purchase(context)
    if pending_purchase is not None:
        quantity = safe_int(keyword, -1)
        if quantity <= 0:
            await update.message.reply_text("请输入要购买的数量，直接发数字即可，例如：1", reply_markup=MENU_KEYBOARD)
            return
        clear_pending_purchase(context)
        product_id = safe_int(pending_purchase.get("product_id"), -1)
        category_id = safe_int(pending_purchase.get("category_id"), 0)
        page = safe_int(pending_purchase.get("page"), 0)
        try:
            payload = await call_blocking(supplier.get_product_detail, product_id)
        except SupplierApiError as exc:
            await update.message.reply_text(f"获取商品详情失败: {exc}", reply_markup=MENU_KEYBOARD)
            return
        row = payload.get("data") or {}
        product_name = str(row.get("productName") or f"商品 {product_id}")
        unit_price = resolve_sell_price(settings, row)
        caption, caption_entities = build_purchase_confirm_text(product_name, unit_price, quantity)
        keyboard = build_purchase_confirm_keyboard(product_id, quantity, category_id, page)
        if PURCHASE_CONFIRM_IMAGE_PATH.exists():
            with PURCHASE_CONFIRM_IMAGE_PATH.open("rb") as photo_fp:
                await update.message.reply_photo(
                    photo=photo_fp,
                    caption=caption,
                    caption_entities=caption_entities,
                    reply_markup=keyboard,
                )
        else:
            await update.message.reply_text(caption, entities=caption_entities, reply_markup=keyboard)
        return
    if not should_trigger_product_search(keyword):
        return
    try:
        payload = await call_blocking(supplier.search_products, keyword)
    except SupplierApiError as exc:
        await update.message.reply_text(f"搜索失败: {exc}")
        return
    rows = payload.get("data") or []
    if not rows:
        await update.message.reply_text("没有搜到商品。", reply_markup=MENU_KEYBOARD)
        return

    text, entities = build_search_results_text(keyword, rows, lambda row: resolve_sell_price(settings, row))
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows[:SEARCH_RESULTS_LIMIT]:
        product_id = safe_int(row.get("productId"))
        category_id = safe_int(row.get("categoryId"))
        sell_price = resolve_sell_price(settings, row)
        buttons.append([plain_catalog_button(f"{shorten(str(row.get('productName')), 22)} | ${sell_price:.2f}", f"prd:{product_id}:{category_id}:0")])
    buttons.append([InlineKeyboardButton("🛒 浏览全部分类", callback_data="nav:cats")])
    await update.message.reply_text(text, entities=entities, reply_markup=InlineKeyboardMarkup(buttons))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await should_ignore_for_closed_business(update, context):
        return
    settings, _, supplier = get_services(context)
    query = update.callback_query
    if query is None or not query.data:
        return

    parts = query.data.split(":")
    action = parts[0]
    if action != "rchg":
        clear_pending_recharge(context)

    if action == "adm":
        await handle_admin_callback(update, context, parts)
        return

    if action == "nav":
        clear_pending_purchase(context)
        clear_pending_recharge(context)
        target = parts[1] if len(parts) > 1 else ""
        if target == "cats":
            await show_categories(update, context)
            return
        if target == "menu":
            await show_start_menu(update, context)
            return
        if target == "profile":
            await show_profile(update, context)
            return
        if target == "recharge":
            await show_recharge(update, context)
            return
        if target == "orders":
            await show_orders(update, context)
            return
        if target == "close":
            await reply_inline(update, "已关闭。")
            return

    if action == "rchg" and len(parts) >= 2:
        if parts[1] == "back":
            clear_pending_purchase(context)
            clear_pending_recharge(context)
            await show_recharge(update, context, None)
            return
        if parts[1] == "close":
            clear_pending_purchase(context)
            clear_pending_recharge(context)
            if update.callback_query is not None and update.callback_query.message is not None:
                try:
                    await update.callback_query.message.delete()
                    return
                except BadRequest:
                    pass
            await reply_inline(update, "已取消充值。")
            return
        if parts[1] in {"custom", "create", "paid", "cancel"}:
            legacy_parts = ["rchg", "okpay", *parts[1:]]
            parts = legacy_parts
        if parts[1] == "select" and len(parts) >= 3:
            clear_pending_purchase(context)
            clear_pending_recharge(context)
            await show_recharge(update, context, parts[2])
            return
        channel = parts[1] if len(parts) > 1 else "okpay"
        if channel not in {"okpay", "trc20"}:
            channel = "okpay"
        subaction = parts[2] if len(parts) > 2 else ""
        if subaction == "custom":
            clear_pending_purchase(context)
            set_pending_recharge(context, channel)
            prompt = "请输入充值金额" if channel == "trc20" else "请输入OKPay充值金额"
            await reply_inline(update, prompt)
            return
        if subaction == "create" and len(parts) >= 4:
            clear_pending_purchase(context)
            clear_pending_recharge(context)
            amount = safe_float(parts[3], 0.0)
            if channel == "trc20":
                await create_trc20_topup_order(update, context, amount)
            else:
                await create_okpay_topup_order(update, context, amount)
            return
        if subaction == "paid" and len(parts) >= 4:
            clear_pending_purchase(context)
            clear_pending_recharge(context)
            if channel == "trc20":
                await check_trc20_topup_order(update, context, parts[3])
            else:
                await check_okpay_topup_order(update, context, parts[3])
            return
        if subaction == "cancel" and len(parts) >= 4:
            clear_pending_purchase(context)
            clear_pending_recharge(context)
            if channel == "trc20":
                await cancel_trc20_topup_order(update, context, parts[3])
            else:
                await cancel_okpay_topup_order(update, context, parts[3])
            return

    if action == "cat" and len(parts) == 3:
        clear_pending_purchase(context)
        category_id = safe_int(parts[1], -1)
        page = safe_int(parts[2], 0)
        if category_id <= 0:
            await reply_inline(update, "分类参数不合法。")
            return
        await show_products(update, context, category_id, page)
        return

    if action == "prd" and len(parts) == 4:
        clear_pending_purchase(context)
        product_id = safe_int(parts[1], -1)
        category_id = safe_int(parts[2], 0)
        page = safe_int(parts[3], 0)
        if product_id <= 0:
            await reply_inline(update, "商品参数不合法。")
            return
        try:
            payload = await call_blocking(supplier.get_product_detail, product_id)
        except SupplierApiError as exc:
            await reply_inline(update, f"获取商品详情失败: {exc}")
            return
        row = payload.get("data") or {}
        text, entities, keyboard = render_product_detail_view_configured(settings, row, category_id, page)
        await reply_inline(update, text, keyboard, entities=entities)
        return

    if action == "qbuy" and len(parts) == 5:
        product_id = safe_int(parts[1], -1)
        category_id = safe_int(parts[3], 0)
        page = safe_int(parts[4], 0)
        if product_id <= 0:
            await reply_inline(update, "快捷购买参数不合法。")
            return
        set_pending_purchase(context, product_id, category_id, page)
        await reply_inline(update, "请发送需要购买的数量，直接回复数字即可，例如：1")
        return

    if action == "cbuy" and len(parts) == 3:
        clear_pending_purchase(context)
        user = update.effective_user
        product_id = safe_int(parts[1], -1)
        quantity = safe_int(parts[2], 0)
        if user is None or product_id <= 0 or quantity <= 0:
            await reply_inline(update, "快捷购买参数不合法。")
            return
        await query.answer("正在创建订单...")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
        try:
            result = await execute_purchase(context, user.id, user.username or "", user.full_name or "", product_id, quantity)
        except SupplierApiError as exc:
            if query.message is not None:
                await query.message.reply_text(f"获取商品详情失败: {exc}", reply_markup=MENU_KEYBOARD)
            else:
                await reply_inline(update, f"获取商品详情失败: {exc}")
            return
        if result and query.message is not None:
            result_text, result_entities = result
            await query.message.reply_text(result_text, entities=result_entities, reply_markup=MENU_KEYBOARD)
        elif result:
            result_text, result_entities = result
            await reply_inline(update, result_text, entities=result_entities)
        elif query.message is not None:
            await query.message.reply_text(
                order_created_caption(),
                reply_markup=MENU_KEYBOARD,
                parse_mode="HTML",
            )
        return

    await query.answer("暂不支持这个按钮", show_alert=False)


async def poll_processing_orders(context: ContextTypes.DEFAULT_TYPE) -> None:
    application = getattr(context, "application", None)
    if application is None:
        return
    poll_lock = application.bot_data.setdefault("order_poll_lock", asyncio.Lock())
    if poll_lock.locked():
        logger.info("订单轮询仍在执行，跳过本轮")
        return

    async with poll_lock:
        settings, store, supplier = get_services(context)
        processing_rows = await call_blocking(store.list_processing_orders, settings.order_poll_limit)
        pending_delivery_rows = await call_blocking(
            store.list_pending_delivery_orders,
            settings.order_poll_limit,
            settings.delivery_retry_cooldown_seconds,
        )
        if not processing_rows and not pending_delivery_rows:
            return

        semaphore = asyncio.Semaphore(settings.order_poll_concurrency)

        async def run_processing_row(task_id: str) -> None:
            async with semaphore:
                await poll_single_processing_order(context, task_id)

        async def run_delivery_row(row: dict[str, Any]) -> None:
            async with semaphore:
                await deliver_order_file(
                    context,
                    row,
                    supplier,
                    include_ready_photo=True,
                    notify_failure=False,
                )

        tasks = [run_processing_row(str(row["task_id"])) for row in processing_rows]
        tasks.extend(run_delivery_row(row) for row in pending_delivery_rows)
        await asyncio.gather(*tasks)


async def on_application_post_init(application: Application) -> None:
    application.bot_data["main_loop"] = asyncio.get_running_loop()
    await ensure_okpay_callback_server(application)


def build_application(settings: Settings) -> Application:
    store = Store(settings.database_path)
    supplier = SupplierClient(settings)

    application = ApplicationBuilder().token(settings.bot_token).post_init(on_application_post_init).build()
    application.bot_data["settings"] = settings
    application.bot_data["store"] = store
    application.bot_data["supplier"] = supplier
    application.bot_data["runtime_config"] = store.get_runtime_settings()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu))
    application.add_handler(CommandHandler("help", reply_help))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(CommandHandler("me", me))
    application.add_handler(CommandHandler("categories", categories))
    application.add_handler(CommandHandler("products", products))
    application.add_handler(CommandHandler("product", product))
    application.add_handler(CommandHandler("buy", buy))
    application.add_handler(CommandHandler("orders", orders))
    application.add_handler(CommandHandler("order", order))
    application.add_handler(CommandHandler("supplier_balance", supplier_balance))
    application.add_handler(CommandHandler("add", credit))
    application.add_handler(CommandHandler("credit", credit))
    application.add_handler(CallbackQueryHandler(show_notice, pattern=r"^nav:notice$"))
    application.add_handler(CallbackQueryHandler(show_language, pattern=r"^nav:language$"))
    application.add_handler(CallbackQueryHandler(on_callback))
    button_pattern = "^(" + "|".join(re.escape(text) for text in sorted(NON_SEARCH_BUTTON_TEXTS)) + ")$"
    application.add_handler(MessageHandler(filters.Regex(button_pattern), route_menu_text))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_admin_photo_input))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_text_rich))

    if application.job_queue is not None:
        application.job_queue.run_repeating(
            poll_processing_orders,
            interval=settings.order_poll_seconds,
            first=settings.order_poll_first_seconds,
            name="poll_processing_orders",
        )
        application.job_queue.run_repeating(
            poll_trc20_topups,
            interval=settings.trongrid_poll_seconds,
            first=5,
            name="poll_trc20_topups",
        )
    return application


def main() -> None:
    settings = load_settings()
    application = build_application(settings)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
