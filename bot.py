from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

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
PENDING_ADMIN_KEY = "pending_admin_action"
ADMIN_USERS_PAGE_SIZE = 8
ADMIN_SEND_SCOPE_SINGLE = "single"
ADMIN_SEND_SCOPE_ALL = "all"
RUNTIME_KEY_RECHARGE_ADDRESS = "recharge_address"
RUNTIME_KEY_OKPAY_CONFIG = "okpay_config"
RUNTIME_KEY_CUSTOMER_SERVICE = "customer_service_contact"
RUNTIME_KEY_RESTOCK_CHANNEL = "restock_channel"
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


def effective_okpay_config(context: ContextTypes.DEFAULT_TYPE) -> str:
    return runtime_value(context, RUNTIME_KEY_OKPAY_CONFIG, "")


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
    return premium_text_prefix(ORDER_CREATED_EMOJI_ID, "⏳", "订单已创建，正在检查账号存活并打包，请稍后...")


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


async def send_menu_message(update: Update, text: str) -> None:
    if update.message is not None:
        await update.message.reply_text(text, reply_markup=MENU_KEYBOARD)
    elif update.callback_query is not None:
        await update.callback_query.message.reply_text(text, reply_markup=MENU_KEYBOARD)


async def reply_help(update: Update, context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
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
        f"👤 {settings.shop_title} - 个人中心",
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


async def show_recharge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, store, _ = get_services(context)
    user = update.effective_user
    balance = 0.0
    if user is not None:
        await call_blocking(store.ensure_user, user.id, user.username or "", user.full_name or "")
        balance = await call_blocking(store.get_balance, user.id)
    recharge_address = effective_recharge_address(context)
    okpay_config = effective_okpay_config(context)
    extra_lines: list[str] = []
    if recharge_address:
        extra_lines.extend(["", f"充值地址：{recharge_address}"])
    if okpay_config:
        extra_lines.extend(["", f"OKPAY 配置：{okpay_config}"])
    text = (
        f"💰 {settings.shop_title} - 充值中心\n\n"
        f"当前余额：{format_money(balance)} USDT\n\n"
        f"{settings.recharge_text}"
        + "\n".join(extra_lines)
    )
    keyboard = InlineKeyboardMarkup(
        [
            [premium_inline_button(BUTTON_MAIN_MENU, "nav:menu", HOME_EMOJI_ID)],
        ]
    )
    await reply_inline(update, text, keyboard)


async def show_customer_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, _, _ = get_services(context)
    text = premium_text_prefix(
        CUSTOMER_SERVICE_EMOJI_ID,
        "☎️",
        f"联系客服：{effective_customer_service_contact(context, settings)}",
    )
    await reply_inline(update, text, parse_mode="HTML")


async def show_notice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    lines = [f"用户列表 {page + 1}/{total_pages}", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    start_index = page * ADMIN_USERS_PAGE_SIZE
    for index, row in enumerate(rows, start=1):
        username_text = f"@{row.get('username')}" if row.get("username") else "未设置用户名"
        lines.append(f"{start_index + index}. {format_user_created_at(row.get('created_at'))} | {user_label(row)} | {username_text}")
        lines.append(f"ID: {row.get('user_id')} | 余额: {format_money(safe_float(row.get('balance')))} USDT")
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
    await reply_inline(update, "\n".join(lines).strip(), InlineKeyboardMarkup(buttons))


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
            f"当前充值地址：{effective_recharge_address(context) or '未配置'}\n"
            f"当前充值说明：{settings.recharge_text}"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("修改充值地址", callback_data="adm:set:raddr")],
                [InlineKeyboardButton("返回后台", callback_data="adm:home")],
            ]
        )
    elif section == "okpay":
        text = (
            "OKPAY 配置\n\n"
            f"当前配置：{effective_okpay_config(context) or '未配置'}\n"
            "后面接 OKPAY API 时，先从这里取配置。"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("修改 OKPAY 配置", callback_data="adm:set:okpay")],
                [InlineKeyboardButton("返回后台", callback_data="adm:home")],
            ]
        )
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
        await call_blocking(store.set_runtime_setting, setting_key, value, user.id)
        get_runtime_config(context)[setting_key] = value
        await call_blocking(store.log_admin_action, user.id, "admin_setting_update", setting_key, value)
        clear_pending_admin_action(context)
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
    clear_pending_purchase(context)
    await show_start_menu(update, context)


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_pending_purchase(context)
    await show_start_menu(update, context)


async def me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_profile(update, context)


async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_categories(update, context)


async def products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    product_id: int,
    quantity: int,
) -> str | None:
    settings, store, supplier = get_services(context)
    await call_blocking(store.ensure_user, user_id, username)

    detail_payload = await call_blocking(supplier.get_product_detail, product_id)
    row = detail_payload.get("data") or {}
    unit_price = resolve_sell_price(settings, row)
    total_stock = safe_int(row.get("totalStock"))
    product_name = str(row.get("productName") or f"商品 {product_id}")
    total_price = unit_price * quantity

    if total_stock < quantity:
        return f"库存不足。当前库存 {total_stock}，你要买 {quantity}"

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
        )

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
        return (
            f"上游下单失败: {exc}\n"
            f"已退款 {format_money(total_price)} USDT\n"
            f"当前余额: {format_money(refunded)} USDT"
        )

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
        return (
            f"下单失败: {upstream_reason}\n"
            f"已退款 {format_money(total_price)} USDT\n"
            f"当前余额: {format_money(refunded)} USDT"
        )

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
    return None


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        result = await execute_purchase(context, user.id, user.username or "", product_id, quantity)
    except SupplierApiError as exc:
        await update.message.reply_text(f"获取商品详情失败: {exc}")
        return
    if result:
        await update.message.reply_text(result, reply_markup=MENU_KEYBOARD)
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
            if file_url:
                try:
                    zip_path = await call_blocking(download_delivery_file, supplier, task_id, file_url)
                    if DELIVERY_READY_IMAGE_PATH.exists():
                        with DELIVERY_READY_IMAGE_PATH.open("rb") as photo_fp:
                            delivery_text, delivery_entities = build_delivery_ready_text(
                                str(final_row.get("product_name") or f"商品 {final_row.get('product_id')}"),
                                quantity,
                                quantity_success,
                                refund_amount,
                            )
                            await context.bot.send_photo(
                                chat_id=int(final_row["user_id"]),
                                photo=photo_fp,
                                caption=delivery_text,
                                caption_entities=delivery_entities,
                            )
                    with zip_path.open("rb") as document_fp:
                        await context.bot.send_document(
                            chat_id=int(final_row["user_id"]),
                            document=document_fp,
                            filename=delivery_display_filename(
                                str(final_row.get("product_name") or f"商品 {final_row.get('product_id')}"),
                                quantity,
                                file_url,
                            ),
                            reply_markup=MENU_KEYBOARD,
                        )
                except Exception:
                    logger.exception("发送订单 zip 文件失败: %s", task_id)
                    await context.bot.send_message(
                        chat_id=int(final_row["user_id"]),
                        text=f"zip 文件发送失败，请稍后用 /order {task_id} 重试。",
                        reply_markup=MENU_KEYBOARD,
                    )
        summary = f"订单完成，成功数量 {quantity_success}/{quantity}"
        if refund_amount > 0:
            summary += f"，已退款 {format_money(refund_amount)} USDT"
        return final_state, summary

    return "unknown", f"未知订单状态: {status}"


async def order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, store, _ = get_services(context)
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
        lines.append("发货文件: 机器人会直接发送 zip 文件")
    await update.message.reply_text("\n".join(lines), reply_markup=MENU_KEYBOARD)


async def orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_orders(update, context)


async def supplier_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        text = (
            f"已给用户 {target_user_id} 增加 {format_money(amount)} USDT\n"
            f"当前余额: {format_money(balance)} USDT"
        )
        user_notice = (
            "余额变动提醒\n"
            f"已增加: {format_money(amount)} USDT\n"
            f"当前余额: {format_money(balance)} USDT"
        )
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
        user_notice = (
            "余额变动提醒\n"
            f"已扣减: {format_money(debit_amount)} USDT\n"
            f"当前余额: {format_money(balance)} USDT"
        )
    if target_user_id != user.id:
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=user_notice,
                reply_markup=MENU_KEYBOARD,
            )
        except Exception:
            logger.exception("发送余额变动提醒失败: %s", target_user_id)
    await update.message.reply_text(text, reply_markup=MENU_KEYBOARD)


async def route_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not update.message.text:
        return
    text = update.message.text.strip()
    if text == BUTTON_PRODUCTS or text == BUTTON_ACCOUNT_LIST:
        clear_pending_purchase(context)
        await show_categories(update, context)
        return
    if text in {BUTTON_MAIN_MENU, BOTTOM_BUTTON_MAIN_MENU, LEGACY_BOTTOM_BUTTON_MAIN_MENU}:
        clear_pending_purchase(context)
        await show_start_menu(update, context)
        return
    if text in {BUTTON_PROFILE, BUTTON_RECHARGE_BALANCE, BOTTOM_BUTTON_RECHARGE_BALANCE, LEGACY_BOTTOM_BUTTON_RECHARGE_BALANCE}:
        clear_pending_purchase(context)
        await show_recharge(update, context)
        return
    if text in {BOTTOM_BUTTON_CUSTOMER_SERVICE, LEGACY_BOTTOM_BUTTON_CUSTOMER_SERVICE}:
        clear_pending_purchase(context)
        await show_customer_service(update, context)
        return
    if text == BUTTON_PURCHASE_NOTICE:
        clear_pending_purchase(context)
        await show_notice(update, context)
        return
    if text == BUTTON_ORDER_HISTORY:
        clear_pending_purchase(context)
        await show_orders(update, context)
        return
    if text == BUTTON_SWITCH_LANGUAGE:
        clear_pending_purchase(context)
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
    settings, _, supplier = get_services(context)
    if update.message is None or not update.message.text:
        return
    keyword = normalize_search_keyword(update.message.text)
    if await handle_admin_text_input(update, context, keyword):
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
    settings, _, supplier = get_services(context)
    query = update.callback_query
    if query is None or not query.data:
        return

    parts = query.data.split(":")
    action = parts[0]

    if action == "adm":
        await handle_admin_callback(update, context, parts)
        return

    if action == "nav":
        clear_pending_purchase(context)
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
            result = await execute_purchase(context, user.id, user.username or "", product_id, quantity)
        except SupplierApiError as exc:
            if query.message is not None:
                await query.message.reply_text(f"获取商品详情失败: {exc}", reply_markup=MENU_KEYBOARD)
            else:
                await reply_inline(update, f"获取商品详情失败: {exc}")
            return
        if result and query.message is not None:
            await query.message.reply_text(result, reply_markup=MENU_KEYBOARD)
        elif result:
            await reply_inline(update, result)
        elif query.message is not None:
            await query.message.reply_text(
                order_created_caption(),
                reply_markup=MENU_KEYBOARD,
                parse_mode="HTML",
            )
        return

    await query.answer("暂不支持这个按钮", show_alert=False)


async def poll_processing_orders(context: ContextTypes.DEFAULT_TYPE) -> None:
    _, store, _ = get_services(context)
    rows = await call_blocking(store.list_processing_orders, 50)
    for row in rows:
        task_id = str(row["task_id"])
        try:
            await finalize_remote_order(context, task_id, notify_user=True)
        except Exception:
            logger.exception("轮询订单失败: %s", task_id)


def build_application(settings: Settings) -> Application:
    store = Store(settings.database_path)
    supplier = SupplierClient(settings)

    application = ApplicationBuilder().token(settings.bot_token).build()
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
            first=10,
            name="poll_processing_orders",
        )
    return application


def main() -> None:
    settings = load_settings()
    application = build_application(settings)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
